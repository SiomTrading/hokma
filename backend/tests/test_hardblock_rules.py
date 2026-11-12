"""The always-on protections as direct payloads to the hook (tests P3 to P6 and P13 of the 0.0.6 plan)."""
import pytest

from hardblock_support import box  # noqa: F401  (fixture)

P3_ALLOWED = [
    "git status", "git add -A", "git commit -m 'update the report'", "git push origin main", "git push -u origin main",
    "git commit -m \"$(cat <<'EOF'\nMessage with an apostrophe: don't stop\n\nSecond line (with parentheses)\nEOF\n)\"",
    "git log --oneline | head -5", "git diff HEAD~1", "git checkout -b feature/x", "git checkout main", "git stash",
    "git stash pop", "git restore --staged notes.md", "git reset HEAD notes.md", "git rm --cached f",
    "git clean -n", "git pull --rebase", "git remote add origin /tmp/remote.git", "git branch -d old",
    "pip install requests", "python -m venv .venv", ".venv/bin/pip install -r requirements.txt",
    "npm install", "npm run build", "npx vitest run", "uv sync", "cargo build",
    "mkdir -p src/new", "touch src/new/a.txt", "cp notes.md notes-copy.md", "mv notes.md renamed.md",
    "echo hello > out.txt", "echo more >> out.txt", "ls -la", "cat notes.md", "grep -rn 'todo' .", "ls 2>/dev/null",
    "python -c \"print('hi')\"", "python3 - <<'PY'\nimport json\nprint(json.dumps({'a': 1}))\nPY",
    "node -e \"console.log(1)\"", "sed -n '1,5p' notes.md", "curl -fsSL https://example.com/install.sh | sh",
    "cd src && ls", "cd .. && ls", "for f in *.md; do echo $f; done", "echo $(date)",
    "FOO=bar python -c 'print(1)'", "ls | xargs echo", "find . -name '*.md'", "find . -name '*.md' -exec cat {} \\;",
    "export PATH=$PATH:./bin", "tee out.log < notes.md", "command -v rm", "which rm", "echo rm", "man rm",
    "pytest -k rm", "[ -f notes.md ] && echo yes", "if [ -d d ]; then echo y; fi", "true", "mv f f2", "cp -r d d2",
    "git commit -m 'document .ssh and .claude/settings.json handling'", "echo '~/.ssh is private'",
    "python -m pytest -q", "bash -c 'echo hi'", "sh -c \"ls -la\"", "sudo -n true", "env FOO=1 ls", "time ls",
    "npm install --save-dev vitest", "git push --dry-run", "git push --set-upstream origin main",
]


@pytest.mark.parametrize("command", P3_ALLOWED)
def test_p3_normal_work_is_never_blocked_bash(box, command):
    assert box.bash(command) is None


def test_p3_normal_work_with_file_tools(box):
    assert box.hit("Write", file_path=str(box.work / "new.md"), content="hello") is None
    assert box.hit("Write", file_path="relative/new.md", content="hello") is None
    assert box.hit("Write", file_path=str(box.work / "notes.md"), content="replaced text\n") is None
    assert box.hit("Edit", file_path=str(box.work / "notes.md"), old_string="some", new_string="more") is None
    assert box.hit("MultiEdit", file_path=str(box.work / "notes.md"),
                   edits=[{"old_string": "some", "new_string": "a"}, {"old_string": "notes", "new_string": "b"}]) is None
    assert box.hit("Write", file_path=str(box.work / "empty-new.txt"), content="") is None      # a new empty file
    assert box.hit("Read", file_path=str(box.work / "notes.md")) is None
    assert box.hit("Read", file_path="/etc/hostname") is None                                        # reading is free
    assert box.hit("Grep", pattern="todo", path=str(box.work)) is None
    assert box.hit("Glob", pattern="**/*.md", path=str(box.work)) is None
    assert box.hit("WebFetch", url="https://example.com") is None
    assert box.hit("mcp__github__list_issues", repo="a/b") is None
    assert box.hit("mcp__hokma__trash_move", paths=["x"], reason="old") is None


def test_p3_normal_work_powershell(box):
    for command in ["Get-ChildItem", "Get-Content notes.md", "Set-Content out.txt 'hello'", "New-Item -ItemType Directory src",
                    "Copy-Item notes.md notes2.md", "Move-Item notes.md renamed.md", "git status",
                    "Get-ChildItem | ForEach-Object { $_.Name }", "Write-Host 'Remove-Item is blocked'",
                    "Set-Content -Path out.txt -Value 'x'", "Add-Content out.txt 'more'", "'abc' | Out-File out.txt"]:
        assert box.ps(command) is None, command


