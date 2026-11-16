"""Path confinement, protected paths, command allowlist, output truncation. Pure functions, no I/O side effects."""
import fnmatch
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

import config


class SafetyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ─── PATHS ──────────────────────────────────────────────────

def is_within(root: str, path: str) -> bool:
    """True if `path` is `root` or inside it. Separator-aware (proj != proj_evil), case-insensitive on Windows."""
    r, p = os.path.normcase(os.path.realpath(root)), os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([r, p]) == r
    except ValueError:  # different drives
        return False


def resolve_in_root(root: Path | str, rel: str) -> Path:
    """Resolve `rel` under `root`; reject absolute paths, '..', drive changes and symlink escapes."""
    if not isinstance(rel, str) or "\x00" in rel:
        raise SafetyError("BAD_PATH", "Invalid path.")
    if os.path.isabs(rel) or os.path.splitdrive(rel)[0] or rel[:1] in ("/", "\\"):
        raise SafetyError("OUTSIDE_ROOT", f"Absolute paths are forbidden: {rel}")
    if ".." in rel.replace("\\", "/").split("/"):
        raise SafetyError("OUTSIDE_ROOT", f"Path traversal is forbidden: {rel}")
    full = os.path.realpath(os.path.join(str(root), rel))
    if not is_within(str(root), full):
        raise SafetyError("OUTSIDE_ROOT", f"Path resolves outside the workspace: {rel}")
    return Path(full)


def rel_key(root: Path | str, full: Path | str) -> str:
    """Stable, case-normalised relative key for tracking files."""
    return os.path.normcase(os.path.relpath(str(full), os.path.realpath(str(root)))).replace("\\", "/")


# ─── PROTECTED PATHS ────────────────────────────────────────

ALWAYS_BLOCKED = [".git/", ".env*"]  # never writable, even with approval


def _matches(rel: str, patterns: list[str]) -> bool:
    p = rel.replace("\\", "/").lower().lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    parts = [x for x in p.split("/") if x]
    for pat in patterns:
        pat = pat.lower()
        if pat.endswith("/"):
            d = [x for x in pat.strip("/").split("/") if x]
            if len(d) == 1 and d[0] in parts:
                return True
            if len(d) > 1 and parts[:len(d)] == d:
                return True
        elif "/" in pat:
            if fnmatch.fnmatch(p, pat):
                return True
        elif parts and fnmatch.fnmatch(parts[-1], pat):
            return True
    return False


def is_protected(rel: str, patterns: list[str]) -> bool:
    return _matches(rel, patterns)


def check_writable(rel: str, protected: list[str], allow_protected: bool = False) -> SafetyError | None:
    if _matches(rel, ALWAYS_BLOCKED):
        return SafetyError("NOT_WRITABLE", f"{rel} can never be modified by the agent.")
    if _matches(rel, protected) and not allow_protected:
        return SafetyError("APPROVAL_REQUIRED", f"{rel} is a protected path and needs explicit approval.")
    return None


# ─── OUTPUT ─────────────────────────────────────────────────

def truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep head + tail of text within ~max_bytes. Returns (text, was_truncated)."""
    if len(text.encode("utf-8", "replace")) <= max_bytes:
        return text, False
    half = max(max_bytes // 2, 1)
    return f"{text[:half]}\n...[truncated]...\n{text[-half:]}", True


# ─── COMMANDS ───────────────────────────────────────────────

FORBIDDEN_CHARS = set("|&;<>`\n\r")
PIP_BAD_FLAGS = {"-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--trusted-host",
                 "--user", "--target", "-t", "--prefix", "--root", "--break-system-packages"}
NPM_INSTALL = {"install", "i", "ci", "add", "install-test", "it"}
QUOTES = ("\"", "'")


@dataclass
class ParsedCommand:
    argv: list[str]   # argv[0] is the resolved executable path
    name: str         # allowlisted executable name, e.g. "python"
    installs: bool    # True for pip/npm install
    args: list[str]


def _split(command: str) -> list[str]:
    toks = shlex.split(command, posix=(os.name != "nt"))
    if os.name == "nt":
        toks = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in QUOTES else t for t in toks]
    return toks


def _is_install(name: str, args: list[str]) -> bool:
    if name in ("python", "py"):
        if args[:2] != ["-m", "pip"]:
            return False
        name, args = "pip", args[2:]
    if name == "pip":
        return bool(args) and args[0] == "install"
    if name == "npm":
        return bool(args) and args[0] in NPM_INSTALL
    return False


def parse_command(command: str, root: Path | str, allow_install: bool = False) -> ParsedCommand:
    """Validate a command against the allowlist. The caller must run it with shell=False."""
    if not command or not command.strip():
        raise SafetyError("EMPTY_COMMAND", "Empty command.")
    if any(c in FORBIDDEN_CHARS for c in command) or "$(" in command:
        raise SafetyError("SHELL_OPERATOR", "Shell operators (| & ; > < ` $( newline) are not allowed. Run one command at a time.")
    try:
        toks = _split(command)
    except ValueError as e:
        raise SafetyError("BAD_COMMAND", f"Cannot parse command: {e}")
    if not toks:
        raise SafetyError("EMPTY_COMMAND", "Empty command.")
    exe, args = toks[0], toks[1:]
    if "/" in exe or "\\" in exe:
        raise SafetyError("NOT_ALLOWED", "Executables must be plain names (no paths).")
    name = exe.lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    if name not in config.ALLOWED_COMMANDS:
        raise SafetyError("NOT_ALLOWED", f"'{exe}' is not an allowed command. Allowed: {', '.join(config.ALLOWED_COMMANDS)}")

    installs = _is_install(name, args)
    if installs and not allow_install:
        raise SafetyError("USE_INSTALL_DEPENDENCY", "Install packages with the install_dependency tool.")
    if allow_install and not installs:
        raise SafetyError("NOT_AN_INSTALL", "install_dependency only accepts 'pip install ...' or 'npm install ...'.")
    if installs:
        _check_install_args(name, args, root)

    path = shutil.which(exe)
    if not path:
        raise SafetyError("NOT_FOUND", f"Executable '{exe}' was not found on PATH.")
    if path.lower().endswith((".cmd", ".bat")) and any(c in a for a in args for c in "^%!\""):
        raise SafetyError("SHELL_OPERATOR", "Characters ^ % ! \" are not allowed in arguments for this command.")
    return ParsedCommand([path, *args], name, installs, args)


def _check_install_args(name: str, args: list[str], root) -> None:
    for a in args:
        if "://" in a or a.startswith("git+"):
            raise SafetyError("BAD_INSTALL", f"URL installs are not allowed: {a}")
        key = a.split("=", 1)[0]
        if key in PIP_BAD_FLAGS and name != "npm":
            raise SafetyError("BAD_INSTALL", f"Flag {key} is not allowed.")
        if a in ("-g", "--global", "--location=global"):
            raise SafetyError("BAD_INSTALL", "Global installs are not allowed.")
    # -r / -c / -e must point inside the workspace
    for i, a in enumerate(args):
        for opt in ("-r", "--requirement", "-c", "--constraint", "-e", "--editable"):
            val = None
            if a == opt and i + 1 < len(args):
                val = args[i + 1]
            elif a.startswith(opt + "=") and opt.startswith("--"):
                val = a.split("=", 1)[1]
            elif opt in ("-r", "-c", "-e") and a.startswith(opt) and len(a) > 2 and a[2] != "-":
                val = a[2:]
            if val is not None and val != ".":
                resolve_in_root(root, val)
