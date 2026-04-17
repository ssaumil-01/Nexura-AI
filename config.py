"""
Nexura AI -- Central Configuration & Observability Setup
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- LANGSMITH OBSERVABILITY --------------------------------

def configure_langsmith():
    api_key = os.getenv("LANGCHAIN_API_KEY", "")
    project = os.getenv("LANGCHAIN_PROJECT", "nexura-ai")

    if api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = api_key
        os.environ["LANGCHAIN_PROJECT"] = project
        os.environ["LANGCHAIN_ENDPOINT"] = os.getenv(
            "LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"
        )
        print(f"[Config] LangSmith tracing ENABLED  -> project: {project}")
    else:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        print("[Config] LangSmith tracing DISABLED (no LANGCHAIN_API_KEY found)")

configure_langsmith()