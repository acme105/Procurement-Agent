"""Small helpers: hashing and timestamps."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _round_floats(obj: Any, dp: int = 6) -> Any:
    if isinstance(obj, float):
        return None if math.isnan(obj) else round(obj, dp)
    if isinstance(obj, dict):
        return {k: _round_floats(v, dp) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, dp) for v in obj]
    return obj


def hash_obj(obj: Any) -> str:
    """Stable hash of any JSON-able value (key order and float noise don't matter)."""
    text = json.dumps(_round_floats(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")
