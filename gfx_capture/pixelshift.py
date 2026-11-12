"""Merge a GFX pixel-shift set into one full-colour image (no demosaicing).

The camera takes 16 frames: 4 groups of 4, the groups offset from each other by
half a pixel. Within the first group the sensor moves by whole pixels (frame 2
along y, frame 3 along x, frame 4 along both), so the 4 frames together see
every scene point through a red, two green and a blue filter. Stacking them
gives true R, G, B at every pixel at the sensor's native resolution (100 MP on
a GFX100S). The half-pixel groups (the 400 MP part) are not used; merge those
with Fujifilm's Pixel Shift Combiner if you need them.

The shift directions are chosen from the data: every candidate layout is tried
on the most detailed tile of the image and the one with the least colour
checkerboarding (lowest total variation) wins. On a soft image all candidates
look the same, and then the choice doesn't matter.

Usage:  python -m gfx_capture.pixelshift <folder with the 16 .RAF files>
"""

from __future__ import annotations

import itertools
import logging
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

XYZ_FROM_SRGB = np.array([[0.4124564, 0.3575761, 0.1804375],
                          [0.2126729, 0.7151522, 0.0721750],
                          [0.0193339, 0.1191920, 0.9503041]])
CHANNELS = ((0, (0,)), (1, (1, 3)), (2, (2,)))  # output R, G, B <- LibRaw colour indices
PAD = 2


def _load(path: Path):
    import rawpy
    with rawpy.imread(str(path)) as r:
        return {
            "data": r.raw_image_visible.copy(),
            "colors": r.raw_colors_visible.copy(),
            "black": np.array(r.black_level_per_channel, np.float32),
            "white": float(r.white_level),
            "wb": np.array(r.camera_whitebalance[:3], np.float64),
            "rgb_xyz": np.array(r.rgb_xyz_matrix[:3], np.float64),
        }


def _candidates():
    for sy, sx in itertools.product((1, -1), (1, -1)):
        for perm in itertools.permutations([(1, 0), (0, 1), (1, 1)]):
            yield [(0, 0)] + [(p[0] * sy, p[1] * sx) for p in perm]


