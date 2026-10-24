from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .core import (BASE, LotteClient, Monitor, ServiceError, Stopped, Telegram, is_stage,
                   load_settings, now_kst, parse_screenings, save_settings, show_time)


class App:
    def __init__(self, root, directory):
        self.root, self.directory = root, directory
        self.events = queue.Queue()
        self.thread = None
        self.stop = threading.Event()
        self.busy = False
        self.closing = False
        self.shows = {}
        self.chats = {}
        self.last_snapshot = set()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.load_error = ""
        try:
            settings, token, chat = load_settings(directory)
        except ServiceError as exc:
            self.load_error = str(exc)
            settings, token, chat = {"interval": 60, "days": 28, "notify_existing": False, "sweep": True}, "", ""
        self.token = tk.StringVar(value=token)
        self.chat = tk.StringVar(value=chat)
        self.interval = tk.StringVar(value=str(settings["interval"]))
        self.days = tk.StringVar(value=str(settings["days"]))
        self.existing = tk.BooleanVar(value=settings["notify_existing"])
        self.sweep = tk.BooleanVar(value=settings["sweep"])
        self.status = tk.StringVar(value="설정 대기 · 텔레그램을 연결한 뒤 감시 시작을 눌러 주세요.")
        self.count = tk.StringVar(value="아직 조회하지 않았어요")
        self.build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)
        if self.load_error:
            self.log(self.load_error)

    def build(self):
        root = self.root
        root.title("롯데시네마 무대인사 알리미")
        width, height = min(1080, root.winfo_screenwidth()-60), min(900, root.winfo_screenheight()-100)
        root.geometry(f"{width}x{height}")
        root.minsize(900, 700)
        root.configure(bg="#f2f4f7")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f4f7")
        style.configure("TLabel", background="#f2f4f7", font=("맑은 고딕", 10))
        style.configure("TCheckbutton", background="#f2f4f7", font=("맑은 고딕", 10))
        style.configure("TButton", font=("맑은 고딕", 10), padding=(12, 7))
        style.configure("Accent.TButton", background="#e33d46", foreground="white")
        style.map("Accent.TButton", background=[("active", "#bf2630"), ("disabled", "#d5d9df")])
        style.configure("Treeview", font=("맑은 고딕", 10), rowheight=29)
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        style.configure("TLabelframe", background="#f2f4f7")
        style.configure("TLabelframe.Label", background="#f2f4f7", font=("맑은 고딕", 10, "bold"))
        main = ttk.Frame(root, padding=24)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="무대인사, 열리면 알려드려요", font=("맑은 고딕", 23, "bold")).pack(anchor="w")
        ttk.Label(main, text="롯데시네마  ·  서울 / 경기 / 인천  ·  모든 영화", foreground="#596579").pack(anchor="w", pady=(5, 16))

        credentials = ttk.LabelFrame(main, text="1. 텔레그램 연결", padding=12)
        credentials.pack(fill="x")
        credentials.columnconfigure(1, weight=1)
        ttk.Label(credentials, text="봇 토큰").grid(row=0, column=0, padx=(0, 12), sticky="w")
        token_entry = ttk.Entry(credentials, textvariable=self.token, show="●", font=("Consolas", 10))
        token_entry.grid(row=0, column=1, sticky="ew", pady=4)
        find = ttk.Button(credentials, text="내 대화 찾기", command=self.find_chats)
        find.grid(row=0, column=2, padx=(8, 0))
        help_button = ttk.Button(credentials, text="연결 방법", command=self.help)
        help_button.grid(row=0, column=3, padx=(8, 0))
        ttk.Label(credentials, text="대화 ID").grid(row=1, column=0, sticky="w")
        self.chat_box = ttk.Combobox(credentials, textvariable=self.chat, font=("맑은 고딕", 10))
        self.chat_box.grid(row=1, column=1, sticky="ew", pady=4)
        test = ttk.Button(credentials, text="테스트 알림 보내기", command=self.test_message)
        test.grid(row=1, column=2, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Label(credentials, text="토큰은 이 PC의 Windows 계정으로 암호화해 저장합니다.", foreground="#67758a").grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))

        options = ttk.LabelFrame(main, text="2. 감시 설정", padding=12)
        options.pack(fill="x", pady=(12, 0))
        ttk.Label(options, text="공지 재확인 간격").grid(row=0, column=0, sticky="w")
        interval_entry = ttk.Spinbox(options, from_=30, to=3600, increment=30, textvariable=self.interval, width=7)
        interval_entry.grid(row=0, column=1, padx=8)
        ttk.Label(options, text="초").grid(row=0, column=2, sticky="w")
        ttk.Label(options, text="전체 시간표 범위").grid(row=0, column=3, padx=(24, 0))
        days_entry = ttk.Spinbox(options, from_=1, to=42, textvariable=self.days, width=7)
        days_entry.grid(row=0, column=4, padx=8)
        ttk.Label(options, text="일").grid(row=0, column=5)
        sweep_check = ttk.Checkbutton(options, text="공지에 없는 무대인사도 찾기 (전체 시간표 순차 조회)", variable=self.sweep)
        sweep_check.grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        existing_check = ttk.Checkbutton(options, text="처음 발견한 이미 예매 가능한 회차도 알림 받기", variable=self.existing)
        existing_check.grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))
        ttk.Label(options, text="공지 조회가 끝난 뒤 설정 간격만큼 대기합니다. 전체 순회는 28일 기준 약 30분 이상 걸릴 수 있어요.", foreground="#67758a").grid(row=3, column=0, columnspan=6, sticky="w", pady=(8, 0))

        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=14)
        self.start_button = ttk.Button(controls, text="감시 시작", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="중지", command=self.stop_monitor, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.preview_button = ttk.Button(controls, text="현재 무대인사 조회", command=self.preview)
        self.preview_button.pack(side="left")
        save_button = ttk.Button(controls, text="설정 저장", command=self.save)
        save_button.pack(side="left", padx=8)
        ttk.Label(controls, text="PC와 이 프로그램이 켜져 있어야 감시합니다.", foreground="#67758a").pack(side="right")
        self.editable = [token_entry, self.chat_box, find, test, interval_entry, days_entry,
                         sweep_check, existing_check, self.start_button, self.preview_button, save_button]

        ttk.Label(main, textvariable=self.status, foreground="#a32435", font=("맑은 고딕", 10, "bold")).pack(anchor="w")
        ttk.Label(main, textvariable=self.count, foreground="#596579").pack(anchor="w", pady=(7, 7))
        frame = ttk.Frame(main)
        columns = ("movie", "cinema", "day", "start", "screen", "seats", "state")
        self.table = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        for name, heading, width in zip(columns, ("영화", "영화관", "상영일", "시간", "상영관", "잔여석", "상태"), (220, 130, 100, 60, 100, 65, 90)):
            self.table.heading(name, text=heading)
            self.table.column(name, width=width, minwidth=width-10, anchor="w" if name in {"movie", "cinema"} else "center")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<Double-1>", self.open_booking)
        self.log_box = tk.Text(main, height=3, bg="#182334", fg="#dce5ef", font=("맑은 고딕", 9), relief="flat", padx=10, pady=8, state="disabled")
        self.log_box.pack(side="bottom", fill="x")
        ttk.Label(main, text="회차를 두 번 누르면 해당 지점 예매 페이지가 열립니다. 좌석은 마지막 조회 시점 기준입니다.", foreground="#67758a").pack(side="bottom", anchor="w", pady=(6, 8))
        frame.pack(fill="both", expand=True)

    def emit(self, kind, value):
        self.events.put((kind, value))

    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{now_kst():%H:%M:%S}] {text}\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 350:
            self.log_box.delete("1.0", "100.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def settings(self):
        try:
            interval, days = int(self.interval.get()), int(self.days.get())
        except ValueError:
            raise ServiceError("확인 간격과 날짜 범위는 정수로 입력해 주세요.") from None
        if not 30 <= interval <= 3600 or not 1 <= days <= 42:
            raise ServiceError("확인 간격은 30~3600초, 날짜 범위는 1~42일입니다.")
        return {"interval": interval, "days": days, "notify_existing": self.existing.get(), "sweep": self.sweep.get()}

    def chat_id(self):
        text = self.chat.get().strip()
        return self.chats.get(text, text)

    def save(self):
        try:
            settings = self.settings()
            if self.token.get().strip():
                Telegram(self.token.get(), self.chat_id())
            save_settings(self.directory, settings, self.token.get().strip(), self.chat_id())
            self.log("설정을 저장했습니다.")
            return True
        except (ServiceError, OSError) as exc:
            messagebox.showerror("설정 확인", str(exc), parent=self.root)
            return False

    def set_busy(self, busy, monitoring=False):
        self.busy = busy
        for widget in self.editable:
            widget.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy and monitoring else "disabled")

    def run_job(self, fn):
        if self.busy:
            return
        self.set_busy(True)
        self.stop = threading.Event()
        def worker():
            try:
                fn()
            except Stopped:
                pass
            except ServiceError as exc:
                self.emit("error", str(exc))
            except Exception as exc:
                self.emit("error", f"작업 오류 ({type(exc).__name__}). 설정과 연결을 확인해 주세요.")
            finally:
                self.emit("done", None)
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def find_chats(self):
        token = self.token.get()
        def job():
            chats = Telegram(token).chats()
            if not chats:
                raise ServiceError("내 봇의 텔레그램 대화창에서 /start를 보낸 뒤 다시 눌러 주세요. 기존 webhook을 사용하는 봇은 대화 ID를 직접 입력하세요.")
            self.emit("chats", chats)
        self.run_job(job)

    def test_message(self):
        token, chat = self.token.get(), self.chat_id()
        def job():
            Telegram(token, chat).send("✅ 롯데시네마 무대인사 알리미 연결 완료\n서울·경기·인천 / 모든 영화\n프로그램에서 ‘감시 시작’을 누르면 감시합니다.")
            self.emit("log", "텔레그램 테스트 알림을 보냈습니다.")
        self.run_job(job)

    def preview(self):
        self.status.set("현재 공지된 무대인사를 조회하는 중…")
        def job():
            client = LotteClient(self.stop)
            cinemas = client.cinemas()
            targets = set()
            for event in client.events():
                for row in client.event_rows(str(event["EventID"])):
                    cid, day = str(row.get("CinemaID", "")), str(row.get("PlayDate", ""))[:10]
                    if cid in cinemas and is_stage(row) and day >= now_kst().date().isoformat():
                        targets.add((cid, day))
            shows = []
            for cid, day in sorted(targets):
                shows.extend(parse_screenings(client.schedule(cinemas[cid], day), cinemas))
            self.emit("snapshot", shows)
            self.emit("log", f"현재 조회 완료: 수도권 {len(cinemas)}개 지점 중 공지된 무대인사 {len(shows)}회차. 알림은 보내지 않았습니다.")
        self.run_job(job)

    def start(self):
        try:
            settings = self.settings()
            token, chat = self.token.get().strip(), self.chat_id()
            Telegram(token, chat)
            if not chat:
                raise ServiceError("대화 ID를 입력하거나 ‘내 대화 찾기’를 눌러 주세요.")
            if not self.save():
                return
        except ServiceError as exc:
            messagebox.showerror("연결 설정", str(exc), parent=self.root)
            return
        self.stop = threading.Event()
        self.set_busy(True, monitoring=True)
        self.status.set("감시 시작 중…")
        self.log("감시를 시작합니다. 첫 조회의 이미 열린 회차는 설정에 따라 처리합니다.")
        def worker():
            try:
                monitor = Monitor(self.directory, settings, token, chat, self.emit, self.stop)
                monitor.telegram.call("getMe")
                monitor.telegram.call("getChat", chat_id=chat)
                monitor.run()
            except ServiceError as exc:
                self.emit("error", str(exc))
            except Exception as exc:
                self.emit("error", f"감시가 중단되었습니다 ({type(exc).__name__}). 다시 시작하기 전에 연결과 파일 저장 공간을 확인해 주세요.")
            finally:
                self.emit("done", None)
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def stop_monitor(self):
        self.stop.set()
        self.status.set("중지 중 · 현재 서버 요청이 끝나면 멈춥니다.")
        self.stop_button.configure(state="disabled")

    def close(self):
        self.closing = True
        self.stop.set()
        if self.thread and self.thread.is_alive():
            self.status.set("종료 중 · 현재 서버 요청을 마무리하고 있습니다.")
            self.set_busy(True)
        else:
            self.root.destroy()

    def update_shows(self, shows, reset=False, scope=None):
        if reset:
            self.shows.clear()
        if scope:
            self.shows = {k: v for k, v in self.shows.items() if (v.cinema_id, v.day) != scope}
        for show in shows:
            self.shows[show.key] = show
        self.shows = {k: v for k, v in self.shows.items() if show_time(v.day, v.start) > now_kst()}
        self.table.delete(*self.table.get_children())
        for show in sorted(self.shows.values(), key=lambda x: (x.day, x.start, x.cinema)):
            self.table.insert("", "end", iid=show.key, values=(show.movie, show.cinema, show.day,
                show.start, show.screen, show.seats, "예매 가능" if show.bookable else "매진/대기"))
        count = sum(s.bookable for s in self.shows.values())
        self.count.set(f"발견한 무대인사 {len(self.shows)}회차  ·  마지막 조회 시 예매 가능 {count}회차")

    def pump(self):
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.set(value)
            elif kind == "log":
                self.log(value)
            elif kind == "shows":
                cid, day, shows = value
                self.update_shows(shows, scope=(cid, day))
            elif kind == "snapshot":
                self.update_shows(value, reset=True)
            elif kind == "chats":
                self.chats = {f"{name} ({cid})": cid for cid, name in value.items()}
                self.chat_box.configure(values=list(self.chats))
                if len(self.chats) == 1:
                    self.chat.set(next(iter(self.chats)))
                else:
                    self.chat.set("")
                self.log("대화 목록을 가져왔습니다. 알림 받을 대화를 선택해 주세요.")
            elif kind == "error":
                self.status.set("오류 · 아래 기록을 확인해 주세요.")
                self.log(value)
            elif kind == "done":
                self.set_busy(False)
                if not self.status.get().startswith("오류"):
                    self.status.set("대기 · 감시 시작을 누르면 알림 감시를 시작합니다.")
        if self.closing and not (self.thread and self.thread.is_alive()):
            self.root.destroy()
            return
        self.root.after(120, self.pump)

    def open_booking(self, _event=None):
        selection = self.table.selection()
        if selection and selection[0] in self.shows:
            webbrowser.open(self.shows[selection[0]].url)

    def help(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("텔레그램 연결 방법")
        dialog.geometry("650x370")
        body = ttk.Frame(dialog, padding=22)
        body.pack(fill="both", expand=True)
        text = ("1. 텔레그램에서 공식 @BotFather를 열고 /newbot을 보냅니다.\n"
                "2. 봇 이름과 사용자명을 정하면 봇 토큰이 나옵니다.\n"
                "3. 그 토큰을 이 프로그램의 ‘봇 토큰’에 붙여넣습니다.\n"
                "4. 방금 만든 내 봇의 대화창을 열고 /start를 보냅니다.\n"
                "5. ‘내 대화 찾기’ → 대화 선택 → ‘테스트 알림 보내기’를 누릅니다.\n"
                "6. 휴대폰에서 테스트 알림을 확인한 뒤 ‘감시 시작’을 누릅니다.\n\n"
                "토큰은 비밀번호입니다. 채팅에 보내지 말고 프로그램에 직접 입력하세요.\n"
                "기존 webhook을 사용하는 봇은 대화 ID를 직접 입력할 수 있습니다.\n"
                "PC가 꺼지거나 절전 상태가 되면 감시가 멈춥니다.")
        ttk.Label(body, text=text, wraplength=600, justify="left").pack(anchor="w")
        ttk.Button(body, text="공식 BotFather 열기", command=lambda: webbrowser.open("https://t.me/BotFather")).pack(anchor="w", pady=(16, 0))

