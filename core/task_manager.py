import json
import os
from typing import Dict, List, Optional, Any

class TaskManager:
    def __init__(self, plan_path: str):
        self.plan_path = plan_path
        self.plan_data = self._load_plan()
        self.tasks: List[Dict[str, Any]] = self.plan_data.get("steps", [])
        self.project_id = self.plan_data.get("project_id")
        self.goal = self.plan_data.get("goal")
        
        # Track states: "pending", "completed", "failed"
        # Track retries: Int
        self.state: Dict[int, str] = {t["step_id"]: "pending" for t in self.tasks}
        self.retries: Dict[int, int] = {t["step_id"]: 0 for t in self.tasks}

    def _load_plan(self) -> Dict:
        if not os.path.exists(self.plan_path):
            raise FileNotFoundError(f"Plan not found at {self.plan_path}")
        with open(self.plan_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_next_task(self) -> Optional[Dict[str, Any]]:
        """
        Returns the next pending task where all dependencies are completed.
        Returns None if all tasks are completed or if blocked.
        """
        for task in self.tasks:
            task_id = task["step_id"]
            if self.state[task_id] == "pending":
                deps = task.get("dependencies", [])
                deps_met = all(self.state.get(dep_id) == "completed" for dep_id in deps)
                if deps_met:
                    return task
        return None

    def mark_completed(self, task_id: int):
        self.state[task_id] = "completed"

    def mark_failed(self, task_id: int):
        self.state[task_id] = "failed"
        self.retries[task_id] += 1
        
    def get_retries(self, task_id: int) -> int:
        return self.retries.get(task_id, 0)
        
    def revert_to_pending(self, task_id: int):
        """Allows orchestrator to reset state on retry routing"""
        self.state[task_id] = "pending"
