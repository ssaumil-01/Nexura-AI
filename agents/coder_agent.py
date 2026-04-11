import os
import json
from typing import Optional
from dotenv import load_dotenv

from langchain_core.prompts import PromptTemplate
from google import genai
from google.genai import types

load_dotenv()

# Define the Prompt
CODER_PROMPT_TEMPLATE = """You are an expert autonomous Coder Agent.

Your objective is to execute the current step from the system's execution plan by invoking the appropriate tool.

========================
🎯 TASK DESCRIPION
========================

Project Goal: {project_goal}

Current Task JSON:
{task_json}

========================
📁 CURRENT WORKSPACE STATE
========================
{workspace_context}
{error_feedback_section}
========================
📌 RULES (CRITICAL)
========================

1. Read the current task, determine what needs to be done.
2. Select the EXACT tool required. 
3. Populate all required tool arguments (for file actions, generate the complete functional code).
4. Do NOT wrap code content in markdown formatting like ```python - provide raw code.
"""

# Define the Tool mapping schemas for Gemini
tools_list = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="create_file",
                description="Creates a new file in the workspace with the specified content.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "target_file": types.Schema(type=types.Type.STRING, description="The absolute or relative file path to create."),
                        "content": types.Schema(type=types.Type.STRING, description="The complete raw content to write to the file.")
                    },
                    required=["target_file", "content"]
                )
            ),
            types.FunctionDeclaration(
                name="write_file",
                description="Overwrites an existing file in the workspace with the specified content.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "target_file": types.Schema(type=types.Type.STRING, description="The file path to update."),
                        "content": types.Schema(type=types.Type.STRING, description="The complete modified code.")
                    },
                    required=["target_file", "content"]
                )
            ),
            types.FunctionDeclaration(
                name="install_dependency",
                description="Installs a language or system dependency.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "target_value": types.Schema(type=types.Type.STRING, description="The command to install the dependency (e.g. 'pip install requests').")
                    },
                    required=["target_value"]
                )
            ),
            types.FunctionDeclaration(
                name="run_command",
                description="Executes a safe shell command.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "target_value": types.Schema(type=types.Type.STRING, description="The shell command string to execute.")
                    },
                    required=["target_value"]
                )
            ),
            types.FunctionDeclaration(
                name="create_directory",
                description="Creates a new directory.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "target_dir": types.Schema(type=types.Type.STRING, description="The path of the directory to create.")
                    },
                    required=["target_dir"]
                )
            ),
        ]
    )
]

class CoderAgent:
    def __init__(self):
        self.prompt = PromptTemplate(
            template=CODER_PROMPT_TEMPLATE,
            input_variables=["project_goal", "task_json", "error_feedback_section", "workspace_context"]
        )
        
        self.api_key = os.getenv("GOOGLE_API_KEY")
        self.model_name = os.getenv("CODER_MODEL_NAME")
        
        if not self.api_key:
            print("Warning: Missing GOOGLE_API_KEY in .env")
            
        self.client = genai.Client(api_key=self.api_key) if self.api_key else None

    def execute_step(self, project_goal: str, task: dict, error_feedback: str = None, workspace_context: str = "") -> Optional[dict]:
        if not self.client:
            print("Error: GenAI Client not configured properly. Missing API key.")
            return None

        task_json = json.dumps(task, indent=2)
        
        error_feedback_section = ""
        if error_feedback:
            error_feedback_section = f"\n========================\n⚠️ PREVIOUS EXECUTION FAILED\n========================\nThe previous attempt failed with the following error:\n{error_feedback}\n\nPlease fix the issue and try again.\n"

        formatted_prompt = self.prompt.format(
            project_goal=project_goal,
            task_json=task_json,
            error_feedback_section=error_feedback_section,
            workspace_context=workspace_context
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=formatted_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    tools=tools_list
                )
            )
            
            # Extract the actual native function call
            if response.function_calls:
                call = response.function_calls[0]
                
                # Convert args back to a standard dict
                args_dict = {}
                if call.args:
                    args_dict = dict(call.args)
                
                return {
                    "name": call.name,
                    "args": args_dict
                }
            else:
                print("Error: The model failed to invoke any tool natively.")
                return None

        except Exception as e:
            print(f"Coder Agent Error: {e}")
            return None
