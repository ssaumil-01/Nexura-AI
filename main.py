import os
import argparse
from agents.planner import PlannerAgent
from core.orchestrator import Orchestrator

def main():
    parser = argparse.ArgumentParser(description="Nexura AI - Autonomous Coding Agent")
    parser.add_argument("--prompt", type=str, help="Natural language request for the project", required=False)
    parser.add_argument("--project", type=str, help="Project name (e.g., 'proj_test')", required=True)
    
    args = parser.parse_args()
    
    workspace_dir = os.path.join(os.path.dirname(__file__), "workspace")
    project_dir = os.path.join(workspace_dir, args.project)

    # Step 1: Run Planner if prompt is provided
    if args.prompt:
        print(f"--- [1] Running Planner ---")
        planner = PlannerAgent()
        plan = planner.plan(args.prompt, project_dir)
        if not plan:
            print("Planner failed to generate a plan. Exiting.")
            return
            
    # Step 2: Run Orchestrator
    print(f"\n--- [2] Running Orchestrator ---")
    plan_path = os.path.join(project_dir, "plan.json")
    if os.path.exists(plan_path):
        orchestrator = Orchestrator(plan_path, project_dir)
        orchestrator.run()
    else:
        print(f"Error: No generated plan found at {plan_path}. Provide a --prompt to plan it first.")

if __name__ == "__main__":
    main()


# python main.py --project "proj_calculator" --prompt "Create a calculator app using c++"
