from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Note:
    id: int
    text: str
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {"id": self.id, "text": self.text, "created": self.created}
