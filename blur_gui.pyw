"""Simple drag-and-drop GUI wrapper for deface (default theme)."""
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import (
    BooleanVar, DoubleVar, StringVar, Tk,
    filedialog, messagebox, ttk,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv"}

WIN_TITLE = "blur faces - free, local, no cap"

# Fixed values for options removed from the UI.
MASK_SCALE = 1.3
MOSAIC_SIZE = "20"

STATUS_WAITING = "等待"
STATUS_DONE = "完成"
STATUS_FAILED = "失败"
STATUS_STOPPED = "已停止"


def find_deface():
    exe = shutil.which("deface")
    if exe:
        return [exe]
    sibling = Path(sys.executable).parent / "deface"
    if sibling.exists():
        return [str(sibling)]
    if IS_WIN:
        candidate = Path(sys.executable).parent / "Scripts" / "deface.exe"
        if candidate.exists():
            return [str(candidate)]
    return [str(sibling)]


def open_path(p: Path):
    """Open a folder or file with the OS default handler."""
    if IS_WIN:
        os_startfile(p)
    elif IS_MAC:
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def os_startfile(p: Path):  # separated for testability / clarity
    import os
    os.startfile(str(p))  # noqa: S606


def parse_dropped(data: str):
    paths, buf, in_brace = [], "", False
    for ch in data:
        if ch == "{":
            in_brace = True
            continue
        if ch == "}":
            in_brace = False
            if buf:
                paths.append(buf)
                buf = ""
            continue
        if ch == " " and not in_brace:
            if buf:
                paths.append(buf)
                buf = ""
            continue
        buf += ch
    if buf:
        paths.append(buf)
    return [p for p in paths if p]


def output_for(src: Path) -> Path:
    """Output next to the input file, never overwriting: *_anonymized.mp4, _1, _2..."""
    out = src.parent / f"{src.stem}_anonymized{src.suffix}"
    n = 1
    while out.exists():
        out = src.parent / f"{src.stem}_anonymized_{n}{src.suffix}"
        n += 1
    return out


class App:
    def __init__(self, root):
        self.root = root
        root.title(WIN_TITLE)
        root.geometry("720x520")
        root.minsize(600, 440)

        self.files: list[Path] = []
        self.statuses: dict[str, str] = {}
        self.outputs: dict[str, str] = {}
        self.mode = StringVar(value="blur")
        self.thresh = DoubleVar(value=0.2)
        self.keep_audio = BooleanVar(value=True)
        self.ui_q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.current_proc: subprocess.Popen | None = None

        self._build_ui()
        self.root.after(100, self._drain_ui)

    def _build_ui(self):
        root = self.root

        # Drop zone
        drop_frame = ttk.LabelFrame(root, text="视频")
        drop_frame.pack(fill="x", padx=12, pady=(12, 6))
        self.drop_label = ttk.Label(
            drop_frame,
            text=("将视频拖到这里，或点击“添加视频”" if DND_AVAILABLE
                  else "拖放不可用，请点击“添加视频”"),
            anchor="center",
            padding=24,
        )
        self.drop_label.pack(fill="x")
        if DND_AVAILABLE:
            for w in (drop_frame, self.drop_label):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        # Options
        opts = ttk.LabelFrame(root, text="选项")
        opts.pack(fill="x", padx=12, pady=6)
        ttk.Label(opts, text="模式:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.mode_box = ttk.Combobox(
            opts, textvariable=self.mode, values=["blur", "mosaic", "solid"],
            state="readonly", width=10,
        )
        self.mode_box.grid(row=0, column=1, sticky="w", pady=6)
        self.keep_check = ttk.Checkbutton(opts, text="保留音频", variable=self.keep_audio)
        self.keep_check.grid(row=0, column=2, sticky="w", padx=16, pady=6)

        ttk.Label(opts, text="检测阈值:").grid(row=1, column=0, sticky="w", padx=8, pady=(0, 8))
        self.thresh_scale = ttk.Scale(opts, from_=0.05, to=0.6,
                                     variable=self.thresh, orient="horizontal")
        self.thresh_scale.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        self.thresh_label = ttk.Label(opts, text=f"{self.thresh.get():.2f}", width=6)
        self.thresh_label.grid(row=1, column=2, sticky="w", padx=8, pady=(0, 8))
        opts.columnconfigure(1, weight=1)
        self.thresh.trace_add("write", lambda *_: self.thresh_label.config(
            text=f"{self.thresh.get():.2f}"))

        # Queue
        q_frame = ttk.LabelFrame(root, text="队列（双击打开所在目录）")
        q_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.tree = ttk.Treeview(q_frame, columns=("status", "output"),
                                 show="tree headings", height=8)
        self.tree.heading("#0", text="文件")
        self.tree.heading("status", text="状态")
        self.tree.heading("output", text="输出")
        self.tree.column("#0", width=260)
        self.tree.column("status", width=130, anchor="center")
        self.tree.column("output", width=260)
        vsb = ttk.Scrollbar(q_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double_click)

        qbtn = ttk.Frame(root)
        qbtn.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(qbtn, text="添加视频", command=self._pick_files).pack(side="left")
        ttk.Button(qbtn, text="移除所选", command=self._remove_selected).pack(side="left", padx=6)
        ttk.Button(qbtn, text="清空", command=self._clear_queue).pack(side="left")

        # Actions + status bar
        action = ttk.Frame(root)
        action.pack(fill="x", padx=12, pady=(0, 4))
        self.start_btn = ttk.Button(action, text="开始", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(action, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        self.status = ttk.Label(root, text="就绪", anchor="w")
        self.status.pack(fill="x", padx=14, pady=(0, 12))

    # -- queue --
    def _on_drop(self, event):
        for p in parse_dropped(event.data):
            self._add_path(Path(p))
        self._refresh_queue()

    def _pick_files(self):
        paths = filedialog.askopenfilenames(
            title="选择视频",
            filetypes=[("视频", " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))),
                       ("所有文件", "*.*")],
        )
        for p in paths:
            self._add_path(Path(p))
        self._refresh_queue()

    def _add_path(self, p: Path):
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.suffix.lower() in VIDEO_EXTS:
                    self._append_file(child)
            return
        if p.suffix.lower() in VIDEO_EXTS:
            self._append_file(p)
        else:
            self.status.config(text=f"已跳过（非视频）: {p.name}")

    def _append_file(self, p: Path):
        key = str(p)
        if key not in self.statuses:
            self.files.append(p)
            self.statuses[key] = STATUS_WAITING
            self.outputs[key] = ""

    def _refresh_queue(self):
        self.tree.delete(*self.tree.get_children())
        for f in self.files:
            key = str(f)
            self.tree.insert("", "end", iid=key, text=f.name,
                             values=(self.statuses.get(key, STATUS_WAITING),
                                     self.outputs.get(key, "")))
        self._update_status_bar()

    def _remove_selected(self):
        for iid in self.tree.selection():
            self.files = [f for f in self.files if str(f) != iid]
            self.statuses.pop(iid, None)
            self.outputs.pop(iid, None)
        self._refresh_queue()

    def _clear_queue(self):
        if self.worker and self.worker.is_alive():
            return
        self.files.clear()
        self.statuses.clear()
        self.outputs.clear()
        self._refresh_queue()

    def _on_double_click(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        parent = Path(sel[0]).parent
        if parent.exists():
            open_path(parent)

    def _update_status_bar(self):
        n = len(self.files)
        self.status.config(text=f"共 {n} 个视频" if n else "就绪")

    # -- run --
    def _set_status(self, key: str, text: str, output: str | None = None):
        self.ui_q.put(("row", key, text, output))

    def _drain_ui(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                if msg[0] == "row":
                    _, key, text, output = msg
                    self.statuses[key] = text
                    if output is not None:
                        self.outputs[key] = output
                    if self.tree.exists(key):
                        self.tree.set(key, "status", text)
                        if output is not None:
                            self.tree.set(key, "output", output)
                elif msg[0] == "bar":
                    self.status.config(text=msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui)

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo("提示", "请先添加至少一个视频")
            return
        self.stop_flag.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(target=self._run_jobs,
                                       args=(list(self.files),), daemon=True)
        self.worker.start()

    def _stop(self):
        self.stop_flag.set()
        if self.current_proc and self.current_proc.poll() is None:
            try:
                self.current_proc.terminate()
            except Exception:
                pass
        self.ui_q.put(("bar", "正在停止…"))

    def _run_jobs(self, files: list[Path]):
        deface_cmd = find_deface()
        total = len(files)
        for i, f in enumerate(files, 1):
            key = str(f)
            if self.stop_flag.is_set():
                self._set_status(key, STATUS_STOPPED)
                break
            out = output_for(f)
            args = list(deface_cmd) + [
                "--thresh", f"{self.thresh.get():.3f}",
                "--mask-scale", f"{MASK_SCALE:.3f}",
                "--replacewith", self.mode.get(),
                "-o", str(out),
            ]
            if self.keep_audio.get():
                args.append("--keep-audio")
            if self.mode.get() == "mosaic":
                args += ["--mosaicsize", MOSAIC_SIZE]
            args.append(str(f))

            self._set_status(key, f"处理中 0% ({i}/{total})", str(out))
            self.ui_q.put(("bar", f"正在处理 [{i}/{total}] {f.name}"))
            rc = self._run_one(args, key, i, total)
            if rc == 0:
                self._set_status(key, STATUS_DONE, str(out))
            elif self.stop_flag.is_set():
                self._set_status(key, STATUS_STOPPED, str(out))
                break
            else:
                self._set_status(key, f"{STATUS_FAILED} (exit {rc})", str(out))
        self.root.after(0, self._jobs_done)

    def _run_one(self, args, key: str, i: int, total: int) -> int:
        try:
            self.current_proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            self._set_status(key, f"{STATUS_FAILED} (deface 未找到)")
            return -1
        assert self.current_proc.stdout is not None
        pct_re = re.compile(r"(\d{1,3})%")
        last = -1
        for line in self.current_proc.stdout:
            m = pct_re.search(line)
            if m:
                try:
                    pct = max(0, min(100, int(m.group(1))))
                except ValueError:
                    continue
                if pct != last:
                    last = pct
                    self._set_status(key, f"处理中 {pct}% ({i}/{total})")
        return self.current_proc.wait()

    def _jobs_done(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.ui_q.put(("bar", "完成" if not self.stop_flag.is_set() else "已停止"))


def main():
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
    else:
        root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
