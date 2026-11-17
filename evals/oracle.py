"""TEST-ONLY "oracle" model for the evals: a scripted agent that knows a correct solution for each task in tasks.yaml.

It lets us run the whole V1 pipeline (planner parsing, validator, execution manager, verification, git checkpoints,
approvals, state, retries) on all 14 eval tasks with NO LLM, no network and no cost. If a task fails here, the bug is in
the pipeline or in the eval itself, never in a model. It does not measure model quality: use `evals/run.py` for that.

    python evals/run.py --oracle
"""
import json
import os
import re
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

TASKS_FILE = Path(__file__).with_name("tasks.yaml")
TAG = re.compile(r"\[oracle:([a-z_]+)\]")


def _w(path: str, content: str) -> tuple[str, dict]:
    return ("create_file", {"target_file": path, "content": content})


def _rewrite(path: str, content: str) -> list[tuple[str, dict]]:
    """Existing file: read it first (the pipeline enforces read-before-write), then replace it."""
    return [("read_file", {"target_file": path}), ("write_file", {"target_file": path, "content": content})]


CALC = '''import ast
import operator
import sys

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def evaluate(node):
    if isinstance(node, ast.Expression):
        return evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](evaluate(node.left), evaluate(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -evaluate(node.operand)
    raise ValueError("unsupported expression")


def main():
    value = evaluate(ast.parse("".join(sys.argv[1:]), mode="eval"))
    print(int(value) if float(value).is_integer() else value)


if __name__ == "__main__":
    main()
'''

SOLUTIONS: dict[str, list[tuple[str, dict]]] = {
    "calc_cli": [_w("calc.py", CALC)],
    "fastapi_users": [
        _w("app.py", 'from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get("/health")\ndef health():\n'
                     '    return {"status": "ok"}\n\n\n@app.get("/users")\ndef users():\n'
                     '    return [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Linus"}]\n'),
        _w("requirements.txt", "fastapi\nuvicorn\n")],
    "flask_hello": [
        _w("app.py", 'from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.route("/")\ndef index():\n'
                     '    return "Hello, Nexura!"\n'),
        _w("requirements.txt", "flask\n")],
    "static_site": [
        _w("index.html", '<!doctype html>\n<html><head><link rel="stylesheet" href="style.css"></head>\n<body>\n'
                         '<h1 id="title">Welcome</h1>\n<button id="btn">Click me</button>\n<script src="script.js"></script>\n'
                         '</body></html>\n'),
        _w("style.css", "body { font-family: sans-serif; text-align: center; }\n"),
        _w("script.js", 'document.getElementById("btn").addEventListener("click", function () {\n'
                        '  document.getElementById("title").textContent = "Clicked!";\n});\n')],
    "streamlit_app": [
        _w("app.py", 'import numpy as np\nimport pandas as pd\nimport streamlit as st\n\nn = st.slider("Points", 10, 200, 50)\n'
                     'data = pd.DataFrame(np.random.randn(n, 2), columns=["a", "b"])\nst.line_chart(data)\n'),
        _w("requirements.txt", "streamlit\nnumpy\npandas\n")],
    "algo_tests": [
        _w("sorting.py", "def merge_sort(items):\n    items = list(items)\n    if len(items) <= 1:\n        return items\n"
                         "    mid = len(items) // 2\n    left, right = merge_sort(items[:mid]), merge_sort(items[mid:])\n"
                         "    out, i, j = [], 0, 0\n    while i < len(left) and j < len(right):\n"
                         "        if left[i] <= right[j]:\n            out.append(left[i])\n            i += 1\n"
                         "        else:\n            out.append(right[j])\n            j += 1\n    return out + left[i:] + right[j:]\n"),
        _w("test_sorting.py", "from sorting import merge_sort\n\n\ndef test_empty():\n    assert merge_sort([]) == []\n\n\n"
                              "def test_single():\n    assert merge_sort([1]) == [1]\n\n\n"
                              "def test_duplicates():\n    assert merge_sort([3, 1, 2, 3]) == [1, 2, 3, 3]\n\n\n"
                              "def test_sorted():\n    assert merge_sort([1, 2, 3]) == [1, 2, 3]\n")],
    "node_script": [_w("sum.js", "const nums = process.argv.slice(2).map(Number);\nconsole.log(nums.reduce((a, b) => a + b, 0));\n")],
    "json_config": [
        _w("config_parser.py", 'import json\n\n\ndef load_config(path):\n    with open(path, encoding="utf-8") as f:\n'
                               '        cfg = json.load(f)\n    for key in ("host", "port"):\n        if key not in cfg:\n'
                               '            raise ValueError(f"missing required key: {key}")\n    port = cfg["port"]\n'
                               '    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:\n'
                               '        raise ValueError("port must be an integer between 1 and 65535")\n    return cfg\n')],
    "package_bugfix": _rewrite("mathlib/stats.py", "def mean(values):\n    return sum(values) / len(values)\n\n\n"
                               "def median(values):\n    s = sorted(values)\n    mid = len(s) // 2\n"
                               "    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2\n"),
    "add_function": _rewrite("utils.py", 'import re\n\n\ndef add(a, b):\n    return a + b\n\n\ndef shout(text):\n'
                             '    return text.upper() + "!"\n\n\ndef slugify(text):\n'
                             '    text = re.sub(r"\\s+", "-", text.strip().lower())\n    return re.sub(r"[^a-z0-9-]", "", text)\n'),
    "todo_cli": [
        _w("todo.py", 'import json\nimport sys\nfrom pathlib import Path\n\nFILE = Path("todos.json")\n\n\n'
                      'def load():\n    return json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else []\n\n\n'
                      'def main():\n    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), " ".join(sys.argv[2:])\n'
                      '    if cmd == "add" and rest:\n        todos = load() + [rest]\n'
                      '        FILE.write_text(json.dumps(todos), encoding="utf-8")\n    elif cmd == "list":\n'
                      '        for item in load():\n            print(item)\n    else:\n'
                      '        sys.exit("usage: todo.py add <text> | list")\n\n\nif __name__ == "__main__":\n    main()\n')],
    "csv_report": [
        _w("report.py", 'import csv\nfrom collections import defaultdict\n\ntotals = defaultdict(float)\n'
                        'with open("sales.csv", newline="", encoding="utf-8") as f:\n    for row in csv.DictReader(f):\n'
                        '        totals[row["region"]] += float(row["amount"])\nfor region in sorted(totals):\n'
                        '    print(f"{region}: {totals[region]:g}")\n')],
    "validators": [
        _w("validators.py", 'import re\n\n\ndef is_email(text):\n'
                            '    return bool(re.fullmatch(r"[\\w.+-]+@[\\w-]+(\\.[\\w-]+)*\\.[A-Za-z]{2,}", text))\n\n\n'
                            'def is_phone(text):\n    if not re.fullmatch(r"\\+?[\\d\\s().-]+", text):\n        return False\n'
                            '    return 7 <= len(re.sub(r"\\D", "", text)) <= 15\n')],
    "tests_for_existing": [
        _w("test_shapes.py", 'import math\n\nimport pytest\n\nfrom shapes import circle_area, rect_area\n\n\n'
                             'def test_circle_area():\n    assert circle_area(1) == pytest.approx(math.pi)\n\n\n'
                             'def test_negative_radius():\n    with pytest.raises(ValueError):\n        circle_area(-1)\n\n\n'
                             'def test_rect_area():\n    assert rect_area(2, 3) == 6\n')],
}


