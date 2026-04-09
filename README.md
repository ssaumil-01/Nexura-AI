# Nexura AI — Orchestrated Graph-RAG powered autonomous code generation system

## 1. Vision

Build a Python-based autonomous coding system that:
· Takes a natural language prompt
· Plans the project (plan.md)
· Builds code incrementally (file-by-file)
· Executes and debugs
· Uses tools (filesystem, terminal, github)
· Evolves with RAG → Graph RAG

## 2. System Philosophy

· Planner defines WHAT
· RAG defines WHERE
· Agent defines HOW
· Orchestrator controls EVERYTHING

STRICT RULE: Agents NEVER control flow. Orchestrator ALWAYS controls execution.

## 3. System Architecture

High-Level Flow:
User Input → Planner Agent → plan.md → Orchestrator Loop → (RAG Layer) → Coder Agent → Tools Execution → Debugger Agent → Repeat

## 4. Repository Structure

### 4.1 Root Structure (Nexura AI)

```
nexura-ai/
├── agents/            # Planner, Coder, Debugger agents
├── tools/             # Filesystem, Terminal, GitHub MCP tools
├── core/              # Orchestrator, validators, task manager
├── rag/               # Retrieval (V2) and Graph layer (V3)
├── memory/            # State, logs, history
├── sandbox/           # Execution isolation (Docker later)
├── config.py          # Global configs
├── main.py            # Entry point
└── workspace/         # Generated projects (STRICT ISOLATION)
    ├── proj_001/
    │   ├── plan.md
    │   ├── main.py
    │   ├── requirements.txt
    │   └── src/
    ├── proj_002/
    │   ├── plan.md
    │   └── app/
```

### 4.2 Workspace (Execution Layer)

Purpose:
All AI-generated code lives here. This directory is fully isolated from system logic.

Rules:
· NEVER write outside workspace/
· Each project has its own folder
· All tools must operate within project_path

### 4.3 Project Path Handling

```
project_id = "proj_001"
project_path = f"workspace/{project_id}/"
```

### 4.4 Tool Scoping

· Filesystem: full_path = os.path.join(project_path, relative_path)
· Terminal: subprocess.run(cmd, cwd=project_path)

### 4.5 Guardrails (Workspace Level)

· Block path traversal (../)
· Block absolute paths
· Restrict writes to project directory
· Optional: restrict file types

### 4.6 Lifecycle

Prompt → Create Project → plan.md → Build → Run → Debug → (V3: Commit → Deploy)

## 5. plan.md (MASTER CONTROL FILE)

Will Convert into Nosql database in V2+ for better management.

Structure:
· Project metadata
· Step-by-step tasks
· Status tracking

Example:

```
# Project: FastAPI App
## Steps
[ ] Step 1: Create main.py
[ ] Step 2: Initialize FastAPI app
[ ] Step 3: Add route
[ ] Step 4: Run server
```

## 6. Agents (Detailed)

### 6.1 Planner Agent

· Purpose: Convert prompt → structured plan
· Output: plan.md
· Rules: Must produce granular steps; No vague tasks

### 6.2 Coder Agent

· Purpose: Execute one task at a time
· Allowed Actions: read_file, write_file
· Constraints: One file per step; Must follow plan

### 6.3 Debugger Agent

· Purpose: Fix runtime errors
· Input: error logs
· Constraints: Cannot add new features; Only fix current issue

## 7. Tools (Detailed)

### 7.1 Filesystem Tool

· Functions: read_file(path), write_file(path, content), list_files()
· Purpose: Project creation and modification

### 7.2 Terminal Tool

· Functions: run_command(cmd)
· Purpose: Execute project; Install dependencies

### 7.3 GitHub Tool (V2)

· Functions: create_repo(), commit(), push()

## 8. Orchestrator (CORE SYSTEM)

Responsibilities:
· Load plan.md
· Pick next task
· Enforce constraints
· Call agents
· Validate outputs
· Handle retries

Execution Loop:

