import json
from types import SimpleNamespace

from setpoint.tuning import (BOUNDS, Overlay, apply_overlay, better_or_equal,
                             slug)


def _spec(max_turns=25, no_progress=None, explicit=()):
    return SimpleNamespace(
        execute=SimpleNamespace(max_turns=max_turns, plan_hint=""),
        stop=SimpleNamespace(no_progress_after=no_progress),
        explicit=list(explicit))


def test_better_or_equal_ordering():
    assert better_or_equal({"passed": True, "iters": 5, "usd": 1.0},
                           {"passed": False, "iters": 1, "usd": 0.1})
    assert better_or_equal({"passed": True, "iters": 3, "usd": 2.0},
                           {"passed": True, "iters": 4, "usd": 0.5})
    assert not better_or_equal({"passed": True, "iters": 4, "usd": 2.0},
                               {"passed": True, "iters": 4, "usd": 0.5})
    assert better_or_equal({"passed": True, "iters": 4, "usd": 0.5},
                           {"passed": True, "iters": 4, "usd": 0.5})


def test_apply_overlay_sets_and_clamps(tmp_path):
    spec = _spec()
    apply_overlay(spec, {"max_turns": 999, "no_progress_after": 1,
                         "plan_hint": "x" * 999})
    assert spec.execute.max_turns == BOUNDS["max_turns"][1]        # clamped to 50
    assert spec.stop.no_progress_after == BOUNDS["no_progress_after"][0]  # clamped to 2
    assert len(spec.execute.plan_hint) == 400


def test_apply_overlay_respects_explicit_user_values(tmp_path):
    spec = _spec(max_turns=40, no_progress=3,
                 explicit=["execute.max_turns", "stop.no_progress_after"])
    apply_overlay(spec, {"max_turns": 20, "no_progress_after": 5, "plan_hint": "h"})
    assert spec.execute.max_turns == 40
    assert spec.stop.no_progress_after == 3
    assert spec.execute.plan_hint == "h"     # hint is never user-set, always applies


def test_overlay_push_load_roundtrip(tmp_path):
    ov = Overlay("k", root=tmp_path)
    assert ov.load() == {}
    ov.push({"max_turns": 35}, {"passed": False, "iters": 8, "usd": 1.0})
    assert ov.load() == {"max_turns": 35}


def test_reconcile_reverts_on_worse(tmp_path):
    ov = Overlay("k", root=tmp_path)
    ov.push({"max_turns": 30}, {"passed": True, "iters": 3, "usd": 0.5})
    ov.push({"max_turns": 40}, {"passed": True, "iters": 3, "usd": 0.5})
    assert ov.load() == {"max_turns": 40}
    assert ov.reconcile({"passed": False, "iters": 8, "usd": 2.0}) == "reverted"
    assert ov.load() == {"max_turns": 30}


def test_reconcile_keeps_and_rebaselines_on_better(tmp_path):
    ov = Overlay("k", root=tmp_path)
    ov.push({"max_turns": 30}, {"passed": False, "iters": 8, "usd": 1.0})
    assert ov.reconcile({"passed": True, "iters": 2, "usd": 0.2}) == "kept"
    raw = json.loads(ov.path.read_text())
    assert raw["versions"][-1]["stats"]["passed"] is True   # new baseline


def test_reconcile_empty_overlay(tmp_path):
    assert Overlay("k", root=tmp_path).reconcile({"passed": True, "iters": 1, "usd": 0}) == "empty"


def test_corrupt_overlay_ignored(tmp_path):
    ov = Overlay("k", root=tmp_path)
    ov.path.parent.mkdir(parents=True, exist_ok=True)
    ov.path.write_text("{broken")
    assert ov.load() == {}
    assert ov.reconcile({"passed": True, "iters": 1, "usd": 0}) == "empty"


def test_slug():
    assert slug("My Spec.setpoint.yaml") == "my-spec-setpoint-yaml"