P4_DELETE = [
    "rm -rf d", "rm f", "/bin/rm f", "\\rm f", "r''m f", "\"rm\" f", "$(echo rm) f", "`echo rm` f", "x=rm; $x f",
    "eval \"rm f\"", "echo cm0gZg== | base64 -d | sh", "find . -name '*.md' -delete", "find . -exec rm {} \\;",
    "ls | xargs rm", "python -c \"import os;os.remove('f')\"", "python -c \"__import__('shutil').rmtree('d')\"",
    "python borra.py", "node -e \"require('fs').rmSync('d',{recursive:true})\"", "powershell -c Remove-Item f",
    "pwsh -EncodedCommand UgBlAG0AbwB2AGUALQBJAHQAZQBtACAAZgA=", "cmd /c del f", "cmd /c \"rd /s /q d\"", "ri f",
    "del f", "erase f", "rmdir d", "unlink f", "shred f",
    "mv f /dev/null",
    # more shapes of the same rule
    "R''M f", "r\\m f", "$'\\x72m' f", "/usr/bin/env rm f", "sudo rm f", "env -i rm f", "nohup rm f &",
    "timeout 5 rm f", "xargs -n1 rm < list.txt", "bash -c 'rm f'", "sh -c \"rm -rf d\"", "zsh -lc 'rm f'",
    "echo a; rm f", "true && rm f", "false || rm f", "ls & rm f", "(rm f)", "{ rm f; }", "if true; then rm f; fi",
    "for x in a b; do rm $x; done", "find d -type f -exec rm -f {} +", "find . -execdir rm {} \\;",

    "rm -- -weird", "exec rm f", "busybox rm f",
    "echo x | tee out.txt; rm out.txt", "x=$(rm f)", "echo $(rm f)", "echo `rm f`", "cat <(rm f)",
    "python3 -c 'import pathlib; pathlib.Path(\"f\").unlink()'", "node -e \"fs.unlinkSync('f')\"",
    "python - <<'PY'\nimport shutil\nshutil.rmtree('d')\nPY", "bash <<'EOF'\nrm f\nEOF", "sh <<< 'rm f'",
    "perl -e 'unlink \"f\"'", "ruby -e 'File.delete(\"f\")'", "mv d /dev/null", "r\"\"m f", "\\/bin/rm f",
    "python -c \"from os import remove; remove('f')\"", "node -e \"require('fs').promises.rm('d')\"",
    "iex 'echo 1'", "wsl rm f", "wsl -- rm f", "stdbuf -o0 rm f", "ionice -c 3 rm f", "nice -n 5 rm f",
    "sudo -u root rm f", "env FOO=1 BAR=2 rm f", "command rm f", "builtin rm f",
]


@pytest.mark.parametrize("command", P4_DELETE)
def test_p4_delete_is_blocked_and_the_files_do_not_change(box, command):
    (box.work / "borra.py").write_text("import os\nos.unlink('f')\n")
    (box.work / "list.txt").write_text("f\n")
    before = box.tree_hash()
    assert box.bash(command) == "delete", command
    assert box.tree_hash() == before                                  # the analysis never runs the command


P4_POWERSHELL = [
    "Remove-Item f", "Remove-Item -Recurse -Force d", "ri f", "rm f", "del f", "erase f", "rd d", "rmdir d",
    "[IO.File]::Delete('f')", "[System.IO.Directory]::Delete('d', $true)", "Get-ChildItem | Remove-Item",
    "Get-ChildItem *.md | ForEach-Object { Remove-Item $_ }", "Invoke-Expression 'ls'", "iex 'ls'", "& Remove-Item f",
    "& 'C:\\Windows\\System32\\cmd.exe' /c del f", "cmd.exe /c rd /s /q d", "powershell.exe -Command Remove-Item f",
    "pwsh -c \"Remove-Item f\"", "powershell -enc AAAA", "pwsh -e AAAA", "Start-Process cmd -ArgumentList '/c del f'",
    "REMOVE-ITEM f", "Move-Item f NUL", "(Get-Item f).Delete()", "del /f /q f", "$x = 'f'; Remove-Item $x",
    "Remove-Item $env:TEMP\\x",
]


@pytest.mark.parametrize("command", P4_POWERSHELL)
def test_p4_delete_powershell(box, command):
    assert box.ps(command) == "delete", command


def test_p4_windows_binary_paths_in_a_shell_tool(box):
    assert box.bash("C:\\Windows\\System32\\cmd.exe /c del f") == "delete"
    assert box.bash("\"C:\\Windows\\System32\\cmd.exe\" /c \"rd /s /q d\"") == "delete"
    assert box.bash("C:/Windows/System32/cmd.exe /c del f") == "delete"


def test_p4_scripts_of_the_working_folder_are_read(box):
    (box.work / "clean.sh").write_text("#!/bin/sh\nset -e\nrm -rf build\n")
    (box.work / "clean.js").write_text("const fs = require('fs');\nfs.rmSync('build', {recursive: true});\n")
    (box.work / "clean.ps1").write_text("Remove-Item build -Recurse\n")
    (box.work / "clean.bat").write_text("@echo off\r\nrd /s /q build\r\n")
    (box.work / "tool.py").write_text("from pathlib import Path\nPath('x').unlink()\n")
    (box.work / "wrap.py").write_text("import subprocess\nsubprocess.run(['rm', '-rf', 'build'])\n")
    (box.work / "wrap2.py").write_text("import os\nos.system('rm -rf build')\n")
    (box.work / "safe.py").write_text("items = [1, 2]\nitems.remove(1)\nprint(items)\n")
    (box.work / "safe.sh").write_text("#!/bin/sh\necho hello\n")
    for command in ["bash clean.sh", "sh ./clean.sh", "./clean.sh", "source clean.sh", ". ./clean.sh", "node clean.js",
                    "python tool.py", "python3 wrap.py", "python wrap2.py", "cat clean.sh | bash", "bash < clean.sh"]:
        assert box.bash(command) == "delete", command
    assert box.ps("pwsh -File clean.ps1") == "delete"
    assert box.ps("./clean.ps1") == "delete"
    assert box.ps(".\\clean.bat") == "delete"
    assert box.bash("python safe.py") is None                                            # list.remove is not a delete
    assert box.bash("bash safe.sh") is None
    assert box.bash("python missing.py") is None


def test_p4_dependencies_are_not_scanned(box):
    (box.work / "node_modules" / ".bin").mkdir(parents=True)
    (box.work / "node_modules" / ".bin" / "tool").write_text("#!/usr/bin/env node\nrequire('fs').rmSync('x')\n")
    assert box.bash("./node_modules/.bin/tool") is None










