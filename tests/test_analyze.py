from setpoint.analyze import fingerprint, is_repeat, normalize_feedback


def test_same_failure_different_paths_and_lines_match():
    a = "FAILED /Users/jeff/proj/tests/test_api.py:42: AssertionError: expected 200 got 500"
    b = "FAILED /home/ci/build/tests/test_api.py:97: AssertionError: expected 200 got 500"
    assert fingerprint(a) == fingerprint(b)


def test_different_failures_differ():
    assert fingerprint("ImportError: no module named foo") != \
        fingerprint("SyntaxError: invalid syntax on line 3")


def test_normalize_strips_durations_and_timestamps():
    a = normalize_feedback("2 failed in 0.34s at 2026-08-07T10:00:01")
    b = normalize_feedback("2 failed in 1.99s at 2026-08-07T11:23:45")
    assert a == b


def test_normalize_collapses_whitespace_and_case():
    assert normalize_feedback("Error:   Foo\n\tbar") == normalize_feedback("error: foo bar")


def test_is_repeat_exact_fingerprint():
    fb = "AssertionError: expected True"
    priors = [(fingerprint(fb), normalize_feedback(fb), "assertion")]
    assert is_repeat("AssertionError: expected True", "", priors) == fingerprint(fb)


def test_is_repeat_near_match_needs_same_category():
    a = "ImportError: cannot import name 'get_user' from 'app.auth.helpers'"
    b = "ImportError: cannot import name 'get_users' from 'app.auth.helpers'"
    priors = [(fingerprint(a), normalize_feedback(a), "import-error")]
    assert fingerprint(b) != fingerprint(a)          # not an exact match
    assert is_repeat(b, "import-error", priors) is not None   # near-match + same category
    assert is_repeat(b, "type-error", priors) is None          # category differs
    assert is_repeat(b, "", priors) is None                    # no category, no near-match


def test_is_repeat_none_when_unrelated():
    priors = [(fingerprint("ImportError: no module named foo"),
               normalize_feedback("ImportError: no module named foo"), "import-error")]
    assert is_repeat("SyntaxError: invalid syntax", "import-error", priors) is None


def test_normalize_preserves_numbers_in_identifiers():
    r"""Numbers embedded in identifiers are preserved as complete runs.

    The _NUM regex uses negative lookbehind on \w (word characters: [a-zA-Z0-9_])
    to ensure that NO digit in an identifier-suffix is stripped. This prevents
    partial matches within multi-digit runs (e.g., 'port80' must be preserved
    entirely, not partially collapsed to 'port8#').

    Guarantee: Any digit preceded by a word character (letter, digit, or underscore)
    is NOT stripped. This preserves:
    - Algorithm names (sha256, sha512, md5)
    - Version identifiers (test2, test3, v1, v2)
    - Port numbers (port8080, port9090, port80, port81)
    - Error codes (error404, error409)

    Only 'bare' numbers (not preceded by word chars) are stripped:
    - Line numbers (":42:" → ":#:")
    - Bare counters ("attempt 1" → "attempt #")
    - Durations ("0.34s" → "#s") [timestamp pre-pass handles T##:##:##]
    """
    # Different algorithms must produce different fingerprints
    assert fingerprint("ImportError: cannot import 'sha256'") != \
           fingerprint("ImportError: cannot import 'sha512'")

    # Different test/version identifiers must produce different fingerprints
    assert fingerprint("test2 failed") != fingerprint("test3 failed")

    # Critical: multi-digit identifier suffixes must not partially collapse
    # (these would previously fail with (?<![a-zA-Z_]) lookbehind)
    assert fingerprint("port80") != fingerprint("port81")  # 2nd digit differs
    assert fingerprint("error404") != fingerprint("error409")  # 2nd+ digit differs
    assert fingerprint("test10") != fingerprint("test19")  # 2nd digit differs

    # But bare numbers (not in identifiers) are still stripped
    assert normalize_feedback("attempt 1") == normalize_feedback("attempt 2")
    assert normalize_feedback("line 42") == normalize_feedback("line 97")
