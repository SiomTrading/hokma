"""A stand-in for `claude` that only knows `--version`, `auth status` and `auth login`.

Sign-in state is the file `logged_in` inside the config folder named by the environment; its content is the plan
(empty = the CLI does not report one). `auth login` prints a link, waits for one line of input (the code) and signs in.
"""
import json
import os
import sys
from pathlib import Path

config = Path(os.environ["CLAUDE_CONFIG_DIR"])
marker = config / "logged_in"
args = sys.argv[1:]
debug_file = None
if args[:1] == ["--debug-file"]:
    if os.environ.get("FAKE_NO_DEBUG_FILE"):
        print("error: unknown option '--debug-file'")
        sys.exit(1)
    debug_file, args = args[1], args[2:]
    with open(os.path.join(os.path.dirname(debug_file), "fake-env.json"), "w") as out:
        json.dump(dict(os.environ), out)


def debug(line):
    if debug_file:
        with open(debug_file, "a") as out:
            out.write(line + "\n")


if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)")
elif args[:2] == ["auth", "status"]:
    if marker.exists():
        data = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
        plan = marker.read_text().strip()
        if plan:
            data["subscriptionType"] = plan
        print(json.dumps(data))
    else:
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        sys.exit(1)
elif args[:2] == ["auth", "login"] and os.environ.get("FAKE_LOGIN_403"):
    print("Opening browser to sign in...")
    print("If the browser did not open, visit: https://claude.com/cai/oauth/authorize?code=true&state=SECRETSTATE123")
    print("Paste code here if prompted > ", end="", flush=True)
    sys.stdin.readline()
    debug("2026-11-11T10:00:00.000Z [DEBUG] Remote settings: No settings found (404)")
    debug("2026-11-11T10:00:01.000Z [ERROR] OAuth login failed: Request failed with status code 403 token=" + "Z9y8X7w6" * 5)
    print("Login failed: Request failed with status code 403")
    sys.exit(1)
elif args[:2] == ["auth", "login"] and os.environ.get("FAKE_LOGIN_FAIL"):
    print("\x1b[31mError: OAuth flow failed\x1b[0m")
    print("callback https://claude.com/cai/oauth/authorize?code=true&state=SECRETSTATE123")
    print("token sk-ant-api03-ABCDEFGHIJKLMNOP and " + "A1b2C3d4" * 5)
    sys.exit(3)
elif args[:2] == ["auth", "login"]:
    print("Opening browser to sign in...")
    print("If the browser did not open, visit: https://claude.com/cai/oauth/authorize?code=true&state=abc")
    print("Paste code here if prompted > ", end="", flush=True)
    code = sys.stdin.readline().strip()
    if code == "good-code":
        config.mkdir(parents=True, exist_ok=True)
        marker.write_text(os.environ.get("FAKE_PLAN", "max"))
        print("Login successful")
    else:
        print("Invalid code")
        sys.exit(1)
else:
    sys.exit(2)
