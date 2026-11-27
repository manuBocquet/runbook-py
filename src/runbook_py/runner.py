"""Executes a normalized plan and writes the JSON report."""

import errno
import os
import platform
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time

from . import SCHEMA_VERSION, __version__
from .plan import UndefinedVariable, render
from .remote import SSH_ERROR_RC, Remote, ssh_error_message
from .util import OutputBuffer, fqdn, now_iso, redact, sanitize, slug, write_json_atomic

OK, FAILED, TIMEOUT, ERROR = "ok", "failed", "timeout", "error"
SKIPPED, DECLINED = "skipped", "declined"
BAD = (FAILED, TIMEOUT, ERROR)

EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_REQUIREMENTS, EXIT_LOCKED, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130


class Interrupted(Exception):
    pass


def os_release():
    info = {}
    try:
        with open("/etc/os-release") as handle:
            for line in handle:
                if "=" in line:
                    key, value = line.rstrip("\n").split("=", 1)
                    info[key] = value.strip().strip('"')
    except OSError:
        pass
    return info


def find_shell(preferred=""):
    for candidate in ([preferred] if preferred else []) + ["/bin/bash", "/usr/bin/bash", "/bin/sh"]:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def check_remote_requirements(requires, facts):
    problems = []
    if requires.get("root") and facts.get("uid") != "0":
        problems.append("must run as root on the remote host (uid %s)" % facts.get("uid", "?"))
    wanted = [x.lower() for x in requires.get("os", [])]
    if wanted:
        ids = set(facts.get("os_ids", "").lower().split())
        if not ids & set(wanted):
            problems.append("remote OS %r is not one of: %s" % (facts.get("os", "?"), ", ".join(wanted)))
    for binary in facts.get("missing", []):
        problems.append("command not found on the remote host: %s" % binary)
    return problems


def check_requirements(requires):
    problems = []
    if requires.get("root") and os.geteuid() != 0:
        problems.append("must run as root (current uid %d)" % os.geteuid())
    wanted = [x.lower() for x in requires.get("os", [])]
    if wanted:
        info = os_release()
        ids = {info.get("ID", "").lower()} | set(info.get("ID_LIKE", "").lower().split())
        if not ids & set(wanted):
            problems.append("OS %r is not one of: %s" % (info.get("PRETTY_NAME", platform.platform()), ", ".join(wanted)))
    for binary in requires.get("commands", []):
        if not shutil.which(binary):
            problems.append("command not found in PATH: %s" % binary)
    return problems


class Lock(object):
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        if not self.path:
            return True
        import fcntl
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                self.handle.close()
                self.handle = None
                return False
            raise
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write("%d\n" % os.getpid())
        self.handle.flush()
        return True

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None


class LivePrinter(object):
    """Prints a stream line by line while it is captured."""

    def __init__(self, console, prefix, code=None, patterns=()):
        self.console = console
        self.patterns = patterns
        self.prefix = prefix
        self.code = code
        self.pending = b""

    def feed(self, chunk):
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self._emit(line)

    def flush(self):
        if self.pending:
            self._emit(self.pending)
            self.pending = b""

    def _emit(self, line):
        text = redact(sanitize(line.decode("utf-8", "replace")), self.patterns)
        self.console.write(self.console.paint(self.prefix, "90") + self.console.paint(text, self.code))


