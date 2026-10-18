"""A stand-in for `claude -p … --output-format stream-json` used by tests/test_scheduler.py
and the scheduler live check. Prints stream-json events; never calls a model.

STUB_CLAUDE_MODE: ok (default) · noworkflow (init lists no Workflow tool, then idles) ·
sleep (starts a grandchild, then idles until killed) · fail (exit 3).
STUB_CLAUDE_PIDS: a file to append this pid (and the grandchild's) to.
"""
import json
import os
import subprocess
import sys
import time

mode = os.environ.get("STUB_CLAUDE_MODE", "ok")
pids = os.environ.get("STUB_CLAUDE_PIDS")


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def note(pid):
    if pids:
        with open(pids, "a") as f:
            f.write(f"{pid}\n")


note(os.getpid())
tools = ["Bash", "Read", "Skill"] + ([] if mode == "noworkflow" else ["Workflow"])
emit({"type": "system", "subtype": "init", "model": "stub", "permissionMode": "default", "tools": tools})
prompt = sys.argv[sys.argv.index("-p") + 1] if "-p" in sys.argv else ""
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": f"stub got: {prompt} args={' '.join(sys.argv[1:])}"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "echo GH_TOKEN=ghp_" + "a" * 36}}]}})
if mode in ("sleep", "noworkflow"):
    if mode == "sleep":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        note(child.pid)
    time.sleep(120)
if mode == "fail":
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"})
    sys.exit(3)
emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5, "total_cost_usd": 0,
      "result": "done", "permission_denials": []})
