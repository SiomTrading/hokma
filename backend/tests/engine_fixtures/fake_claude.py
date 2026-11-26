"""A stand-in for the `claude` CLI that replays stream-json recorded from a real run (see the .json fixtures).

Behaviour is chosen by a tag in the user's text:
  (none)    plain answer                 [perm]   the agent asks permission to use a tool and waits for the answer
  [slow]    streams until interrupted    [hang]   streams and ignores the interrupt (must be killed)
  [error]   the turn ends with an error result      [crash]  the process dies without a result
  [block]   a protection hook refused a tool call: a line is appended to <data>/guard/blocks.jsonl, then a plain answer
  [agent]   a subagent started in the background, with its steps and its end (agent_turn.json, recorded from CLI 2.1.331)
  [agent-perm]  the same, and the subagent asks permission for its Read step and waits for the answer
  [agent-slow]  the subagent is started and the turn streams until interrupted (an agent still working at a stop)
  [bgshell] a Bash command started in the background and its end (background_turn.json)
  [bgagent] an agent started in the background: the turn ends with its launch, and when the agent ends the CLI opens a turn
            of its own with its summary (what CLI 2.1.332 does, bug 1257). [bgagent2] two agents that end 0.4 s apart: two
            automatic turns in a row. [bgagent-never] one that never ends. [bgagent-long] an automatic turn that takes 1 s.
            [bgagent-slow] 1.5 s between the notice and the automatic turn. [bgagent-empty] an automatic turn with no text.
            [bgagent-late] 6 s between the notice and the automatic turn. [bgagent-nocont] the notice and no turn at all.
            [bgagent-fused] two notices and a single automatic turn for both.
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


def turn_agent(permission=False, slow=False):
    lines = load("agent_turn")["lines"]
    if slow:                                         # up to the receipt of the launch, then an endless stream
        cut = next(i for i, m in enumerate(lines) if m["type"] == "user") + 1
        for m in lines[:cut]:
            emit(m)
        return turn_slow()
    for m in lines:
        emit(m)
        step = m.get("parent_tool_use_id") and m["type"] == "assistant" and \
            [b for b in m["message"]["content"] if b.get("type") == "tool_use"]
        if permission and step:
            request_id = "agent-permission"
            emit({"type": "control_request", "request_id": request_id, "request": {
                "subtype": "can_use_tool", "tool_name": step[0]["name"], "input": step[0]["input"],
                "tool_use_id": step[0]["id"]}})
            while True:
                answer = get()
                if answer is None:
                    return False
                if answer != "timeout" and answer.get("type") == "control_response" and \
                        answer["response"].get("request_id") == request_id:
                    log({"event": "control_response", "response": answer["response"]})
                    break
    return True


AGENT_ID, AGENT_TASK = "toolu_01V2Y745ejshFWsbf43NPKZ3", "a856388600df4b08e"


def say(text, init=False, tail=0.0):
    """A turn that says `text`, shaped like text_turn.json (its first line, the init, only when `init`)."""
    lines = load("text_turn")["lines"]
    for m in (lines if init else lines[1:]):
        emit(json.loads(json.dumps(m).replace("pong", text)))
        if tail and m["type"] == "stream_event" and m["event"].get("type") == "content_block_delta":
            time.sleep(tail)


def agent_lines(suffix):
    """The lines of agent_turn.json with their ids made unique: (launch up to the receipt, what the agent does after)."""
    data = load("agent_turn")["lines"]
    cut = next(i for i, m in enumerate(data) if m["type"] == "user") + 1
    swap = lambda m: json.loads(json.dumps(m).replace(AGENT_ID, AGENT_ID + suffix).replace(AGENT_TASK, AGENT_TASK + suffix))
    return [swap(m) for m in data[1:cut]], [swap(m) for m in data[cut:-1]]


due = []          # (time, function): what the CLI does by itself once it is idle


def turn_bgagent(text):
    agents = ["A", "B"] if ("[bgagent2]" in text or "[bgagent-fused]" in text) else ["A"]
    emit(load("agent_turn")["lines"][0])                        # init
    for suffix in agents:
        launch, _after = agent_lines(suffix)
        for m in launch:
            emit(m)
    say("launched")
    if "[bgagent-never]" in text:
        return
    for n, suffix in enumerate(agents):
        _launch, after = agent_lines(suffix)

        def ends(after=after, suffix=suffix, last=n == len(agents) - 1):
            for m in after:
                emit(m)
            if "[bgagent-nocont]" in text or ("[bgagent-fused]" in text and not last):
                return                                           # the notice is on the pipe and the CLI opens no turn (yet)
            if "[bgagent-late]" in text:
                time.sleep(6.0)                                  # the CLI takes longer than the grouping window to open its turn
            elif "[bgagent-slow]" in text:
                time.sleep(1.5)                                  # the notice is on the pipe, the turn of the CLI is not yet
            emit(load("agent_turn")["lines"][0])                # the CLI opens a turn of its own
            say("" if "[bgagent-empty]" in text else ("agents A and B finished" if "[bgagent-fused]" in text else f"agent {suffix} finished"), tail=1.0 if "[bgagent-long]" in text else 0.0)
        due.append((time.time() + 0.6 + 0.4 * n, ends))


def turn_background_shell():
    for m in load("background_turn")["lines"]:
        emit(m)


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
        msg = get(0.05 if due else None)
        if msg is None:
            return 0
        if msg == "timeout" or msg.get("type") != "user":
            if due and time.time() >= due[0][0]:
                due.pop(0)[1]()
            continue
        text = user_text(msg)
        log({"event": "user", "message": msg["message"], "session": SESSION})
        if "[crash]" in text:
            sys.stderr.write("simulated crash\n")
            sys.stderr.flush()
            return 3
        if "[perm]" in text:
            alive = turn_permission()
        elif "[agent-perm]" in text:
            alive = turn_agent(permission=True)
        elif "[agent-slow]" in text:
            alive = turn_agent(slow=True)
        elif "[bgagent" in text:
            turn_bgagent(text)
            alive = True
        elif "[agent]" in text:
            alive = turn_agent()
        elif "[bgshell]" in text:
            turn_background_shell()
            alive = True
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
