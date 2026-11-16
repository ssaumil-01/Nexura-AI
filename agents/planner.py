import os
import re
from typing import Optional
from dotenv import load_dotenv

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import ValidationError
from google import genai
from google.genai import types

import config
from core.schemas import Acceptance, ExecutionPlan, Step  # noqa: F401  (re-exported for callers)
from core.task_manager import validate_plan

load_dotenv()

PLAN_ATTEMPTS = 3  # first try + 2 retries with the error fed back


class PlanError(RuntimeError):
    """The planner could not produce a valid plan."""


PROMPT_TEMPLATE = """You are a senior software architect designing execution plans for an autonomous AI system.

Your task is to convert a user request into a STRICT, structured execution plan.

⚠️ IMPORTANT:
You MUST return ONLY valid JSON.
Do NOT include explanations, markdown, or extra text.
Your response will be parsed automatically and ANY deviation will break the system.

========================
📦 OUTPUT FORMAT (STRICT)
========================

{format_instructions}

========================
🎯 OBJECTIVE
========================

Break the user request into a sequence of SMALL, ATOMIC, EXECUTABLE steps.

Each step must represent EXACTLY ONE action.

========================
📌 RULES (CRITICAL)
========================

1. ONE STEP = ONE LOGICAL UNIT OF WORK
   The Coder Agent can call multiple tools per step, so steps should be logical goals.
   ❌ BAD: "Step 1: Create index.html" → "Step 2: Write HTML structure" → "Step 3: Create style.css"
   ✅ GOOD: "Step 1: Build the complete calculator UI (HTML + CSS + JS)"

2. KEEP PLANS SMALL BUT VERIFIABLE (3-8 STEPS)
   Each step is one logical unit that can be checked on its own. The Coder Agent handles the details.

3. EACH STEP HAS: step_id, title, description, dependencies, acceptance
   The Coder Agent decides which tools to use — you just describe WHAT to do.

3b. ACCEPTANCE CHECKS (REQUIRED, objective, machine-checkable)
   Every step needs at least one check from this list (paths are relative to the project folder):
   - {{"type": "file_exists", "path": "app.py"}}
   - {{"type": "syntax_ok", "paths": ["app.py", "utils.py"]}}
   - {{"type": "command_ok", "command": "python -m pytest -q", "timeout_s": 60}}
   - {{"type": "output_contains", "command": "python main.py 2+2", "text": "4"}}
   - {{"type": "http_status", "start_command": "python app.py", "url": "http://127.0.0.1:5000/health", "expect": 200, "wait_s": 10}}
   Commands are ONE program (python, pip, node, npm, npx, uvicorn, pytest, streamlit, flask) with no shell operators (| & ; > <).
   Only a step that purely writes documentation may use "acceptance": [] together with "documentation_only": true.

4. DO NOT SPECIFY TOOLS OR FILE PATHS IN THE PLAN
   The agent autonomously picks tools based on the task description.

5. DO NOT USE VAGUE WORDS:
   ❌ "setup", "handle", "implement"
   ✅ "create", "add", "write", "define", "install", "build"

6. INCLUDE ALL REQUIRED STEPS:
   - project setup (directories + files)
   - dependency installation (if needed)
   - core implementation
   - testing / execution (if applicable)

7. DO NOT WRITE CODE

8. STEP IDS:
   - must start from 1
   - must increment sequentially

9. DEPENDENCIES:
   - use step_id references to EARLIER steps only
   - empty list if none

========================
🧠 THINKING GUIDELINES
========================

- Think like assigning tasks to a senior developer — give logical goals, not micro-instructions
- Prefer FEWER high-level steps over many tiny ones
- Ensure final output is runnable
- Avoid hidden assumptions

========================
📥 USER REQUEST
========================

{user_prompt}
"""

FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    m = FENCE.match(text.strip())
    return (m.group(1) if m else text).strip()


