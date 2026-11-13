"""A stand-in for the `claude` CLI that replays stream-json recorded from a real run (see the .json fixtures).

Behaviour is chosen by a tag in the user's text:
  (none)    plain answer                 [perm]   the agent asks permission to use a tool and waits for the answer
  [slow]    streams until interrupted    [hang]   streams and ignores the interrupt (must be killed)
  [error]   the turn ends with an error result      [crash]  the process dies without a result
  [block]   a protection hook refused a tool call: a line is appended to <data>/guard/blocks.jsonl, then a plain answer
  [child]   starts a command that hangs (a `git clone` waiting for a password) in its own session, holding the
            output pipes, then streams and ignores the interrupt: the case of task 3175
Everything the process receives is appended to the file named by FAKE_CLAUDE_LOG (one JSON object per line).
"""
import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
argv = sys.argv[1:]


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


SESSION = flag("--resume") or flag("--session-id") or str(uuid.uuid4())
LOG = os.environ.get("FAKE_CLAUDE_LOG")


def log(entry):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def load(name):
    return json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))


def emit(obj):
    text = json.dumps(obj, ensure_ascii=False).replace("{{SESSION}}", SESSION).replace("{{CWD}}", os.getcwd())
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


inbox = queue.Queue()


def read_stdin():
    for line in sys.stdin:
        line = line.strip()
        if line:
            inbox.put(json.loads(line))
    inbox.put(None)


def get(timeout=None):
    try:
        return inbox.get(timeout=timeout)
    except queue.Empty:
        return "timeout"


def user_text(message):
    blocks = message["message"]["content"]
    return " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


def turn_text():
    for m in load("text_turn")["lines"]:
        emit(m)


def turn_block():
    folder = os.path.join(os.environ["HOKMA_DATA_DIR"], "guard")
    os.makedirs(folder, exist_ok=True)
    line = {"ts": "2026-11-05T12:00:00Z", "rule": "delete", "tool": "Bash", "target": "rm informes/viejo.md",
            "conversation_id": os.environ.get("HOKMA_CONVERSATION_ID", "")}
    if os.environ.get("HOKMA_TASK_ID"):
        line["task_id"] = os.environ["HOKMA_TASK_ID"]
    with open(os.path.join(folder, "blocks.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    turn_text()


def turn_error(text="simulated failure"):
    for m in load("text_turn")["lines"][:-1]:
        emit(m)
    result = dict(load("text_turn")["lines"][-1])
    result.update({"subtype": "error_during_execution", "is_error": True, "result": text})
    emit(result)


def turn_permission():
    data = load("permission_turn")
    for m in data["pre"]:
        emit(m)
    emit(data["request"])
    request_id = data["request"]["request_id"]
    while True:
        msg = get()
        if msg is None:
            return False
        if msg != "timeout" and msg.get("type") == "control_response" and \
                msg["response"].get("request_id") == request_id:
            body = msg["response"].get("response", {})
            log({"event": "control_response", "response": msg["response"]})
            for m in data["post_allow" if body.get("behavior") == "allow" else "post_deny"]:
                emit(m)
            return True


def turn_slow(responsive=True):
    data = load("interrupt_turn")
    for m in data["pre"]:
        emit(m)
    while True:
        emit(data["delta"])
        msg = get(0.05)
        if msg is None:
            return False
        if msg != "timeout" and msg.get("type") == "control_request" and msg["request"]["subtype"] == "interrupt":
            log({"event": "interrupt"})
            if not responsive:
                time.sleep(3600)
            ack = dict(data["interrupt_ack"])
            ack["response"] = dict(ack["response"], request_id=msg["request_id"])
            emit(ack)
            for m in data["post"]:
                emit(m)
            return True


def main():
    log({"event": "start", "pid": os.getpid(), "argv": argv, "env": dict(os.environ), "cwd": os.getcwd()})
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        msg = get()
        if msg is None:
            return 0
        if msg == "timeout" or msg.get("type") != "user":
            continue
        text = user_text(msg)
        log({"event": "user", "message": msg["message"], "session": SESSION})
        if "[crash]" in text:
            sys.stderr.write("simulated crash\n")
            sys.stderr.flush()
            return 3
        if "[perm]" in text:
            alive = turn_permission()
        elif "[child]" in text:
            import subprocess
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"],
                                     start_new_session=True)       # leaves the process group, keeps stdout/stderr
            log({"event": "child", "pid": child.pid})
            alive = turn_slow(responsive=False)
        elif "[hang]" in text:
            alive = turn_slow(responsive=False)
        elif "[slow]" in text:
            alive = turn_slow()
        elif "[error]" in text:
            turn_error()
            alive = True
        elif "[notlogged]" in text:
            turn_error("Not logged in \u00b7 Please run /login")
            alive = True
        elif "[block]" in text:
            turn_block()
            alive = True
        else:
            turn_text()
            alive = True
        if not alive:
            return 0


if __name__ == "__main__":
    sys.exit(main())