def _merge_region(frames, colors, black, offsets, y0, y1, x0, x1) -> np.ndarray:
    """Return (3, y1-y0, x1-x0) float32 camera RGB for output pixels [y0:y1, x0:x1]."""
    out = np.zeros((3, y1 - y0, x1 - x0), np.float32)
    for data, (dy, dx) in zip(frames, offsets):
        a = data[y0 - dy:y1 - dy, x0 - dx:x1 - dx].astype(np.float32)
        col = colors[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
        a -= black[col]
        for ch, ids in CHANNELS:
            m = np.isin(col, ids)
            out[ch][m] += a[m]
    out[1] *= 0.5
    return out


def _total_variation(img: np.ndarray) -> float:
    return float(sum(np.abs(np.diff(c, axis=0)).mean() + np.abs(np.diff(c, axis=1)).mean() for c in img))


def _detail_tile(data: np.ndarray, size: int = 768) -> tuple[int, int]:
    """Top-left corner of the tile with the most fine detail (green sites of frame 1)."""
    g = data[0::2, 1::2].astype(np.float32)
    step = size // 2
    best, corner = -1.0, (data.shape[0] // 2, data.shape[1] // 2)
    for ty in range(PAD, g.shape[0] - step - PAD, step):
        for tx in range(PAD, g.shape[1] - step - PAD, step):
            t = g[ty:ty + step, tx:tx + step]
            e = float(np.abs(np.diff(t, axis=0)).mean() + np.abs(np.diff(t, axis=1)).mean())
            if e > best:
                best, corner = e, (ty * 2, tx * 2)
    return corner


def choose_offsets(frames, colors, black) -> tuple[list[tuple[int, int]], float]:
    y, x = _detail_tile(frames[0])
    y, x = max(y, PAD), max(x, PAD)
    size = 768
    scores = sorted((_total_variation(_merge_region(frames, colors, black, offs, y, y + size, x, x + size)), offs)
                    for offs in _candidates())
    best, runner_up = scores[0][0], scores[1][0]
    margin = (runner_up - best) / best if best else 0.0
    return scores[0][1], margin


def _srgb_from_cam(rgb_xyz: np.ndarray) -> np.ndarray:
    cam_from_srgb = rgb_xyz @ XYZ_FROM_SRGB
    cam_from_srgb /= cam_from_srgb.sum(axis=1, keepdims=True)
    return np.linalg.inv(cam_from_srgb).astype(np.float32)


def _encode_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def merge(raf_paths, out_tif: Path, out_jpg: Path | None = None, progress=None) -> dict:
    """Merge the first 4 frames of a pixel-shift set into a 16-bit sRGB TIFF (+ half-size JPEG preview)."""
    import tifffile
    from PIL import Image

    paths = sorted(Path(p) for p in raf_paths)
    if len(paths) < 4:
        raise ValueError(f"need at least 4 pixel-shift frames, got {len(paths)}")
    say = progress or (lambda s: None)
    say("Merging pixel shift: reading frames...")
    loaded = [_load(p) for p in paths[:4]]
    frames = [f["data"] for f in loaded]
    colors, black, white = loaded[0]["colors"], loaded[0]["black"], loaded[0]["white"]
    wb = loaded[0]["wb"] / loaded[0]["wb"][1]
    matrix = _srgb_from_cam(loaded[0]["rgb_xyz"]) * wb.astype(np.float32)[None, :]  # WB, then camera -> sRGB
    scale = 1.0 / (white - float(black.mean()))
    del loaded

    say("Merging pixel shift: finding frame offsets...")
    offsets, margin = choose_offsets(frames, colors, black)
    log.info("pixel-shift offsets %s (margin %.1f%%)", offsets, 100 * margin)

    H, W = frames[0].shape
    y_lo, y_hi, x_lo, x_hi = PAD, H - PAD, PAD, W - PAD
    out = np.empty((y_hi - y_lo, x_hi - x_lo, 3), np.uint16)
    strip = 512
    for y0 in range(y_lo, y_hi, strip):
        y1 = min(y0 + strip, y_hi)
        cam = _merge_region(frames, colors, black, offsets, y0, y1, x_lo, x_hi)
        rgb = np.tensordot(matrix, cam, axes=1) * scale        # (3, h, w)
        out[y0 - y_lo:y1 - y_lo] = (_encode_srgb(rgb).transpose(1, 2, 0) * 65535 + 0.5).astype(np.uint16)
        say(f"Merging pixel shift: {100 * (y1 - y_lo) // (y_hi - y_lo)}%")
    del frames

    out_tif = Path(out_tif)
    tifffile.imwrite(out_tif, out, photometric="rgb",
                     description=f"gfx-capture pixel-shift merge of {', '.join(p.name for p in paths[:4])}; offsets {offsets}")
    if out_jpg:
        prev = Image.fromarray((out[::2, ::2] >> 8).astype(np.uint8))
        prev.save(out_jpg, quality=92)
    return {"tif": str(out_tif), "jpg": str(out_jpg) if out_jpg else None, "offsets": offsets,
            "margin": margin, "size": (out.shape[1], out.shape[0])}


def merge_folder(folder: Path, progress=None) -> dict:
    folder = Path(folder)
    rafs = sorted(folder.glob("*.RAF"))
    stem = folder.name[: -len("_pixelshift")] if folder.name.endswith("_pixelshift") else folder.name
    base = folder.parent / f"{stem}_pixelshift_color"
    return merge(rafs, base.with_suffix(".tif"), base.with_name(base.name + "_preview.jpg"), progress)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    info = merge_folder(Path(sys.argv[1]), progress=print)
    print(info)