class PlannerAgent:
    def __init__(self):
        # The parser is only used to render format instructions for the prompt
        self.parser = PydanticOutputParser(pydantic_object=ExecutionPlan)
        self.prompt = PromptTemplate(
            template=PROMPT_TEMPLATE,
            input_variables=["user_prompt"],
            partial_variables={"format_instructions": self.parser.get_format_instructions()}
        )

        self.api_key = os.getenv("GOOGLE_API_KEY")
        self.model_name = config.model_name("planner")
        self.prompt_version = config.PROMPT_VERSIONS["planner"]

        if not self.api_key:
            print("Warning: Missing GOOGLE_API_KEY in .env")

        self.client = genai.Client(api_key=self.api_key) if self.api_key else None

    def call_llm(self, prompt: str) -> str:
        """Call Gemini with a JSON response schema so the output is structured."""
        if not self.client:
            raise ValueError("GenAI Client is not initialized.")
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=config.TEMPERATURE,
                response_mime_type="application/json",
                response_schema=ExecutionPlan,
            )
        )
        return response.text.strip()

    def plan(self, user_prompt: str, project_path: str, feedback: str = None, previous_plan: dict = None) -> ExecutionPlan:
        """Generate a validated plan. Retries up to PLAN_ATTEMPTS-1 times, feeding the error back. Raises PlanError."""
        if not self.client:
            raise PlanError("GenAI client not configured (missing GOOGLE_API_KEY).")

        print(f"[Planner] role=planner prompt_version={self.prompt_version} model={self.model_name}")
        prompt = self.prompt.format(user_prompt=user_prompt)
        if feedback and previous_plan:
            import json
            prompt += f"""

========================
PREVIOUS PLAN (REJECTED BY USER)
========================
{json.dumps(previous_plan, indent=2)}

========================
USER FEEDBACK
========================
{feedback}

Please generate an IMPROVED plan that addresses the user's feedback above.
"""

        last_error = ""
        for attempt in range(PLAN_ATTEMPTS):
            attempt_prompt = prompt if not last_error else prompt + f"""

========================
YOUR PREVIOUS ANSWER WAS INVALID
========================
{last_error}

Return corrected JSON only.
"""
            try:
                raw = self.call_llm(attempt_prompt)
                plan_obj = ExecutionPlan.model_validate_json(strip_fences(raw))
                problems = validate_plan(plan_obj)
                if problems:
                    raise ValueError("; ".join(problems))
            except (ValidationError, ValueError) as e:
                last_error = str(e)[:1500]
                print(f"[Planner] attempt {attempt + 1}/{PLAN_ATTEMPTS} invalid: {last_error[:200]}")
                continue
            except Exception as e:  # API/network failure: not fixable by re-prompting
                raise PlanError(f"Planner LLM call failed: {str(e)[:200]}") from e

            md_path, json_path = self.save_plan(plan_obj, project_path)
            print(f"Plan (JSON) saved to: {json_path}")
            print(f"Plan (Markdown) saved to: {md_path}")
            return plan_obj

        raise PlanError(f"Planner produced no valid plan after {PLAN_ATTEMPTS} attempts: {last_error[:300]}")

    def save_plan(self, plan: ExecutionPlan, project_path: str) -> tuple[str, str]:
        """
        Save the generated plan to the project workspace both as JSON and Markdown.
        """
        # Ensure project directory exists
        os.makedirs(project_path, exist_ok=True)
        
        # 1. Save JSON representation for programmatic use
        json_file = os.path.join(project_path, "plan.json")
        with open(json_file, "w", encoding="utf-8") as f:
            f.write(plan.model_dump_json(indent=2))
            
        # 2. Save Markdown representation for user readability
        md_file = os.path.join(project_path, "plan.md")
        md_content = self._generate_markdown(plan)
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_content)
            
        return md_file, json_file

    def _generate_markdown(self, plan: ExecutionPlan) -> str:
        """
        Generates a human-readable markdown version of the execution plan.
        """
        md = f"# Project: {plan.project_id}\n"
        md += f"**Goal**: {plan.goal}\n\n"
        md += "## Execution Steps\n\n"
        
        for step in plan.steps:
            deps = f" (depends on: {step.dependencies})" if step.dependencies else ""
            md += f"### Step {step.step_id}: {step.title}{deps}\n"
            md += f"{step.description}\n\n"
            for acc in step.acceptance:
                md += f"- [ ] check: {acc.model_dump_json(exclude_none=True)}\n"
            md += "\n"

        return md

if __name__ == "__main__":
    # Test Execution Flow
    planner = PlannerAgent()
    sample_request = "Create a FastAPI backend with a users route and dockerize it."
    workspace_dir = os.path.join(os.path.dirname(__file__), "..", "workspace")
    project_dir = os.path.join(workspace_dir, "proj_fastapi")
    
    generated_plan = planner.plan(sample_request, project_dir)
    if generated_plan:
        print("\nPlan object successfully returned.")