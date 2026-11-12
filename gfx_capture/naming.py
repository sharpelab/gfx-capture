"""File naming: a template with {sample}, {date}, {time}, {n} tokens."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

DEFAULT_TEMPLATE = "{sample}_{date}_{time}"
TOKENS = ("sample", "date", "time", "n")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def clean(text: str) -> str:
    """Make text safe for a Windows file name."""
    text = _BAD.sub("-", text.strip())
    return text.strip(" .") or "untitled"


def render(template: str, sample: str, counter: int, when: dt.datetime | None = None) -> str:
    when = when or dt.datetime.now()
    values = {
        "sample": clean(sample) if sample.strip() else "sample",
        "date": when.strftime("%Y%m%d"),
        "time": when.strftime("%H%M%S"),
        "n": f"{counter:04d}",
    }
    try:
        stem = template.format(**values)
    except (KeyError, IndexError, ValueError):
        stem = DEFAULT_TEMPLATE.format(**values)
    return clean(stem)


def unique_stem(folder: Path, stem: str, exts=(".RAF", ".JPG", ".HEIC", "")) -> str:
    """Append _2, _3, ... if any file (or folder) with this stem already exists."""
    candidate, k = stem, 2
    while any((folder / f"{candidate}{e}").exists() for e in exts):
        candidate, k = f"{stem}_{k}", k + 1
    return candidate


def target_folder(base: Path, by_date: bool, when: dt.datetime | None = None) -> Path:
    when = when or dt.datetime.now()
    return base / when.strftime("%Y-%m-%d") if by_date else base
