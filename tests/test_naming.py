import datetime as dt

from gfx_capture import naming, xsdk

WHEN = dt.datetime(2026, 11, 11, 18, 5, 9)


def test_render_default():
    assert naming.render(naming.DEFAULT_TEMPLATE, "RMG 12/b", 3, WHEN) == "RMG 12-b_20261111_180509"


def test_render_counter_and_bad_template():
    assert naming.render("{sample}-{n}", "flake", 7, WHEN) == "flake-0007"
    assert naming.render("{oops}", "flake", 7, WHEN) == "flake_20261111_180509"


def test_unique_stem(tmp_path):
    (tmp_path / "a.RAF").write_bytes(b"")
    (tmp_path / "a_2.JPG").write_bytes(b"")
    assert naming.unique_stem(tmp_path, "a") == "a_3"
    assert naming.unique_stem(tmp_path, "b") == "b"


def test_format_shutter_fallback():
    assert xsdk.format_shutter(7812) == "1/128 s"
    assert xsdk.format_shutter(2000000) == "2 s"
    assert xsdk.format_shutter(-1) == "BULB"
    assert xsdk.format_shutter(7812, {7812: '1/125"'}) == "1/125 s"


def test_image_info_layout():
    import ctypes
    assert ctypes.sizeof(xsdk.ImageInfo) == 32 + 6 * ctypes.sizeof(ctypes.c_long) + ctypes.sizeof(ctypes.c_void_p)
