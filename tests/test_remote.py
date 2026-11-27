"""Remote execution over SSH, tested with a fake ssh client that runs the remote command locally.

The fake ssh hands the remote command line to `sh -c`, as sshd does with the user's login shell,
and logs its arguments so the generated ssh command line can be checked.
"""

import json
import os
import stat
import subprocess

from conftest import by_id

from runbook_py.plan import PlanError, normalize_plan
from runbook_py.remote import Remote

FAKE_SSH = """#!/bin/sh
log="${FAKE_SSH_LOG:-/dev/null}"
for a in "$@"; do printf '%s\\n' "$a" >> "$log"; done
echo "--" >> "$log"
for a in "$@"; do [ "$a" = "-O" ] && exit 0; done
[ -n "$FAKE_SSH_FAIL" ] && { echo "ssh: connect to host example port 22: Connection refused" >&2; exit 255; }
for a in "$@"; do last="$a"; done
exec /bin/sh -c "$last"
"""


def _setup(tmp_path, rb):
    fake = tmp_path / "fake-ssh"
    fake.write_text(FAKE_SSH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / ".runbook.ini").write_text("[runbook]\nssh = %s\n" % fake)
    log = tmp_path / "ssh.log"
    rb.env["FAKE_SSH_LOG"] = str(log)
    return log


def _calls(log):
    calls, current = [], []
    for line in log.read_text().splitlines():
        if line == "--":
            calls.append(current)
            current = []
        else:
            current.append(line)
    return calls


REMOTE = {"host": "fw.example", "user": "admin", "port": 2222, "identity_file": "~/.ssh/id_runbook",
          "ssh_options": ["StrictHostKeyChecking=yes"]}


def test_remote_commands_behave_like_local_ones(rb, tmp_path):
    log = _setup(tmp_path, rb)
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, res = rb.run({"name": "remote", "remote": REMOTE, "requires": {"commands": ["sh"]}, "commands": [
        {"id": "redir", "cmd": "ls /nonexistent 2>/dev/null; echo \"rc=$?\""},
        {"id": "quotes", "cmd": "printf '%s|' \"a b\" 'c\"d' \"e'f\" 'bang!'; echo"},
        {"id": "multi", "cmd": "for i in 1 2; do\n  echo line$i\ndone"},
        {"id": "cap", "cmd": "echo captured-value", "capture": "VAL"},
        {"id": "use", "cmd": "echo {{VAL}} $VAL {{host}} $RUNBOOK_HOST"},
        {"id": "envcwd", "cmd": "pwd; echo \"$FOO\"", "cwd": str(workdir), "env": {"FOO": "a b $HOME"}},
        {"id": "stdin", "cmd": "od -c", "stdin": "x\ny"},
        {"id": "stdin_nl", "cmd": "wc -c", "stdin": "ab\n\n"},
        {"id": "nostdin", "cmd": "cat; echo end"},
        {"id": "argv", "cmd": ["printf", "%s\\n", "it's $x"], "shell": False},
        {"id": "rc", "cmd": "exit 3", "expect_rc": 3},
        {"id": "skip", "cmd": "echo no", "skip_if": "test -n \"$VAL\""},
    ]})
    assert proc.returncode == 0, proc.stderr
    r = by_id(res)
    assert r["redir"]["stdout"].startswith("rc=")
    assert r["quotes"]["stdout"] == "a b|c\"d|e'f|bang!|\n"
    assert r["multi"]["stdout"] == "line1\nline2\n"
    assert r["use"]["stdout"] == "captured-value captured-value fw.example fw.example\n"
    assert r["envcwd"]["stdout"] == "%s\na b $HOME\n" % workdir
    assert "x  \\n   y" in r["stdin"]["stdout"]
    assert r["stdin_nl"]["stdout"].strip() == "4"
    assert r["nostdin"]["stdout"] == "end\n"
    assert r["argv"]["stdout"] == "it's $x\n"
    assert r["rc"]["status"] == "ok" and r["skip"]["status"] == "skipped"
    assert res["remote"]["target"] == "admin@fw.example" and res["remote"]["port"] == "2222"
    assert res["runner"]["host"] and res["host"]

    calls = _calls(log)
    first = calls[0]
    assert first[0] == "-T" and first[-2:] == ["admin@fw.example", "exec /bin/sh -s"]
    assert first.index("StrictHostKeyChecking=yes") < first.index("BatchMode=yes")
    assert "-p" in first and first[first.index("-p") + 1] == "2222"
    assert first[first.index("-i") + 1] == os.path.join(rb.env["HOME"], ".ssh", "id_runbook")
    assert "IdentitiesOnly=yes" in first and "ControlMaster=auto" in first
    assert any("-O" in call for call in calls), "the shared connection is closed at the end"


