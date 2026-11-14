from tkinter import filedialog
import os


def choose_file():

    file_path = filedialog.askopenfilename()
    return file_path

def mul_download(path):
    if not path:
        return
    with open (path, 'r', encoding='utf-8') as f:

        lines = f.readlines()
        for line in lines:
            if not line:
                continue
            os.system(f"python bili_audio.py {line.strip()}")
        os.system("pause")

def mul_download_input():
    videos = []
    while True:
        video = input("|")
        if not video:
            break
        videos.append(video)
    for video in videos:
        os.system(f"python bili_audio.py {video}")
    os.system("pause")

if __name__ == '__main__':
    option = input("1.选择一个文档\n2.输入\n:")
    if option == '1':
        path = choose_file()
        mul_download(path)
    elif option == '2':
        mul_download_input()