def test_p5_what_is_not_emptying(box):
    assert box.bash("echo hi > f") is None                    # replaced with content
    assert box.bash("> brand-new.txt") is None                # a new empty file
    assert box.bash("echo -n > brand-new.txt") is None
    assert box.bash("cat notes.md > f") is None
    assert box.bash("echo x >> f") is None
    assert box.ps("Set-Content f 'x'") is None
    assert box.ps("Out-File f -Append") is None
    assert box.hit("Edit", file_path=str(box.work / "f"), old_string="content", new_string="stuff") is None
    (box.work / "blank.txt").write_text("")
    assert box.hit("Write", file_path=str(box.work / "blank.txt"), content="") is None   # already empty


P6_CASES = [
    # (tool, input, rule)
    ("Write", lambda b: {"file_path": str(b.data / "notes.txt"), "content": "x"}, None),
    ("Write", lambda b: {"file_path": str(b.data / "current"), "content": "b"}, None),
    ("Read", lambda b: {"file_path": str(b.data / "database.json")}, "secrets"),
    ("Read", lambda b: {"file_path": str(b.data / "claude-config" / ".credentials.json")}, "secrets"),
    ("Read", lambda b: {"file_path": str(b.data / "hokma.db")}, "secrets"),
    ("Grep", lambda b: {"pattern": "x", "path": str(b.data)}, "secrets"),
    ("Glob", lambda b: {"pattern": "*", "path": str(b.data / "claude-config")}, None),   # task 3163: listing is free
    ("Read", lambda b: {"file_path": "~/.ssh/id_ed25519"}, "secrets"),
    ("Read", lambda b: {"file_path": "/home/someone/.aws/credentials"}, "secrets"),
    ("Read", lambda b: {"file_path": "/home/someone/.config/gh/hosts.yml"}, "secrets"),
    ("Read", lambda b: {"file_path": "/home/someone/.git-credentials"}, "secrets"),
    ("Write", lambda b: {"file_path": "/tmp/outside-file.txt", "content": "x"}, "outside_workdir"),
    ("Write", lambda b: {"file_path": "../outside.txt", "content": "x"}, "outside_workdir"),
    ("Edit", lambda b: {"file_path": "/etc/hosts", "old_string": "a", "new_string": "b"}, "outside_workdir"),
    ("NotebookEdit", lambda b: {"notebook_path": "/tmp/n.ipynb", "new_source": "x"}, "outside_workdir"),
    ("mcp__broker__place_order", lambda b: {"symbol": "SPY"}, "real_money"),
    ("mcp__x__submit_order", lambda b: {}, "real_money"),
    ("mcp__x__cancel-order", lambda b: {}, "real_money"),
    ("mcp__x__order_create", lambda b: {}, "real_money"),
    ("mcp__x__modify_order", lambda b: {}, "real_money"),
]


@pytest.mark.parametrize("tool,make,rule", P6_CASES)
def test_p6_tools(box, tool, make, rule):
    assert box.hit(tool, **make(box)) == rule


P6_COMMANDS = [
    ("cat %LOCALAPPDATA%\\Hokma\\hokma.db", "secrets"),
    ("type %LOCALAPPDATA%\\Hokma\\claude-config\\settings.json", None),      # task 3163: reading is free
    ("cat ~/Library/Application Support/Hokma/hokma.db", "secrets"),
    ("cat \"~/Library/Application Support/Hokma/hokma.db\"", "secrets"),
    ("ls $HOME/.local/share/hokma/runtime", None),















    ("echo x > /tmp/outside.txt", "outside_workdir"), ("echo x >> /etc/hosts", "outside_workdir"),
    ("tee /tmp/x < f", "outside_workdir"), ("cp f /tmp/f2", "outside_workdir"), ("mv f /tmp/f2", "outside_workdir"),
    ("mkdir /tmp/newdir", "outside_workdir"), ("touch /tmp/x", "outside_workdir"), ("mkdir -p ~/stuff", "outside_workdir"),
    ("cp -t /tmp f", "outside_workdir"), ("cd /tmp && touch a", "outside_workdir"), ("cd /tmp; echo x > a", "outside_workdir"),
    ("cd / && mkdir a", "outside_workdir"), ("pushd /tmp && touch a", "outside_workdir"), ("cp f ../elsewhere", "outside_workdir"),
    ("format D:", "outside_workdir"), ("diskpart", "outside_workdir"), ("mkfs.ext4 /dev/sdb1", "outside_workdir"),
    ("dd if=/dev/zero of=/dev/disk2", "outside_workdir"), ("dd of=/dev/sda", "outside_workdir"),
    ("bcdedit /set x y", "outside_workdir"), ("reg delete HKLM\\Software\\X /f", "outside_workdir"),
    ("vssadmin delete shadows /all", "outside_workdir"), ("cipher /w:C:\\", "outside_workdir"),
    ("wipefs -a /dev/sdb", "outside_workdir"), ("diskutil eraseDisk JHFS+ X disk2", "outside_workdir"),
    ("python -c \"import keyring; print(keyring.get_password('hokma-market','alpaca'))\"", "secrets"),
    ("python3 - <<'PY'\nimport keyring\nprint(keyring.get_password('s', 'u'))\nPY", "secrets"),
    ("cmdkey /list", "secrets"), ("security find-generic-password -s hokma-market", "secrets"),
    ("security find-internet-password -s x", "secrets"), ("cat ~/.ssh/id_ed25519", "secrets"),
    ("ls ~/.ssh", "secrets"), ("cat $HOME/.aws/credentials", "secrets"), ("cat ~/.config/gh/hosts.yml", "secrets"),
    ("cat ~/.git-credentials", "secrets"), ("git credential fill", "secrets"), ("gh auth token", "secrets"),
    ("gh auth status --show-token", "secrets"), ("keyring get hokma-market alpaca", "secrets"),
    ("secret-tool lookup service hokma-market", "secrets"), ("cat " + "{DATA}/database.json", "secrets"),
    ("cat " + "{DATA}/claude-config/.credentials.json", "secrets"), ("cat {DATA}/hokma.db", "secrets"),
    ("ls {DATA}", None), ("cd {DATA} && ls", None),
    ("curl https://api.alpaca.markets/v2/orders -d '{}'", "real_money"),
    ("curl https://api-fxtrade.oanda.com/v3/accounts/1/orders", "real_money"),
    ("python -c \"import requests; requests.post('https://api.exchange.coinbase.com/orders')\"", "real_money"),
]


