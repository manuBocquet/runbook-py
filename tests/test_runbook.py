import json
import os

from conftest import by_id

from runbook_py.plan import PlanError, normalize_plan
from runbook_py.schema import PLAN_SCHEMA
from runbook_py import util
from runbook_py.util import OutputBuffer, sanitize


# ---------------------------------------------------------------- basics

def test_basic_run_and_statuses(rb):
    proc, res = rb.run({"name": "basic", "commands": [
        {"id": "ok", "cmd": "echo hello; echo oops >&2"},
        {"id": "bad", "cmd": "exit 3", "stop_on_error": False},
        {"id": "rc2", "cmd": ["sh", "-c", "exit 2"], "shell": False, "expect_rc": [0, 2]},
        {"id": "nobin", "cmd": ["/does/not/exist"], "shell": False, "stop_on_error": False},
        "echo shorthand",
    ]})
    assert proc.returncode == 1
    r = by_id(res)
    assert r["ok"]["status"] == "ok" and r["ok"]["stdout"] == "hello\n" and r["ok"]["stderr"] == "oops\n"
    assert r["bad"]["status"] == "failed" and r["bad"]["rc"] == 3
    assert r["rc2"]["status"] == "ok"
    assert r["nobin"]["status"] == "error" and "FileNotFoundError" in r["nobin"]["error"]
    assert r["cmd5"]["status"] == "ok"
    assert res["summary"] == {"ok": 3, "failed": 1, "error": 1, "total": 5}
    assert res["schema_version"] == 1 and res["plan_name"] == "basic"


def test_stop_on_error_skips_the_rest(rb):
    proc, res = rb.run({"commands": [{"id": "a", "cmd": "false"}, {"id": "b", "cmd": "echo never"}]})
    r = by_id(res)
    assert r["b"]["status"] == "skipped" and "stopped after a" in r["b"]["reason"]
    assert res["stopped_at"] == "a"


def test_timeout_kills_process_group(rb):
    proc, res = rb.run({"commands": [{"id": "slow", "cmd": "sleep 30 & sleep 30", "timeout": 1}]})
    e = by_id(res)["slow"]
    assert e["status"] == "timeout" and e["duration_s"] < 10


def test_stdin_and_env(rb):
    proc, res = rb.run({"defaults": {"env": {"A": "1"}}, "commands": [
        {"id": "in", "cmd": "cat", "stdin": "one\ntwo\n"},
        {"id": "env", "cmd": "echo $A$B $RUNBOOK_PLAN", "env": {"B": "2"}},
        {"id": "devnull", "cmd": "cat"},
    ]})
    r = by_id(res)
    assert r["in"]["stdout"] == "one\ntwo\n"
    assert r["env"]["stdout"].startswith("12 ")
    assert r["devnull"]["stdout"] == ""


def test_retries(rb, tmp_path):
    counter = tmp_path / "count"
    proc, res = rb.run({"commands": [{"id": "flaky", "retries": 3, "retry_delay": 0,
                                      "cmd": "n=$(cat %s 2>/dev/null || echo 0); n=$((n+1)); echo $n > %s; [ $n -ge 3 ]" % (counter, counter)}]})
    e = by_id(res)["flaky"]
    assert e["status"] == "ok" and e["attempts"] == 3


# ---------------------------------------------------------------- variables, capture, conditions

def test_variables_precedence(rb, tmp_path):
    (tmp_path / ".runbook.ini").write_text("[vars]\nb = from-config\nc = from-config\n")
    proc, res = rb.run({"vars": {"a": "from-plan", "b": "from-plan", "c": "from-plan"},
                        "commands": [{"id": "v", "cmd": "echo {{a}} {{b}} {{c}} {{ host }}"}]}, "--var", "c=from-cli")
    out = by_id(res)["v"]["stdout"].split()
    assert out[:3] == ["from-plan", "from-config", "from-cli"]
    assert by_id(res)["v"]["cmd_template"].startswith("echo {{a}}")