```
while not done:
    task = get_next_task()
    response = coder_agent(task)
    validate(response)
    apply(response)
    result = execute()
    if result.failed:
        debugger_agent(result)
    else:
        mark_done(task)
```

## 9. Guardrails (STRICT)

· 9.1 Task Enforcement: Only current step allowed
· 9.2 One File Rule: Only one file per action
· 9.3 Retry Limit: Max 3 retries
· 9.4 Mandatory Read Before Write
· 9.5 JSON Validation
· 9.6 No Plan Modification by Agents

## 10. RAG Architecture

· V1: No RAG
· V2: Embeddings + in-memory search
· V3: Graph RAG

## 11. Version Breakdown

### V1

· Features: Planner, Orchestrator, File + Terminal tools, Basic debug loop
· Goal: Build simple backend

### V2

· Features: Embedding-based retrieval, Context awareness

### V3

· Features: Graph-based dependency tracking, Hybrid retrieval, GitHub MCP integration (automated repo + CI/CD + deployment)

GitHub MCP Integration (V3):
· Purpose: Enable agents (via orchestrator control) to manage repository lifecycle and trigger deployments safely.
· Capabilities: create_repo(name), create_branch(name), commit(files, message), push(branch), open_pull_request()

## 12. Memory Architecture

### 12.1 Overview

Nexura AI uses a layered memory system to support decision-making, context retrieval, and reproducibility.

### 12.2 Memory Types

1. Session Memory (Short-Term)
   Tracks current execution state and maintains iteration context.

```
{
  "project_id": "proj_001",
  "current_task": "Create main.py",
  "step_number": 1,
  "attempt": 1,
  "last_action": {
    "action": "write_file",
    "path": "main.py"
  }
}
```

2. Project Memory (Long-Term per Project)
   Tracks full project lifecycle and enables resumability.

3. Retrieval Memory (RAG Layer)
   Provides relevant context to agents.

4. System Memory (Logs & Debugging)
   Trace execution, debug failures, and enable reproducibility.

### 12.3 Memory Folder Structure

```
memory/
├── session/
│   └── current_session.json
├── projects/
│   └── proj_001.json
├── logs/
│   └── execution_logs.json
└── rag/
    ├── embeddings.pkl
    └── index.faiss
```

### 12.4 Memory Flow

Planner → plan.md → Project Memory updated → Orchestrator Loop → Session Memory updated → RAG Memory queried → Agent executes → System Memory logs results.

### 12.5 Memory Access Pattern

```
session = load_session()
project = load_project_memory()
context = rag.retrieve(task)
agent_input = {
    "task": session["current_task"],
    "project_state": project,
    "context": context
}
```

### 12.6 Guardrails

· Agents cannot directly modify memory
· Only orchestrator updates memory
· All memory must follow strict schema validation
· Limit context size passed to agents

### 12.7 Future Enhancements (V3+)

· Learning Memory: Pattern recognition for common errors and solutions.
· Graph Memory: Nodes (files, functions) and Edges (dependencies, imports, calls).

## 12A. Action Schema (MANDATORY)

```
{
  "action": "write_file | read_file | run_command | commit | push",
  "path": "string (relative only)",
  "content": "string (optional)",
  "command": "string (optional)",
  "metadata": {}
}
```

## 12B. Validator Module

File: core/validator.py
Responsibilities:
· validate_json_schema(response)
· validate_file_path(path)
· validate_allowed_actions(action)
· validate_single_file_constraint(response)
· validate_task_alignment(response, current_task)

## 12C. Retry & Escalation Strategy

· If retries == 1: retry_same_prompt()
· Elif retries == 2: add_error_context()
· Elif retries == 3: switch_strategy_prompt()
· Else: mark_failed()

## 12D. Prompt Versioning

All agent executions must include prompt version (e.g., prompt_version = "v1.0") stored in logs for traceability.

## 12E. Execution Verification Layer

Do not trust agent success output.
Checks:
· Process started
· Expected logs present
· Port active (if applicable)

## 12F. File Change Tracking

```
{
  "file": "main.py",
  "change_type": "created | modified",
  "timestamp": "...",
  "task_id": 2
}
```

