"""Settings saved between runs in %APPDATA%\\gfx-capture\\config.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .naming import DEFAULT_TEMPLATE


def _config_path() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(root) / "gfx-capture" / "config.json"


@dataclass
class Config:
    sdk_dir: str = ""
    save_dir: str = str(Path.home() / "Pictures" / "GFX100S")
    by_date: bool = True
    template: str = DEFAULT_TEMPLATE
    sample: str = "sample"
    counter: int = 1

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(_config_path().read_text())
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in known})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        p = _config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))