def test_undefined_variable_is_rejected_before_running(rb):
    proc = rb(rb.plan({"commands": [{"cmd": "echo {{missing}}"}]}), "-y", "--no-lock")
    assert proc.returncode == 2 and "{{missing}}" in proc.stderr


def test_capture_used_later_but_not_before(rb):
    proc = rb("validate", rb.plan({"commands": [{"id": "use", "cmd": "echo {{X}}"}, {"id": "cap", "cmd": "echo v", "capture": "X"}]}))
    assert proc.returncode == 2 and "captured by a later command" in proc.stderr

    proc, res = rb.run({"commands": [
        {"id": "cap", "cmd": "printf 'a b\\n\\n'", "capture": "X"},
        {"id": "tpl", "cmd": "echo [{{X}}]"},
        {"id": "env", "cmd": "echo \"[$X]\""},
    ]})
    r = by_id(res)
    assert r["tpl"]["stdout"] == "[a b]\n" and r["env"]["stdout"] == "[a b]\n"
    assert res["captures"] == {"X": "a b"}


def test_capture_keeps_full_value_even_when_report_is_truncated(rb):
    proc, res = rb.run({"commands": [
        {"id": "cap", "cmd": "head -c 5000 /dev/zero | tr '\\0' x", "capture": "BIG"},
        {"id": "len", "cmd": "printf %s \"$BIG\" | wc -c"},
    ]}, "--max-output", "100")
    r = by_id(res)
    assert r["len"]["stdout"].strip() == "5000"
    assert r["cap"]["truncated"] is True and len(r["cap"]["stdout"]) < 300


def test_only_if_and_skip_if(rb):
    proc, res = rb.run({"defaults": {"stop_on_error": False}, "commands": [
        {"id": "good", "cmd": "true"},
        {"id": "bad", "cmd": "false"},
        {"id": "after_good", "cmd": "echo yes", "only_if": "good"},
        {"id": "after_both", "cmd": "echo no", "only_if": ["good", "bad"]},
        {"id": "idem", "cmd": "echo no", "skip_if": "test 1 -eq 1"},
        {"id": "notidem", "cmd": "echo yes", "skip_if": "test 1 -eq 2"},
    ]})
    r = by_id(res)
    assert r["after_good"]["status"] == "ok"
    assert r["after_both"]["status"] == "skipped" and "only_if bad=failed" in r["after_both"]["reason"]
    assert r["idem"]["status"] == "skipped" and r["idem"]["skip_if"]["rc"] == 0
    assert r["notidem"]["status"] == "ok" and r["notidem"]["skip_if"]["rc"] == 1


# ---------------------------------------------------------------- selection and resume

PLAN5 = {"defaults": {"stop_on_error": False}, "commands": [
    {"id": "a", "cmd": "echo a"}, {"id": "b", "cmd": "echo b"}, {"id": "c", "cmd": "echo c"},
    {"id": "d", "cmd": "echo d"}, {"id": "e", "cmd": "echo e"}]}


def test_from_only_skip(rb):
    _, res = rb.run(PLAN5, "--from", "c")
    assert [e["status"] for e in res["results"]] == ["skipped", "skipped", "ok", "ok", "ok"]
    _, res = rb.run(PLAN5, "--only", "b,d")
    assert [e["status"] for e in res["results"]] == ["skipped", "ok", "skipped", "ok", "skipped"]
    _, res = rb.run(PLAN5, "--skip", "a,e")
    assert [e["status"] for e in res["results"]] == ["skipped", "ok", "ok", "ok", "skipped"]
    proc, _ = rb.run(PLAN5, "--only", "zz")
    assert proc.returncode == 2 and "unknown command id" in proc.stderr


