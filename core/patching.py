"""V2-5.6: robust patching. A patch is a list of hunks {search_text, replace_text}. Rules:
- every search block must match EXACTLY ONE place (0 or >1 matches -> a precise error with candidate line ranges, nothing edited);
- trailing whitespace and line-ending differences in the search block are tolerated;
- the file's own line endings (CRLF/LF, even mixed), BOM and encoding are preserved byte for byte outside the edited spans;
- all hunks are matched against the ORIGINAL text and applied together: all or none (overlapping hunks are an error);
- size caps. Read-before-write, protected paths, approvals and checkpoints are enforced by the callers (validator/tools)."""
import os
import re
import tempfile
from dataclasses import dataclass

from core.safety import SafetyError

MAX_HUNKS = 20
MAX_HUNK_CHARS = 20_000
MAX_CANDIDATES = 5


@dataclass
class Decoded:
    text: str
    encoding: str      # python codec, BOM included for utf-8-sig / utf-16
    eol: str           # the file's dominant line ending


def decode(raw: bytes) -> Decoded:
    if raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    else:
        enc = "utf-8"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        raise SafetyError("BINARY_FILE", "The file is not valid UTF-8/UTF-16 text; it cannot be patched.")
    if "\x00" in text:
        raise SafetyError("BINARY_FILE", "The file looks binary; it cannot be patched.")
    crlf, lf = text.count("\r\n"), text.count("\n") - text.count("\r\n")
    return Decoded(text, enc, "\r\n" if crlf > lf else "\n")


def encode(text: str, enc: str) -> bytes:
    if enc == "utf-16":                       # keep the BOM and byte order the file had
        return text.encode("utf-16")
    return text.encode(enc)


def normalize(text: str) -> tuple[str, list[int]]:
    """Trailing spaces/tabs stripped from every line, every line ending turned into \\n. Returns (normalized, map) where
    map[i] is the offset in `text` of normalized[i] and map[len(normalized)] == len(text)."""
    out: list[str] = []
    mp: list[int] = []
    i, n = 0, len(text)
    while i < n:
        j = i
        while j < n and text[j] not in "\r\n":
            j += 1
        c = j
        while c > i and text[c - 1] in " \t":
            c -= 1
        out.append(text[i:c])
        mp.extend(range(i, c))
        if j < n:
            out.append("\n")
            mp.append(j)
            i = j + (2 if text[j] == "\r" and j + 1 < n and text[j + 1] == "\n" else 1)
        else:
            i = j
    mp.append(n)
    return "".join(out), mp


def _line_of(norm: str, pos: int) -> int:
    return norm.count("\n", 0, pos) + 1


def _find_all(hay: str, needle: str) -> list[int]:
    out, start = [], 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def _ranges(norm: str, hits: list[int], length: int) -> str:
    parts = []
    for h in hits[:MAX_CANDIDATES]:
        a, b = _line_of(norm, h), _line_of(norm, h + max(length - 1, 0))
        parts.append(f"{a}" if a == b else f"{a}-{b}")
    return ", ".join(parts) + (" ..." if len(hits) > MAX_CANDIDATES else "")


def check_hunks(hunks) -> list[tuple[str, str]]:
    """Structure and size caps. Returns [(search, replace)] or raises SafetyError."""
    if not isinstance(hunks, list) or not hunks:
        raise SafetyError("BAD_HUNK", "hunks must be a non-empty list of {search_text, replace_text}.")
    if len(hunks) > MAX_HUNKS:
        raise SafetyError("PATCH_TOO_LARGE", f"At most {MAX_HUNKS} hunks per patch.")
    out = []
    for i, h in enumerate(hunks, 1):
        if not isinstance(h, dict) or not isinstance(h.get("search_text"), str) or not isinstance(h.get("replace_text"), str):
            raise SafetyError("BAD_HUNK", f"hunk {i}: needs string search_text and replace_text.")
        if not h["search_text"].strip():
            raise SafetyError("BAD_HUNK", f"hunk {i}: search_text must not be empty.")
        if len(h["search_text"]) > MAX_HUNK_CHARS or len(h["replace_text"]) > MAX_HUNK_CHARS:
            raise SafetyError("PATCH_TOO_LARGE", f"hunk {i}: search/replace text is over {MAX_HUNK_CHARS} characters.")
        out.append((h["search_text"], h["replace_text"]))
    return out


