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
