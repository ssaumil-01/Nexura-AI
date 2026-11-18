"""V2-5.6: patching edge cases (unique match, whitespace tolerance, CRLF/BOM/encoding, atomic multi-hunk)."""
import pytest

from core import patching
from core.safety import SafetyError


def h(s, r):
    return {"search_text": s, "replace_text": r}


def patch(raw: bytes, *hunks):
    return patching.patch_bytes(raw, list(hunks), 1_000_000)


def test_unique_match_is_replaced():
    assert patch(b"a = 1\nb = 2\n", h("b = 2", "b = 3")) == b"a = 1\nb = 3\n"


def test_no_match_reports_where_the_first_line_appears():
    with pytest.raises(SafetyError) as e:
        patch(b"x = 1\ndef f():\n    return 1\n", h("def f():\n    return 2", "pass"))
    assert e.value.code == "PATCH_NO_MATCH" and "line(s) 2" in e.value.message


def test_no_match_without_any_similar_line():
    with pytest.raises(SafetyError) as e:
        patch(b"x = 1\n", h("zzz", "y"))
    assert e.value.code == "PATCH_NO_MATCH" and "line(s)" not in e.value.message


def test_ambiguous_match_lists_candidate_lines_and_edits_nothing():
    raw = b"print(1)\nx = 1\nprint(1)\n"
    with pytest.raises(SafetyError) as e:
        patch(raw, h("print(1)", "print(2)"))
    assert e.value.code == "PATCH_AMBIGUOUS" and "1, 3" in e.value.message


def test_trailing_whitespace_in_the_search_block_is_tolerated():
    assert patch(b"a = 1   \nb = 2\n", h("a = 1\nb = 2", "a = 9\nb = 9")) == b"a = 9\nb = 9\n"
    assert patch(b"a = 1\nb = 2\n", h("a = 1  \nb = 2  ", "z")) == b"z\n"


def test_crlf_is_preserved_and_the_search_may_use_lf():
    raw = b"one\r\ntwo\r\nthree\r\n"
    assert patch(raw, h("one\ntwo", "ONE\nTWO\nEXTRA")) == b"ONE\r\nTWO\r\nEXTRA\r\nthree\r\n"


def test_mixed_line_endings_are_kept_outside_the_edit():
    raw = b"a\r\nb\nc\r\nd\n"
    assert patch(raw, h("c", "C")) == b"a\r\nb\nC\r\nd\n"


def test_bom_and_encoding_are_preserved():
    raw = b"\xef\xbb\xbfx = 1\r\ny = 2\r\n"
    out = patch(raw, h("y = 2", "y = 3"))
    assert out == b"\xef\xbb\xbfx = 1\r\ny = 3\r\n"
    u16 = "p = 1\nq = 2\n".encode("utf-16")
    assert patch(u16, h("q = 2", "q = 5")).decode("utf-16") == "p = 1\nq = 5\n"
    assert patch(u16, h("q = 2", "q = 5")).startswith(b"\xff\xfe")


def test_non_ascii_text_round_trips():
    assert patch("name = 'héllo'\n".encode(), h("héllo", "wörld")) == "name = 'wörld'\n".encode()


def test_multi_hunk_is_applied_together_and_order_does_not_matter():
    raw = b"a\nb\nc\nd\n"
    assert patch(raw, h("d", "D"), h("a", "A")) == b"A\nb\nc\nD\n"


def test_multi_hunk_is_atomic_when_one_hunk_fails():
    raw = b"a\nb\n"
    with pytest.raises(SafetyError):
        patch(raw, h("a", "A"), h("nope", "x"))      # nothing is returned, so nothing can be written


def test_overlapping_hunks_are_rejected():
    with pytest.raises(SafetyError) as e:
        patch(b"abc def\n", h("abc def", "x"), h("def", "y"))
    assert e.value.code == "PATCH_OVERLAP"


def test_caps_and_bad_input():
    for bad in ([], None, [h("", "x")], [{"search_text": "a"}], [h("a" * 20_001, "x")], [h("a", "x")] * 21):
        with pytest.raises(SafetyError):
            patching.check_hunks(bad)
    with pytest.raises(SafetyError) as e:
        patching.patch_bytes(b"a\n", [h("a", "b" * 50)], 10)
    assert e.value.code == "TOO_LARGE"


def test_binary_files_are_refused():
    with pytest.raises(SafetyError) as e:
        patch(b"\x89PNG\x00\x01", h("x", "y"))
    assert e.value.code == "BINARY_FILE"


def test_stray_newlines_around_the_block_are_tolerated_at_end_of_file():
    assert patch(b"a\nb", h("b\n", "c\n")) == b"a\nc"


def test_write_atomic_replaces_the_file(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"old")
    patching.write_atomic(f, b"new")
    assert f.read_bytes() == b"new" and [p.name for p in tmp_path.iterdir()] == ["x.txt"]
