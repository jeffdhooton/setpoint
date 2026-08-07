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
    """Numbers embedded in identifiers are preserved to distinguish different code.

    Identifiers like 'sha256', 'test2', 'port8080' are NOT normalized because they
    represent different code versions, algorithms, or configurations and should
    produce different fingerprints. Only 'bare' numbers (variable counters, line
    numbers, timestamps) are stripped.

    Trade-off: the regex avoids collapsing variables (retry counts, timestamps)
    which improves repeat detection, while accepting that different algorithm
    names (sha256 vs sha512) remain distinct.
    """
    # Different algorithms are preserved as different failures
    assert normalize_feedback("ImportError: cannot import 'sha256'") != \
           normalize_feedback("ImportError: cannot import 'sha512'")

    # Different test versions are preserved as different failures
    assert normalize_feedback("test2 failed") != normalize_feedback("test3 failed")

    # Different ports are preserved as different failures
    assert normalize_feedback("port8080") != normalize_feedback("port9090")

    # But bare numbers are still stripped
    assert normalize_feedback("attempt 1") == normalize_feedback("attempt 2")
    assert normalize_feedback("line 42") == normalize_feedback("line 97")
