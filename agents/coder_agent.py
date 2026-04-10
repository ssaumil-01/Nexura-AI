import os
from typing import Optional
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

CODER_PROMPT_TEMPLATE = """You are an expert autonomous Coder Agent.

Your objective is to generate ONLY the precise file content for the user's task.

========================
🎯 TASK DESCRIPION
========================
Project Goal: {project_goal}

Current Step: {action}
Target: {target}
Description: {description}

========================
📌 RULES (CRITICAL)
========================
1. Return ONLY the raw content to be written to the file.
2. DO NOT include markdown formatting blocks like ```python or ```bash unless the file itself expects it.
3. DO NOT output explanations, introductory text, or concluding text.
4. If writing a shell script, include the shebang if necessary.
5. Provide complete code for the requested functionality.
"""

class CoderAgent:
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY")
        self.model_name = os.getenv("CODER_MODEL_NAME", "gemini-2.5-pro")
        
        self.client = genai.Client(api_key=self.api_key) if self.api_key else None

    def generate_code(self, project_goal: str, action: str, target: str, description: str) -> Optional[str]:
        if not self.client:
            print("Error: GenAI Client not configured properly. Missing API key.")
            return None

        prompt = CODER_PROMPT_TEMPLATE.format(
            project_goal=project_goal,
            action=action,
            target=target,
            description=description
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                )
            )
            
            # Clean up the output if the LLM still tries to wrap it in markdown block.
            content = response.text.strip()
            if content.startswith("```") and content.endswith("```"):
                lines = content.split("\n")
                if len(lines) >= 2:
                    content = "\n".join(lines[1:-1]).strip()
                    
            return content

        except Exception as e:
            print(f"Coder Agent Error: {e}")
            return None
