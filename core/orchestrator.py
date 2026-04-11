import os

import time
from core.task_manager import TaskManager
from core.validator import Validator
from core.execution_manager import ExecutionManager
from core.context_builder import ContextBuilder
from agents.coder_agent import CoderAgent

class Orchestrator:
    def __init__(self, plan_path: str, workspace_path: str):
        print("[Orchestrator] Initializing Pipeline...")
        self.task_manager = TaskManager(plan_path)
        self.validator = Validator()
        self.execution_manager = ExecutionManager(workspace_path)
        self.context_builder = ContextBuilder(workspace_path)
        self.coder_agent = CoderAgent()
        
        # Max retries allowable
        self.MAX_RETRIES = 3
        # Dictionary to track errors during retries
        self.step_errors = {}

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
                    print("[Orchestrator] [*] All tasks successfully completed!")
                break

            step_id = task["step_id"]
            action_type = task["action_type"]
            target = task["target"]
            
            print(f"\n[Orchestrator] === Executing Task {step_id}: {task['title']} ===")
            
            error_feedback = self.step_errors.get(step_id)
            workspace_context = self.context_builder.build_context()

            # 2. Coder Agent
            print("[Orchestrator] Passing task to Coder Agent...")
            agent_response = self.coder_agent.execute_step(
                project_goal=self.task_manager.goal,
                task=task,
                error_feedback=error_feedback,
                workspace_context=workspace_context
            )
            
            if not agent_response:
                print(f"[Orchestrator] Coder Agent failed to generate response.")
                self._handle_failure(step_id, "Coder Agent failed to generate response.")
                continue
                
            # Overwrite action_type, target and content from the agent's response
            function_name = agent_response["name"]
            function_args = agent_response["args"]

            # Update the task dict to pass into ExecutionManager
            task["function_name"] = function_name
            task["function_args"] = function_args

            # 3. Validation
            print("[Orchestrator] Validating tool call schema and arguments...")
            if not self.validator.validate_tool_call(function_name, function_args):
                print(f"[Orchestrator] Task {step_id} failed security validation. Halting pipeline.")
                self.task_manager.mark_failed(step_id)
                break

            # 4. Execution
            print("[Orchestrator] Dispatching to Execution Manager...")
            success, message = self.execution_manager.execute_tool_call(function_name, function_args)
            
            if success:
                self.task_manager.mark_completed(step_id)
                self.step_errors.pop(step_id, None) # Clear error flag
                print(f"[Orchestrator] Task {step_id} marked as completed.")
            else:
                self._handle_failure(step_id, message)

    def _handle_failure(self, step_id: int, error_message: str):
        self.task_manager.mark_failed(step_id)
        current_retries = self.task_manager.get_retries(step_id)
        
        if current_retries < self.MAX_RETRIES:
            print(f"[Orchestrator] Retrying task {step_id}. Attempt {current_retries}/{self.MAX_RETRIES}")
            self.step_errors[step_id] = error_message
            self.task_manager.revert_to_pending(step_id)
            time.sleep(2) # Backoff
        else:
            print(f"[Orchestrator] Task {step_id} exhausted all retries. Failing project workflow.\nFinal Error: {error_message}")

if __name__ == "__main__":
    # Test block
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plan_path = os.path.join(base_dir, "workspace", "proj_calculator", "plan.json")
    workspace_path = os.path.join(base_dir, "workspace", "proj_calculator")
    
    if os.path.exists(plan_path):
        orchestrator = Orchestrator(plan_path, workspace_path)
        orchestrator.run()
    else:
        print(f"No existing plan found at: {plan_path}")
