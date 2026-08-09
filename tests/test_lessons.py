import json
import subprocess
from pathlib import Path

from setpoint.lessons import CAP, LessonStore, StoredLesson, repo_key


def _lesson(fp, ts="2026-08-07T00:00:00", hits=1, text="do the thing"):
    return StoredLesson(ts=ts, run="r", goal="g", fingerprint=fp,
                        normalized=f"norm {fp}", category="c", lesson=text, hits=hits)


def test_repo_key_uses_origin_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "git@github.com:jeffdhooton/setpoint.git"], check=True)
    key = repo_key(tmp_path)
    assert "jeffdhooton" in key and "setpoint" in key
    assert "/" not in key and ":" not in key


def test_repo_key_falls_back_to_path(tmp_path):
    key = repo_key(tmp_path)          # no git repo at all
    assert key and "/" not in key


def test_promote_and_load_roundtrip(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa")])
    loaded = store.load()
    assert len(loaded) == 1 and loaded[0].fingerprint == "aaa"


def test_promote_dedupes_by_fingerprint_incrementing_hits(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa", ts="2026-08-01T00:00:00")])
    store.promote([_lesson("aaa", ts="2026-08-07T00:00:00")])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].hits == 2
    assert loaded[0].ts == "2026-08-07T00:00:00"


def test_cap_evicts_lowest_hits_then_oldest(tmp_path):
    store = LessonStore("k", root=tmp_path)
    old_low = _lesson("victim", ts="2026-01-01T00:00:00", hits=1)
    keepers = [_lesson(f"fp{i:03}", ts="2026-06-01T00:00:00", hits=2) for i in range(CAP - 1)]
    store.promote([old_low] + keepers)
    store.promote([_lesson("newcomer", ts="2026-08-07T00:00:00", hits=1)])
    fps = {sl.fingerprint for sl in store.load()}
    assert len(fps) == CAP
    assert "victim" not in fps and "newcomer" in fps


def test_top_ranks_hits_then_recency(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("low", ts="2026-08-07T00:00:00", hits=1),
                   _lesson("high", ts="2026-01-01T00:00:00", hits=5)])
    assert store.top(1)[0].fingerprint == "high"


def test_load_skips_corrupt_lines(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa")])
    with store.path.open("a") as f:
        f.write("{not json\n")
    assert len(store.load()) == 1


def test_env_root_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_LESSONS_ROOT", str(tmp_path / "custom"))
    store = LessonStore("k")
    assert store.path == tmp_path / "custom" / "k.jsonl"


def test_promote_text_only_updates_if_fresher(tmp_path):
    """Regression: older lesson text should not clobber fresher phrasing."""
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa", ts="2026-08-07T00:00:00", text="new text")])
    store.promote([_lesson("aaa", ts="2026-01-01T00:00:00", text="stale text")])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].lesson == "new text"
    assert loaded[0].hits == 2
    assert loaded[0].ts == "2026-08-07T00:00:00"


def test_promote_validated_rules(tmp_path):
    # A validated by progress (next iter's fingerprint differs), B validated by
    # the pass that follows it.
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="passed", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f1", usd=0,
                   lesson="lesson A", fingerprint="fpA"),
        IterRecord(n=2, plan="", summary="", passed=False, feedback="f2", usd=0,
                   lesson="lesson B", fingerprint="fpB"),
        IterRecord(n=3, plan="", summary="", passed=True, feedback="ok", usd=0),
    ])
    store = LessonStore("k", root=tmp_path)
    promoted = promote_validated(state, "the goal", store)
    fps = {sl.fingerprint for sl in promoted}
    assert fps == {"fpA", "fpB"}
    assert all(sl.goal == "the goal" for sl in promoted)


def test_promote_validated_skips_unvalidated_tail_and_repeats(tmp_path):
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="stopped", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="lesson A", fingerprint="fpA"),
        IterRecord(n=2, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="lesson A", fingerprint="fpA"),  # same failure again
    ])
    store = LessonStore("k", root=tmp_path)
    assert promote_validated(state, "g", store) == []
    assert store.load() == []


