import os
import re
import time
from dotenv import load_dotenv

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import ValidationError
from google import genai
from google.genai import types

import config
from agents.llm import TRANSIENT, backoff_delay, quota_message
from core.schemas import Acceptance, ExecutionPlan, Step  # noqa: F401  (re-exported for callers)
from core.task_manager import validate_plan

load_dotenv()

PLAN_ATTEMPTS = 3  # first try + 2 retries with the error fed back


class PlanError(RuntimeError):
    """The planner could not produce a valid plan."""


EXISTING_SECTION = """
========================
EXISTING PROJECT (this is NOT a new project: change what is there)
========================
The workspace already contains code. Plan CHANGES to it:
- PREFER EDITING existing files over creating new ones. For every step, list in "files" the EXISTING files (paths from the file
  list below, exactly as written) the step edits, and in "new_files" the new files it creates. Never put an existing path in
  "new_files" or a path that is not in the list in "files".
- Name the specific files and functions/classes in each step's description.
- Derive acceptance checks from the project itself: use the project's own test command (below) as a command_ok check, and its
  run command for http_status checks. Do not invent commands the project does not have.
- Keep every existing behaviour working; do not rewrite or reformat code that the request does not touch.
{extra_commands}
PROJECT SUMMARY:
{summary}

PROJECT PROFILE (detected; may be edited by the user):
{profile}

FILES ({shown} of {total} shown):
{files}

CODE RELEVANT TO THE REQUEST (retrieved, reference only):
{hits}
"""