def test_resume_reruns_only_what_did_not_succeed(rb, tmp_path):
    flag = tmp_path / "flag"
    plan = {"commands": [
        {"id": "cap", "cmd": "echo captured", "capture": "V"},
        {"id": "gate", "cmd": "test -f %s" % flag},
        {"id": "use", "cmd": "echo {{V}}", "only_if": "cap"},
    ]}
    _, first = rb.run(plan)
    assert [e["status"] for e in first["results"]] == ["ok", "failed", "skipped"]
    previous = tmp_path / "first.json"
    previous.write_text(json.dumps(first))
    flag.write_text("")
    _, second = rb.run(plan, "--resume", previous)
    r = by_id(second)
    assert r["cap"]["status"] == "skipped" and r["cap"]["reason"] == "ok in the resumed run"
    assert r["gate"]["status"] == "ok"
    assert r["use"]["status"] == "ok" and r["use"]["stdout"] == "captured\n"


# ---------------------------------------------------------------- output handling

def test_redaction_everywhere(rb):
    proc, res = rb.run({"vars": {"pw": "s3cret"}, "redact": ["s3cret"], "commands": [
        {"id": "x", "cmd": "echo pass={{pw}}; echo s3cret >&2"}]})
    e = by_id(res)["x"]
    assert "s3cret" not in json.dumps(res)
    assert e["stdout"] == "pass=[REDACTED]\n" and e["stderr"] == "[REDACTED]\n"
    assert res["vars"]["pw"] == "[REDACTED]"


def test_ansi_and_carriage_returns_are_cleaned(rb):
    proc, res = rb.run({"commands": [{"id": "c", "cmd": "printf '\\033[32mPASS\\033[0m\\n10%%\\r50%%\\r100%%\\n'"}]})
    assert by_id(res)["c"]["stdout"] == "PASS\n100%\n"


def test_output_buffer_head_and_tail():
    buf = OutputBuffer(10)
    for chunk in (b"0123", b"456789", b"abcdef"):
        buf.add(chunk)
    assert buf.truncated and buf.total == 16
    text = buf.text()
    assert text.startswith("01234") and text.endswith("bcdef") and "6 bytes truncated" in text
    assert sanitize("a\x1b[1;31mb\x1b[0m\r\nc") == "ab\nc"


# ---------------------------------------------------------------- config, requirements, lock

def test_config_file_and_errors(rb, tmp_path):
    (tmp_path / ".runbook.ini").write_text("[runbook]\nmax_output = 20\ntimeout = 1\nredact =\n    hush\n")
    _, res = rb.run({"commands": [{"id": "big", "cmd": "seq 1 200; echo hush"}, {"id": "t", "cmd": "sleep 5"}]})
    r = by_id(res)
    assert r["big"]["truncated"] and "[REDACTED]" in r["big"]["stdout"]
    assert r["t"]["status"] == "timeout"
    assert res["config_files"][0].endswith(".runbook.ini")

    (tmp_path / ".runbook.ini").write_text("[runbook]\nconfrim = all\n")
    proc, _ = rb.run({"commands": ["true"]})
    assert proc.returncode == 2 and "unknown key 'confrim'" in proc.stderr


def test_requirements_abort_without_running(rb, tmp_path):
    marker = tmp_path / "ran"
    proc, res = rb.run({"requires": {"commands": ["definitely-not-a-binary"]},
                        "commands": [{"cmd": "touch %s" % marker}]})
    assert proc.returncode == 3 and not marker.exists()
    assert "requirements not met" in res["aborted"]


