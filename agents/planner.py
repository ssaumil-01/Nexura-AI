import os
import re
import json
from typing import Optional, List, Literal, Union, Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from google import genai
from google.genai import types

load_dotenv()

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

2. KEEP PLANS CONCISE (3-5 STEPS)
   Each step groups related work together. The Coder Agent handles the details.

3. EACH STEP HAS: step_id, title, description, dependencies
   The Coder Agent decides which tools to use — you just describe WHAT to do.

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
   - use step_id references
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

class Step(BaseModel):
    step_id: int = Field(description="Step ID starting from 1, incrementing sequentially")
    title: str = Field(description="Short title for the step")
    description: str = Field(description="Detailed description of what to accomplish in this step")
    dependencies: List[int] = Field(description="List of step_ids this step depends on", default_factory=list)

class ExecutionPlan(BaseModel):
    project_id: str = Field(description="A unique ID for the project")
    goal: str = Field(description="The main goal of the execution plan")
    steps: List[Step] = Field(description="List of execution steps")


class PlannerAgent:
    def __init__(self):
        # We configure the parser using the defined Pydantic model
        self.parser = PydanticOutputParser(pydantic_object=ExecutionPlan)
        
        # We configure the PromptTemplate
        self.prompt = PromptTemplate(
            template=PROMPT_TEMPLATE,
            input_variables=["user_prompt"],
            partial_variables={"format_instructions": self.parser.get_format_instructions()}
        )
        
        # Load environment variables
        self.api_key = os.getenv("GOOGLE_API_KEY")
        self.model_name = os.getenv("PLANNER_MODEL_NAME", "gemini-2.5-pro")

        if not self.api_key:
            print("Warning: Missing GOOGLE_API_KEY in .env")
            
        self.client = genai.Client(api_key=self.api_key) if self.api_key else None

    def call_llm(self, prompt: str) -> str:
        """Call the Gemini LLM via google-genai directly."""
        try:
            if not self.client:
                raise ValueError("GenAI Client is not initialized.")
                
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                )
            )
            return response.text.strip()
        except Exception as e:
            print(f"Gemini API Error: {e}")
            raise e

    def plan(self, user_prompt: str, project_path: str, feedback: str = None, previous_plan: dict = None) -> Optional[ExecutionPlan]:
        """
        Complete execution flow:
        1. user input -> provided as user_prompt
        2. Build LLM Prompt (with optional feedback from previous iteration)
        3. Call LLM (via google-genai directly)
        4. Validate Output and Parse (Langchain Parser)
        5. Save JSON & Markdown
        """
        if not self.client:
            print("Error: GenAI Client not configured properly. Missing API key.")
            return None

        if feedback and previous_plan:
            print("Re-planning based on user feedback...")
        else:
            print("Calling LLM to generate execution plan...")
        
        try:
            # Generate prompt with user request and formatting instructions
            formatted_prompt = self.prompt.format(user_prompt=user_prompt)
            
            # If there is feedback from the user, append it to the prompt
            if feedback and previous_plan:
                import json
                prev_plan_str = json.dumps(previous_plan, indent=2)
                formatted_prompt += f"""

========================
PREVIOUS PLAN (REJECTED BY USER)
========================
{prev_plan_str}

========================
USER FEEDBACK
========================
{feedback}

Please generate an IMPROVED plan that addresses the user's feedback above.
"""
            
            # Call LLM directly using google-genai
            output_content = self.call_llm(formatted_prompt)
            
            # Parse output into Pydantic Model (this validates schemas)
            plan_obj = self.parser.parse(output_content)
            
            print("Successfully validated generated plan schema.")
            
            # Save the plan (both JSON and Markdown for viewing)
            md_path, json_path = self.save_plan(plan_obj, project_path)
            print(f"Plan (JSON) saved to: {json_path}")
            print(f"Plan (Markdown) saved to: {md_path}")
            
            return plan_obj

        except Exception as e:
            print(f"Error during planning: {e}")
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
