"""V2-5.1: deterministic detection of what an existing project is and how it runs/tests. No LLM, no network, only reads a
handful of well-known files inside the workspace. Unknown -> empty fields. The user can override any field (stored apart from
the detection, so a re-detect never loses an override)."""
import json
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

import config
from core.safety import contains_secret

MAX_READ = 200_000
MONOREPO_DIRS = ("packages", "apps", "services", "libs")
PY_ENTRIES = ("main.py", "app.py", "manage.py", "server.py", "wsgi.py", "asgi.py", "run.py", "src/main.py", "src/app.py")
JS_ENTRIES = ("index.js", "server.js", "app.js", "src/index.js", "src/index.ts", "src/main.ts", "src/main.tsx", "src/main.jsx", "src/App.tsx")
LIST_FIELDS = ("languages", "package_managers", "frameworks", "entry_points", "run_commands", "test_commands", "env_needs")


class ProjectProfile(BaseModel):
    languages: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    run_commands: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    lint_command: str | None = None
    env_needs: list[str] = Field(default_factory=list)     # NAMES of environment variables only, never values
    subprojects: list[str] = Field(default_factory=list)   # monorepo: relative folders that have their own manifest
    subproject: str = ""                                   # which one this profile describes ("" = the root)

    def commands(self) -> list[str]:
        return [*self.run_commands, *self.test_commands, *([self.lint_command] if self.lint_command else [])]


def _text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except OSError:
        return ""


def _add(lst: list[str], *items: str) -> None:
    for i in items:
        if i and i not in lst:
            lst.append(i)


def _python(root: Path, p: ProjectProfile) -> bool:
    pyproject = _text(root / "pyproject.toml")
    reqs = [f for f in sorted(root.glob("requirements*.txt"))[:5]]
    if not (pyproject or reqs or (root / "setup.py").exists() or (root / "Pipfile").exists()):
        return False
    _add(p.languages, "python")
    deps = " ".join(_text(f).lower() for f in reqs)
    toml: dict = {}
    if pyproject:
        try:
            toml = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            toml = {}
        proj = toml.get("project", {})
        deps += " " + " ".join(str(d).lower() for d in proj.get("dependencies", []))
        for group in proj.get("optional-dependencies", {}).values():
            deps += " " + " ".join(str(d).lower() for d in group)
        deps += " " + " ".join(str(k).lower() for k in toml.get("tool", {}).get("poetry", {}).get("dependencies", {}))
        deps += " " + " ".join(str(k).lower() for k in toml.get("tool", {}).get("poetry", {}).get("group", {}).get("dev", {}).get("dependencies", {}))
    tool = toml.get("tool", {})
    poetry, uv = "poetry" in tool or (root / "poetry.lock").exists(), "uv" in tool or (root / "uv.lock").exists()
    manager = "poetry" if poetry else "uv" if uv else "pip"
    _add(p.package_managers, manager)
    for name, label in (("fastapi", "FastAPI"), ("flask", "Flask"), ("django", "Django"), ("streamlit", "Streamlit")):
        if re.search(rf"(?<![\w-]){name}(?![\w-])", deps):
            _add(p.frameworks, label)
    prefix = {"poetry": "poetry run ", "uv": "uv run "}.get(manager, "")
    has_pytest = ("pytest" in deps or "pytest" in tool or (root / "pytest.ini").exists() or (root / "conftest.py").exists()
                  or "pytest" in _text(root / "tox.ini") or (root / "tests").is_dir())
    if has_pytest:
        _add(p.test_commands, f"{prefix}python -m pytest" if not prefix else f"{prefix}pytest")
    elif (root / "tests").is_dir() or any(root.glob("test_*.py")):
        _add(p.test_commands, f"{prefix}python -m unittest" if not prefix else f"{prefix}python -m unittest")
    if "ruff" in tool or (root / "ruff.toml").exists() or "ruff" in deps:
        p.lint_command = f"{prefix}ruff check ." if prefix else "python -m ruff check ."
    elif (root / ".flake8").exists() or "flake8" in deps:
        p.lint_command = f"{prefix}flake8" if prefix else "python -m flake8"
    for e in PY_ENTRIES:
        if (root / e).is_file():
            _add(p.entry_points, e)
    for e in p.entry_points:
        src, mod = _text(root / e), e[:-3].replace("/", ".")
        if e.endswith("manage.py") and "Django" in p.frameworks:
            _add(p.run_commands, f"{prefix}python manage.py runserver")
        elif re.search(r"\bFastAPI\(", src):
            _add(p.run_commands, f"{prefix}uvicorn {mod}:app")
        elif re.search(r"\bFlask\(", src):
            _add(p.run_commands, f"{prefix}flask --app {mod} run")
        elif "streamlit" in src and "Streamlit" in p.frameworks:
            _add(p.run_commands, f"{prefix}streamlit run {e}")
    if not p.run_commands and p.entry_points and not any(e.endswith(("manage.py", "wsgi.py", "asgi.py")) for e in p.entry_points):
        _add(p.run_commands, f"{prefix}python {p.entry_points[0]}")
    return True


