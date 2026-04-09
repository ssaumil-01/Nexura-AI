import os
import re
from typing import Optional
from dotenv import load_dotenv
from huggingface_hub import InferenceClient

load_dotenv()

PLANNER_PROMPT_TEMPLATE = """You are an expert software architect and planning agent.

Your task is to convert a user request into a highly detailed, step-by-step execution plan for an autonomous coding system.

The system will execute ONE step at a time, so your plan must be extremely precise, granular, and unambiguous.

========================
🎯 OBJECTIVE
========================
Break the user request into a sequence of atomic steps that can be executed independently.

========================
📌 RULES (STRICT)
========================

1. Each step must represent ONLY ONE action.
   - One file creation OR one file modification OR one command.

2. Each step must be small and executable.
   ❌ BAD: "Build authentication system"
   ✅ GOOD: "Create auth.py"

3. Steps must be sequential and logically ordered.

4. Do NOT combine multiple actions in one step.
   ❌ BAD: "Create main.py and add routes"
   ✅ GOOD:
      Step 1: Create main.py
      Step 2: Add route to main.py

5. Use explicit file names and paths wherever possible.

6. Include dependency installation steps (e.g., pip install) when required.

7. Include execution steps (e.g., run server) at the end.

8. Avoid vague words:
   ❌ "setup", "handle", "implement", "optimize"
   ✅ "create", "add", "write", "define"

9. Do NOT assume hidden steps.
   Include everything required to make the project runnable.

10. Do NOT write any code.
    Only write the plan.

========================
📄 OUTPUT FORMAT (MANDATORY)
========================

- Output MUST be in markdown checklist format.
- Each step must follow EXACTLY this format:

[ ] Step 1: <clear instruction>
[ ] Step 2: <clear instruction>

- Do NOT include explanations.
- Do NOT include extra text before or after.

========================
🧠 THINKING GUIDELINES
========================

- Think like a senior developer breaking tasks for a junior developer.
- Ensure that each step can be executed without needing future context.
- Prefer more steps over fewer steps.
- Ensure the final system is runnable.

========================
📥 USER REQUEST
========================
{user_prompt}
"""

class PlannerAgent:
    def __init__(self, llm_client=None):
        """
        Initialize PlannerAgent.
        :param llm_client: An instantiated LLM client. If None, initializes InferenceClient from .env
        """
        if llm_client:
            self.llm_client = llm_client
        else:
            api_key = os.getenv("PLANNER_AGENT_API_KEY")
            if api_key:
                self.llm_client = InferenceClient(api_key=api_key)
            else:
                self.llm_client = None

    def plan(self, user_prompt: str, project_path: str) -> Optional[str]:
        """
        Complete execution flow:
        1. user input -> provided as user_prompt
        2. Build LLM Prompt
        3. Call LLM
        4. Get Output
        5. Validate Output
        6. Save
        """
        # 2. Build LLM Prompt
        prompt = self.build_prompt(user_prompt)
        
        # 3. Call LLM & 4. Get Output
        print("Calling LLM to generate plan...")
        output = self.call_llm(prompt)
        
        if not output:
            print("Error: Received empty output from LLM.")
            return None
        
        # 5. Validate Output
        if not self.validate_output(output):
            print("Error: Output failed validation. It must match the checklist format.")
            return None
        
        # 6. Save
        plan_path = self.save_plan(output, project_path)
        print(f"Plan saved to {plan_path}")
        
        return output

    def build_prompt(self, user_prompt: str) -> str:
        """Construct the prompt using the configured template."""
        return PLANNER_PROMPT_TEMPLATE.format(user_prompt=user_prompt)

    def call_llm(self, prompt: str) -> str:
        """
        Call the LLM using Hugging Face InferenceClient for deepseek-ai/DeepSeek-R1.
        """
        if self.llm_client:
            try:
                response = self.llm_client.chat.completions.create(
                    model="Qwen/Qwen3-Coder-Next",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=8192,
                    temperature=0.2
                )
                msg = response.choices[0].message
                content = getattr(msg, "content", "") or ""
                reasoning = getattr(msg, "reasoning", "") or ""
                
                return content.strip() if content.strip() else reasoning.strip()
            except Exception as e:
                print(f"LLM API Error: {e}")
                return ""
        
        # Example dummy response if no client is provided:
        return "[ ] Step 1: Initialize project\n[ ] Step 2: Create main.py\n"

    def validate_output(self, output: str) -> bool:
        """
        Validate that the output conforms to the mandatory checklist format:
        [ ] Step X: <clear instruction>
        """
        # Search for at least one step matching the format
        pattern = r"\[ \] Step \d+: .+"
        if re.search(pattern, output):
            return True
        return False

    def save_plan(self, plan_content: str, project_path: str) -> str:
        """
        Save the generated plan.md to the project workspace.
        """
        # Ensure project directory exists
        os.makedirs(project_path, exist_ok=True)
        
        plan_file = os.path.join(project_path, "plan.md")
        with open(plan_file, "w", encoding="utf-8") as f:
            f.write(plan_content)
            
        return plan_file

if __name__ == "__main__":
    # Test Execution Flow
    planner = PlannerAgent()
    sample_request = "Create a self-validating ai agent."
    project_dir = os.path.join(os.path.dirname(__file__), "..", "workspace", "proj_001")
    
    generated_plan = planner.plan(sample_request, project_dir)
    if generated_plan:
        print("Generated Plan:\n", generated_plan)
