"""Minimal ctypes binding to the FUJIFILM X/GFX Camera Control SDK (XAPI).

The SDK itself is NOT part of this repository. Download it from Fujifilm
(https://www.fujifilm-x.com/global/camera-control-sdk/), accept their license,
and point gfx-capture at the folder that contains XAPI.dll.

Only the calls and constants gfx-capture needs are bound here.
"""

from __future__ import annotations

import ctypes as C
import os
import re
from pathlib import Path

L = C.c_long

# --- constants used by gfx-capture -------------------------------------------
COMPLETE = 0
IF_USB = 0x0001
PRIORITY_PC = 0x0002

AE_MANUAL = 0x0001
AE_PROGRAM = 0x0006

DRIVE_SINGLE = 0x0004
DRIVE_PIXELSHIFT = 0x0010

RELEASE_SHOOT_S1OFF = 0x0104  # full press, then let go
RELEASE_S1ON = 0x0200         # half press
RELEASE_N_S1OFF = 0x0004      # let go of half press
RELEASE_PIXELSHIFT = 0x4000   # start pixel-shift multi-shot (after half press)

FMT_RAW = 1
FMT_LIVE = 4
FMT_NONE = 5
FMT_JPEG = 7
FMT_HEIF = 0x12
STILL_FORMATS = {FMT_RAW: ".RAF", FMT_JPEG: ".JPG", FMT_HEIF: ".HEIC"}

API_START_LIVEVIEW = 0x3301
API_STOP_LIVEVIEW = 0x3302
API_SET_LIVEVIEW_QUALITY = 0x3323
API_SET_LIVEVIEW_SIZE = 0x3325
LIVEVIEW_QUALITY_FINE = 1
LIVEVIEW_SIZE_L = 1

ERR_BUSY = 0x1006
ERR_SHOOT = 0x1008
ERR_COMMUNICATION = 0x2001

ERROR_NAMES = {
    0x0000: "no error", 0x1001: "sequence", 0x1002: "parameter",
    0x1003: "invalid camera", 0x1004: "library load", 0x1005: "unsupported",
    0x1006: "busy", 0x1007: "AF timeout", 0x1008: "shoot error",
    0x1009: "frame buffer full", 0x1010: "standby", 0x1011: "no driver",
    0x1015: "invalid USB mode (set the camera to USB tether shooting)",
    0x1017: "running other function", 0x2001: "communication",
    0x2002: "timeout", 0x2003: "invalid combination", 0x2004: "write error",
    0x2005: "card full", 0x3001: "hardware", 0x9001: "internal",
    0x9002: "memory full", 0x9100: "unknown",
}


class ImageInfo(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("internal_name", C.c_char * 32),
        ("format", L),
        ("data_size", L),
        ("height", L),
        ("width", L),
        ("bit_depth", L),
        ("preview_size", L),
        ("image_handle", C.c_void_p),
    ]

    @property
    def fmt(self) -> int:
        return self.format & 0xFF

    @property
    def name(self) -> str:
        return self.internal_name.split(b"\x00", 1)[0].decode(errors="replace")


class SDKError(RuntimeError):
    def __init__(self, call: str, api: int, err: int):
        self.call, self.api, self.err = call, api, err
        super().__init__(f"{call} failed: {ERROR_NAMES.get(err, hex(err))} (api {api:#x}, err {err:#x})")


def find_sdk_dir(hint: str | None = None) -> Path | None:
    """Return a folder containing XAPI.dll, trying the hint, $GFX_SDK_DIR, then C:\\FujiSDK."""
    candidates = [hint, os.environ.get("GFX_SDK_DIR")]
    for c in candidates:
        if c and (Path(c) / "XAPI.dll").is_file():
            return Path(c)
    for root in (Path(r"C:\FujiSDK"), Path.home() / "FujiSDK"):
        if root.is_dir():
            for p in sorted(root.rglob("XAPI.dll"), reverse=True):  # newest SDK version first
                if "64bit" in str(p) and "Windows" in str(p):
                    return p.parent
    return None


def shutter_labels(sdk_dir: Path | None) -> dict[int, str]:
    """Read shutter-speed labels from the user's own SDK header (HEADERS/XAPI.H), if present."""
    labels: dict[int, str] = {}
    if sdk_dir is None:
        return labels
    for up in (sdk_dir, *sdk_dir.parents[:4]):
        h = up / "HEADERS" / "XAPI.H"
        if h.is_file():
            pat = re.compile(r"#define\s+XSDK_SHUTTER_\w+\s+(-?\d+)\s*//\s*(.+)")
            for line in h.read_text(errors="replace").splitlines():
                m = pat.match(line.strip())
                if m:
                    labels.setdefault(int(m.group(1)), m.group(2).strip())
            break
    return labels


def format_shutter(value: int, labels: dict[int, str] | None = None) -> str:
    if labels and value in labels:
        return labels[value].replace('"', " s").strip()
    if value == -1:
        return "BULB"
    if value <= 0:
        return "?"
    seconds = value / 1e6
    if seconds < 0.75:
        return f"1/{round(1 / seconds)} s"
    return f"{seconds:g} s"