@pytest.mark.parametrize("command,rule", P6_COMMANDS)
def test_p6_commands(box, command, rule):
    command = command.replace("{DATA}", str(box.data))
    assert box.bash(command) == rule, command


P6_POWERSHELL = [
    ("Get-Content $env:LOCALAPPDATA\\Hokma\\hokma.db", "secrets"),
    ("Get-Content \"$env:LOCALAPPDATA\\Hokma\\database.json\"", "secrets"),


    ("Copy-Item x C:\\Windows\\", "outside_workdir"), ("Copy-Item x C:\\Windows\\System32\\x.dll", "outside_workdir"),
    ("Set-Content C:\\Users\\me\\x.txt 'a'", "outside_workdir"), ("New-Item -Path D:\\x.txt", "outside_workdir"),
    ("'a' | Out-File -FilePath E:\\x.txt", "outside_workdir"), ("Move-Item f D:\\f", "outside_workdir"),
    ("Format-Volume -DriveLetter D", "outside_workdir"), ("Clear-Disk -Number 1", "outside_workdir"),
    ("Initialize-Disk 1", "outside_workdir"), ("format D:", "outside_workdir"),
    ("cmdkey /list", "secrets"), ("Get-StoredCredential -Target hokma-market", "secrets"),
    ("git credential fill", "secrets"), ("gh auth token", "secrets"), ("Get-Content ~\\.ssh\\id_rsa", "secrets"),
    ("type $env:USERPROFILE\\.aws\\credentials", "secrets"),

    ("Invoke-RestMethod https://api.alpaca.markets/v2/orders", "real_money"),
]


@pytest.mark.parametrize("command,rule", P6_POWERSHELL)
def test_p6_powershell(box, command, rule):
    assert box.ps(command) == rule, command


def test_p6_the_rule_names_are_the_ones_the_database_accepts():
    from hokma.db.repos.governance import HARD_RULES
    from hokma.kit import hardblock
    five = {"delete", "hokma_internal", "outside_workdir", "secrets", "real_money"}      # the locks left by 0.0.22, by id
    assert set(hardblock.RULES) == five and set(HARD_RULES) == five and len(hardblock.RULES) == 5


def test_p13_the_key_cannot_be_read_from_the_agent(box):
    for command in ["python -c \"import keyring;print(keyring.get_password('hokma-market','alpaca'))\"",
                    "python3 -c 'from keyring import get_password as g; print(g(\"hokma-market\", \"x\"))'",
                    "cmdkey /list", "security find-generic-password -s hokma-market -w",
                    "keyring get hokma-market alpaca", "secret-tool lookup service hokma-market",
                    "cat {DATA}/database.json", "cat {DATA}/claude-config/.credentials.json",
                    "grep -r hokma-market ."]:
        assert box.bash(command.replace("{DATA}", str(box.data))) in ("secrets",), command
    (box.work / "peek.py").write_text("import keyring\nprint(keyring.get_password('hokma-market', 'alpaca'))\n")
    assert box.bash("python peek.py") == "secrets"
    assert box.hit("Read", file_path=str(box.data / "database.json")) == "secrets"
    assert box.hit("Grep", pattern="password", path=str(box.data)) == "secrets"
    assert box.ps("cmdkey /list") == "secrets"
    assert box.ps("[Windows.Security.Credentials.PasswordVault]::new().RetrieveAll()") in (None, "secrets")


def test_nothing_else_is_refused_hokma_is_the_owners(box):
    """Spot checks of the libertad: anything outside the five rules goes through."""
    for command in ["rm_not_a_command_name_here_but_arg echo", "chmod +x run.sh", "chmod -R 755 .", "kill -9 12345",
                    "sed -i 's/a/b/' notes.md", "pip uninstall -y requests", "docker ps", "ssh-keygen -t ed25519 -f key",
                    "curl -o data.csv https://example.com/data.csv", "tar -xzf a.tgz", "unzip a.zip -d out",
                    "gh pr create --fill", "gh repo view", "git clone https://example.com/r.git sub",
                    "git tag -d v1", "git push origin --tags",
                    "python script.py", "python -c 'open(\"x\", \"w\").write(\"1\")'", "make clean", "npm run clean",
                    "ls /etc", "cat /etc/hostname", "ps aux", "df -h", "whoami", "env", "printenv PATH"]:
        assert box.bash(command) is None, command


# ------------------------------------------------------------------------------------------ bug 1036 extra patterns

P_EXTRA_BLOCKED = [
    ("cat <<EOF | sh\nrm f\nEOF", "delete"), ("cat <<EOF | bash\nrm -rf d\nEOF", "delete"),
    ("bash <(echo rm f)", "delete"), ("sh <(printf 'rm f')", "delete"), ("source <(echo rm f)", "delete"),



    ("cat keyring_pass.cfg", "secrets"), ("ls ~/.local/share/python_keyring/", "secrets"),
    ("python -c \"__import__('keyring')\"", "secrets"),
    ("python -c \"import importlib; importlib.import_module('keyring')\"", "secrets"),
    ("python -c \"import importlib; importlib.import_module('key'+'ring')\"", "secrets"),
    ("python -c \"import importlib; importlib.import_module('keyr'+'ing')\"", "secrets"),
]
P_EXTRA_FREE = [
    "ls", "git commit -m 'update'", "pip install keyring", "pip install requests", "npm install", "git branch -d old",
    "git stash", "git stash pop", "git stash push -m wip", "git branch", "git branch -a", "cat <<EOF | sh\nls\nEOF",
    "cat <<EOF > out.txt\nhello\nEOF", "bash <(echo ls)", "source <(cat notes.md)", "dd if=a of=b", "tee out.log < notes.md",
    "echo hi | tee new-file.txt", "tee -a f </dev/null", "python -c \"import importlib; importlib.import_module('json')\"",
    "python -c \"__import__('os')\"", "python -c \"print('key' + 'board')\"",
]


