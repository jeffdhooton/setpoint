"""Orchestrator-side client for scry fleet rooms.

Speaks MCP stdio JSON-RPC to a `scry mcp` subprocess. This is only for the
fleet orchestrator (create room, post tasks, watch the board, close room);
workers talk to the room through their own engine's scry MCP configuration.
"""
from __future__ import annotations

import json
import subprocess


class RoomError(RuntimeError):
    pass


class RoomClient:
    def __init__(self, argv: list[str] | None = None):
        self.argv = argv or ["scry", "mcp"]
        self._proc: subprocess.Popen | None = None
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------

    def _ensure(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            try:
                self._proc = subprocess.Popen(
                    self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True)
            except FileNotFoundError:
                raise RoomError(f"scry binary not found: {self.argv[0]}")
            self._rpc("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "setpoint", "version": "1"}})
            self._notify("notifications/initialized")
        return self._proc

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None

    def __enter__(self) -> "RoomClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _send(self, obj: dict) -> None:
        proc = self._proc
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id,
                    "method": method, "params": params})
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RoomError(f"scry mcp exited during {method}")
            resp = json.loads(line)
            # Skip notifications (have "method" key or ID doesn't match)
            if "method" in resp or resp.get("id") != self._next_id:
                continue
            if "error" in resp:
                raise RoomError(f"{method}: {resp['error']}")
            return resp["result"]

    def _tool(self, name: str, args: dict) -> dict | list:
        self._ensure()
        result = self._rpc("tools/call", {"name": name, "arguments": args})
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise RoomError(f"{name}: {text}")
        return json.loads(text)

    # -- room API ----------------------------------------------------------

    def create_room(self, run_id: str, repo: str) -> dict:
        return self._tool("scry_room_create", {"run_id": run_id, "repo": repo})

    def close_room(self, room_id: str) -> dict:
        return self._tool("scry_room_close", {"room_id": room_id})

    def post_task(self, room_id: str, title: str, body: str = "",
                  depends_on: list[str] | None = None, interfaces: str = "") -> dict:
        args: dict = {"room_id": room_id, "title": title}
        if body:
            args["body"] = body
        if depends_on:
            args["depends_on"] = depends_on
        if interfaces:
            args["interfaces"] = interfaces
        return self._tool("scry_task_post", args)

    def list_tasks(self, room_id: str) -> list[dict]:
        return self._tool("scry_task_list", {"room_id": room_id})

    def post(self, room_id: str, from_: str, kind: str, body: str,
             task_id: str = "") -> dict:
        args = {"room_id": room_id, "from": from_, "kind": kind, "body": body}
        if task_id:
            args["task_id"] = task_id
        return self._tool("scry_post", args)

    def read(self, room_id: str, cursor: int = 0, limit: int = 50) -> dict:
        return self._tool("scry_read", {"room_id": room_id, "cursor": cursor,
                                        "limit": limit})
