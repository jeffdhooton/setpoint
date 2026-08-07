from setpoint.memory import Memory, IterRecord, RunState


def test_append_load_roundtrip(tmp_path):
    m = Memory("demo", root=tmp_path)
    m.start()
    m.append(IterRecord(n=1, plan="do X", summary="did X", passed=False,
                         feedback="test failed", usd=0.10, score=None))
    m.append(IterRecord(n=2, plan="fix X", summary="fixed X", passed=True,
                        feedback="all green", usd=0.05, score=None))

    loaded = Memory("demo", root=tmp_path).load()
    assert isinstance(loaded, RunState)
    assert loaded.name == "demo"
    assert len(loaded.iters) == 2
    assert loaded.iters[1].passed is True
    assert round(loaded.spent_usd, 2) == 0.15


def test_context_block_includes_history(tmp_path):
    m = Memory("demo", root=tmp_path)
    m.start()
    m.append(IterRecord(1, "plan a", "summary a", False, "lint error on line 4", 0.01, None))
    block = m.context_block()
    assert "iteration 1" in block.lower()
    assert "lint error on line 4" in block


def test_load_missing_returns_fresh(tmp_path):
    state = Memory("never", root=tmp_path).load()
    assert state.iters == []
    assert state.status == "new"


def test_old_state_json_without_lesson_fields_loads(tmp_path):
    import json
    from setpoint.memory import Memory
    root = tmp_path / "runs"
    (root / "t").mkdir(parents=True)
    (root / "t" / "state.json").write_text(json.dumps({
        "name": "t", "status": "stopped", "spent_usd": 0.1,
        "iters": [{"n": 1, "plan": "p", "summary": "s", "passed": False,
                   "feedback": "f", "usd": 0.1}],
    }))
    state = Memory("t", root=root).load()
    assert state.iters[0].lesson == ""
    assert state.iters[0].fingerprint == ""
    assert state.iters[0].repeat_of == ""


def test_context_block_lists_lessons_deduped(tmp_path):
    from setpoint.memory import IterRecord, Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.append(IterRecord(n=1, plan="p", summary="s", passed=False, feedback="f",
                          usd=0.0, lesson="pin the dep version", fingerprint="abc123"))
    mem.append(IterRecord(n=2, plan="p", summary="s", passed=False, feedback="f",
                          usd=0.0, lesson="pin the dep version", fingerprint="abc123"))
    block = mem.context_block()
    assert "## Lessons so far" in block
    assert block.count("pin the dep version") == 1
    assert "[abc123]" in block


def test_context_block_no_lessons_section_when_none(tmp_path):
    from setpoint.memory import IterRecord, Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.append(IterRecord(n=1, plan="p", summary="s", passed=False, feedback="f", usd=0.0))
    assert "Lessons so far" not in mem.context_block()


def test_log_marks_repeat_iterations(tmp_path):
    from setpoint.memory import IterRecord, Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.append(IterRecord(n=1, plan="p", summary="s", passed=False, feedback="f",
                          usd=0.0, repeat_of="abc123"))
    assert "repeat of lesson abc123" in mem.log_path.read_text()


def test_memory_note_appends_to_log(tmp_path):
    from setpoint.memory import Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.note("PLAN omitted required Lessons line after re-prompt")
    assert "PLAN omitted" in mem.log_path.read_text()
