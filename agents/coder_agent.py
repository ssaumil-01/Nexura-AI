import os
import json
from typing import Optional, Literal
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from google import genai
from google.genai import types

load_dotenv()

class Target(BaseModel):
    type: Literal["file", "directory", "command"] = Field(description="Type of the target: file, directory, or command")
    value: str = Field(description="The path or command string")

class CoderOutput(BaseModel):
    action_type: Literal["create_file", "modify_file", "install_dependency", "run_command", "create_directory"] = Field(description="Action to perform")
    target: Target = Field(description="Target of the action containing type and value")
    content: Optional[str] = Field(description="The raw code content or command string to execute. Null if no content needed. MUST be clean text without markdown code blocks.", default=None)

CODER_PROMPT_TEMPLATE = """You are an expert autonomous Coder Agent.

Your objective is to execute the current step from the system's execution plan.

⚠️ IMPORTANT:
You MUST return ONLY valid JSON.
Do NOT include explanations, markdown, or extra text.
Your response will be parsed automatically and ANY deviation will break the system.

========================
📦 OUTPUT FORMAT (STRICT)
========================

{format_instructions}

========================
🎯 TASK DESCRIPION
========================

Project Goal: {project_goal}

Current Task JSON:
{task_json}

========================
📌 RULES (CRITICAL)
========================

1. Determine the EXACT Action and Target from the given task.
2. If the action involves creating or modifying a file (create_file, modify_file), generate the COMPLETE and FUNCTIONAL file content.
3. Put the generated code/content into the "content" field of the JSON.
4. DO NOT wrap the "content" field in markdown blocks like ```python or ```bash unless the target file itself literally requires those.
5. If the action is a shell command or directory creation, the "content" field can be an empty string or null.
"""

class CoderAgent:
    def __init__(self):
        self.parser = PydanticOutputParser(pydantic_object=CoderOutput)
        
        self.prompt = PromptTemplate(
            template=CODER_PROMPT_TEMPLATE,
            input_variables=["project_goal", "task_json"],
            partial_variables={"format_instructions": self.parser.get_format_instructions()}
        )
        
        self.api_key = os.getenv("GOOGLE_API_KEY")
        self.model_name = os.getenv("CODER_MODEL_NAME")
        
        if not self.api_key:
            print("Warning: Missing GOOGLE_API_KEY in .env")
            
        self.client = genai.Client(api_key=self.api_key) if self.api_key else None

    def execute_step(self, project_goal: str, task: dict) -> Optional[dict]:
        if not self.client:
            print("Error: GenAI Client not configured properly. Missing API key.")
            return None

        # Format the task dict into a nice JSON string
        task_json = json.dumps(task, indent=2)

        formatted_prompt = self.prompt.format(
            project_goal=project_goal,
            task_json=task_json
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=formatted_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                )
            )
            
            output_content = response.text.strip()
            
            # Parse output into Pydantic Model (this validates schemas)
            parsed_output = self.parser.parse(output_content)
            
            return parsed_output.model_dump()

        except Exception as e:
            print(f"Coder Agent Error: {e}")
            return None