## 12G. Dependency Map (Pre-Graph RAG)

```
dependency_map = {
  "auth.py": ["user_model.py"],
  "routes.py": ["auth.py"]
}
```

## 12H. Security Layer

· Allowed Commands: python, pip install, uvicorn
· Blocked: rm -rf, sudo, arbitrary shell/network calls

## 12I. Context Builder

```
def build_context(task, project_state):
    return {
        "task": task,
        "relevant_files": retrieve(task),
        "current_file": current_file,
        "dependencies": dependency_map
    }
```

## 12J. Metrics & Observability

```
{
  "task_success_rate": 0.82,
  "avg_retries": 1.4,
  "error_types": {
    "import_error": 10
  }
}
```

## 12K. Config System

File: config.py
· MAX_RETRIES = 3
· MAX_FILE_SIZE = 500
· TEMPERATURE = 0.2
· ALLOWED_ACTIONS = [...]

## 12L. Logging Format

```
{
  "timestamp": "...",
  "project_id": "...",
  "task_id": 2,
  "agent": "coder",
  "input": {},
  "output": {},
  "result": "success | failure"
}
```

## 12M. Failure Recovery

On restart:
· Load project memory
· Parse plan.md
· Resume from first incomplete task

## 12N. Developer Guidelines

· NEVER bypass orchestrator
· NEVER allow direct agent tool execution
· ALWAYS validate outputs
· KEEP system deterministic
· LOG everything

## 13. Execution Environment

· V1: Local execution
· V2+: Docker sandbox

## 14. Risks & Mitigations

| Risk             | Solution          |
| ---------------- | ----------------- |
| Infinite loops   | Retry limit       |
| Bad plans        | Strict planner    |
| Context overload | RAG               |
| File corruption  | Read-before-write |

## 15. Implementation Order

1. Planner
2. Orchestrator
3. Filesystem Tool
4. Coder Agent
5. Execution Loop
6. Debugger
7. RAG

## 16. Final Principle

Control > Intelligence

Your system will succeed based on how well you control agents — not how powerful they are.

END

---

# Core Module — Nexura AI

## Overview

The core/ module is the control plane of Nexura AI. It is responsible for orchestrating execution, enforcing constraints, managing state, and ensuring system reliability.

The core module does NOT generate code or directly execute tools.
It strictly controls and validates all operations.

## Folder Structure

```
core/
├── orchestrator.py
├── task_manager.py
├── validator.py
├── context_builder.py
├── execution_manager.py
├── retry_manager.py
├── state_manager.py
```

## 1. orchestrator.py

Purpose
Acts as the central controller (brain) of the system.

Responsibilities
· Execute the main system loop
· Fetch next task from task manager
· Build context for agents
· Invoke coder/debugger agents
· Validate agent outputs via validator
· Delegate execution to execution manager
· Handle retries and failures
· Update project state via state manager
· Maintain strict control flow

## 2. task_manager.py

Purpose
Handles plan parsing and task lifecycle management.

## 3. validator.py

Purpose
Acts as the security and correctness layer.

## 4. context_builder.py

Purpose
Constructs the exact input context provided to agents.

## 5. execution_manager.py

Purpose
Handles execution of validated actions via tools.

## 6. retry_manager.py

Purpose
Manages failure handling and retry strategy.

## 7. state_manager.py

Purpose
Handles memory and state persistence.

## Core Module Interaction Flow

```
orchestrator
   ↓
task_manager → get next task
   ↓
context_builder → build agent input
   ↓
agent execution
   ↓
validator → validate output
   ↓
execution_manager → execute action
   ↓
retry_manager → handle failures
   ↓
state_manager → update memory
```

## Critical Developer Rules

NEVER:
· Allow agents to execute tools directly
· Bypass validator checks
· Modify memory outside state_manager

ALWAYS:
· Route all operations through orchestrator
· Validate every agent output before execution
· Log every step for debugging and traceability

## Key Insight

The core/ module determines system stability.
If designed correctly:
· The system is scalable, safe, and debuggable
If weak:
· The entire system becomes unpredictable and unreliable