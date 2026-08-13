from __future__ import annotations

import sys
from pathlib import Path

import pytest

from setpoint.room import RoomClient, RoomError

FAKE = [sys.executable, str(Path(__file__).parent / "fake_room_mcp.py")]


@pytest.fixture()
def client():
    with RoomClient(argv=FAKE) as c:
        yield c


def test_room_roundtrip(client):
    room = client.create_room("run-1", "/repo")
    assert room["status"] == "open" and room["id"]

    t1 = client.post_task(room["id"], "build API", interfaces="GET /leads")
    t2 = client.post_task(room["id"], "build UI", depends_on=[t1["id"]])
    board = client.list_tasks(room["id"])
    assert [t["title"] for t in board] == ["build API", "build UI"]
    assert board[1]["depends_on"] == [t1["id"]]

    client.post(room["id"], from_="orchestrator", kind="status", body="launched")
    read = client.read(room["id"], cursor=0)
    assert read["cursor"] == 1 and read["messages"][0]["kind"] == "status"
    # incremental
    read2 = client.read(room["id"], cursor=read["cursor"])
    assert read2["messages"] == [] and read2["cursor"] == 1

    closed = client.close_room(room["id"])
    assert closed["status"] == "closed"


def test_tool_error_raises(client):
    with pytest.raises(RoomError, match="not found"):
        client.list_tasks("nope")


def test_close_idempotent():
    c = RoomClient(argv=FAKE)
    c.create_room("r", "/x")
    c.close()
    c.close()  # second close must not raise


def test_missing_binary():
    c = RoomClient(argv=["definitely-not-a-binary-xyz"])
    with pytest.raises(RoomError, match="not found"):
        c.create_room("r", "/x")


def test_notification_skipping():
    with RoomClient(argv=FAKE + ["--noisy"]) as c:
        room = c.create_room("run-1", "/repo")
        assert room["status"] == "open" and room["id"]

        t1 = c.post_task(room["id"], "build API", interfaces="GET /leads")
        t2 = c.post_task(room["id"], "build UI", depends_on=[t1["id"]])
        board = c.list_tasks(room["id"])
        assert [t["title"] for t in board] == ["build API", "build UI"]
        assert board[1]["depends_on"] == [t1["id"]]

        c.post(room["id"], from_="orchestrator", kind="status", body="launched")
        read = c.read(room["id"], cursor=0)
        assert read["cursor"] == 1 and read["messages"][0]["kind"] == "status"

        closed = c.close_room(room["id"])
        assert closed["status"] == "closed"