def apply_hunks(original: str, hunks, eol: str | None = None) -> str:
    """The patched text (original line endings kept), or SafetyError PATCH_NO_MATCH / PATCH_AMBIGUOUS / PATCH_OVERLAP."""
    pairs = check_hunks(hunks)
    norm, mp = normalize(original)
    dominant = eol or ("\r\n" if original.count("\r\n") * 2 > original.count("\n") else "\n")
    spans: list[tuple[int, int, str, int]] = []      # (start, end) in `original`, replacement, hunk number
    for i, (search, replace) in enumerate(pairs, 1):
        ns, _ = normalize(search)
        hits = _find_all(norm, ns)
        if not hits and ns.strip("\n") != ns:      # models often add stray leading/trailing newlines to the block
            lead, trail = len(ns) - len(ns.lstrip("\n")), len(ns) - len(ns.rstrip("\n"))
            ns = ns.strip("\n")
            replace = replace[lead:] if lead and replace.startswith("\n" * lead) else replace
            replace = replace[:len(replace) - trail] if trail and replace.endswith("\n" * trail) else replace
            hits = _find_all(norm, ns) if ns else []
        if not hits:
            first = next((ln.strip() for ln in ns.split("\n") if ln.strip()), "")
            near = [m.start() for m in re.finditer(re.escape(first), norm)] if first else []
            hint = (f" The first line of your search block appears at line(s) {_ranges(norm, near, len(first))}; "
                    "read the file again and copy the block exactly.") if near else " Read the file again and copy the block exactly."
            raise SafetyError("PATCH_NO_MATCH", f"hunk {i}: search_text was not found.{hint}")
        if len(hits) > 1:
            raise SafetyError("PATCH_AMBIGUOUS", f"hunk {i}: search_text matches {len(hits)} places (lines {_ranges(norm, hits, len(ns))}). "
                                                 "Include more surrounding lines so it matches exactly one place.")
        a, b = hits[0], hits[0] + len(ns)
        start = mp[a]
        end = mp[b - 1] + 1 if b > a else start       # original offset just after the last matched character
        if b > a and norm[b - 1] == "\n":             # the match ends with a line ending: swallow the whole original ending (\r\n)
            end = mp[b]
        span_text = original[start:end]
        use = "\r\n" if "\r\n" in span_text else "\n" if "\n" in span_text else dominant
        rep = replace.replace("\r\n", "\n").replace("\r", "\n").replace("\n", use)
        spans.append((start, end, rep, i))
    spans.sort()
    for (_s1, e1, _, h1), (s2, _, _, h2) in zip(spans, spans[1:], strict=False):
        if s2 < e1:
            raise SafetyError("PATCH_OVERLAP", f"hunks {h1} and {h2} overlap; merge them into one hunk.")
    out, pos = [], 0
    for s, e, rep, _ in spans:
        out += [original[pos:s], rep]
        pos = e
    out.append(original[pos:])
    return "".join(out)


def patch_bytes(raw: bytes, hunks, max_bytes: int) -> bytes:
    d = decode(raw)
    new = apply_hunks(d.text, hunks, d.eol)
    data = encode(new, d.encoding)
    if len(data) > max_bytes:
        raise SafetyError("TOO_LARGE", f"The patched file would exceed {max_bytes} bytes.")
    return data


def write_atomic(path, data: bytes) -> None:
    """Write next to the target, then replace: a crash never leaves a half-written file."""
    d = os.path.dirname(str(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".nexura-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.chmod(tmp, os.stat(path).st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
