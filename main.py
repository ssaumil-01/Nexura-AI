import os
import argparse
from dotenv import load_dotenv

# Activate LangSmith tracing before any LangChain/LangGraph imports
import config  # noqa: F401  — side-effect: sets tracing env vars

from langgraph.types import Command
from core.graph import build_graph
import tools.file_tools as ft
import tools.system_tools as st
import tools.directory_tools as dt

load_dotenv()


def run_graph(graph, initial_state, config):
    """Runs the graph with interrupt handling for plan review and tool approval."""
    
    # First run
    for event in graph.stream(initial_state, config, stream_mode="updates"):
        _print_event(event)
    
    # Handle interrupts (plan review loop + tool approvals)
    while True:
        # Check if graph is paused at an interrupt
        snapshot = graph.get_state(config)
        
        if not snapshot.next:
            # Graph finished — no more nodes to run
            break
        
        # There's a pending interrupt — check what it is
        if snapshot.tasks and snapshot.tasks[0].interrupts:
            interrupt_value = snapshot.tasks[0].interrupts[0].value
            print(f"\n>> {interrupt_value}")
            user_input = input(">> ").strip()
            
            # Resume the graph with the user's input
            for event in graph.stream(
                Command(resume=user_input), config, stream_mode="updates"
            ):
                _print_event(event)
        else:
            break


def _print_event(event):
    """Prints tool execution results from graph events."""
    for node_name, updates in event.items():
        if node_name == "tools":
            msgs = updates.get("messages", [])
            for msg in msgs:
                if hasattr(msg, "content") and hasattr(msg, "name"):
                    print(f"  [{msg.name}] -> {msg.content[:200]}")


def main():
    parser = argparse.ArgumentParser(description="Nexura AI - Autonomous Coding Agent")
    parser.add_argument("--prompt", type=str, required=True, help="Natural language request for the project")
    parser.add_argument("--project", type=str, required=True, help="Project name (e.g., 'proj_test')")
    parser.add_argument("--interactive", action="store_true", help="Enable human-in-the-loop mode")
    
    args = parser.parse_args()
    
    workspace_path = os.path.join(os.path.dirname(__file__), "workspace", args.project)
    os.makedirs(workspace_path, exist_ok=True)
    
    # Set workspace path for all tool modules
    ft.WORKSPACE_PATH = workspace_path
    st.WORKSPACE_PATH = workspace_path
    dt.WORKSPACE_PATH = workspace_path
    
    # Build the LangGraph pipeline
    graph = build_graph(interactive=args.interactive)
    
    # Thread config for checkpointing + LangSmith metadata
    mode_label = "interactive" if args.interactive else "autonomous"
    config = {
        "configurable": {"thread_id": args.project},
        # LangSmith trace metadata — shows up in the dashboard
        "metadata": {
            "project": args.project,
            "mode": mode_label,
        },
        "tags": ["nexura-ai", args.project, mode_label],
    }
    
    # Initial state
    initial_state = {
        "messages": [],
        "user_prompt": args.prompt,
        "workspace_path": workspace_path,
        "plan": None,
        "tasks": [],
        "current_task_index": 0,
        "execution_history": [],
        "is_interactive": args.interactive,
        "plan_feedback": None,
        "tool_calls_count": 0,
    }
    
    print(f"Nexura AI -- Starting project: {args.project}")
    print(f"Mode: {'Interactive (HITL)' if args.interactive else 'Autonomous'}\n")
    
    run_graph(graph, initial_state, config)
    
    print("\n[OK] Pipeline complete!")
    print(f"\n---> LangSmith Traces: https://smith.langchain.com/o/default/projects/p/{args.project}")


if __name__ == "__main__":
    main()


# Usage:
# python main.py --project "proj_calculator" --prompt "Create a calculator app using HTML, CSS and JS"
# python main.py --project "proj_test" --prompt "Create a hello world python script" --interactive
# python main.py --project "proj_test" --prompt "Create an interactive streamlit app that generates random data to display line and bar charts, while also allowing basic interaction through sliders, text inputs, and selection boxes. Its purpose isn’t complex functionality, but to showcase how layout techniques (columns, tabs, expanders) and built-in Streamlit components can be used to create a clean, engaging interface that fills the screen effectively."
# npm run tauri dev