def _norm(s: str) -> str:
    return " ".join(s.split())


def _tasks() -> dict[str, dict]:
    import yaml
    return {t["id"]: t for t in yaml.safe_load(TASKS_FILE.read_text(encoding="utf-8")) if not t.get("smoke")}


def _plan(prompt: str) -> str:
    text = _norm(prompt)
    for tid, task in _tasks().items():
        if _norm(task["prompt"]) in text:
            return json.dumps({"project_id": tid, "goal": task["title"], "steps": [{
                "step_id": 1, "title": f"[oracle:{tid}] {task['title']}", "description": task["prompt"], "dependencies": [],
                "acceptance": task["checks"]}]})
    raise RuntimeError("oracle: no eval task matches this request")


class OracleModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "nexura-oracle"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = "\n".join(str(m.content) for m in messages)
        m = TAG.search(text)
        steps = SOLUTIONS.get(m.group(1)) if m else None
        if m and m.group(1) in os.environ.get("ORACLE_SABOTAGE", "").split(","):   # self-test: a wrong solution must FAIL the eval
            steps = [_w("noop.txt", "this task was deliberately not solved\n")]
        # Round 2 (a tool result is in the conversation) or the debugger: finish, or report that the oracle is stuck.
        if steps is None or any(x.type == "tool" for x in messages):
            calls = [("task_complete", {"summary": "oracle done"})]
        else:
            calls = [*steps, ("task_complete", {"summary": "oracle done"})]
        msg = AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"oracle-{i}"} for i, (n, a) in enumerate(calls)])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def install() -> None:
    import agents.llm
    import agents.planner
    os.environ.setdefault("GOOGLE_API_KEY", "fake-key-for-tests")
    os.environ.setdefault("PLANNER_MODEL_NAME", "oracle")
    os.environ.setdefault("CODER_MODEL_NAME", "oracle")
    agents.llm.set_llm(OracleModel())
    agents.planner.PlannerAgent.call_llm = lambda self, prompt: _plan(prompt)
