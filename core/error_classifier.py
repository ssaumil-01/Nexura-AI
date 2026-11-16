"""Tiny regex classifier mapping command output to an error category (metrics + debugger hints)."""
import re

RULES = [
    ("missing_dependency", r"ModuleNotFoundError|No module named|Cannot find module|npm ERR! missing|command not found|is not recognized"),
    ("import_error", r"ImportError|cannot import name"),
    ("syntax_error", r"SyntaxError|IndentationError|TabError|Unexpected token|invalid syntax"),
    ("port_in_use", r"Address already in use|EADDRINUSE|Errno 98|WinError 10048|port is already allocated"),
    ("test_failure", r"AssertionError|FAILED|\d+ failed|Tests? failed|assert "),
]
HINTS = {
    "missing_dependency": "A module/program is missing: add it with install_dependency (and to requirements.txt/package.json).",
    "import_error": "An import failed: check the module/file names and the import path.",
    "syntax_error": "There is a syntax error: re-read the file and fix the exact line reported.",
    "port_in_use": "The port is busy: use a different port or stop the process using it.",
    "test_failure": "A test failed: read the assertion, compare expected vs actual, fix the code under test.",
    "unknown": "Read the error output carefully and fix the root cause.",
}


def classify(text: str) -> str:
    for name, pattern in RULES:
        if re.search(pattern, text or "", re.IGNORECASE):
            return name
    return "unknown"