@pytest.mark.parametrize("command,rule", P_EXTRA_BLOCKED)
def test_p6_extra_patterns_are_blocked_and_files_stay(box, command, rule):
    before = box.tree_hash()
    assert box.bash(command) == rule, command
    assert box.tree_hash() == before


@pytest.mark.parametrize("command", P_EXTRA_FREE)
def test_p3_normal_work_next_to_the_extra_patterns_stays_free(box, command):
    assert box.bash(command) is None, command


@pytest.mark.parametrize("tool", ["mcp__b__create_order", "mcp__b__create-order", "mcp__b__CreateOrder",
                                  "mcp__b__replace_order", "mcp__b__order_create"])
def test_p6_create_order_family_is_real_money(box, tool):
    assert box.hit(tool) == "real_money"


@pytest.mark.parametrize("tool", ["mcp__b__get_orders", "mcp__b__list_orders", "mcp__b__create_orderbook_view",
                                  "mcp__docs__search"])
def test_p3_reading_orders_is_free(box, tool):
    assert box.hit(tool) is None


# ------------------------------------------------------------------------------------------ bug 1035

P_TOKEN_SECRETS = [
    "cat ~/.config/google-chrome/Default/Local\\ Storage/leveldb/000003.log",
    "grep -r hokma.token ~/.config/chromium/Default",
    "strings '/home/u/.mozilla/firefox/abc.default/storage/default/http+++127.0.0.1+8765/ls/data.sqlite'",
    "cat '/Users/u/Library/Application Support/Google/Chrome/Default/Local Storage/leveldb/CURRENT'",
    "type \"C:/Users/u/AppData/Local/Google/Chrome/User Data/Default/Local Storage/leveldb/000003.log\"",
    "ls ~/.config/microsoft-edge/Default/Session\\ Storage",
]


@pytest.mark.parametrize("command", P_TOKEN_SECRETS)
def test_1035_browser_profile_storage_is_a_secret(box, command):
    before = box.tree_hash()
    assert box.bash(command) == "secrets", command
    assert box.tree_hash() == before




@pytest.mark.parametrize("command", ["curl http://127.0.0.1:8765/health", "ls ~/.config", "ls ~/.config/git",
                                     "git commit -m 'trash and permission mode docs'", "cat notes.md"])
def test_1035_normal_work_next_to_it_stays_free(box, command):
    assert box.bash(command) is None, command


P_TOKEN_SECRETS_MORE = [
    "cat ~/.config/BraveSoftware/Brave-Browser/Default/Cookies",
    "cat ~/.config/google-chrome/Local\\ State",
    "grep -a hokma /home/u/snap/chromium/common/chromium/Default/Local\\ Storage/leveldb/000003.log",
    "cp -r ~/.var/app/org.mozilla.firefox/.mozilla/firefox/abc.default /tmp/x",
    "ls ~/Library/Safari",
    "cat '/Users/u/Library/Application Support/Firefox/Profiles/abc.default/webappsstore.sqlite'",
    "cat '/Users/u/Library/Application Support/Microsoft Edge/Default/Session Storage/000003.log'",
    "type \"C:\\Users\\u\\AppData\\Local\\Microsoft\\Edge\\User Data\\Default\\IndexedDB\"",
    "type \"C:\\Users\\u\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles\\abc.default\\storage\\default\\x\\ls\\data.sqlite\"",
    "dir \"C:\\Users\\u\\AppData\\Local\\BraveSoftware\\Brave-Browser\\User Data\\Default\"",
    "cat /proc/1234/environ",
    "tr '\\0' '\\n' < /proc/$(pgrep hokma)/environ",
    "cat /proc/*/cmdline",
    "strings /proc/4321/mem",
    "python3 -c \"print(open('/proc/1/environ','rb').read())\"",
    "strings /srv/browser-copy/Default/Local\\ Storage/leveldb/000005.ldb",
]


@pytest.mark.parametrize("command", P_TOKEN_SECRETS_MORE)
def test_1035_every_browser_and_other_process_secret_is_refused(box, command):
    assert box.bash(command) == "secrets", command


@pytest.mark.parametrize("command", ["cat /proc/self/status", "cat /proc/cpuinfo",
                                     "ls /proc", "cat /proc/meminfo", "ps aux", "cat notes.md",
                                     "ls ~/Library/Preferences", "cat ~/.config/git/config", "ls ~/.mozilla-notes",
                                     "echo 'local storage and session storage are web APIs' > storage-notes.md"])
def test_1035_the_normal_work_around_those_paths_stays_free(box, command):
    assert box.bash(command) is None, command


def test_1035_the_owner_confirmation_and_session_calls_are_secrets_the_rest_is_free(box):
    for command in ["curl -X POST http://127.0.0.1:8765/api/session/owner-nonce -d '{}'",
                    "curl -H 'X-Hokma-Owner: 00' http://127.0.0.1:8765/api/trash"]:
        assert box.bash(command) == "secrets", command
    for command in ["curl -X POST http://127.0.0.1:8765/api/trash/4/restore",
                    "curl -X POST http://127.0.0.1:8765/api/checkpoints/abc/restore-file -d '{}'"]:
        assert box.bash(command) is None, command


