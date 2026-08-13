"""Fake `scry mcp` stdio server: line-delimited JSON-RPC, in-memory rooms.

Run as: python tests/fake_room_mcp.py
Supports initialize, notifications/initialized, tools/call for the 8 room
tools. Tool results mirror scry's: result.content[0].text is a JSON blob,
result.isError True with a message text on failure.
"""
from __future__ import annotations

import json
import sys

rooms: dict[str, dict] = {}
tasks: dict[tuple[str, str], dict] = {}
msgs: dict[str, list[dict]] = {}
counter = {"n": 0}


def _id() -> str:
    counter["n"] += 1
    return f"id{counter['n']}"


def tool_result(rid, payload, is_error=False):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": body}],
                       "isError": is_error}}


def handle(name, a, rid):
    if name == "scry_room_create":
        room = {"id": _id(), "run_id": a["run_id"], "repo": a.get("repo", ""),
                "status": "open"}
        rooms[room["id"]] = room
        msgs[room["id"]] = []
        return tool_result(rid, room)
    room = rooms.get(a.get("room_id", ""))
    if room is None:
        return tool_result(rid, f'room {a.get("room_id")!r} not found', True)
    if name == "scry_room_close":
        room["status"] = "closed"
        return tool_result(rid, room)
    if name == "scry_task_post":
        t = {"id": _id(), "room_id": room["id"], "title": a["title"],
             "body": a.get("body", ""), "depends_on": a.get("depends_on", []),
             "interfaces": a.get("interfaces", ""), "status": "open"}
        tasks[(room["id"], t["id"])] = t
        return tool_result(rid, t)
    if name == "scry_task_list":
        return tool_result(rid, [t for (r, _), t in sorted(tasks.items()) if r == room["id"]])
    if name == "scry_post":
        m = {"seq": len(msgs[room["id"]]) + 1, "room_id": room["id"],
             "task_id": a.get("task_id", ""), "from": a["from"],
             "kind": a["kind"], "body": a["body"]}
        msgs[room["id"]].append(m)
        return tool_result(rid, m)
    if name == "scry_read":
        cur = int(a.get("cursor", 0))
        out = [m for m in msgs[room["id"]] if m["seq"] > cur][: int(a.get("limit", 50))]
        return tool_result(rid, {"messages": out,
                                 "cursor": out[-1]["seq"] if out else cur})
    return tool_result(rid, f"unknown tool {name}", True)


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": rid,
                          "result": {"protocolVersion": "2024-11-05",
                                     "capabilities": {}, "serverInfo": {"name": "fake"}}}),
              flush=True)
    elif method == "tools/call":
        p = req["params"]
        print(json.dumps(handle(p["name"], p.get("arguments", {}), rid)), flush=True)
    # notifications: no response
