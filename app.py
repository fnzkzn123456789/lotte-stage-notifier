"""Windows 실행 진입점. 추가 pip 패키지 없이 Python 3.10+에서 실행."""
import os
import sys
from pathlib import Path


def main():
    import tkinter as tk
    from tkinter import messagebox
    from notifier.gui import App
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    directory = base / "data"
    directory.mkdir(exist_ok=True)
    root = tk.Tk()
    lock = (directory / "app.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            root.withdraw()
            messagebox.showinfo("알리미 실행 중", "이미 실행 중인 알리미 창을 사용해 주세요.")
            root.destroy()
            lock.close()
            return
    try:
        App(root, directory)
        root.mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
