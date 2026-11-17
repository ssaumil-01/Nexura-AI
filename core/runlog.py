"""Structured JSONL run logs. One line per event; secrets are redacted and long fields capped."""
import json
import threading
from pathlib import Path

from core.safety import redact_secrets
from core.schemas import LogRecord

CAP = 600


class RunLogger:
    def __init__(self, path: Path | str, project_id: str | None, run_id: str):
        self.path, self.project_id, self.run_id = Path(path), project_id, run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, agent: str, task_id: int | None = None, **fields) -> LogRecord:
        for k in ("input_summary", "output_summary", "result"):
            if k in fields and fields[k] is not None:
                text = redact_secrets(str(fields[k]))
                fields[k] = text if len(text) <= CAP else text[:CAP] + "..."
        rec = LogRecord(agent=agent, task_id=task_id, project_id=self.project_id, run_id=self.run_id, **fields)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(rec.model_dump_json() + "\n")
        return rec

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
