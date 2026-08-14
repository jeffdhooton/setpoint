"""Multi-fleet live viewer. Discovers fleets from ~/.setpoint/fleets/*/room.json.
Usage: python3 fleet_viewer.py [port]"""
import json, subprocess, sys, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
RUNS = Path.home() / ".setpoint" / "runs"
FLEETS = Path.home() / ".setpoint" / "fleets"
START = time.time()

class Scry:
    def __init__(self):
        self.p, self.lock, self.n = None, threading.Lock(), 0
    def _ensure(self):
        if self.p is None or self.p.poll() is not None:
            self.p = subprocess.Popen(["/Users/jeff/go/bin/scry", "mcp"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self._rpc("initialize", {"protocolVersion": "2024-11-05",
                "capabilities": {}, "clientInfo": {"name": "viewer", "version": "2"}})
            self.p.stdin.write(json.dumps({"jsonrpc": "2.0",
                "method": "notifications/initialized"}) + "\n")
            self.p.stdin.flush()
    def _rpc(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n,
            "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("scry mcp died")
            r = json.loads(line)
            if "method" in r or r.get("id") != self.n:
                continue
            return r["result"]
    def tool(self, name, args):
        with self.lock:
            self._ensure()
            r = self._rpc("tools/call", {"name": name, "arguments": args})
            return json.loads(r["content"][0]["text"])

scry = Scry()


def frozen_statuses(fleet_dir, manifest):
    """A fleet whose report.md is newer than its room.json has ENDED: its
    member statuses are frozen in the report's table. Live run state can't
    be trusted for ended fleets — a later wave reusing a member spec name
    overwrites ~/.setpoint/runs/<name>/ (aliasing)."""
    rp = fleet_dir / "report.md"
    if not rp.exists() or rp.stat().st_mtime <= (fleet_dir / "room.json").stat().st_mtime:
        return None
    out = {}
    for line in rp.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in manifest.get("members", {}):
            out[parts[0]] = parts[1]
    return out or None

ARCHIVE_MARKER = ".archived"

# Anything here means the member finished its work. "passed" is the
# no-room outcome; the rest are the review-aware fleet statuses.
DONE_STATUSES = {"passed", "review-approved", "completed-capped", "unreviewed"}


def member_state(fleet_name, member):
    """A member's state.json. Fleet members live under the fleet's own runs
    dir; older fleets predate that and kept state in the global runs root, so
    fall back rather than showing every historical fleet as 'pending'."""
    for p in (FLEETS / fleet_name / "runs" / member / "state.json",
              RUNS / member / "state.json"):
        try:
            return json.loads(p.read_text())
        except Exception:
            continue
    return None


def is_archived(fleet_dir):
    return (fleet_dir / ARCHIVE_MARKER).exists()


def set_archived(name, archived):
    """Archiving is a marker file, not a delete: a finished fleet leaves the
    view but keeps its transcript. `setpoint fleet rm` is the real delete."""
    d = FLEETS / name
    if not d.is_dir():
        return False
    marker = d / ARCHIVE_MARKER
    if archived:
        marker.write_text("")
    else:
        marker.unlink(missing_ok=True)
    return True


def fleets(include_archived=False):
    out = []
    if FLEETS.exists():
        for d in FLEETS.iterdir():
            mf = d / "room.json"
            if not mf.exists():
                continue
            archived = is_archived(d)
            if archived and not include_archived:
                continue
            m = json.loads(mf.read_text())
            m["name"] = d.name
            m["mtime"] = mf.stat().st_mtime
            m["archived"] = archived
            repo = m.get("repo", "")
            m["project"] = Path(repo).name if repo else "?"
            final = frozen_statuses(d, m)
            if final is not None:
                statuses, m["active"] = list(final.values()), False
                m["final"] = final
            else:
                statuses = []
                for mem in m.get("members", {}):
                    st = member_state(d.name, mem)
                    statuses.append(st.get("status", "?") if st else "pending")
                m["active"] = any(s == "running" for s in statuses)
            m["passed"] = sum(1 for s in statuses if s in DONE_STATUSES)
            m["total"] = len(statuses)
            out.append(m)
    return sorted(out, key=lambda m: (-m["active"], -m["mtime"]))

def state(fleet_name):
    manifest = next((f for f in fleets(include_archived=True)
                     if f["name"] == fleet_name), None)
    if not manifest:
        return {"error": f"unknown fleet {fleet_name!r}", "members": {},
                "board": [], "messages": [], "started": START}
    members = {}
    frozen = manifest.get("final")
    for m in manifest.get("members", {}):
        if frozen and m in frozen:
            # The frozen status is authoritative (a later wave can overwrite
            # live run state), but the iteration count is not carried in
            # report.md — read it from the member's own state so an ended
            # fleet does not claim every member ran zero iterations.
            d = member_state(fleet_name, m)
            members[m] = {"status": frozen[m],
                          "iters": len(d.get("iters", [])) if d else 0}
            continue
        d = member_state(fleet_name, m)
        members[m] = ({"status": d.get("status", "?"), "iters": len(d.get("iters", []))}
                      if d else {"status": "pending", "iters": 0})
    outcome = None
    if manifest.get("final") is not None:
        rp = FLEETS / fleet_name / "report.md"
        if rp.exists():
            txt = rp.read_text()
            if "## Outcome" in txt:
                outcome = txt.split("## Outcome", 1)[1].split("## Room transcript", 1)[0].strip()
    out = {"members": members, "board": [], "messages": [], "outcome": outcome,
           "room": manifest["room_id"], "fleet": fleet_name,
           "started": START, "error": None,
           "version": (Path(__file__).parent / "fleet_page.html").stat().st_mtime}
    try:
        out["board"] = scry.tool("scry_task_list", {"room_id": manifest["room_id"]})
        out["messages"] = scry.tool("scry_read",
            {"room_id": manifest["room_id"], "cursor": 0, "limit": 500})["messages"]
    except Exception as e:
        out["error"] = str(e)
    return out

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, body, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/fleets.json":
            return self._send(
                json.dumps(fleets(include_archived=q.get("all", ["0"])[0] == "1")).encode(),
                "application/json")
        if u.path in ("/archive", "/unarchive"):
            want = u.path == "/archive"
            if q.get("finished", ["0"])[0] == "1":
                # Clear the backlog in one go: every fleet that is not
                # currently running. Live fleets are never swept.
                names = [f["name"] for f in fleets() if not f["active"]]
            else:
                names = q.get("fleet", [])
            done = [n for n in names if set_archived(n, want)]
            return self._send(json.dumps({"ok": True, "changed": done}).encode(),
                              "application/json")
        if u.path == "/state.json":
            q = parse_qs(u.query)
            name = q.get("fleet", [None])[0] or (fleets() or [{"name": ""}])[0]["name"]
            return self._send(json.dumps(state(name)).encode(), "application/json")
        # any other path: serve the page; the page reads ?fleet= itself
        page = (Path(__file__).parent / "fleet_page.html").read_text()
        return self._send(page.encode(), "text/html; charset=utf-8")

print(f"fleet viewer: http://localhost:{PORT}")
HTTPServer(("127.0.0.1", PORT), H).serve_forever()