CHECK_QUALITY_SECTION = """
========================
CHECK QUALITY (V2)
========================
- A check must prove the step WORKS, not only that a file exists. syntax_ok and file_exists alone are NOT enough for a step
  that adds or changes behaviour: add at least one command_ok / output_contains / http_status check that runs the new code,
  e.g. the project's tests or `python -c "from todo import add_task; ..."` with an assert.
- Put logic in plain functions that tests can call, and keep UI files thin.
- When a step writes or changes tests, its check runs them (`python -m pytest -q`).
"""

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
   For http_status the "url" field is REQUIRED (the full http://127.0.0.1:PORT/path to request; use the port from start_command).
   Commands are ONE program from EXACTLY this list: python, python3, py, pip, node, npm, npx, uvicorn, pytest, streamlit, flask.
   NOT available (never use them in checks): curl, wget, cat, grep, ls, dir, echo, type, rm, sh, bash, powershell.
   - To check a web endpoint use an http_status check (never curl). To check file contents use
     {{"type": "output_contains", "command": "python -c \"print(open('index.html').read())\"", "text": "<h1"}}.
   - No shell operators (| & ; > <) outside quotes. Run tests with `python -m pytest -q` (not bare `pytest`).
   Acceptance checks must NEVER install packages (no `pip install`/`npm install`): installing is part of the step's work, not a check.
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

        self.client = (genai.Client(api_key=self.api_key, http_options=types.HttpOptions(timeout=config.LLM_TIMEOUT_S * 1000, api_version="v1beta"))  # v1 rejects preview models
                       if self.api_key else None)

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

    def call_text(self, prompt: str) -> str:
        """Plain-text completion with the planner's model (used for the repo summary)."""
        if not self.client:
            raise ValueError("GenAI Client is not initialized.")
        response = self.client.models.generate_content(
            model=self.model_name, contents=prompt, config=types.GenerateContentConfig(temperature=config.TEMPERATURE))
        return (response.text or "").strip()

    def _call_with_retry(self, prompt: str) -> str:
        """Transient API errors (rate limit, 5xx 'high demand', timeouts) are retried with backoff; others fail at once."""
        for attempt in range(config.LLM_MAX_ATTEMPTS):
            try:
                return self.call_llm(prompt)
            except Exception as e:
                text = str(e)
                quota = quota_message(text)
                if quota:
                    raise RuntimeError(quota) from e
                if attempt + 1 >= config.LLM_MAX_ATTEMPTS or not TRANSIENT.search(text):
                    raise
                wait = backoff_delay(attempt, text)
                print(f"[Planner] transient API error ({text[:80]}...), retrying in {wait:.0f}s")
                time.sleep(wait)
        raise AssertionError("unreachable")

    @staticmethod
    def existing_section(context: dict) -> str:
        import json
        nl = chr(10)
        extra = context.get("extra_commands") or []
        hits = nl.join(f"--- {h['path']} lines {h['lines']}" + (f" ({h['symbol']})" if h.get("symbol") else "") + nl + h["snippet"]
                       for h in context.get("hits", [])) or "(none)"
        note = f"- Besides the usual commands, these programs are approved for checks in this workspace: {', '.join(extra)}." + nl if extra else ""
        return EXISTING_SECTION.format(
            extra_commands=note, summary=context.get("summary") or "(unavailable)",
            profile=json.dumps(context.get("profile") or {}, indent=1),
            shown=len(context.get("files", [])), total=context.get("files_total", 0),
            files=nl.join(context.get("files", [])), hits=hits)

    def plan(self, user_prompt: str, project_path: str, feedback: str = None, previous_plan: dict = None,
             context: dict = None) -> ExecutionPlan:
        """Generate a validated plan. Retries up to PLAN_ATTEMPTS-1 times, feeding the error back. Raises PlanError."""
        if not self.client:
            raise PlanError("GenAI client not configured (missing GOOGLE_API_KEY).")

        print(f"[Planner] role=planner prompt_version={self.prompt_version} model={self.model_name}")
        prompt = self.prompt.format(user_prompt=user_prompt)
        extra_commands = tuple((context or {}).get("extra_commands") or ())
        exists = (context or {}).get("exists")
        if context:
            prompt += self.existing_section(context)
        if config.RETRIEVAL_MODE == "v2":   # V1 keeps its planner prompt byte for byte
            prompt += CHECK_QUALITY_SECTION
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

        last_error, last_raw = "", ""
        self.last_warnings: list[str] = []
        for attempt in range(PLAN_ATTEMPTS):
            attempt_prompt = prompt if not last_error else prompt + f"""

========================
YOUR PREVIOUS ANSWER WAS INVALID
========================
{last_error}

Reminder of the required fields per acceptance type: file_exists(path), syntax_ok(paths), command_ok(command),
output_contains(command, text), http_status(url, and usually start_command). Return corrected JSON only.
"""
            try:
                raw = last_raw = self._call_with_retry(attempt_prompt)
                plan_obj = ExecutionPlan.model_validate_json(strip_fences(raw))
                problems = validate_plan(plan_obj, extra_commands, exists)
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

        repaired = self._repair(last_raw, extra_commands)
        if repaired is not None:
            md_path, json_path = self.save_plan(repaired, project_path)
            print(f"Plan (JSON) saved to: {json_path}")
            return repaired
        raise PlanError(f"Planner produced no valid plan after {PLAN_ATTEMPTS} attempts: {last_error[:300]}")

    def _repair(self, raw: str, extra_commands=()) -> ExecutionPlan | None:
        """Last resort after the retries: drop ONLY the acceptance checks that are malformed or can never run, keep the rest of the
        plan, and say so (plan.warnings). A step left with no checks falls back to default verification (syntax of changed files
        + every command it ran must exit 0), which is weaker but honest. Returns None if the plan is broken in any other way."""
        import json
        try:
            data = json.loads(strip_fences(raw))
            warnings = []
            for step in data.get("steps", []):
                kept = []
                for entry in step.get("acceptance") or []:
                    try:
                        acc = Acceptance.model_validate(entry)
                        probe = Step(step_id=1, title="t", description="d", acceptance=[acc])
                        problems = validate_plan(ExecutionPlan(project_id="p", goal="g", steps=[probe]), extra_commands)
                        problems = [p for p in problems if "no acceptance" not in p]
                    except Exception as e:
                        problems = [str(e).splitlines()[-1] if str(e) else "invalid"]
                    if problems:
                        warnings.append(f"step {step.get('step_id')}: dropped acceptance check {json.dumps(entry)[:120]}: {problems[0][:160]}")
                    else:
                        kept.append(entry)
                step["acceptance"] = kept
                if not kept and not step.get("documentation_only"):
                    step["documentation_only"] = True   # no usable check: default verification applies; flagged below
                    warnings.append(f"step {step.get('step_id')}: no usable acceptance check; default verification only")
            plan = ExecutionPlan.model_validate(data)
            if validate_plan(plan, extra_commands) or not warnings:
                return None
            self.last_warnings = warnings
            for w in warnings:
                print(f"[Planner] WARNING: {w}")
            return plan
        except Exception:
            return None

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