def test_lock_prevents_parallel_runs(rb, tmp_path):
    import fcntl
    lock = tmp_path / "rb.lock"
    (tmp_path / ".runbook.ini").write_text("[runbook]\nlock_file = %s\n" % lock)
    with open(str(lock), "w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        proc = rb(rb.plan({"commands": ["true"]}), "-y")
    assert proc.returncode == 4 and "another runbook is running" in proc.stderr


def test_dangerous_without_terminal_is_declined(rb):
    proc = rb(rb.plan({"commands": [{"id": "d", "cmd": "echo hi", "dangerous": True}]}),
              "--no-lock", "--no-stream", "-o", "out.json")
    res = json.loads((rb.tmp / "out.json").read_text())
    assert by_id(res)["d"]["status"] == "declined"


# ---------------------------------------------------------------- plan validation, schema, CLI

def test_plan_validation_messages():
    bad = [
        ({"commands": []}, "non-empty"),
        ({"commands": [{"cmd": "x", "colour": 1}]}, "unknown key"),
        ({"commands": [{"id": "a", "cmd": "x"}, {"id": "a", "cmd": "y"}]}, "duplicate id"),
        ({"commands": [{"cmd": "x", "only_if": "later"}, {"id": "later", "cmd": "y"}]}, "earlier command"),
        ({"commands": [{"cmd": "x", "timeout": "10"}]}, "timeout"),
        ({"commands": [{"cmd": "x", "dangerous": "yes"}]}, "dangerous"),
        ({"commands": [{"cmd": "x", "shell": False}]}, "argv"),
        ({"commands": [{"cmd": "x", "capture": "PATH"}]}, "reserved"),
        ({"defaults": {"capture": "X"}, "commands": ["x"]}, "defaults"),
        ({"redact": ["("], "commands": ["x"]}, "regex"),
    ]
    for data, needle in bad:
        try:
            normalize_plan(data)
        except PlanError as exc:
            assert needle in str(exc), (data, str(exc))
        else:
            raise AssertionError("accepted: %r" % data)


def test_schema_accepts_and_rejects_like_the_validator():
    jsonschema = __import__("jsonschema")
    jsonschema.Draft7Validator.check_schema(PLAN_SCHEMA)
    validator = jsonschema.Draft7Validator(PLAN_SCHEMA)
    examples = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
    for name in sorted(os.listdir(examples)):
        if name.endswith(".json"):
            with open(os.path.join(examples, name)) as handle:
                data = json.load(handle)
            errors = list(validator.iter_errors(data))
            assert not errors, (name, [e.message for e in errors])
            normalize_plan(data)
    assert list(validator.iter_errors({"commands": [{"cmd": "x", "colour": 1}]}))


def test_cli_subcommands(rb, tmp_path):
    assert json.loads(rb("schema").stdout)["title"] == "runbook-py plan"
    assert "[runbook]" in rb("init-config").stdout
    assert rb("init-config", tmp_path / "x.ini").returncode == 0
    assert rb("init-config", tmp_path / "x.ini").returncode == 2
    plan = rb.plan({"name": "v", "commands": [{"id": "a", "cmd": "echo {{host}}", "dangerous": True}]})
    proc = rb("validate", plan)
    assert proc.returncode == 0 and "1 dangerous" in proc.stderr
    proc = rb(plan, "--dry-run")
    assert proc.returncode == 0 and "DANGEROUS" in proc.stderr
    proc, res = rb.run({"commands": [{"id": "bad", "cmd": "echo out; echo err >&2; exit 1"}]})
    out = tmp_path / "result.json"
    text = rb("report", out).stdout
    assert "bad" in text and "err" in text
    md = rb("report", out, "--format", "markdown").stdout
    assert md.startswith("# Runbook report") and "| `bad` | failed | 1 |" in md
    assert rb("--version").stdout.startswith("runbook-py ")


def test_published_schema_file_is_up_to_date():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "schema", "runbook-plan.schema.json")) as handle:
        assert json.load(handle) == json.loads(json.dumps(PLAN_SCHEMA)), "run scripts/gen_schema.py"


def test_fqdn_falls_back_to_hostname_when_reverse_dns_hangs(monkeypatch):
    # GitHub macOS runners: the reverse lookup behind getfqdn() can hang for minutes.
    import threading
    release = threading.Event()
    monkeypatch.setattr(util.socket, "getfqdn", lambda *a: release.wait(30) and "late.example")
    monkeypatch.setattr(util.socket, "gethostname", lambda: "short")
    monkeypatch.setattr(util, "_fqdn_cache", [])
    try:
        assert util.fqdn(timeout=0.2) == "short"
        assert util.fqdn() == "short"  # computed once per process
    finally:
        release.set()
