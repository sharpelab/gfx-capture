"""Camera worker thread. All SDK calls happen on this one thread.

The GUI talks to it through `request(...)` (commands in) and `events` (results out).
Live-view frames are not queued; the newest one sits in `latest_frame`.

GFX100S notes (learned the hard way):
  * live view needs PC priority, and shutter control in PC priority is XSDK_Release
  * a shot fails with SHOOT_ERROR while live view runs, so live view is stopped for each shot
  * right after a shot the camera reports BUSY for a few seconds; StartLiveView is retried
  * StartLiveView also returns BUSY until the image buffer is empty, so the buffer is drained first
  * pixel shift = drive mode PIXELSHIFT, half press, then RELEASE_PIXELSHIFT; 16 RAFs arrive ~5 s apart
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

from . import xsdk
from .xsdk import SDKError

log = logging.getLogger(__name__)


class CameraWorker(threading.Thread):
    def __init__(self, sdk_dir: Path):
        super().__init__(daemon=True, name="camera")
        self.sdk_dir = Path(sdk_dir)
        self.events: queue.Queue = queue.Queue()
        self._commands: queue.Queue = queue.Queue()
        self._quit = threading.Event()
        self._frame_lock = threading.Lock()
        self._frame: tuple[int, bytes] | None = None
        self._frame_seq = 0
        self.sdk: xsdk.XSDK | None = None
        self.connected = False
        self.live = False

    # ---- API used by the GUI ----
    def request(self, name: str, **kwargs) -> None:
        self._commands.put((name, kwargs))

    def shutdown(self, timeout: float = 10.0) -> None:
        self._quit.set()
        self.join(timeout)

    @property
    def latest_frame(self) -> tuple[int, bytes] | None:
        with self._frame_lock:
            return self._frame

    def _emit(self, kind: str, *payload) -> None:
        self.events.put((kind, *payload))

    # ---- thread body ----
    def run(self) -> None:
        try:
            self.sdk = xsdk.XSDK(self.sdk_dir)
            self.sdk.init()
        except Exception as exc:  # noqa: BLE001
            log.exception("SDK load failed")
            self._emit("fatal", f"Could not load the Fujifilm SDK from {self.sdk_dir}:\n{exc}")
            return
        try:
            while not self._quit.is_set():
                if not self.connected:
                    self._try_connect()
                    if not self.connected:
                        self._sleep(2.0)
                        continue
                try:
                    self._handle_commands()
                    self._pump_live_view()
                except SDKError as exc:
                    if exc.err == xsdk.ERR_COMMUNICATION:
                        log.warning("lost camera: %s", exc)
                        self._disconnect()
                        self._emit("status", "Camera disconnected; reconnecting...")
                    else:
                        log.exception("camera error")
                        self._emit("error", str(exc))
        finally:
            self._disconnect()
            self.sdk.exit()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._quit.is_set():
            time.sleep(0.05)

    # ---- connection ----
    def _try_connect(self) -> None:
        sdk = self.sdk
        try:
            if sdk.detect() < 1:
                self._emit("status", "Waiting for camera (USB, tether shooting mode)...")
                return
            sdk.open(0)
            sdk.set_priority_pc()
            sdk.set_drive_mode(xsdk.DRIVE_SINGLE)
            sdk.configure_live_view()
            self.connected = True
            self._start_live_view()
            self._emit("status", "Connected")
            self._emit_settings()
        except SDKError as exc:
            log.warning("connect failed: %s", exc)
            sdk.close()
            self.connected = False
            hint = " (power-cycle the camera if this persists)" if exc.err == xsdk.ERR_BUSY else ""
            self._emit("status", f"Waiting for camera: {xsdk.ERROR_NAMES.get(exc.err, hex(exc.err))}{hint}")

    def _disconnect(self) -> None:
        if self.sdk and self.connected:
            try:
                self.sdk.stop_live_view()
                self._drain(None)
            except SDKError:
                pass
            self.sdk.close()
        self.connected = False
        self.live = False

    # ---- live view ----
    def _drain(self, leftover_dir: Path | None) -> None:
        """Empty the camera buffer. Live frames are dropped; stray stills are saved, never lost."""
        for _ in range(200):
            info = self.sdk.image_info()
            if info is None or info.fmt == xsdk.FMT_NONE:
                return
            data = self.sdk.read_image(info.data_size)
            if info.fmt in xsdk.STILL_FORMATS:
                folder = leftover_dir or Path.home() / "Pictures"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"leftover_{info.name or int(time.time())}").write_bytes(data)
            time.sleep(0.02)

    def _start_live_view(self, timeout: float = 20.0) -> None:
        sdk = self.sdk
        sdk.stop_live_view()  # clears a live view left running by a crashed session
        end = time.time() + timeout
        while True:
            self._drain(None)
            try:
                sdk.start_live_view()
                self.live = True
                return
            except SDKError as exc:
                if exc.err != xsdk.ERR_BUSY or time.time() > end:
                    self.live = False
                    raise
                time.sleep(0.25)

    def _pump_live_view(self) -> None:
        if not self.live:
            time.sleep(0.05)
            return
        info = self.sdk.image_info()
        if info is None or info.fmt == xsdk.FMT_NONE:
            time.sleep(0.02)
            return
        data = self.sdk.read_image(info.data_size)
        if info.fmt == xsdk.FMT_LIVE:
            with self._frame_lock:
                self._frame_seq += 1
                self._frame = (self._frame_seq, data)

    # ---- commands ----
    def _handle_commands(self) -> None:
        while True:
            try:
                name, kw = self._commands.get_nowait()
            except queue.Empty:
                return
            handler = getattr(self, f"_cmd_{name}")
            self._emit("busy", True)
            try:
                handler(**kw)
            except SDKError as exc:
                if exc.err == xsdk.ERR_COMMUNICATION:
                    raise
                log.exception("command %s failed", name)
                self._emit("error", f"{name}: {exc}")
            finally:
                if self.connected and not self.live:
                    try:
                        self._start_live_view()
                    except SDKError as exc:
                        self._emit("error", f"Live view did not restart: {exc}")
                self._emit("busy", False)

    def _emit_settings(self) -> None:
        sdk = self.sdk
        manual = sdk.get_ae_mode() == xsdk.AE_MANUAL
        settings = {"manual": manual, "shutter": None, "iso": None, "shutters": [], "isos": []}
        if manual:
            for key, cap, get in (("shutter", sdk.cap_shutter, sdk.get_shutter), ("iso", sdk.cap_iso, sdk.get_iso)):
                try:
                    settings[key + "s"] = cap()
                    settings[key] = get()
                except SDKError as exc:
                    log.warning("reading %s failed: %s", key, exc)
        self._emit("settings", settings)

    def _pause_live_view(self, leftover_dir: Path | None = None) -> None:
        """Settings changes and shots return BUSY/SHOOT_ERROR while live view runs."""
        self.sdk.stop_live_view()
        self.live = False
        self._drain(leftover_dir)

    def _ensure_single_drive(self, timeout: float = 20.0) -> None:
        """Make sure the camera is in single-shot drive mode.

        Right after a pixel shift the camera is BUSY for a while, and a normal release
        in pixel-shift drive mode fails with SHOOT_ERROR, so retry until it takes.
        """
        sdk = self.sdk
        end = time.time() + timeout
        while True:
            try:
                if sdk.get_drive_mode() == xsdk.DRIVE_SINGLE:
                    return
                sdk.set_drive_mode(xsdk.DRIVE_SINGLE)
                log.info("drive mode set back to single")
            except SDKError as exc:
                if exc.err == xsdk.ERR_COMMUNICATION or time.time() > end:
                    raise
            self._drain(None)
            time.sleep(0.5)

    def _cmd_exposure(self, manual: bool, shutter: int | None = None, iso: int | None = None) -> None:
        sdk = self.sdk
        self._pause_live_view()
        sdk.set_ae_mode(xsdk.AE_MANUAL if manual else xsdk.AE_PROGRAM)
        if manual:
            if shutter is not None:
                sdk.set_shutter(shutter)
            if iso is not None:
                sdk.set_iso(iso)
        self._emit_settings()

    def _collect(self, save, expected: int, timeout: float, quiet: float, progress: bool = False) -> int:
        """Read stills off the camera, passing each to save(info, data). Returns the count."""
        start = time.time()
        got, last = 0, None
        while time.time() - start < timeout:
            info = self.sdk.image_info()
            fmt = info.fmt if info else xsdk.FMT_NONE
            if fmt in xsdk.STILL_FORMATS:
                save(info, self.sdk.read_image(info.data_size))
                got, last = got + 1, time.time()
                if progress:
                    self._emit("progress", f"Pixel shift: {got}/{expected} frames transferred")
            elif fmt == xsdk.FMT_LIVE:
                self.sdk.read_image(info.data_size)
            else:
                if last is not None and (time.time() - last > quiet or (got >= expected and time.time() - last > 1.5)):
                    break
                time.sleep(0.1)
        return got

    def _cmd_capture(self, folder: str, stem: str) -> None:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        sdk = self.sdk
        self._emit("progress", "Capturing...")
        self._pause_live_view(folder)
        self._ensure_single_drive()
        sdk.release(xsdk.RELEASE_SHOOT_S1OFF)
        saved: list[Path] = []

        def save(info, data):
            path = folder / f"{stem}{xsdk.STILL_FORMATS[info.fmt]}"
            path.write_bytes(data)
            saved.append(path)
            self._emit("progress", f"Saved {path.name}")

        self._collect(save, expected=2, timeout=120, quiet=3.0)
        self._emit("captured", [str(p) for p in saved], False)

    def _cmd_pixelshift(self, folder: str, stem: str) -> None:
        out = Path(folder) / f"{stem}_pixelshift"
        out.mkdir(parents=True, exist_ok=True)
        sdk = self.sdk
        self._emit("progress", "Pixel shift: shooting (keep everything still)...")
        self._pause_live_view(Path(folder))
        saved: list[Path] = []
        try:
            sdk.set_drive_mode(xsdk.DRIVE_PIXELSHIFT)
            sdk.release(xsdk.RELEASE_S1ON)
            sdk.release(xsdk.RELEASE_PIXELSHIFT)

            def save(info, data):
                path = out / f"{stem}_{len(saved) + 1:02d}{xsdk.STILL_FORMATS[info.fmt]}"
                path.write_bytes(data)
                saved.append(path)

            self._collect(save, expected=16, timeout=600, quiet=20.0, progress=True)
        finally:
            try:
                sdk.release(xsdk.RELEASE_N_S1OFF)
            except SDKError as exc:
                log.info("half-press release after pixel shift: %s", exc)
            try:
                self._ensure_single_drive()
            except SDKError as exc:
                log.error("could not leave pixel-shift drive mode: %s", exc)
                self._emit("error", "Camera is still in pixel-shift mode; the next capture will retry switching back.")
        self._emit("captured", [str(p) for p in saved], True)
