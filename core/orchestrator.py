import os
import time
from core.task_manager import TaskManager
from core.validator import Validator
from core.execution_manager import ExecutionManager
from agents.coder_agent import CoderAgent

class Orchestrator:
    def __init__(self, plan_path: str, workspace_path: str):
        print("[Orchestrator] Initializing Pipeline...")
        self.task_manager = TaskManager(plan_path)
        self.validator = Validator()
        self.execution_manager = ExecutionManager(workspace_path)
        self.coder_agent = CoderAgent()
        
        # Max retries allowable
        self.MAX_RETRIES = 3

    def run(self):
        print(f"[Orchestrator] Starting execution for Project: {self.task_manager.project_id}")
        
        while True:
            # 1. Fetch next task
            task = self.task_manager.get_next_task()
            
            if not task:
                pending_count = sum(1 for status in self.task_manager.state.values() if status == "pending")
                if pending_count > 0:
                    print("[Orchestrator] Execution halted! Tasks are blocked by uncompleted dependencies.")
                else:
                    print("[Orchestrator] 🎉 All tasks successfully completed!")
                break

            step_id = task["step_id"]
            action_type = task["action_type"]
            target = task["target"]
            
            print(f"\n[Orchestrator] === Executing Task {step_id}: {task['title']} ===")
            
            # 2. Validation
            print("[Orchestrator] Validating task schema...")
            if not self.validator.validate_action(action_type, target):
                print(f"[Orchestrator] Task {step_id} failed security validation. Halting pipeline.")
                self.task_manager.mark_failed(step_id)
                break

            # 3. Decision Routing (Agent vs Direct)
            content = None
            if action_type in ["create_file", "modify_file"]:
                print("[Orchestrator] Task requires content generation. Calling Coder Agent...")
                content = self.coder_agent.generate_code(
                    project_goal=self.task_manager.goal,
                    action=action_type,
                    target=target["value"],
                    description=task["description"]
                )
                if not content:
                    print(f"[Orchestrator] Coder Agent failed to generate content.")
                    self._handle_failure(step_id)
                    continue
            else:
                print("[Orchestrator] Action is explicitly simple. Bypassing Agent.")

            # 4. Execution
            print("[Orchestrator] Dispatching to Execution Manager...")
            success = self.execution_manager.execute_task(task, content)
            
            if success:
                self.task_manager.mark_completed(step_id)
                print(f"[Orchestrator] Task {step_id} marked as completed.")
            else:
                self._handle_failure(step_id)

    def _handle_failure(self, step_id: int):
        self.task_manager.mark_failed(step_id)
        current_retries = self.task_manager.get_retries(step_id)
        
        if current_retries < self.MAX_RETRIES:
            print(f"[Orchestrator] Retrying task {step_id}. Attempt {current_retries}/{self.MAX_RETRIES}")
            self.task_manager.revert_to_pending(step_id)
            time.sleep(2) # Backoff
        else:
            print(f"[Orchestrator] Task {step_id} exhausted all retries. Failing project workflow.")

if __name__ == "__main__":
    # Test block
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plan_path = os.path.join(base_dir, "workspace", "proj_fastapi", "plan.json")
    workspace_path = os.path.join(base_dir, "workspace", "proj_fastapi")
    
    if os.path.exists(plan_path):
        orchestrator = Orchestrator(plan_path, workspace_path)
        orchestrator.run()
    else:
        print(f"No existing plan found at: {plan_path}")
