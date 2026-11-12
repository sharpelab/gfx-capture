"""Tkinter GUI for gfx-capture."""

from __future__ import annotations

import io
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import naming, xsdk
from .camera import CameraWorker
from .config import Config, _config_path

log = logging.getLogger(__name__)
SIDEBAR = 320


class App:
    def __init__(self, root: tk.Tk, cfg: Config, sdk_dir: Path):
        self.root, self.cfg = root, cfg
        self.shutter_labels = xsdk.shutter_labels(sdk_dir)
        self.worker = CameraWorker(sdk_dir)
        self.frame_seq = 0
        self.busy = False
        self.connected = False
        self.settings: dict = {}
        self._photo = self._thumb = None

        root.title("gfx-capture")
        root.geometry("1280x800")
        root.minsize(900, 600)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        root.bind("<space>", self.on_space)

        self.worker.start()
        root.after(40, self.poll_frame)
        root.after(100, self.poll_events)

    # ---------- layout ----------
    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self.view = tk.Label(root, bg="#111", fg="#bbb", text="Waiting for camera...", font=("Segoe UI", 14))
        self.view.grid(row=0, column=0, sticky="nsew")

        side = ttk.Frame(root, padding=10, width=SIDEBAR)
        side.grid(row=0, column=1, sticky="ns")
        side.columnconfigure(0, weight=1)
        r = 0

        self.status = ttk.Label(side, text="Starting...", wraplength=SIDEBAR - 20, foreground="#666")
        self.status.grid(row=r, column=0, sticky="w"); r += 1

        # naming
        box = ttk.LabelFrame(side, text="File name", padding=8)
        box.grid(row=r, column=0, sticky="ew", pady=(10, 0)); r += 1
        box.columnconfigure(1, weight=1)
        self.sample_var = tk.StringVar(value=self.cfg.sample)
        self.template_var = tk.StringVar(value=self.cfg.template)
        ttk.Label(box, text="Sample").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.sample_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(box, text="Template").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(box, textvariable=self.template_var).grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(4, 0))
        ttk.Label(box, text="Tokens: {sample} {date} {time} {n}", foreground="#888").grid(row=2, column=0, columnspan=2, sticky="w")
        self.preview = ttk.Label(box, text="", foreground="#246")
        self.preview.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        for v in (self.sample_var, self.template_var):
            v.trace_add("write", lambda *_: self.update_preview())

        # folder
        box = ttk.LabelFrame(side, text="Save folder", padding=8)
        box.grid(row=r, column=0, sticky="ew", pady=(10, 0)); r += 1
        box.columnconfigure(0, weight=1)
        self.folder_label = ttk.Label(box, text=self.cfg.save_dir, wraplength=SIDEBAR - 40)
        self.folder_label.grid(row=0, column=0, columnspan=2, sticky="w")
        self.by_date_var = tk.BooleanVar(value=self.cfg.by_date)
        ttk.Checkbutton(box, text="Subfolder per day", variable=self.by_date_var, command=self.update_preview).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(box, text="Change...", command=self.choose_folder).grid(row=1, column=1, sticky="e", pady=(4, 0))

        # exposure
        box = ttk.LabelFrame(side, text="Exposure", padding=8)
        box.grid(row=r, column=0, sticky="ew", pady=(10, 0)); r += 1
        box.columnconfigure(1, weight=1)
        self.mode_var = tk.StringVar(value="Auto")
        self.shutter_var = tk.StringVar()
        self.iso_var = tk.StringVar()
        ttk.Label(box, text="Mode").grid(row=0, column=0, sticky="w")
        self.mode_box = ttk.Combobox(box, textvariable=self.mode_var, values=["Auto", "Manual"], state="readonly", width=14)
        self.mode_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(box, text="Shutter").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.shutter_box = ttk.Combobox(box, textvariable=self.shutter_var, state="disabled", width=14)
        self.shutter_box.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(4, 0))
        ttk.Label(box, text="ISO").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.iso_box = ttk.Combobox(box, textvariable=self.iso_var, state="disabled", width=14)
        self.iso_box.grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=(4, 0))
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: self.send_exposure())
        self.shutter_box.bind("<<ComboboxSelected>>", lambda e: self.send_exposure())
        self.iso_box.bind("<<ComboboxSelected>>", lambda e: self.send_exposure())

        # actions
        style = ttk.Style()
        style.configure("Big.TButton", font=("Segoe UI", 13, "bold"), padding=8)
        self.capture_btn = ttk.Button(side, text="Capture  (Space)", style="Big.TButton", command=self.capture)
        self.capture_btn.grid(row=r, column=0, sticky="ew", pady=(14, 0)); r += 1
        self.ps_btn = ttk.Button(side, text="Pixel shift (~1.5 min + merge)", command=self.pixelshift)
        self.ps_btn.grid(row=r, column=0, sticky="ew", pady=(6, 0)); r += 1
        ttk.Button(side, text="Open folder", command=self.open_folder).grid(row=r, column=0, sticky="ew", pady=(6, 0)); r += 1
        self.progress = ttk.Label(side, text="", wraplength=SIDEBAR - 20)
        self.progress.grid(row=r, column=0, sticky="w", pady=(8, 0)); r += 1

        # last shot
        box = ttk.LabelFrame(side, text="Last shot", padding=8)
        box.grid(row=r, column=0, sticky="ew", pady=(10, 0)); r += 1
        self.thumb = ttk.Label(box)
        self.thumb.grid(row=0, column=0)
        self.last_label = ttk.Label(box, text="(none yet)", wraplength=SIDEBAR - 40, foreground="#666")
        self.last_label.grid(row=1, column=0, sticky="w")

        self.update_preview()
        self.set_buttons()

    # ---------- naming / folders ----------
    def target(self) -> tuple[Path, str]:
        folder = naming.target_folder(Path(self.cfg.save_dir), self.by_date_var.get())
        stem = naming.render(self.template_var.get(), self.sample_var.get(), self.cfg.counter)
        return folder, naming.unique_stem(folder, stem)

    def update_preview(self) -> None:
        folder, stem = self.target()
        self.preview.config(text=f"Next: {stem}.RAF")

    def choose_folder(self) -> None:
        d = filedialog.askdirectory(initialdir=self.cfg.save_dir, title="Save photos in...")
        if d:
            self.cfg.save_dir = str(Path(d))
            self.folder_label.config(text=self.cfg.save_dir)
            self.save_cfg()
            self.update_preview()

    def open_folder(self) -> None:
        folder = naming.target_folder(Path(self.cfg.save_dir), self.by_date_var.get())
        if not folder.exists():
            folder = Path(self.cfg.save_dir)
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def save_cfg(self) -> None:
        self.cfg.sample = self.sample_var.get()
        self.cfg.template = self.template_var.get()
        self.cfg.by_date = self.by_date_var.get()
        self.cfg.save()

    # ---------- actions ----------
    def set_buttons(self) -> None:
        state = "normal" if (self.connected and not self.busy) else "disabled"
        self.capture_btn.config(state=state)
        self.ps_btn.config(state=state)
        self.mode_box.config(state="readonly" if state == "normal" else "disabled")
        manual = state == "normal" and self.settings.get("manual")
        self.shutter_box.config(state="readonly" if manual and self.settings.get("shutters") else "disabled")
        self.iso_box.config(state="readonly" if manual and self.settings.get("isos") else "disabled")

    def on_space(self, event) -> None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return
        self.capture()

    def _start_shot(self, command: str) -> None:
        if not self.connected or self.busy:
            return
        self.save_cfg()
        folder, stem = self.target()
        self.cfg.counter += 1
        self.cfg.save()
        self.busy = True
        self.set_buttons()
        self.worker.request(command, folder=str(folder), stem=stem)

    def capture(self) -> None:
        self._start_shot("capture")

    def pixelshift(self) -> None:
        if messagebox.askokcancel("Pixel shift", "Takes 16 frames (~1.8 GB) over about 1.5 minutes.\n"
                                  "Keep the setup completely still.\n\nAfterwards a full-colour TIFF is made "
                                  "automatically (about a minute). For the 400 MP version, open the frames "
                                  "in Fujifilm's Pixel Shift Combiner.", parent=self.root):
            self._start_shot("pixelshift")

    def send_exposure(self) -> None:
        manual = self.mode_var.get() == "Manual"
        kw: dict = {"manual": manual}
        if manual and self.settings.get("manual"):
            s = {self.fmt_shutter(v): v for v in self.settings.get("shutters", [])}
            i = {xsdk.format_iso(v): v for v in self.settings.get("isos", [])}
            kw["shutter"] = s.get(self.shutter_var.get())
            kw["iso"] = i.get(self.iso_var.get())
        self.busy = True
        self.set_buttons()
        self.worker.request("exposure", **kw)

    def fmt_shutter(self, v: int) -> str:
        return xsdk.format_shutter(v, self.shutter_labels)

    # ---------- polling ----------
    def poll_frame(self) -> None:
        frame = self.worker.latest_frame
        if frame and frame[0] != self.frame_seq:
            self.frame_seq = frame[0]
            w, h = max(self.view.winfo_width(), 100), max(self.view.winfo_height(), 100)
            try:
                img = Image.open(io.BytesIO(frame[1]))
                img.draft("RGB", (w, h))
                img = img.convert("RGB")
                img.thumbnail((w, h))
                self._photo = ImageTk.PhotoImage(img)
                self.view.config(image=self._photo, text="")
            except Exception:  # noqa: BLE001  (a corrupt frame is not worth crashing for)
                pass
        self.root.after(40, self.poll_frame)

    def poll_events(self) -> None:
        try:
            while True:
                kind, *payload = self.worker.events.get_nowait()
                getattr(self, f"ev_{kind}")(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def ev_status(self, text: str) -> None:
        self.status.config(text=text)
        self.connected = text == "Connected"
        if not self.connected:
            self.view.config(image="", text=text)
            self._photo = None
        self.set_buttons()

    def ev_busy(self, busy: bool) -> None:
        self.busy = busy
        if not busy and self.progress.cget("text").startswith(("Capturing", "Pixel shift: shooting")):
            self.progress.config(text="")
        self.set_buttons()

    def ev_progress(self, text: str) -> None:
        self.progress.config(text=text)

    def ev_settings(self, s: dict) -> None:
        self.settings = s
        self.mode_var.set("Manual" if s["manual"] else "Auto")
        self.shutter_box.config(values=[self.fmt_shutter(v) for v in s["shutters"]])
        self.iso_box.config(values=[xsdk.format_iso(v) for v in s["isos"]])
        self.shutter_var.set(self.fmt_shutter(s["shutter"]) if s["shutter"] is not None else "")
        self.iso_var.set(xsdk.format_iso(s["iso"]) if s["iso"] is not None else "")
        self.set_buttons()

    def ev_captured(self, paths: list[str], pixelshift: bool) -> None:
        self.update_preview()
        if not paths:
            self.progress.config(text="No image came back from the camera.")
            return
        first = Path(paths[0])
        if pixelshift:
            self.progress.config(text=f"Pixel shift done: {len(paths)} frames in {first.parent.name}")
            self.last_label.config(text=f"{first.parent.name}\\ ({len(paths)} RAFs)")
            self.thumb.config(image="")
            if len(paths) >= 4:
                self.start_merge(first.parent)
            return
        self.progress.config(text="Saved " + ", ".join(Path(p).name for p in paths))
        self.last_label.config(text=first.stem)
        jpg = next((p for p in paths if p.upper().endswith(".JPG")), None)
        if jpg:
            self.show_thumb(jpg)

    def start_merge(self, folder: Path) -> None:
        """Merge the pixel-shift frames into a full-colour TIFF on a background thread."""
        try:
            from . import pixelshift
        except ImportError as exc:
            self.progress.config(text=f"Pixel-shift merge unavailable ({exc}); install numpy, rawpy, tifffile.")
            return

        def run():
            try:
                info = pixelshift.merge_folder(folder, progress=lambda s: self.worker.events.put(("progress", s)))
                self.worker.events.put(("merged", info))
            except Exception as exc:  # noqa: BLE001
                log.exception("pixel-shift merge failed")
                self.worker.events.put(("error", f"pixel-shift merge failed: {exc}"))

        threading.Thread(target=run, daemon=True, name="merge").start()

    def ev_merged(self, info: dict) -> None:
        tif = Path(info["tif"])
        w, h = info["size"]
        self.progress.config(text=f"Merged: {tif.name} ({w}x{h}, full colour)")
        self.last_label.config(text=tif.name)
        if info.get("jpg"):
            self.show_thumb(info["jpg"])

    def show_thumb(self, path: str) -> None:
        try:
            img = Image.open(path)
            img.draft("RGB", (SIDEBAR - 40, 300))
            img = img.convert("RGB")
            img.thumbnail((SIDEBAR - 40, 300))
            self._thumb = ImageTk.PhotoImage(img)
            self.thumb.config(image=self._thumb)
        except Exception:  # noqa: BLE001
            log.exception("thumbnail failed")

    def ev_error(self, text: str) -> None:
        self.progress.config(text="Error: " + text)

    def ev_fatal(self, text: str) -> None:
        messagebox.showerror("gfx-capture", text, parent=self.root)
        self.root.destroy()

    def on_close(self) -> None:
        if self.busy and not messagebox.askokcancel("gfx-capture", "A capture is still running. Quit anyway?"):
            return
        self.save_cfg()
        self.status.config(text="Closing camera...")
        self.root.update_idletasks()
        self.worker.shutdown()
        self.root.destroy()


def main() -> None:
    log_path = _config_path().parent / "gfx-capture.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_path, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = tk.Tk()
    cfg = Config.load()
    sdk_dir = xsdk.find_sdk_dir(cfg.sdk_dir)
    if sdk_dir is None:
        messagebox.showinfo("gfx-capture", "Select the Fujifilm SDK folder that contains XAPI.dll\n"
                            "(e.g. ...\\REDISTRIBUTABLES\\Windows\\64bit).")
        d = filedialog.askdirectory(title="Folder containing XAPI.dll")
        sdk_dir = xsdk.find_sdk_dir(d)
        if sdk_dir is None:
            messagebox.showerror("gfx-capture", "XAPI.dll not found. See the README for setup.")
            return
    cfg.sdk_dir = str(sdk_dir)
    cfg.save()
    App(root, cfg, sdk_dir)
    root.mainloop()