def test_remote_ssh_errors_and_timeouts(rb, tmp_path):
    _setup(tmp_path, rb)
    proc, res = rb.run({"remote": {"host": "h"}, "commands": [
        {"id": "e255", "cmd": "echo boom >&2; exit 255", "stop_on_error": False},
        {"id": "ok255", "cmd": "exit 255", "expect_rc": [255]},
        {"id": "slow", "cmd": "sleep 30", "timeout": 1, "stop_on_error": False},
    ]})
    r = by_id(res)
    assert r["e255"]["status"] == "error" and "ssh failed (rc 255): boom" in r["e255"]["error"]
    assert r["ok255"]["status"] == "ok"
    assert r["slow"]["status"] == "timeout"
    assert proc.returncode == 1


def test_remote_requirements_and_unreachable_host(rb, tmp_path):
    _setup(tmp_path, rb)
    proc, res = rb.run({"remote": {"host": "h"}, "requires": {"commands": ["sh", "no-such-binary-xyz"]},
                        "commands": [{"id": "a", "cmd": "echo never"}]})
    assert proc.returncode == 3 and "no-such-binary-xyz" in res["aborted"]
    assert by_id(res)["a"]["status"] == "skipped"

    rb.env["FAKE_SSH_FAIL"] = "1"
    proc, res = rb.run({"remote": {"host": "h"}, "commands": [{"id": "a", "cmd": "echo never"}]})
    assert proc.returncode == 3 and "Connection refused" in res["aborted"]


def test_remote_command_line_overrides(rb, tmp_path):
    log = _setup(tmp_path, rb)
    plan = {"remote": {"host": "{{fw}}", "user": "admin"}, "vars": {"fw": "fw1"},
            "commands": [{"id": "a", "cmd": "echo {{host}}"}]}
    proc = rb("validate", rb.plan(plan))
    assert proc.returncode == 0 and "via ssh admin@fw1" in proc.stderr
    proc = rb("validate", rb.plan(plan), "--local")
    assert proc.returncode == 0 and "via ssh" not in proc.stderr

    proc, res = rb.run(plan, "--remote", "root@fw2", "--port", "2200", "-i", "/k", "--ssh-option", "ControlMaster=no")
    assert by_id(res)["a"]["stdout"] == "fw2\n"
    first = _calls(log)[0]
    assert "root@fw2" in first and "2200" in first and "/k" in first
    assert first.index("ControlMaster=no") < first.index("ControlMaster=auto")

    proc, res = rb.run(plan, "--local")
    assert "remote" not in res and by_id(res)["a"]["status"] == "ok"

    proc = rb(rb.plan({"commands": ["true"]}), "--port", "22", "--no-lock", "-y")
    assert proc.returncode == 2 and "--remote" in proc.stderr


def test_remote_plan_validation():
    bad = [
        ({"remote": {"user": "x"}, "commands": ["x"]}, "remote.host"),
        ({"remote": {"host": "h", "colour": 1}, "commands": ["x"]}, "unknown key(s) in remote"),
        ({"remote": {"host": "h", "ssh_options": [1]}, "commands": ["x"]}, "ssh_options"),
        ({"remote": {"host": "h", "port": True}, "commands": ["x"]}, "remote.port"),
        ({"remote": "h", "commands": ["x"]}, "must be an object"),
    ]
    for data, needle in bad:
        try:
            normalize_plan(data)
        except PlanError as exc:
            assert needle in str(exc), (data, str(exc))
        else:
            raise AssertionError("accepted: %r" % data)
    assert normalize_plan({"remote": {"host": "h", "port": 22}, "commands": ["x"]})["remote"]["port"] == "22"


def test_remote_script_keeps_stdin_byte_for_byte():
    remote = Remote({"host": "h"})
    for data in ["", "a", "a\n", "a\n\n", "line 'q' \"d\" $HOME `x` \\\n", "RUNBOOK_STDIN_\nx",
                 "a \\\nb\n", "ends with \\", "( ) $( ) ) %s %b -n '' \\n\r\n", "é ü ✓\n\n\n"]:
        script = remote.script("cat", stdin_data=data)
        out = subprocess.run(["/bin/sh", "-s"], input=script.encode(), stdout=subprocess.PIPE).stdout.decode()
        assert out == data, (data, out)
    script = remote.script("exit 7", env={"A": "1"}, cwd="/", timeout=5)
    assert subprocess.run(["/bin/sh", "-s"], input=script.encode()).returncode == 7
    try:
        remote.script("true", env={"BAD-NAME": "1"})
    except ValueError as exc:
        assert "BAD-NAME" in str(exc)
    else:
        raise AssertionError("invalid variable name accepted")


def test_remote_example_plan_is_valid():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "examples", "remote-pfsense-check.json")) as handle:
        plan = normalize_plan(json.load(handle))
    assert plan["remote"]["shell"] == "/bin/sh"
