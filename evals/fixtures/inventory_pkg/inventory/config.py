import json
from pathlib import Path

DEFAULTS = {"tax_rate": 0.0, "low_stock_threshold": 5, "currency": "USD"}


def load_config(path="inventory.json"):
    """Settings from inventory.json; unknown keys are kept, missing ones use DEFAULTS."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {**DEFAULTS, **data}