class Runner(object):
    def __init__(self, plan, cfg, options, console, variables, patterns):
        self.plan = plan
        self.cfg = cfg
        self.opt = options
        self.console = console
        self.vars = dict(variables)
        self.exports = {}
        self.patterns = patterns
        self.confirm = options.confirm or cfg["confirm"]
        self.max_output = cfg["max_output"] if options.max_output is None else options.max_output
        self.strip_ansi = cfg["strip_ansi"]
        self.shell = find_shell(cfg["shell"])
        self.remote = Remote(plan["remote"], cfg["ssh"]) if plan.get("remote") else None
        self.facts = {}
        stream = options.stream or cfg["stream"]
        if stream == "auto":
            self.stream = hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and not options.quiet
        else:
            self.stream = stream == "true"
        self.statuses = {}
        self.resumed = {}
        self.report = None
        self.out_path = None

    # ------------------------------------------------------------------ helpers
    def _r(self, text):
        return redact(text, self.patterns)

    def _base_env(self):
        env = os.environ.copy()
        env.update({
            "RUNBOOK": "1",
            "RUNBOOK_PLAN": self.plan["name"],
            "RUNBOOK_HOST": self.vars.get("host", ""),
            "RUNBOOK_RESULT": self.out_path or "",
        })
        env.update(self.exports)
        return env

    def _remote_env(self, env_extra):
        env = {"RUNBOOK": "1", "RUNBOOK_PLAN": self.plan["name"], "RUNBOOK_HOST": self.vars.get("host", "")}
        env.update(self.exports)
        env.update(env_extra)
        return env

    def _command(self, c, cmd, env_extra, cwd, stdin_data, timeout):
        """argv, environment, cwd and stdin of the process to start, locally or through ssh."""
        if self.remote:
            script = self.remote.script(cmd, self._remote_env(env_extra), cwd, stdin_data, timeout)
            return self.remote.argv(), os.environ.copy(), None, script
        env = self._base_env()
        env.update(env_extra)
        argv = [self.shell, "-c", cmd] if c["shell"] else cmd
        return argv, env, cwd, stdin_data

    def _ssh_failure(self, c, result):
        """rc 255 is ssh's own error code (connection, authentication, host key)."""
        if (self.remote and result.get("status") == FAILED and result.get("rc") == SSH_ERROR_RC
                and SSH_ERROR_RC not in c["expect_rc"]):
            result["status"] = ERROR
            result["error"] = "ssh failed (rc 255): %s" % self._r(ssh_error_message(result.get("stderr")))
        return result

    def _write(self):
        if self.report is None:
            return
        counts = {}
        for entry in self.report["results"]:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        counts["total"] = len(self.plan["commands"])
        self.report["summary"] = counts
        self.report["finished"] = now_iso()
        self.report["duration_s"] = round(time.time() - self._t0, 3)
        self.report["captures"] = {k: self._r(v) for k, v in self.exports.items() if k in self._capture_names}
        write_json_atomic(self.out_path, self.report)

    # ------------------------------------------------------------------ process
    def _spawn(self, argv, env, cwd, stdin_data, timeout, live, out_limit=None):
        limit = self.max_output if out_limit is None else out_limit
        buffers = {"stdout": OutputBuffer(limit), "stderr": OutputBuffer(self.max_output)}
        printers = {}
        if live:
            printers = {"stdout": LivePrinter(self.console, "    │ ", None, self.patterns),
                        "stderr": LivePrinter(self.console, "    ┃ ", "33", self.patterns)}
        started = time.time()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or None, env=env,
                                    start_new_session=True)
        except (OSError, ValueError) as exc:
            return {"status": ERROR, "rc": None, "error": "%s: %s" % (type(exc).__name__, exc),
                    "duration_s": round(time.time() - started, 3)}, buffers

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
        sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
        to_send = b""
        if stdin_data is not None:
            to_send = stdin_data.encode("utf-8")
            if to_send:
                os.set_blocking(proc.stdin.fileno(), False)
                sel.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                proc.stdin.close()
        deadline = started + timeout if timeout else None
        timed_out = False
        try:
            while sel.get_map():
                wait = 0.5
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        timed_out = True
                        break
                    wait = min(wait, remaining)
                for key, _ in sel.select(wait):
                    name = key.data
                    if name == "stdin":
                        try:
                            sent = os.write(key.fileobj.fileno(), to_send[:65536])
                            to_send = to_send[sent:]
                        except (BrokenPipeError, OSError):
                            to_send = b""
                        if not to_send:
                            sel.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    buffers[name].add(chunk)
                    if name in printers:
                        printers[name].feed(chunk)
        except KeyboardInterrupt:
            self._kill(proc)
            raise Interrupted()
        finally:
            for printer in printers.values():
                printer.flush()

        if timed_out:
            self._kill(proc)
            for key in list(sel.get_map().values()):
                try:
                    while True:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            break
                        buffers[key.data].add(chunk)
                except (OSError, ValueError, KeyError):
                    pass
        sel.close()
        rc = proc.wait()
        result = {"rc": rc, "duration_s": round(time.time() - started, 3)}
        if timed_out:
            result["status"] = TIMEOUT
            result["error"] = "timed out after %ss" % timeout
        return result, buffers

    @staticmethod
    def _kill(proc):
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def _text(self, buffer):
        text = buffer.text()
        return sanitize(text) if self.strip_ansi else text

    def _run_once(self, c, argv, env, cwd, stdin_data, timeout, live):
        # A capturing command keeps its whole stdout (the value must be exact); the report
        # still shows the usual head + tail.
        out_limit = 0 if c["capture"] else self.max_output
        result, buffers = self._spawn(argv, env, cwd, stdin_data, timeout, live, out_limit)
        out, err = buffers["stdout"], buffers["stderr"]
        full_stdout = self._text(out)
        if "status" not in result:
            result["status"] = OK if result["rc"] in c["expect_rc"] else FAILED
        shown = full_stdout
        truncated = out.truncated
        if c["capture"] and self.max_output and out.total > self.max_output:
            trimmed = OutputBuffer(self.max_output)
            trimmed.add(full_stdout.encode("utf-8"))
            shown, truncated = trimmed.text(), True
        result.update({
            "stdout": self._r(shown),
            "stderr": self._r(self._text(err)),
            "stdout_bytes": out.total,
            "stderr_bytes": err.total,
            "truncated": truncated or err.truncated,
        })
        return self._ssh_failure(c, result), full_stdout

    # ------------------------------------------------------------------ main loop
    def _confirm(self, c, label, shown_cmd):
        need = self.confirm == "all" or (self.confirm == "dangerous" and c["dangerous"])
        if not need:
            return "y"
        self.console.write(self.console.paint(label + ("  DANGEROUS" if c["dangerous"] else ""), "33;1"), force=True)
        if c["description"]:
            self.console.write("    # " + c["description"], force=True)
        self.console.write("    $ " + shown_cmd, force=True)
        try:
            with open("/dev/tty") as tty:
                sys.stderr.write("    Run? [y/N/a/q] (y=yes, N=skip, a=yes to all, q=quit) ")
                sys.stderr.flush()
                answer = tty.readline().strip().lower()
        except OSError:
            self.console.warn("no terminal available to confirm; command skipped (use --yes to run unattended)")
            return "n"
        if answer in ("y", "yes"):
            return "y"
        if answer in ("a", "all"):
            self.confirm = "none"
            return "y"
        if answer in ("q", "quit"):
            return "q"
        return "n"

    def _selected(self, c, index):
        o = self.opt
        if o.only and c["id"] not in o.only:
            return "not selected (--only)"
        if o.skip and c["id"] in o.skip:
            return "not selected (--skip)"
        if o.start_from and index < self._from_index:
            return "before --from %s" % o.start_from
        return None

    def run(self, out_path):
        self.out_path = out_path
        plan, cmds = self.plan, self.plan["commands"]
        self._t0 = time.time()
        self._capture_names = {c["capture"] for c in cmds if c["capture"]}
        ids = [c["id"] for c in cmds]
        self._from_index = ids.index(self.opt.start_from) + 1 if self.opt.start_from else 0

        info = os_release()
        try:
            uid = os.geteuid()
        except AttributeError:
            uid = None
        try:
            return self._run(out_path, plan, cmds, info, uid)
        finally:
            if self.remote:
                self.remote.close()

    def _run(self, out_path, plan, cmds, info, uid):
        self.report = {
            "runbook_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "plan_name": plan["name"],
            "plan_description": plan["description"],
            "plan_file": plan.get("_path"),
            "plan_sha256": plan.get("_sha256"),
            "host": self.vars.get("host"),
            "fqdn": fqdn(),
            "os": info.get("PRETTY_NAME", platform.platform()),
            "kernel": platform.release(),
            "python": platform.python_version(),
            "user": self.vars.get("user"),
            "uid": uid,
            "cwd": os.getcwd(),
            "started": now_iso(),
            "finished": None,
            "duration_s": None,
            "interrupted": False,
            "stopped_at": None,
            "aborted": None,
            "options": {
                "confirm": self.confirm, "max_output": self.max_output, "stream": self.stream,
                "strip_ansi": self.strip_ansi, "redact_count": len(self.patterns),
                "only": self.opt.only, "skip": self.opt.skip, "from": self.opt.start_from,
                "resume": self.opt.resume_path,
            },
            "config_files": self.cfg.files,
            "vars": {k: self._r(str(v)) for k, v in sorted(self.vars.items())},
            "captures": {},
            "summary": {},
            "results": [],
        }

        if self.remote:
            self.report["runner"] = {"host": socket.gethostname(), "os": self.report["os"], "uid": uid}
            self.report["remote"] = self.remote.describe()
            problems = self._connect(plan["requires"])
        else:
            problems = check_requirements(plan["requires"])
        if problems:
            self.report["aborted"] = "requirements not met: " + "; ".join(problems)
            for c in cmds:
                self.report["results"].append({"id": c["id"], "status": SKIPPED, "reason": "requirements not met"})
            self._write()
            for problem in problems:
                self.console.error(problem)
            return EXIT_REQUIREMENTS

        if not self.remote and not self.shell and any(c["shell"] for c in cmds):
            self.console.error("no usable shell found (/bin/bash or /bin/sh)")
            return EXIT_INVALID

        where = self.vars.get("host")
        if self.remote:
            where = "%s (ssh %s)" % (self.facts.get("hostname") or where, self.remote.target)
        self.console.write(self.console.paint("▶ %s — %d command(s) on %s" % (plan["name"], len(cmds), where), "36;1"))
        stop_reason = None
        try:
            for index, c in enumerate(cmds, 1):
                label = "[%d/%d] %s" % (index, len(cmds), c["id"])
                entry = {"id": c["id"], "description": c["description"], "dangerous": c["dangerous"]}
                status = self._process(c, index, label, entry, stop_reason)
                self.statuses[c["id"]] = status
                if status in BAD and c["stop_on_error"] and not stop_reason:
                    stop_reason = "stopped after %s %s" % (c["id"], status)
                    self.report["stopped_at"] = c["id"]
                if entry.get("reason") == "quit":
                    stop_reason = "user quit"
                    self.report["stopped_at"] = c["id"]
                self.report["results"].append(entry)
                self._write()
        except Interrupted:
            self.report["interrupted"] = True
            done = {e["id"] for e in self.report["results"]}
            for c in cmds:
                if c["id"] not in done:
                    self.report["results"].append({"id": c["id"], "status": SKIPPED, "reason": "interrupted"})
            self._write()
            self.console.write(self.console.paint("Interrupted. Partial report: %s" % out_path, "33"), force=True)
            return EXIT_INTERRUPTED

        self._write()
        summary = self.report["summary"]
        bad = sum(summary.get(k, 0) for k in BAD)
        parts = ", ".join("%s=%d" % (k, v) for k, v in sorted(summary.items()) if k != "total")
        self.console.write(self.console.paint("■ Done in %.1fs — %s" % (self.report["duration_s"], parts), "31;1" if bad else "32;1"))
        return EXIT_FAILED if bad else EXIT_OK

    def _connect(self, requires):
        """Opens the shared ssh connection and reads the target's facts (uid, OS, missing binaries)."""
        if not self.remote.available():
            return ["ssh client not found: %s (set [runbook] ssh in .runbook.ini)" % self.remote.ssh_binary]
        self.remote.open()
        timeout = self.remote.connect_timeout + 30
        result, buffers = self._spawn(self.remote.argv(), os.environ.copy(), None,
                                      Remote.probe_script(requires.get("commands", [])), timeout, False)
        stdout, stderr = self._text(buffers["stdout"]), self._text(buffers["stderr"])
        if result.get("status") == TIMEOUT:
            return ["ssh %s: no answer after %ss" % (self.remote.target, timeout)]
        if result.get("rc") != 0:
            return ["ssh %s failed (rc %s): %s" % (self.remote.target, result.get("rc"),
                                                    self._r(ssh_error_message(stderr)))]
        self.facts = Remote.parse_probe(stdout)
        self.report["remote"].update({"hostname": self.facts.get("hostname"), "uid": self.facts.get("uid")})
        self.report.update({"host": self.facts.get("hostname") or self.report["host"],
                            "fqdn": self.facts.get("hostname") or self.report["fqdn"],
                            "os": self.facts.get("os"), "kernel": self.facts.get("kernel"),
                            "uid": int(self.facts["uid"]) if self.facts.get("uid", "").isdigit() else None})
        return check_remote_requirements(requires, self.facts)

    def _effective_ok(self, cid):
        return self.statuses.get(cid) == OK or cid in self.resumed

    def _process(self, c, index, label, entry, stop_reason):
        con = self.console

        def done(status, **extra):
            entry["status"] = status
            entry.update(extra)
            colour = {OK: "32", SKIPPED: "90", DECLINED: "90"}.get(status, "31")
            details = ""
            if entry.get("rc") is not None:
                details += " rc=%s" % entry["rc"]
            if "duration_s" in entry:
                details += " %.2fs" % entry["duration_s"]
            if entry.get("attempts", 1) > 1:
                details += " (%d attempts)" % entry["attempts"]
            if entry.get("reason"):
                details += " (%s)" % entry["reason"]
            if entry.get("error"):
                details += " (%s)" % entry["error"]
            con.write("    → " + con.paint(status + details, colour))
            return status

        def skip(reason):
            con.write(con.paint("%s skipped: %s" % (label, reason), "90"))
            entry.setdefault("cmd", self._r(c["cmd"] if isinstance(c["cmd"], str) else " ".join(c["cmd"])))
            entry["status"] = SKIPPED
            entry["reason"] = reason
            return SKIPPED

        if stop_reason:
            return skip(stop_reason)
        reason = self._selected(c, index)
        if reason:
            return skip(reason)
        if c["id"] in self.opt.resume_ok:
            self.resumed[c["id"]] = True
            if c["capture"]:
                value = self.opt.resume_captures.get(c["capture"])
                if value is None or "[REDACTED]" in value:
                    con.write("%s: capture %s not reusable from the previous run, running again" % (label, c["capture"]))
                    self.resumed.pop(c["id"], None)
                else:
                    self.vars[c["capture"]] = value
                    self.exports[c["capture"]] = value
            if c["id"] in self.resumed:
                return skip("ok in the resumed run")
        for dep in c["only_if"]:
            if not self._effective_ok(dep):
                return skip("only_if %s=%s" % (dep, self.statuses.get(dep, "not run")))

        try:
            cmd = render(c["cmd"], self.vars)
            cwd = render(c["cwd"], self.vars)
            env_extra = render(c["env"], self.vars)
            skip_if = render(c["skip_if"], self.vars)
            stdin_data = render(c["stdin"], self.vars)
        except UndefinedVariable as exc:
            entry["cmd"] = self._r(c["cmd"] if isinstance(c["cmd"], str) else " ".join(c["cmd"]))
            return done(ERROR, error="undefined variable {{%s}}" % exc.name)

        shown = cmd if isinstance(cmd, str) else " ".join(cmd)
        entry["cmd"] = self._r(shown)
        if cmd != c["cmd"]:
            entry["cmd_template"] = self._r(c["cmd"] if isinstance(c["cmd"], str) else " ".join(c["cmd"]))
        timeout = c["timeout"] if c["_explicit_timeout"] else self.cfg["timeout"]
        entry.update({"timeout": timeout, "expect_rc": c["expect_rc"], "stop_on_error": c["stop_on_error"]})
        if c["capture"]:
            entry["capture"] = c["capture"]

        if skip_if:
            argv, env, run_cwd, probe_stdin = self._command(dict(c, shell=True), skip_if, env_extra, cwd, None,
                                                            timeout or 60)
            probe, _ = self._spawn(argv, env, run_cwd, probe_stdin, timeout or 60, False)
            entry["skip_if"] = {"cmd": self._r(skip_if), "rc": probe.get("rc")}
            if probe.get("rc") == 0 and probe.get("status") != TIMEOUT:
                con.write(con.paint("%s skipped: skip_if succeeded" % label, "90"))
                entry["status"] = SKIPPED
                entry["reason"] = "skip_if succeeded"
                if c["capture"]:
                    con.warn("%s was skipped, so {{%s}} is not set" % (c["id"], c["capture"]))
                return SKIPPED

        answer = self._confirm(c, label, self._r(shown))
        if answer == "q":
            entry["reason"] = "quit"
            return done(DECLINED)
        if answer == "n":
            return done(DECLINED, reason="answered no")

        try:
            argv, env, run_cwd, run_stdin = self._command(c, cmd, env_extra, cwd, stdin_data, timeout)
        except ValueError as exc:
            return done(ERROR, error=str(exc))
        live = self.stream if c["stream"] is None else c["stream"]
        con.write("%s $ %s" % (con.paint(label, "1"), self._r(shown)))
        attempts = 0
        entry["started"] = now_iso()
        while True:
            attempts += 1
            result, raw_stdout = self._run_once(c, argv, env, run_cwd, run_stdin, timeout, live)
            if result["status"] not in BAD or attempts > c["retries"]:
                break
            con.write(con.paint("    ↻ %s, retry %d/%d in %ss" % (result["status"], attempts, c["retries"], c["retry_delay"]), "33"))
            time.sleep(c["retry_delay"])
        entry.update(result)
        entry["attempts"] = attempts
        if c["capture"] and entry["status"] == OK:
            value = raw_stdout.rstrip("\n")
            self.vars[c["capture"]] = value
            self.exports[c["capture"]] = value
        status = entry.pop("status")
        return done(status)
