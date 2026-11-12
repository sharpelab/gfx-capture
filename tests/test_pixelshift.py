import numpy as np

from gfx_capture import pixelshift as ps


def _scene(h, w, seed=0):
    """Smooth colour scene with sharp edges: what the camera would see."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([1000 + 3 * xx, 800 + 2 * yy, 1200 + xx + yy])
    for _ in range(40):
        y, x, r = rng.integers(0, h), rng.integers(0, w), rng.integers(3, 15)
        img[:, (yy - y) ** 2 + (xx - x) ** 2 < r * r] += rng.uniform(500, 3000, size=(3, 1))
    return img


def _shoot(scene, offsets):
    """Bayer frames (RGGB-like LibRaw indices [[0,1],[3,2]]); frame pixel (y,x) sees scene (y+dy, x+dx)."""
    H, W = scene.shape[1] - 4, scene.shape[2] - 4
    colors = np.tile(np.array([[0, 1], [3, 2]], np.uint8), (H // 2, W // 2))
    chan = np.array([0, 1, 2, 1])[colors]
    frames = []
    for dy, dx in offsets:
        sub = scene[:, 2 + dy:2 + dy + H, 2 + dx:2 + dx + W]
        frames.append(np.take_along_axis(sub, chan[None], 0)[0].astype(np.uint16))
    return frames, colors


def test_merge_recovers_scene_and_offsets():
    true = [(0, 0), (-1, 0), (0, 1), (-1, 1)]
    scene = _scene(1604, 1604)
    frames, colors = _shoot(scene, true)
    black = np.zeros(4, np.float32)
    offsets, margin = ps.choose_offsets(frames, colors, black)
    assert offsets == true and margin > 0.05
    out = ps._merge_region(frames, colors, black, offsets, 2, 402, 2, 402)
    np.testing.assert_allclose(out, scene[:, 4:404, 4:404].astype(np.int64).astype(np.float32), atol=1)


def test_other_direction_is_found_too():
    true = [(0, 0), (1, 0), (0, -1), (1, -1)]
    frames, colors = _shoot(_scene(1604, 1604, seed=3), true)
    assert ps.choose_offsets(frames, colors, np.zeros(4, np.float32))[0] == true
