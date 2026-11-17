"""One command for everything CI runs (this repository's own checks only):  python scripts/check.py [--no-ext]"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
STEPS = [
    ("lint (ruff)", [PY, "-m", "ruff", "check", "."], ROOT),
    ("unit + integration tests (pytest)", [PY, "-m", "pytest", "-q", "--runslow"], ROOT),
    ("eval smoke (fake model)", [PY, "evals/run.py", "--fake", "--out", str(ROOT / "evals" / "results" / "_ci")], ROOT),
]
EXT = [
    ("extension typecheck (tsc)", ["npm", "run", "typecheck"], ROOT / "extension"),
    ("extension build + package", ["npm", "run", "package"], ROOT / "extension"),
]


def main() -> int:
    steps = STEPS + ([] if "--no-ext" in sys.argv else EXT)
    failed = []
    for name, cmd, cwd in steps:
        print(f"\n== {name}")
        shell = sys.platform == "win32" and cmd[0] == "npm"
        if subprocess.run(cmd, cwd=cwd, shell=shell).returncode != 0:
            failed.append(name)
    print("\n" + ("ALL CHECKS PASSED" if not failed else "FAILED: " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
