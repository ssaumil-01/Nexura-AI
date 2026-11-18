import os
import sys
import json
import time
import argparse
from dotenv import load_dotenv

import config as nexura_config

from langgraph.types import Command
from core import metrics
from core.orchestrator import build_graph
from core import runner
from core.checkpoints import GitError
from core.runner import DirtyWorkspace, ResumeError, prepare_resume, start_run
from core.state_manager import StateManager, WorkspaceBusy, make_checkpointer
from core.task_manager import run_summary

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
            if isinstance(interrupt_value, dict):  # approval request (interactive mode or risk gate)
                print(f"\n>> Task {interrupt_value.get('task_id')}: approval needed - {interrupt_value.get('reason')}")
                for c in interrupt_value.get("calls", []):
                    print(f"   - {c['name']}({json.dumps(c['args'], default=str)[:300]})")
                for d in interrupt_value.get("diffs", []):
                    print(f"   --- {d['change_type']}: {d['file']}\n{d['diff'][:1500]}")
                print(">> Type 'yes' to approve; anything else rejects it and is sent to the agent as feedback:")
            else:
                print(f"\n>> {interrupt_value}")
            user_input = input(">> ").strip()
            
            # Resume the graph with the user's input
            for event in graph.stream(
                Command(resume=user_input), config, stream_mode="updates"
            ):
                _print_event(event)
        else:
            break

    return graph.get_state(config).values


def print_summary(values: dict) -> int:
    """Prints the honest run outcome. Returns the process exit code (0 only if every task is done)."""
    s = run_summary(values)
    print(f"\n=== Run {s['run_status'].upper()} ===")
    if s["run_error"]:
        print(f"Error: {s['run_error']}")
    for label in ("done", "failed", "skipped"):
        for t in s[label]:
            reason = f" ({t['reason']})" if t["reason"] else ""
            print(f"  [{label}] {t['step_id']}. {t['title']}{reason}")
    return 0 if s["run_status"] == "completed" else 1


def _print_event(event):
    """Prints tool execution results from graph events."""
    for node_name, updates in event.items():
        if node_name == "tools":
            msgs = updates.get("messages", [])
            for msg in msgs:
                if hasattr(msg, "content") and hasattr(msg, "name"):
                    print(f"  [{msg.name}] -> {msg.content[:200]}")


def start_index(root):
    """V2: index the workspace in the background (the run uses the V1 context until it is ready). None if unavailable."""
    try:
        from rag.embedder import make_embedder
        from rag.indexer import IndexManager
    except ImportError:
        print("[Index] indexing packages not installed (pip install -r requirements-index.txt); using the basic context.")
        return None
    from core.runner import workspace_id_for
    wid = workspace_id_for(str(root))
    mgr = IndexManager(make_embedder, emit=lambda w, t, d: print(f"[Index] {t} {d}") if t != "index_progress" else None)
    mgr.start(wid, root)
    ix = mgr.indexer(wid, root)
    ix.open()   # so the run's edits keep the index fresh while it builds
    return ix