def test_1035_the_hook_process_refuses_a_cat_of_the_browser_profile(box):
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat ~/.config/google-chrome/Default/Local\\ Storage/"
                                                              "leveldb/000003.log"}, "cwd": str(box.work)}
    done = box.run_hook(payload)
    assert done.returncode == 2 and done.stderr.splitlines()[0] == "HOKMA_BLOCKED secrets"
    for tool, key in (("Read", "file_path"), ("Grep", "path")):
        payload = {"tool_name": tool, "tool_input": {key: "/home/u/.mozilla/firefox/abc.default/storage",
                                                     "pattern": "hokma"}, "cwd": str(box.work)}
        assert box.run_hook(payload).returncode == 2, tool


# ------------------------------------------------------------------------------------------ bug 1036

P_HISTORY_FREE = [
    "git gc", "git gc --auto", "git gc --prune=never", "git reflog", "git reflog show HEAD", "git stash", "git stash pop",
    "git stash list", "git stash push -m wip", "git worktree list",
    "git branch -d merged", "git branch feature", "git tag v1.0", "git tag -d v0.9", "git tag -l",
    "git update-ref -m msg refs/heads/new HEAD", "git replace --list", "git config user.name Ana",
    "git config user.email a@example.com", "git config core.hooksPath", "git config --get core.hooksPath",
    "git config --list", "git config core.autocrlf false",
    "git -c user.name=Ana commit -m x", "git remote -v", "git remote add origin x",
    "git status", "git log --oneline", "git commit -m 'gc prune worktree remove'", "git add -A",
    # task 3045: reading, listing and unsetting stay free in every form; a value that only looks like a key is a value
    "git config get core.hooksPath", "git config list", "git config list --show-origin",
    "git config --get-all alias.x", "git config --get-regexp '^alias'", "git config --name-only --list",
    "git -cuser.name=Ana commit -m x", "git --config-env=user.name=NAME commit -m x", "git config set user.name Ana",
    # task 3045 last round: reads and the short list of keys stay free
    "git config user.name 'a b'", "git config --global init.defaultBranch main",
    "git config --local user.email a@b.c", "git config --global core.autocrlf input", "git config unset user.name",
    "git config --unset user.email", "git config --global --unset core.autocrlf", "git config --add user.name x",
    "git config --show-origin --list", "git config --list --show-scope", "git config --show-origin --get user.name",
    "git config --global --list", "git config --get-all user.email", "git config -l", "git config --local --get user.name",
    "git config get user.name", "git config list --show-origin --show-scope", "git config user.name", "git config",
    "git -c user.name=x commit -m x", "git -c user.name=x -c user.email=y commit -m x", "git -c core.autocrlf=false add -A",
    "git -c init.defaultbranch=main init", "git --config-env=user.email=EMAIL commit -m x",
    "git clone -c user.name=x repo", "git clone repo dir", "git clone -q repo", "git commit -c HEAD",
    "git switch -c feature", "git push origin main", "git branch -d merged", "git stash",
]




@pytest.mark.parametrize("command", P_HISTORY_FREE)
def test_1036_ordinary_git_work_stays_free(box, command):
    assert box.bash(command) is None, command










@pytest.mark.parametrize("command", [
    "mv notes.md notes2.md", "mv .gitignore .gitignore.bak", "mv .github .github-old", "mv .gitkeep d/", "mv a.git b.git",
    "mv src/.claudeignore x", "mv .claude/commands/a.md d/", "mv f g",
    "ln -s notes.md link.md", "ln -s ../.github workflows", "ln -sf .gitignore ignore-link", "ln notes.md hard.md",
    "cp -r .git backup-git", "cp notes.md n.md",
    "python3 -c \"import shutil; shutil.move('old.txt', 'new.txt')\"",
    "python3 -c \"import os; os.rename('a.txt', 'b.txt')\"",
    "python3 -c \"print('repo.git'.replace('.git', ''))\"",
    "python3 -c \"import os; os.symlink('notes.md', 'n')\"",
    "node -e \"require('fs').renameSync('a', 'b')\"",
    "echo '.git is the folder' > about.md", "cat .git/HEAD", "ls -la .claude",
])
def test_1036_normal_moves_and_links_stay_free(box, command):
    assert box.bash(command) is None, command


@pytest.mark.parametrize("command", ["New-Item -ItemType Directory -Path docs", "New-Item notes2.md -ItemType File",
                                     "Move-Item notes.md notes2.md", "Rename-Item notes.md n.md",
                                     "New-Item -ItemType SymbolicLink -Path l -Target notes.md"])
def test_1036_normal_powershell_moves_and_links_stay_free(box, command):
    assert box.ps(command) is None, command


# The evasions below are NOT closed in this task. Each one is "accepted B for task 3030": the owner's restore
# points (refs/hokma, P10) bring the files back, and the closing belongs to the sandbox of task 3030.
ACCEPTED_B_3030 = [
    "sed -i d f", "awk '' > f", "alias zz=rm", "ln -sf /dev/null f", "tar --remove-files -cf /tmp/a.tar f",
    "python3 -c \"open('f','w')\"", "rsync -a --delete empty/ d/",
]


@pytest.mark.parametrize("command", ACCEPTED_B_3030)
def test_1036_accepted_b_for_task_3030_is_documented_not_closed(box, command):
    assert box.bash(command) is None, command            # fails the day one of them gets closed: move it to P_*


# task 3045, last round: global options white list, program options, variables that steer Git
GIT_GLOBAL_FREE = [
    "git -C d status", "git -C . status", "git -C d -C .. status", "git --no-pager log", "git --no-optional-locks status",
    "git --no-pager -C d log --oneline", "git -c user.name=a status", "git -c init.defaultBranch=main init",
    "git --version", "git clone https://example.com/r.git sub",
    "git clone --depth 1 --recurse-submodules https://example.com/r.git",
    "git fetch origin", "git pull --rebase", "git push origin main", "git push -u origin main", "git ls-remote origin",
    "git init", "git archive HEAD -o a.tar", "git fetch --all --prune", "git clone -b main -q https://example.com/r.git",
]
GIT_ENV_FREE = ["echo $GIT_DIR", "echo \"$GIT_EXEC_PATH\"", "printenv GIT_DIR", "echo GIT_DIR=x", "git status",
                "[ -z \"$GIT_DIR\" ] && echo none", "git config --get core.hooksPath"]