def format_iso(value: int) -> str:
    return f"ISO {value}" if value > 0 else f"ISO Auto {-value}"


class XSDK:
    """Thin wrapper: every call raises SDKError on failure. Not thread-safe; use from one thread."""

    def __init__(self, sdk_dir: Path):
        self.sdk_dir = Path(sdk_dir)
        if hasattr(os, "add_dll_directory"):
            self._dll_dir = os.add_dll_directory(str(self.sdk_dir))
        self.lib = C.CDLL(str(self.sdk_dir / "XAPI.dll"))
        self.h = C.c_void_p()
        self.camera_mode = 0

    # -- error handling --
    def last_error(self) -> tuple[int, int]:
        api, err = L(0), L(0)
        self.lib.XSDK_GetErrorNumber(self.h, C.byref(api), C.byref(err))
        return api.value & 0xFFFFFFFF, err.value & 0xFFFFFFFF

    def _call(self, name: str, *args) -> None:
        if getattr(self.lib, name)(*args) != COMPLETE:
            raise SDKError(name, *self.last_error())

    # -- session --
    def init(self) -> None:
        self._call("XSDK_Init", None)

    def exit(self) -> None:
        self.lib.XSDK_Exit()

    def detect(self) -> int:
        n = L(0)
        self._call("XSDK_Detect", L(IF_USB), C.create_string_buffer(20), C.create_string_buffer(20), C.byref(n))
        return n.value

    def open(self, index: int = 0) -> None:
        mode = L(0)
        self._call("XSDK_OpenEx", C.create_string_buffer(f"ENUM:{index}".encode(), 16), C.byref(self.h), C.byref(mode), None)
        self.camera_mode = mode.value

    def close(self) -> None:
        if self.h:
            self.lib.XSDK_Close(self.h)
            self.h = C.c_void_p()

    def set_priority_pc(self) -> None:
        self._call("XSDK_SetPriorityMode", self.h, L(PRIORITY_PC))

    # -- generic get/cap helpers --
    def _get(self, name: str) -> int:
        v = L(0)
        self._call(name, self.h, C.byref(v))
        return v.value

    def _cap(self, name: str, extra: int = 0) -> list[int]:
        n = L(0)
        self._call(name, self.h, C.byref(n), None, *([None] * extra))
        arr = (L * max(n.value, 1))()
        self._call(name, self.h, C.byref(n), arr, *([C.byref(L(0))] * extra))
        return list(arr)[: n.value]

    # -- exposure --
    def get_ae_mode(self) -> int:
        return self._get("XSDK_GetAEMode")

    def set_ae_mode(self, mode: int) -> None:
        self._call("XSDK_SetAEMode", self.h, L(mode))

    def cap_shutter(self) -> list[int]:
        return self._cap("XSDK_CapShutterSpeed", extra=1)

    def get_shutter(self) -> int:
        v, bulb = L(0), L(0)
        self._call("XSDK_GetShutterSpeed", self.h, C.byref(v), C.byref(bulb))
        return v.value

    def set_shutter(self, value: int) -> None:
        self._call("XSDK_SetShutterSpeed", self.h, L(value), L(0))

    def cap_iso(self) -> list[int]:
        return self._cap("XSDK_CapSensitivity")

    def get_iso(self) -> int:
        return self._get("XSDK_GetSensitivity")

    def set_iso(self, value: int) -> None:
        self._call("XSDK_SetSensitivity", self.h, L(value))

    # -- drive / release --
    def set_drive_mode(self, mode: int) -> None:
        self._call("XSDK_SetDriveMode", self.h, L(mode))

    def get_drive_mode(self) -> int:
        return self._get("XSDK_GetDriveMode")

    def release(self, mode: int) -> None:
        self._call("XSDK_Release", self.h, L(mode), C.byref(L(0)), C.byref(L(0)))

    # -- live view --
    def set_prop(self, code: int, *values: int) -> None:
        self._call("XSDK_SetProp", self.h, L(code), L(len(values)), *[L(v) for v in values])

    def start_live_view(self) -> None:
        self.set_prop(API_START_LIVEVIEW)

    def stop_live_view(self) -> None:
        self.lib.XSDK_SetProp(self.h, L(API_STOP_LIVEVIEW), L(0))

    def configure_live_view(self) -> None:
        for code, value in ((API_SET_LIVEVIEW_QUALITY, LIVEVIEW_QUALITY_FINE), (API_SET_LIVEVIEW_SIZE, LIVEVIEW_SIZE_L)):
            try:
                self.set_prop(code, value)
            except SDKError:
                pass

    # -- image buffer --
    def image_info(self) -> ImageInfo | None:
        info = ImageInfo()
        if self.lib.XSDK_ReadImageInfo(self.h, C.byref(info)) != COMPLETE:
            api, err = self.last_error()
            if err == ERR_COMMUNICATION:
                raise SDKError("XSDK_ReadImageInfo", api, err)
            return None
        return info

    def read_image(self, size: int) -> bytes:
        buf = C.create_string_buffer(size)
        self._call("XSDK_ReadImage", self.h, buf, C.c_ulong(size))
        return buf.raw

    def delete_image(self) -> None:
        self.lib.XSDK_DeleteImage(self.h)