def _node(root: Path, p: ProjectProfile) -> bool:
    try:
        pkg = json.loads(_text(root / "package.json") or "null")
    except ValueError:
        pkg = None
    if not isinstance(pkg, dict):
        return False
    _add(p.languages, "typescript" if (root / "tsconfig.json").exists() else "javascript")
    manager = "yarn" if (root / "yarn.lock").exists() else "pnpm" if (root / "pnpm-lock.yaml").exists() else "npm"
    _add(p.package_managers, manager)
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    for name, label in (("express", "Express"), ("react", "React"), ("vite", "Vite"), ("next", "Next.js"), ("vue", "Vue"),
                        ("fastify", "Fastify"), ("jest", "Jest"), ("vitest", "Vitest")):
        if name in deps:
            _add(p.frameworks, label)
    scripts = pkg.get("scripts") or {}
    run = lambda s: {"npm": f"npm run {s}", "yarn": f"yarn {s}", "pnpm": f"pnpm run {s}"}[manager]   # noqa: E731
    if "test" in scripts and "no test specified" not in str(scripts["test"]):
        _add(p.test_commands, "npm test" if manager == "npm" else f"{manager} test")
    if "lint" in scripts:
        p.lint_command = run("lint")
    for s in ("dev", "start", "serve"):
        if s in scripts:
            _add(p.run_commands, "npm start" if (s == "start" and manager == "npm") else run(s))
    for e in JS_ENTRIES:
        if (root / e).is_file():
            _add(p.entry_points, e)
    if isinstance(pkg.get("main"), str) and (root / pkg["main"]).is_file():
        _add(p.entry_points, pkg["main"].replace("\\", "/"))
    if not p.run_commands and "Express" in p.frameworks and p.entry_points:
        _add(p.run_commands, f"node {p.entry_points[0]}")
    return True


def _makefile(root: Path, p: ProjectProfile) -> None:
    text = _text(root / "Makefile")
    if not text:
        return
    targets = set(re.findall(r"^([A-Za-z][\w-]*)\s*:(?!=)", text, re.M))
    if "test" in targets and not p.test_commands:
        _add(p.test_commands, "make test")
    if "lint" in targets and not p.lint_command:
        p.lint_command = "make lint"
    for t in ("run", "dev", "serve"):
        if t in targets and not p.run_commands:
            _add(p.run_commands, f"make {t}")


def _readme_hints(root: Path, p: ProjectProfile) -> None:
    """Last resort for the test command: a README line that is exactly a known test command."""
    if p.test_commands:
        return
    for name in ("README.md", "README.rst", "README.txt", "README"):
        text = _text(root / name)
        for line in text.splitlines():
            m = re.fullmatch(r"\s*(?:[$>]\s*)?((?:python -m pytest|pytest|npm test|npm run test|yarn test|pnpm test|make test)(?: [\w./:=-]+)*)\s*", line)
            if m:
                _add(p.test_commands, m.group(1))
                return


def _env_needs(root: Path, p: ProjectProfile) -> None:
    for name in (".env.example", ".env.sample", ".env.template"):
        for line in _text(root / name).splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
            if m and not contains_secret(line):    # names only; a line that looks like a real secret is skipped entirely
                _add(p.env_needs, m.group(1))


def find_subprojects(root: Path) -> list[str]:
    markers = ("package.json", "pyproject.toml", "requirements.txt", "setup.py")
    found = []
    for group in MONOREPO_DIRS:
        base = root / group
        if base.is_dir():
            for d in sorted(base.iterdir())[:50]:
                if d.is_dir() and not d.is_symlink() and any((d / m).is_file() for m in markers):
                    found.append(f"{group}/{d.name}")
    return found


def detect_profile(root: Path | str, subproject: str = "") -> ProjectProfile:
    """Detect the profile of `root` (or of one monorepo subfolder). Never raises; a missing/odd file just yields empty fields."""
    root = Path(root)
    p = ProjectProfile(subprojects=find_subprojects(root), subproject=subproject)
    base = root / subproject if subproject else root
    if subproject:
        from core.safety import resolve_in_root
        base = resolve_in_root(root, subproject)
    try:
        _python(base, p)
        _node(base, p)
        _makefile(base, p)
        _readme_hints(base, p)
        _env_needs(base, p)
    except Exception:       # detection is best-effort; a surprise in one file must not stop a run
        pass
    if subproject:      # commands run from the workspace root, so point them at the subfolder
        p.entry_points = [f"{subproject}/{e}" for e in p.entry_points]
    return p


def apply_overrides(profile: ProjectProfile, overrides: dict | None) -> ProjectProfile:
    """User overrides win field by field. Unknown keys and wrong types are ignored."""
    if not overrides:
        return profile
    data = profile.model_dump()
    for k, v in overrides.items():
        if k in LIST_FIELDS and isinstance(v, list) and all(isinstance(x, str) for x in v):
            data[k] = v
        elif k == "lint_command" and (v is None or isinstance(v, str)):
            data[k] = v or None
    return ProjectProfile(**data)


def needed_executables(profile: ProjectProfile) -> list[str]:
    """Approvable executables (config.EXTRA_COMMAND_CANDIDATES) the profile's commands use that are NOT on the default allowlist."""
    out = []
    for cmd in profile.commands():
        first = (cmd.split() or [""])[0].lower()
        for ext in (".exe", ".cmd", ".bat"):
            first = first.removesuffix(ext)
        if first in config.EXTRA_COMMAND_CANDIDATES and first not in config.ALLOWED_COMMANDS and first not in out:
            out.append(first)
    return out