@pytest.mark.parametrize("command", GIT_GLOBAL_FREE)
def test_3045_ordinary_git_options_stay_free(box, command):
    assert box.bash(command) is None, command






@pytest.mark.parametrize("command", GIT_ENV_FREE)
def test_3045_reading_those_variables_is_free(box, command):
    assert box.bash(command) is None, command




# ---- task 3064: the remembered session (keyring service, cookie stores, launcher, control channel)

SESSION_SECRETS_BASH = [
    "python -c \"import keyring; print(keyring.get_password('hokma-session','owner'))\"",
    "python3 -c 'from keyring import set_password as s; s(\"hokma-session\", \"x\", \"y\")'",
    "keyring get hokma-session owner", "keyring set hokma-session owner",
    "secret-tool lookup service hokma-session", "secret-tool store --label=x service hokma-session",
    "security find-generic-password -s hokma-session -w", "security add-generic-password -s hokma-session -a x -w y",
    "grep -r hokma-session .", "sudo keyring get hokma-session owner", "env X=1 keyring get hokma-session owner",
    "cat ~/.config/google-chrome/Default/Cookies", "cat ~/.config/google-chrome/Default/Network/Cookies",
    "cp ~/.config/chromium/Profile\\ 1/Network/Cookies .", "sqlite3 \"$HOME/.mozilla/firefox/x.default/cookies.sqlite\"",
    "cat /anywhere/profile/cookies.sqlite", "cat /anywhere/profile/cookies.sqlite-wal",
    "find / -name Cookies", "find / -name 'Cookies' -type f", "find ~ -iname cookies", "find . -name cookies.sqlite",
    "find . -name \"Coo*\"", "find . -name '*ookies'", "find . -name '[cC]ookies'", "ls Network/Cookies",
    "cat 'Network/Cookies'", "ls Default/Cookies-journal",
    "bash -c 'find / -name Cookies'", "sudo sh -c \"cat Network/Cookies\"", "xargs cat Network/Cookies",
    "cat /Users/x/Library/Application\\ Support/Google/Chrome/Default/Cookies",
]


@pytest.mark.parametrize("command", SESSION_SECRETS_BASH)
def test_3064_session_secrets_are_refused_bash(box, command):
    assert box.bash(command) == "secrets", command


SESSION_SECRETS_PS = [
    "Get-StoredCredential -Target hokma-session",
    "Get-ChildItem -Recurse -Filter Cookies",
    "Get-ChildItem -Path C:\\Users\\me -Recurse -Include Cookies",
    "gci -r -filter cookies",
    "Copy-Item \"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\\Network\\Cookies\" .",
    "Get-Content $env:APPDATA\\Mozilla\\Firefox\\Profiles\\x\\cookies.sqlite",
    "Get-ChildItem Default\\Network\\Cookies",
    "Get-ChildItem -Recurse -Filter Coo*",
    "[System.IO.File]::ReadAllBytes('Network\\Cookies')",
    "cmdkey /list",
]


@pytest.mark.parametrize("command", SESSION_SECRETS_PS)
def test_3064_session_secrets_are_refused_powershell(box, command):
    assert box.ps(command) == "secrets", command


def test_3064_session_secrets_by_the_file_tools(box):
    for path in ["~/.config/google-chrome/Default/Network/Cookies", "/x/y/Network/Cookies",
                 "/x/profile/cookies.sqlite"]:
        assert box.hit("Read", file_path=path) == "secrets", path
    assert box.hit("Glob", pattern="**/Cookies") == "secrets"
    assert box.hit("Glob", pattern="**/cookies.sqlite") == "secrets"


def test_3064_a_script_that_names_the_session_is_refused(box):
    (box.work / "peek.py").write_text("import keyring\nprint(keyring.get_password('hokma-session', 'owner'))\n")
    (box.work / "peek2.py").write_text("p = 'x/Network/Cookies'\nprint(open(p, 'rb').read())\n")
    assert box.bash("python peek.py") == "secrets"
    assert box.bash("python peek2.py") == "secrets"


INDEXEDDB_BLOCKED_BASH = [
    "ls ~/.config/google-chrome/Default/IndexedDB", "cat 'Default/IndexedDB/http_hokma.localhost_8765.indexeddb.leveldb/000003.log'",
    "strings ./http_x_1.indexeddb.blob/1/00", "find / -name '*.indexeddb.leveldb'", "ls x/y.indexeddb.blob",
    "find . -name IndexedDB", "find . -iname 'indexeddb'", "ls /tmp/profile_indexeddb", "cp -r a-indexeddb b",
    "ls -d index*", "ls *ndexeddb*", "find . -name '[iI]ndexed*'", "ls Indexed*",
    "ls ~/snap/firefox/common/.mozilla/firefox/x.default/storage/default/https+++hokma.localhost",
    "ls storage/default/https+++hokma.localhost+8765", "ls storage/default/hokma.localhost",
    "grep -r hokma.localhost ~", "bash -c 'cat x.indexeddb.leveldb/CURRENT'", "sudo find / -name IndexedDB",
    "xargs cat Default/IndexedDB/x/000003.log",
]


@pytest.mark.parametrize("command", INDEXEDDB_BLOCKED_BASH)
def test_hardblock_the_device_key_store_is_refused_bash(box, command):
    assert box.bash(command) == "secrets", command


