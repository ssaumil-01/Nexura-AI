#!/bin/bash

# Root files
touch main.py config.py

# Core directories
mkdir -p agents tools core rag sandbox workspace memory

# Agents
mkdir -p agents
touch agents/planner.py agents/coder.py agents/debugger.py

# Tools
mkdir -p tools
touch tools/filesystem.py tools/terminal.py tools/github_tool.py

# Core system
mkdir -p core
touch core/orchestrator.py core/task_manager.py core/validator.py core/context_builder.py core/execution_manager.py core/retry_manager.py core/state_manager.py

# RAG layer
mkdir -p rag
touch rag/retriever.py rag/graph_rag.py

# Memory system
mkdir -p memory/session memory/projects memory/logs memory/rag

touch memory/session/current_session.json
touch memory/projects/proj_001.json
touch memory/logs/execution_logs.json
touch memory/rag/embeddings.pkl
touch memory/rag/index.faiss

# Workspace (execution layer)
mkdir -p workspace/proj_001/src

touch workspace/proj_001/plan.md
touch workspace/proj_001/main.py
touch workspace/proj_001/requirements.txt

# Fix empty folder tracking
touch workspace/.gitkeep
touch sandbox/.gitkeep
touch memory/.gitkeep
touch workspace/proj_001/src/.gitkeep

echo "Project structure created successfully."