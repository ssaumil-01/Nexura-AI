import json
from pathlib import Path

DEFAULTS = {"title": "Notes", "max_notes": 100}


def load_settings(path="settings.json"):
    """Read settings.json next to the app; missing keys fall back to DEFAULTS."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {**DEFAULTS, **data}
