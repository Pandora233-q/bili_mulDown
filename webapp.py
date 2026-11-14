#!/usr/bin/env python3
"""BiliAudio 的本地网页界面：搜索、下载、试听，一条龙。

    python webapp.py
    python bili_audio.py --web

服务只监听 127.0.0.1，下载文件通过临时 token（6 小时）提供，避免任意路径读取。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from bili_audio import (
    APP_DIR,
    AUDIO_FORMATS,
    DEFAULT_OUT,
    WEB_DIR,
    BiliAudioError,
    download_audio,
    human_size,
    search_first_video,
)


@dataclass
class AppContext:
    index_path: Path
    out_dir: Path
    semaphore: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(2))
    files: dict[str, tuple[Path, float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cookie_file: str | None = None

    def register(self, path: Path) -> str:
        token = uuid.uuid4().hex
        expires = time.time() + 6 * 3600
        with self.lock:
            self._cleanup_locked()
            self.files[token] = (path.resolve(), expires)
        return token

    def lookup(self, token: str) -> Path | None:
        with self.lock:
            self._cleanup_locked()
            entry = self.files.get(token)
            if entry is None:
                return None
            path, _expires = entry
            if not path.is_file():
                self.files.pop(token, None)
                return None
            return path

    def _cleanup_locked(self) -> None:
        now = time.time()
        expired = [key for key, (_path, expires) in self.files.items() if expires < now]
        for key in expired:
            self.files.pop(key, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "BiliAudio/1.0"
    protocol_version = "HTTP/1.0"
    ctx: AppContext

    # 静默处理常规访问日志，只在服务端出错时由上层打印。
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/api/health":
            self._send_json(200, {"ok": True, "service": "BiliAudio"})
        elif path.startswith("/api/file/"):
            self._serve_file(path[len("/api/file/") :], parse_qs(parsed.query))
        elif path == "/favicon.ico":
            self._send_bytes(204, b"", "image/x-icon")
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/fetch":
            self._send_json(404, {"error": "not found"})
            return
        self._handle_fetch()

    # ---------- 页面 ----------

    def _serve_index(self) -> None:
        try:
            data = self.ctx.index_path.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"无法读取页面：{exc}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 搜索 + 下载（NDJSON 流） ----------

    def _handle_fetch(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._send_json(400, {"error": "请求体无效"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体必须是 JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "请求体必须是 JSON 对象"})
            return

        keyword = str(payload.get("keyword") or "").strip()
        audio_format = str(payload.get("format") or "mp3").lower()
        try:
            pick = int(payload.get("pick") or 1)
        except (TypeError, ValueError):
            pick = 1
        if not keyword:
            self._send_json(400, {"error": "请输入关键词"})
            return
        if len(keyword) > 100:
            self._send_json(400, {"error": "关键词最多 100 个字符"})
            return
        if audio_format not in AUDIO_FORMATS:
            self._send_json(400, {"error": "不支持的音频格式"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True

        cancel = threading.Event()

        def emit(event: dict) -> None:
            try:
                line = json.dumps(event, ensure_ascii=False) + "\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                cancel.set()
                raise

        try:
            emit(
                {
                    "type": "status",
                    "stage": "search",
                    "message": "正在检索 B 站…",
                }
            )
            video = search_first_video(
                keyword,
                pick=pick,
                cookie_file=self.ctx.cookie_file,
            )
            emit({"type": "video", "video": video.to_dict()})
            emit(
                {
                    "type": "status",
                    "stage": "download",
                    "message": "准备下载音频…",
                }
            )

            with self.ctx.semaphore:
                path = download_audio(
                    video,
                    out_dir=self.ctx.out_dir,
                    audio_format=audio_format,
                    cookie_file=self.ctx.cookie_file,
                    on_event=emit,
                    cancel=cancel,
                )

            token = self.ctx.register(path)
            size = path.stat().st_size
            emit(
                {
                    "type": "done",
                    "token": token,
                    "filename": path.name,
                    "size": size,
                    "size_text": human_size(size),
                    "format": audio_format,
                    "file_url": f"/api/file/{token}",
                    "inline_url": f"/api/file/{token}?inline=1",
                }
            )
        except (BrokenPipeError, ConnectionResetError):
            cancel.set()
            return
        except BiliAudioError as exc:
            self._safe_emit(emit, {"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免浏览器一直转圈
            self._safe_emit(
                emit,
                {
                    "type": "error",
                    "message": f"程序内部错误：{exc}",
                },
            )

    @staticmethod
    def _safe_emit(emit, event: dict) -> None:
        try:
            emit(event)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 文件 ----------

    def _serve_file(self, token: str, query: dict[str, list[str]]) -> None:
        token = token.strip("/")
        path = self.ctx.lookup(token)
        if path is None:
            self._send_json(404, {"error": "文件不存在或链接已过期"})
            return

        try:
            size = path.stat().st_size
        except OSError:
            self._send_json(404, {"error": "文件已被移动或删除"})
            return

        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            parsed = self._parse_range(range_header, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            status = 206

        length = max(0, end - start + 1)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() == ".mp3":
            content_type = "audio/mpeg"
        elif path.suffix.lower() == ".m4a":
            content_type = "audio/mp4"
        elif path.suffix.lower() == ".wav":
            content_type = "audio/wav"

        inline = str((query.get("inline") or ["0"])[0]).lower() in {"1", "true", "yes"}
        disposition = "inline" if inline else "attachment"
        ascii_name = f"audio{path.suffix.lower() or '.bin'}"
        encoded_name = quote(path.name, safe="")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Disposition",
            f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
        )
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def _parse_range(header: str, size: int) -> tuple[int, int] | None:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
        if not match:
            return None
        first, last = match.groups()
        if not first and not last:
            return None
        try:
            if not first:
                suffix = int(last)
                if suffix <= 0:
                    return None
                start = max(0, size - suffix)
                end = size - 1
            else:
                start = int(first)
                end = int(last) if last else size - 1
        except ValueError:
            return None
        if start >= size or start > end:
            return None
        return start, min(end, size - 1)

    # ---------- 工具 ----------

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )


def _default_cookie_file() -> str | None:
    configured = os.environ.get("BILI_COOKIES")
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser())
    local = APP_DIR / "cookies.txt"
    if local.is_file():
        return str(local)
    return None


def run(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    out_dir: str | Path = DEFAULT_OUT,
) -> int:
    index_path = WEB_DIR / "index.html"
    if not index_path.is_file():
        print(f"找不到页面文件：{index_path}", file=sys.stderr)
        return 1

    out_path = Path(out_dir).expanduser().resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    Handler.ctx = AppContext(
        index_path=index_path,
        out_dir=out_path,
        cookie_file=_default_cookie_file(),
    )

    server = None
    last_error: OSError | None = None
    for candidate in range(port, port + 10):
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
            break
        except OSError as exc:
            last_error = exc
            continue
    if server is None:
        print(
            f"无法启动本地服务：{last_error}\n"
            f"端口 {port}–{port + 9} 都被占用了，换一个：--port 8800",
            file=sys.stderr,
        )
        return 1

    server.daemon_threads = True
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    print(f"BiliAudio 已启动 → {url}")
    print(f"下载目录：{out_path}")
    print("按 Ctrl+C 停止。")

    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
