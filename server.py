import os
import json
import asyncio
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv

from langgraph.types import Command
from core.graph import build_graph
import tools.file_tools as ft
import tools.system_tools as st
import tools.directory_tools as dt

from langgraph.checkpoint.memory import InMemorySaver

load_dotenv()

app = FastAPI(title="Nexura-AI Server")

# Enable CORS for frontend interaction
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage for active graphs and states
# In a production app, this would use a persistent store or database
checkpointers: Dict[str, Any] = {}

class ChatRequest(BaseModel):
    project_id: str
    prompt: Optional[str] = None
    resume_input: Optional[str] = None
    interactive: bool = False

class ProjectCreate(BaseModel):
    project_id: str

@app.get("/")
async def root():
    return {"status": "ok", "message": "Nexura-AI Server is running"}

@app.post("/projects")
async def create_project(req: ProjectCreate):
    workspace_path = os.path.join(os.path.dirname(__file__), "workspace", req.project_id)
    os.makedirs(workspace_path, exist_ok=True)
    if req.project_id not in checkpointers:
        checkpointers[req.project_id] = InMemorySaver()
    return {"project_id": req.project_id, "path": workspace_path}

@app.get("/projects")
async def list_projects():
    workspace_dir = os.path.join(os.path.dirname(__file__), "workspace")
    if not os.path.exists(workspace_dir):
        return []
    return [d for d in os.listdir(workspace_dir) if os.path.isdir(os.path.join(workspace_dir, d))]

@app.get("/files/{project_id}")
async def list_files(project_id: str, path: str = "."):
    workspace_path = os.path.join(os.path.dirname(__file__), "workspace", project_id)
    target_path = os.path.abspath(os.path.join(workspace_path, path))
    
    if not target_path.startswith(os.path.abspath(workspace_path)):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not os.path.exists(target_path):
        return []
        
    items = []
    for item in os.listdir(target_path):
        if item == ".git" or item == "__pycache__": continue
        item_path = os.path.join(target_path, item)
        is_dir = os.path.isdir(item_path)
        items.append({
            "name": item,
            "is_dir": is_dir,
            "path": os.path.relpath(item_path, workspace_path).replace("\\", "/")
        })
    return items

@app.get("/file-content/{project_id}")
async def get_file_content(project_id: str, path: str):
    workspace_path = os.path.join(os.path.dirname(__file__), "workspace", project_id)
    target_path = os.path.abspath(os.path.join(workspace_path, path))
    
    if not target_path.startswith(os.path.abspath(workspace_path)):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not os.path.exists(target_path) or os.path.isdir(target_path):
        raise HTTPException(status_code=404, detail="File not found")
        
    with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
        return {"content": f.read()}

async def event_generator(project_id: str, prompt: str = None, resume_input: str = None, interactive: bool = False):
    """Generator for streaming graph events using SSE."""
    workspace_path = os.path.join(os.path.dirname(__file__), "workspace", project_id)
    
    # Configure tools
    ft.WORKSPACE_PATH = workspace_path
    st.WORKSPACE_PATH = workspace_path
    dt.WORKSPACE_PATH = workspace_path
    
    if project_id not in checkpointers:
        checkpointers[project_id] = InMemorySaver()
    
    checkpointer = checkpointers[project_id]
    graph = build_graph(interactive=interactive, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": project_id}}
    
    if resume_input:
        # Resuming from an interrupt
        input_data = Command(resume=resume_input)
    elif prompt:
        # Starting fresh
        input_data = {
            "messages": [],
            "user_prompt": prompt,
            "workspace_path": workspace_path,
            "plan": None,
            "tasks": [],
            "current_task_index": 0,
            "execution_history": [],
            "is_interactive": interactive,
            "plan_feedback": None,
            "tool_calls_count": 0,
        }
    else:
        yield {"event": "error", "data": "Prompt or resume input required"}
        return

    try:
        # stream() is a generator
        for event in graph.stream(input_data, config, stream_mode="updates"):
            for node_name, updates in event.items():
                payload = {"node": node_name, "updates": updates}
                
                # Special handling for AI messages to stream them correctly
                if node_name == "coder_agent":
                    msgs = updates.get("messages", [])
                    for msg in msgs:
                        # Extract message content for a cleaner event
                        from langchain_core.messages import AIMessage
                        if isinstance(msg, AIMessage):
                            yield {
                                "event": "graph_update",
                                "data": json.dumps({
                                    "node": "coder_agent",
                                    "updates": {
                                        "messages": [{"content": msg.content, "type": "ai"}]
                                    }
                                })
                            }
                            continue

                # Generic update
                yield {
                    "event": "graph_update",
                    "data": json.dumps(payload, default=str)
                }

                # Tool logging
                if node_name == "tools":
                    msgs = updates.get("messages", [])
                    for msg in msgs:
                        if hasattr(msg, "content"):
                            yield {
                                "event": "tool_update",
                                "data": json.dumps({
                                    "tool": getattr(msg, "name", "unknown"),
                                    "content": str(msg.content)[:100]
                                })
                            }

        # Check for interrupts
        snapshot = graph.get_state(config)
        if snapshot.next:
            if snapshot.tasks and snapshot.tasks[0].interrupts:
                interrupt_val = snapshot.tasks[0].interrupts[0].value
                yield {
                    "event": "interrupt",
                    "data": json.dumps({"message": str(interrupt_val)})
                }
        else:
            yield {"event": "complete", "data": "Pipeline complete"}

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        yield {"event": "error", "data": str(e)}

@app.get("/chat/stream")
async def chat_stream(
    project_id: str, 
    prompt: Optional[str] = None, 
    resume_input: Optional[str] = None, 
    interactive: bool = False
):
    return EventSourceResponse(event_generator(
        project_id, 
        prompt=prompt, 
        resume_input=resume_input, 
        interactive=interactive
    ))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)