INDEXEDDB_BLOCKED_PS = [
    "Get-ChildItem -Recurse -Filter IndexedDB", "Get-ChildItem -Recurse -Include *ndexeddb*",
    "Get-Content $env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\\IndexedDB\\x.indexeddb.leveldb\\000003.log",
    "gci -r -filter Indexed*", "Copy-Item .\\http_hokma.localhost_8765.indexeddb.blob .",
]


@pytest.mark.parametrize("command", INDEXEDDB_BLOCKED_PS)
def test_hardblock_the_device_key_store_is_refused_powershell(box, command):
    assert box.ps(command) == "secrets", command


def test_hardblock_the_device_key_store_by_the_file_tools(box):
    for path in ["~/.config/google-chrome/Default/IndexedDB/x.indexeddb.leveldb/000003.log",
                 "/x/y/http_hokma.localhost_8765.indexeddb.blob/1/00"]:
        assert box.hit("Read", file_path=path) == "secrets", path
    assert box.hit("Glob", pattern="**/*.indexeddb.leveldb/*") == "secrets"
    assert box.hit("Glob", pattern="**/*ndexeddb*") == "secrets"


def test_hardblock_the_device_key_store_does_not_block_ordinary_words(box):
    for command in ["ls index.html", "cat src/indexer.py", "git log --oneline", "echo indexing", "ls docs/index.md"]:
        assert box.bash(command) is None, command










SESSION_CALLS_BLOCKED = [
    "curl -X POST http://127.0.0.1:8765/api/session/resume", "curl localhost:1/api/session/logout",
    "curl -sS -X POST 'http://localhost:9/api/session/logout'", "wget --post-data= http://localhost/api/session/resume",
    "python -c \"import httpx; httpx.post('http://127.0.0.1:1/api/session/logout')\"",
    "node -e \"fetch('http://localhost:1/api/session/resume',{method:'POST'})\"",
    "http POST :8765/api/session/resume",
    "ls /run/user/1000/hokma/control.sock", "cat engine.lock", "ls {DATA}/engine.lock", "curl --unix-socket x/control.sock http://x/",
    "nc -U /tmp/z/control.sock", "socat - UNIX-CONNECT:control.sock", "ls //./pipe/hokma-control-me",
    "cat '\\\\.\\pipe\\hokma-control-me'",
]


@pytest.mark.parametrize("command", SESSION_CALLS_BLOCKED)
def test_3064_session_calls_and_control_channel_are_refused(box, command):
    assert box.bash(command.replace("{DATA}", str(box.data))) == "secrets", command


def test_3064_the_data_folder_and_control_channel_do_not_regress(box):
    for command in ["cat {DATA}/hokma.db", "ls {DATA}/control.sock", "cat {DATA}/engine.lock"]:
        assert box.bash(command.replace("{DATA}", str(box.data))) == "secrets", command
    assert box.bash("cat {DATA}/database.json".replace("{DATA}", str(box.data))) == "secrets"
    assert box.ps("Get-Content \"$env:LOCALAPPDATA\\Hokma\\engine.lock\"") == "secrets"
    assert box.hit("Read", file_path=str(box.data / "engine.lock")) == "secrets"
    for call in ["-H 'X-Hokma-Token: x'", "http://localhost:1/api/session/owner-nonce"]:
        assert box.bash(f"curl {call}") == "secrets", call




LAUNCHER_FREE = [
    "hokma --version", "hokma -V", "hokma --help", "hokma doctor", "hokma status", "hokma version", "hokma",
    "hokma doctor --verbose", "hokma.exe --version", "/usr/local/bin/hokma --version", "uv run hokma doctor",
    "uv run hokma --version", "python -m hokma_cli.main --version", "python -m hokma_cli.main doctor",
    "uv run pytest -q", "uv run python -m pytest backend/tests", "python -m pytest", "python main.py start",
    "python app.py stop", "node server.js start", "npm start", "npm run start", "npm stop", "npm run open",
    "yarn start", "systemctl start nginx", "docker start web", "docker stop web", "docker restart web",
    "open index.html", "xdg-open report.pdf", "cmd /c start report.html",
    "git commit -m start", "git commit -m \"start\"", "git commit -m \"hokma start\"", "git commit -m 'stop hokma'",
    "git log --grep=restart", "git status", "echo hokma start", "echo \"hokma stop\"", "printf 'hokma open\\n'",
    "grep start notes.md", "grep -n hokma notes.md", "grep -rn \"hokma start\" notes.md", "cat notes.md",
    "ls -la", "cat cookies.md", "ls cookies.py", "echo cookies", "ls session", "cat my-session.txt",
    "curl https://example.com/api/session", "curl https://example.com/api/sessions/1",
    "sleep 1", "cat start", "ls stop", "touch open", "mkdir restart",
]


@pytest.mark.parametrize("command", LAUNCHER_FREE)
def test_3064_ordinary_commands_stay_free(box, command):
    assert box.bash(command) is None, command


LAUNCHER_FREE_PS = [
    "hokma --version", "hokma doctor", "Start-Process notepad", "Start-Process notepad -ArgumentList notes.md",
    "Start-Process chrome -ArgumentList https://example.com", "Get-ChildItem -Recurse -Filter *.md",
    "Get-Content notes.md", "Write-Output 'hokma stop'", "git commit -m 'start'", "Invoke-Item report.html",
    "Get-Service nginx | Restart-Service",
]


@pytest.mark.parametrize("command", LAUNCHER_FREE_PS)
def test_3064_ordinary_powershell_stays_free(box, command):
    assert box.ps(command) is None, command


def test_3064_the_messages_exist_in_both_catalogs():
    import json
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1] / "hokma" / "catalog"
    en, es = (json.loads((folder / f"{x}.json").read_text(encoding="utf-8")) for x in ("en", "es"))
    for key in ("hardblock.hokma_internal", "hardblock.secrets", "hardblock.hint.hokma_internal", "hardblock.hint.secrets"):
        assert key in en and key in es
