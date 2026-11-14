#!/usr/bin/env python3
"""BiliAudio — 搜索 B 站第一个视频，并用 yt-dlp 下载它的音频。

命令行：
    python bili_audio.py "周杰伦"
    python bili_audio.py --web

只依赖 requests、yt-dlp 与 ffmpeg。默认下载到脚本旁边的 downloads/。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import urllib3.util.connection as urllib3_connection

# 这台机器解析 www.bilibili.com 时会优先返回 AAAA，但 IPv6 不通。
# urllib3 会逐个尝试解析出的地址，导致请求长时间卡住；这里强制走 IPv4。
# yt-dlp 子进程则用 --force-ipv4 做同样的事。
urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = APP_DIR / "downloads"
WEB_DIR = APP_DIR / "web"

SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
BILI_HOME = "https://www.bilibili.com/"
VIDEO_URL = "https://www.bilibili.com/video/{bvid}"
AUDIO_FORMATS = ("mp3", "m4a", "wav")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": BILI_HOME,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}

EventCallback = Callable[[dict[str, Any]], None]


class BiliAudioError(Exception):
    """可以直接展示给用户的错误。"""


class SearchError(BiliAudioError):
    pass


class DownloadError(BiliAudioError):
    pass


@dataclass(slots=True)
class Video:
    bvid: str
    title: str
    author: str
    url: str
    cover: str
    duration: str
    duration_seconds: int | None
    views: int
    danmaku: int
    pubdate: int
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    """去掉 B 站搜索高亮标签、HTML 实体与多余空白。"""
    text = _TAG_RE.sub("", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def format_duration(raw: str | None) -> tuple[str, int | None]:
    """把 B 站返回的 3:33 / 419:2 规范成易读时长，并换算成秒。"""
    if not raw:
        return "", None
    parts = [p for p in raw.strip().split(":") if p.isdigit()]
    if not parts:
        return raw.strip(), None
    nums = [int(p) for p in parts]
    try:
        if len(nums) == 2:
            minutes, seconds = nums
            total = minutes * 60 + seconds
            if minutes >= 60:
                hours, minutes = divmod(minutes, 60)
                return f"{hours}:{minutes:02d}:{seconds:02d}", total
            return f"{minutes}:{seconds:02d}", total
        if len(nums) == 3:
            hours, minutes, seconds = nums
            return f"{hours}:{minutes:02d}:{seconds:02d}", hours * 3600 + minutes * 60 + seconds
    except ValueError:
        pass
    return raw.strip(), None


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _cookie_header_from_file(path: str | os.PathLike[str] | None) -> str | None:
    if not path:
        return None
    cookies_path = Path(path).expanduser()
    if not cookies_path.is_file():
        raise BiliAudioError(f"Cookies 文件不存在：{cookies_path}")
    try:
        from http.cookiejar import MozillaCookieJar

        jar = MozillaCookieJar(str(cookies_path))
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:  # noqa: BLE001 - 一并给出可操作的提示
        raise BiliAudioError(
            f"无法读取 Cookies 文件（需要 Netscape 格式）：{exc}"
        ) from exc
    pairs = [
        f"{cookie.name}={cookie.value}"
        for cookie in jar
        if "bilibili.com" in (cookie.domain or "")
    ]
    return "; ".join(pairs) or None


def _new_session(cookie: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    if cookie:
        session.headers["Cookie"] = cookie
    return session


def search_first_video(
    keyword: str,
    *,
    pick: int = 1,
    cookie_file: str | os.PathLike[str] | None = None,
) -> Video:
    """搜索关键词，返回第 pick 个视频结果（默认第一条）。"""
    keyword = keyword.strip()
    if not keyword:
        raise SearchError("关键词不能为空。")
    if len(keyword) > 100:
        raise SearchError("关键词太长，最多 100 个字符。")
    if pick < 1:
        raise SearchError("结果序号必须从 1 开始。")

    cookie = os.environ.get("BILI_COOKIE") or _cookie_header_from_file(cookie_file)
    session = _new_session(cookie)

    # 先访问首页拿 buvid 等基础 Cookie。风控页（412）也会下发 Cookie，
    # 所以失败不影响后续请求。
    try:
        session.get(BILI_HOME, timeout=(4, 8))
    except requests.RequestException:
        pass

    payload: dict[str, Any] | None = None
    last_message = ""
    for attempt in range(2):
        try:
            response = session.get(
                SEARCH_API,
                params={
                    "search_type": "video",
                    "keyword": keyword,
                    "page": 1,
                },
                timeout=(6, 18),
            )
        except requests.Timeout as exc:
            raise SearchError("连接 B 站超时，请检查网络后重试。") from exc
        except requests.RequestException as exc:
            raise SearchError(f"搜索请求失败：{exc}") from exc

        if response.status_code == 412:
            last_message = "B 站返回 412 风控页"
            if attempt == 0:
                time.sleep(0.8)
                session = _new_session(cookie)
                continue
            break

        if response.status_code >= 400:
            last_message = f"HTTP {response.status_code}"
            if attempt == 0 and response.status_code >= 500:
                time.sleep(0.8)
                continue
            break

        try:
            payload = response.json()
        except ValueError:
            last_message = "响应不是 JSON"
            if attempt == 0:
                time.sleep(0.8)
                session = _new_session(cookie)
                continue
            break

        code = payload.get("code")
        if code == 0:
            break
        last_message = str(payload.get("message") or f"错误码 {code}")
        if code == -412 and attempt == 0:
            time.sleep(0.8)
            session = _new_session(cookie)
            continue
        break

    if not payload or payload.get("code") != 0:
        hint = "；登录 Cookie 可以显著降低风控概率（--cookies cookies.txt）"
        raise SearchError(f"B 站搜索失败：{last_message or '未知错误'}{hint}")

    results = (payload.get("data") or {}).get("result") or []
    videos = [
        item
        for item in results
        if item.get("type") == "video" and item.get("bvid")
    ]
    if not videos:
        raise SearchError(f"没有搜到「{keyword}」相关的视频，换个关键词试试。")
    if pick > len(videos):
        raise SearchError(f"搜索结果只有 {len(videos)} 条视频，取不到第 {pick} 条。")

    item = videos[pick - 1]
    bvid = str(item["bvid"])
    duration_text, duration_seconds = format_duration(str(item.get("duration") or ""))
    cover = str(item.get("pic") or "")
    if cover.startswith("//"):
        cover = "https:" + cover

    try:
        views = int(item.get("play") or 0)
        danmaku = int(item.get("video_review") or 0)
        pubdate = int(item.get("pubdate") or 0)
    except (TypeError, ValueError):
        views = danmaku = pubdate = 0

    return Video(
        bvid=bvid,
        title=clean_text(str(item.get("title") or "")) or bvid,
        author=clean_text(str(item.get("author") or "")),
        url=VIDEO_URL.format(bvid=bvid),
        cover=cover,
        duration=duration_text,
        duration_seconds=duration_seconds,
        views=views,
        danmaku=danmaku,
        pubdate=pubdate,
        description=clean_text(str(item.get("description") or "")),
    )


def _ytdlp_base() -> list[str]:
    """优先用当前解释器里的 yt_dlp 模块，找不到再退回 PATH 里的命令。"""
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        executable = shutil.which("yt-dlp")
        if executable:
            return [executable]
        raise DownloadError(
            "没有找到 yt-dlp。请先运行：python -m pip install -U yt-dlp"
        ) from None


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise DownloadError(
            "没有找到 ffmpeg。yt-dlp 转换/封装音频需要它，请先安装并加入 PATH。"
        )


_PROGRESS_RE = re.compile(
    r"^\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+~?\s*"
    r"(?P<total>[\d.]+\s*[KMGTP]?i?B)"
    r"(?:\s+at\s+(?P<speed>.+?))?"
    r"(?:\s+ETA\s+(?P<eta>[\d:]+|Unknown))?\s*$",
    re.IGNORECASE,
)
_DONE_RE = re.compile(
    r"^\[download\]\s+100%\s+of\s+~?\s*[\d.]+\s*[KMGTP]?i?B\s+in\s+",
    re.IGNORECASE,
)


def download_audio(
    video: Video,
    *,
    out_dir: str | os.PathLike[str] = DEFAULT_OUT,
    audio_format: str = "mp3",
    cookie_file: str | os.PathLike[str] | None = None,
    on_event: EventCallback | None = None,
    cancel: Any = None,
) -> Path:
    """用 yt-dlp 下载并提取音频，返回最终文件路径。

    on_event 会收到 {"type": "status", ...} 与 {"type": "progress", ...}。
    cancel 是一个可选的 threading.Event；被 set 时终止子进程。
    """
    if audio_format not in AUDIO_FORMATS:
        raise DownloadError(f"不支持的音频格式：{audio_format}")
    _require_ffmpeg()

    out_path = Path(out_dir).expanduser().resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    template = str(out_path / "%(title).80s [%(id)s].%(ext)s")
    command = [
        *_ytdlp_base(),
        "--force-ipv4",
        "--newline",
        "--no-warnings",
        "--no-simulate",
        "--progress",
        "--no-colors",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
        "--playlist-items",
        "1",
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        audio_format,
        "--audio-quality",
        "0",
        "--embed-metadata",
        "--print",
        "after_move:__FILE__:%(filepath)s",
        "-o",
        template,
    ]
    if cookie_file:
        command.extend(["--cookies", str(Path(cookie_file).expanduser())])
    command.append(video.url)

    emit(
        {
            "type": "status",
            "stage": "extract",
            "message": "正在解析视频信息…",
        }
    )

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise DownloadError(f"无法启动 yt-dlp：{exc}") from exc

    final_path: Path | None = None
    tail: list[str] = []
    stage = "extract"
    last_percent = -100.0
    cancelled = False

    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            tail.append(line)
            if len(tail) > 14:
                tail.pop(0)

            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                cancelled = True
                process.kill()
                break

            if line.startswith("__FILE__:"):
                final_path = Path(line[len("__FILE__:") :].strip())
                continue

            if line.startswith("[download] Destination:"):
                if stage != "download":
                    stage = "download"
                    emit(
                        {
                            "type": "status",
                            "stage": "download",
                            "message": "正在下载音频…",
                        }
                    )
                continue

            if line.startswith("[ExtractAudio]"):
                if stage != "convert":
                    stage = "convert"
                    emit(
                        {
                            "type": "status",
                            "stage": "convert",
                            "message": f"正在转换为 {audio_format.upper()}…",
                        }
                    )
                continue

            if line.startswith("[Metadata]"):
                if stage != "metadata":
                    stage = "metadata"
                    emit(
                        {
                            "type": "status",
                            "stage": "metadata",
                            "message": "正在写入元数据…",
                        }
                    )
                continue

            if line.startswith("[BiliBili]") or line.startswith("[info]"):
                continue

            match = _PROGRESS_RE.match(line)
            if match:
                try:
                    percent = float(match.group("pct"))
                except ValueError:
                    continue
                if percent - last_percent < 0.5 and percent < 100:
                    continue
                last_percent = percent
                emit(
                    {
                        "type": "progress",
                        "stage": "download",
                        "percent": round(percent, 1),
                        "total": re.sub(r"\s+", " ", match.group("total") or ""),
                        "speed": (match.group("speed") or "").strip(),
                        "eta": (match.group("eta") or "").strip(),
                    }
                )
                continue

            if _DONE_RE.match(line) and last_percent < 100:
                last_percent = 100
                emit(
                    {
                        "type": "progress",
                        "stage": "download",
                        "percent": 100.0,
                        "total": "",
                        "speed": "",
                        "eta": "00:00",
                    }
                )
    finally:
        try:
            process.stdout.close()
        except Exception:
            pass
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)

    if cancelled:
        raise DownloadError("下载已取消。")

    if return_code != 0:
        meaningful = [
            line
            for line in tail
            if not line.startswith("[download]")
            and not line.startswith("__FILE__:")
        ]
        detail = "\n".join(meaningful[-4:]) or "yt-dlp 未返回具体原因"
        if "412" in detail or "403" in detail:
            detail += "\n提示：B 站可能触发了风控，稍后重试，或使用 --cookies cookies.txt。"
        raise DownloadError(f"yt-dlp 下载失败：\n{detail}")

    if final_path is None:
        candidates = sorted(
            out_path.glob(f"*{video.bvid}*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        candidates = [
            path
            for path in candidates
            if path.suffix.lower() in {".mp3", ".m4a", ".wav"}
        ]
        if candidates:
            final_path = candidates[0]

    if final_path is None or not final_path.exists():
        raise DownloadError("yt-dlp 结束了，但没有找到输出的音频文件。")
    if final_path.stat().st_size <= 1024:
        raise DownloadError("输出的音频文件为空，下载可能被 B 站拦截了。")

    return final_path


def _print_link(video: Video, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"video": video.to_dict()}, ensure_ascii=False, indent=2))
    else:
        print(video.url)


def _run_cli_download(
    video: Video,
    *,
    out_dir: Path,
    audio_format: str,
    cookie_file: str | None,
    quiet: bool = False,
) -> Path:
    state = {"last_bucket": -1, "stage": ""}
    interactive = sys.stdout.isatty()

    def on_event(event: dict[str, Any]) -> None:
        if event["type"] == "status":
            message = event["message"]
            if message != state["stage"]:
                state["stage"] = message
                if interactive:
                    print(f"\r  {message:<44}", end="", flush=True)
                else:
                    print(f"  {message}")
            return

        percent = float(event.get("percent", 0))
        bucket = int(percent // 5)
        if bucket == state["last_bucket"] and percent < 100:
            return
        state["last_bucket"] = bucket
        filled = int(percent // 4)
        bar = "█" * filled + "░" * (25 - filled)
        speed = event.get("speed") or ""
        eta = event.get("eta") or ""
        extra = " ".join(part for part in (speed, f"ETA {eta}" if eta else "") if part)
        line = f"  [{bar}] {percent:5.1f}%  {extra}"
        if interactive:
            print(f"\r{line:<78}", end="", flush=True)
        else:
            print(line.rstrip())

    path = download_audio(
        video,
        out_dir=out_dir,
        audio_format=audio_format,
        cookie_file=cookie_file,
        on_event=None if quiet else on_event,
    )
    if interactive:
        print()
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bili_audio.py",
        description="搜索 B 站第一个视频，并用 yt-dlp 下载它的音频。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python bili_audio.py 周杰伦\n"
            "  python bili_audio.py 猫 短片 --format m4a --out D:\\Music\n"
            "  python bili_audio.py 白噪音 --link-only\n"
            "  python bili_audio.py --web\n"
        ),
    )
    parser.add_argument("keyword", nargs="*", help="搜索关键词（不带参数则启动网页界面）")
    parser.add_argument("--web", action="store_true", help="启动本地网页界面")
    parser.add_argument("--port", type=int, default=8765, help="网页端口，默认 8765")
    parser.add_argument(
        "--no-browser", action="store_true", help="网页模式下不自动打开浏览器"
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT), help="音频保存目录，默认 ./downloads"
    )
    parser.add_argument(
        "--format",
        dest="audio_format",
        choices=AUDIO_FORMATS,
        default="mp3",
        help="音频格式，默认 mp3",
    )
    parser.add_argument(
        "--pick", type=int, default=1, help="取第几条视频结果，默认第 1 条"
    )
    parser.add_argument(
        "--link-only", action="store_true", help="只输出视频链接，不下载"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument(
        "--cookies",
        default=None,
        help="Netscape 格式的 cookies.txt（可选，用于提高音质/降低风控）",
    )
    parser.add_argument("--version", action="version", version="BiliAudio 1.0")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.web or not args.keyword:
        try:
            import webapp
        except ImportError as exc:
            print(f"无法加载网页模块：{exc}", file=sys.stderr)
            return 1
        return webapp.run(
            port=args.port,
            open_browser=not args.no_browser,
            out_dir=Path(args.out),
        )

    keyword = " ".join(args.keyword).strip()
    try:
        video = search_first_video(
            keyword,
            pick=args.pick,
            cookie_file=args.cookies,
        )
    except BiliAudioError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.link_only:
        _print_link(video, as_json=args.json)
        return 0

    if not args.json:
        print(f"  关键词   {keyword}")
        print(f"  已锁定   {video.title}")
        print(f"  UP 主    {video.author or '未知'}")
        print(f"  时长     {video.duration or '未知'}")
        print(f"  链接     {video.url}")
        print()

    try:
        path = _run_cli_download(
            video,
            out_dir=Path(args.out).expanduser(),
            audio_format=args.audio_format,
            cookie_file=args.cookies,
            quiet=args.json,
        )
    except BiliAudioError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130

    size = path.stat().st_size
    if args.json:
        print(
            json.dumps(
                {
                    "video": video.to_dict(),
                    "file": str(path),
                    "size": size,
                    "format": args.audio_format,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"  完成     {path}")
        print(f"  大小     {human_size(size)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