def test_promote_validated_skips_empty_lessons(tmp_path):
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="passed", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="", fingerprint="fpA"),   # fallback lesson: no text
        IterRecord(n=2, plan="", summary="", passed=True, feedback="ok", usd=0),
    ])
    store = LessonStore("k", root=tmp_path)
    assert promote_validated(state, "g", store) == []


def test_render_lesson_full_and_partial():
    from setpoint.lessons import render_lesson
    assert render_lesson("update the config", "entrypoint missing", "rename skipped it") == \
        "update the config (bit this repo before: entrypoint missing — because: rename skipped it)"
    assert render_lesson("update the config", "entrypoint missing", "") == \
        "update the config (bit this repo before: entrypoint missing)"
    assert render_lesson("update the config", "", "rename skipped it") == \
        "update the config (because: rename skipped it)"
    assert render_lesson("update the config") == "update the config"


def test_stored_lesson_old_jsonl_line_loads_with_empty_evidence(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        '{"ts": "2026-08-07T00:00:00", "run": "r", "goal": "g", "fingerprint": "abc",'
        ' "normalized": "n", "category": "c", "lesson": "old-format lesson"}\n')
    loaded = store.load()
    assert loaded[0].symptom == "" and loaded[0].root_cause == ""


def test_promote_validated_carries_evidence(tmp_path):
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="passed", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="fix config", fingerprint="fpA",
                   symptom="entrypoint missing", root_cause="rename skipped it"),
        IterRecord(n=2, plan="", summary="", passed=True, feedback="ok", usd=0),
    ])
    store = LessonStore("k", root=tmp_path)
    promoted = promote_validated(state, "g", store)
    assert promoted[0].symptom == "entrypoint missing"
    assert promoted[0].root_cause == "rename skipped it"
    assert store.load()[0].symptom == "entrypoint missing"


def test_promote_merge_refreshes_evidence_with_text(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa", ts="2026-08-01T00:00:00")])
    fresher = _lesson("aaa", ts="2026-08-07T00:00:00", text="newer text")
    fresher.symptom, fresher.root_cause = "new symptom", "new cause"
    store.promote([fresher])
    got = store.load()[0]
    assert got.lesson == "newer text" and got.symptom == "new symptom" \
        and got.root_cause == "new cause"


def _repo_with(tmp_path, *files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return tmp_path


def test_anchored_files_extracts_existing_paths(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "calc/config.json", "calc/core.py")
    text = "When renaming, update the entrypoint field in calc/config.json too."
    assert anchored_files(text, repo) == ["calc/config.json"]


def test_anchored_files_handles_backticks_and_trailing_punctuation(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "calc/config.json")
    assert anchored_files("update `calc/config.json`.", repo) == ["calc/config.json"]


def test_anchored_files_ignores_nonexistent_versions_and_modules(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "calc/config.json")
    text = "on python 3.11 the calc.core module needs docs/missing.md updated"
    assert anchored_files(text, repo) == []


def test_anchored_files_caps_and_dedupes(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "a.txt", "b.txt", "c.txt", "d.txt")
    text = "fix a.txt a.txt b.txt c.txt d.txt"
    assert anchored_files(text, repo) == ["a.txt", "b.txt", "c.txt"]


def test_anchored_files_never_raises(tmp_path):
    from setpoint.lessons import anchored_files
    assert anchored_files("x" * 10000, tmp_path / "no-such-dir") == []


def test_anchored_files_blocks_path_traversal(tmp_path):
    from setpoint.lessons import anchored_files
    # Create repo with sub/ subdirectory
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sub").mkdir()
    # Create a file outside the repo
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    # Create a valid file inside repo
    (repo / "ok.txt").write_text("x")
    # Try to escape with path traversal - should not be anchored
    text = "see sub/../../secret.txt and ok.txt"
    result = anchored_files(text, repo)
    assert result == ["ok.txt"]
    assert "sub/../../secret.txt" not in result