def main():
    parser = argparse.ArgumentParser(description="Nexura AI - Autonomous Coding Agent")
    parser.add_argument("--prompt", type=str, help="Natural language request for the project")
    parser.add_argument("--project", type=str, help="Project name (e.g., 'proj_test')")
    parser.add_argument("--interactive", action="store_true", help="Enable human-in-the-loop mode")
    parser.add_argument("--resume", metavar="RUN_ID", help="Resume an unfinished run (done tasks are kept)")
    parser.add_argument("--approval", choices=["ask_every_edit", "ask_risky", "auto_within_limits"],
                        help="When to ask before editing (default: ask_risky)")
    parser.add_argument("--git-init", action="store_true", help="Initialize git in the workspace if it is not a repository")
    parser.add_argument("--dirty-policy", choices=["checkpoint", "abort"], default="checkpoint",
                        help="What to do if the workspace has uncommitted changes")
    parser.add_argument("--undo", metavar="RUN_ID", help="Undo a run's changes (all of them, or one task with --task)")
    parser.add_argument("--task", type=int, help="With --undo: only this task")
    parser.add_argument("--confirm", action="store_true", help="With --undo: overwrite files you edited since the run")
    parser.add_argument("--baseline", action="store_true",
                        help="Run the project's own test command before and after the run and report new failures (regressions)")
    parser.add_argument("--metrics", metavar="RUN_ID", help="Print metrics for a run and exit")

    args = parser.parse_args()
    nexura_config.configure_observability()
    sm = StateManager(nexura_config.DB_PATH)

    if args.metrics:
        print(json.dumps(metrics.compute(sm, args.metrics), indent=2))
        return
    if args.undo:
        res = runner.undo(sm, args.undo, args.task, args.confirm)
        if res.error:
            print(f"Undo failed: {res.error}")
        elif not res.applied:
            print("Not undone: these files were edited after Nexura changed them (use --confirm to overwrite):")
            print("\n".join(f"  {f}" for f in res.conflicts))
        else:
            print(f"Undone. restored={res.restored} deleted={res.deleted}")
        sys.exit(0 if res.applied else 1)
    if not args.resume and not (args.prompt and args.project):
        parser.error("--prompt and --project are required (unless using --resume or --metrics)")

    try:
        if args.resume:
            project, initial_state = prepare_resume(sm, args.resume, interactive=args.interactive)
            thread_id = f"{project.run_id}-r{int(time.time())}"
        else:
            workspace_path = os.path.join(os.path.dirname(__file__), "workspace", args.project)
            project, initial_state = start_run(sm, args.project, workspace_path, args.prompt, interactive=args.interactive,
                                               approval_level=args.approval, git_init=args.git_init, dirty_policy=args.dirty_policy)
            thread_id = project.run_id
    except (ResumeError, WorkspaceBusy, DirtyWorkspace) as e:
        print(f"Cannot start: {e}")
        sys.exit(2)
    except GitError as e:
        print(f"Cannot start: {e.message}" + ("\nRe-run with --git-init to create a repository here." if e.code == "NOT_A_REPO" else ""))
        sys.exit(2)

    if args.baseline and not args.resume:   # passing the flag is the approval to run the project's tests
        from core.planner_context import effective_profile
        project.baseline_command = (effective_profile(project).test_commands or [""])[0]
    if nexura_config.INDEXING_ENABLED:
        project.indexer = start_index(project.root)

    graph = build_graph(interactive=args.interactive, checkpointer=make_checkpointer(nexura_config.CHECKPOINT_DB_PATH))

    mode_label = "interactive" if args.interactive else "autonomous"
    config = {
        "configurable": {"thread_id": thread_id, "project": project},
        "recursion_limit": 500,
        # LangSmith trace metadata - shows up in the dashboard
        "metadata": {"project": project.project_id, "run_id": project.run_id, "mode": mode_label},
        "tags": ["nexura-ai", project.project_id, mode_label],
    }

    print(f"Nexura AI -- project: {project.project_id}  run: {project.run_id}")
    print(f"Mode: {'Interactive (HITL)' if args.interactive else 'Autonomous'}\n")

    try:
        values = run_graph(graph, initial_state, config)
    except KeyboardInterrupt:
        sm.update_run(project.run_id, "cancelled")
        print(f"\nCancelled. Resume with: python main.py --resume {project.run_id}")
        sys.exit(130)
    finally:
        project.processes.stop_all()

    exit_code = print_summary(values)
    m = metrics.compute(sm, project.run_id)
    print(f"Metrics: success_rate={m['task_success_rate']} retries={m['retries_per_task']} errors={m['error_types']} time={m['wall_time_s']}s")
    print(f"Run id: {project.run_id}  (log: {project.run_dir / 'log.jsonl'})")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()


# Usage:
# python main.py --project "proj_calculator" --prompt "Create a calculator app using HTML, CSS and JS"
# python main.py --project "proj_test" --prompt "Create a hello world python script" --interactive
# python main.py --project "proj_test" --prompt "Create an interactive streamlit app that generates random data to display line and bar charts, while also allowing basic interaction through sliders, text inputs, and selection boxes. Its purpose isn’t complex functionality, but to showcase how layout techniques (columns, tabs, expanders) and built-in Streamlit components can be used to create a clean, engaging interface that fills the screen effectively."
# npm run tauri dev