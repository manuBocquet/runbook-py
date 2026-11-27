"""Remote execution over SSH.

Every command (and every skip_if test) is turned into a POSIX sh script sent on the standard
input of `ssh <target> exec <shell> -s`. The remote login shell only ever parses that short
fixed line, so tcsh, csh or menu shells (pfSense, FreeBSD root) cannot misread the command.
Python is only needed on the machine that runs runbook, not on the target.
"""

import math
import os
import shlex
import shutil
import subprocess
import tempfile

from .plan import NAME_RE, PlanError, UndefinedVariable, render

REMOTE_KEYS = {"host", "user", "port", "identity_file", "ssh_options", "shell", "connect_timeout"}
DEFAULT_REMOTE_SHELL = "/bin/sh"
SSH_ERROR_RC = 255
# Extra seconds given to the remote `timeout` backstop after the local deadline.
REMOTE_GRACE = 5


def validate_remote(remote):
    """Checks the plan's "remote" object; returns a normalized copy (or None)."""
    if remote is None:
        return None
    if not isinstance(remote, dict):
        raise PlanError("'remote' must be an object")
    unknown = set(remote) - REMOTE_KEYS
    if unknown:
        raise PlanError("unknown key(s) in remote: %s (allowed: %s)"
                        % (", ".join(sorted(unknown)), ", ".join(sorted(REMOTE_KEYS))))
    remote = dict(remote)
    if not isinstance(remote.get("host"), str) or not remote["host"].strip():
        raise PlanError("remote.host must be a non-empty string")
    for key in ("user", "identity_file", "shell"):
        if key in remote and remote[key] is not None and not isinstance(remote[key], str):
            raise PlanError("remote.%s must be a string" % key)
    if "port" in remote and remote["port"] is not None:
        port = remote["port"]
        if isinstance(port, bool) or not isinstance(port, (int, str)):
            raise PlanError("remote.port must be an integer or a string")
        remote["port"] = str(port)
    if "connect_timeout" in remote and remote["connect_timeout"] is not None:
        value = remote["connect_timeout"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise PlanError("remote.connect_timeout must be a number of seconds > 0")
    opts = remote.get("ssh_options") or []
    if isinstance(opts, str):
        opts = [opts]
    if not isinstance(opts, list) or not all(isinstance(o, str) for o in opts):
        raise PlanError("remote.ssh_options must be a list of strings such as \"StrictHostKeyChecking=yes\"")
    remote["ssh_options"] = opts
    return remote


def resolve_remote(plan_remote, args):
    """Merges the plan's "remote" with --remote/--port/--identity/--ssh-option/--local."""
    if getattr(args, "local", False):
        return None
    remote = dict(plan_remote) if plan_remote else {}
    target = getattr(args, "remote", None)
    if target:
        if "@" in target:
            user, host = target.rsplit("@", 1)
            remote["user"] = user
        else:
            host = target
        remote["host"] = host
    if getattr(args, "port", None):
        remote["port"] = str(args.port)
    if getattr(args, "identity", None):
        remote["identity_file"] = args.identity
    extra = getattr(args, "ssh_option", None) or []
    if extra:
        # Command-line options first: for ssh the first value given for an option wins.
        remote["ssh_options"] = list(extra) + list(remote.get("ssh_options") or [])
    if not remote:
        return None
    if not remote.get("host"):
        raise PlanError("--port, --identity or --ssh-option need a target: --remote [USER@]HOST or a plan 'remote'")
    return validate_remote(remote)


def render_remote(remote, variables):
    if remote is None:
        return None
    try:
        rendered = render(remote, variables)
    except UndefinedVariable as exc:
        raise PlanError("remote: {{%s}} is not defined (captures cannot be used in 'remote')" % exc.name)
    if not rendered["host"].strip():
        raise PlanError("remote.host is empty")
    return rendered


def _q(value):
    return shlex.quote(str(value))


class Remote(object):
    def __init__(self, spec, ssh_binary="ssh"):
        self.spec = spec
        self.host = spec["host"]
        self.user = spec.get("user") or None
        self.port = spec.get("port") or None
        self.identity = os.path.expanduser(spec["identity_file"]) if spec.get("identity_file") else None
        self.shell = spec.get("shell") or DEFAULT_REMOTE_SHELL
        self.connect_timeout = spec.get("connect_timeout") or 10
        self.ssh_options = list(spec.get("ssh_options") or [])
        self.ssh_binary = ssh_binary or "ssh"
        self.target = "%s@%s" % (self.user, self.host) if self.user else self.host
        self._control_dir = None

    # ------------------------------------------------------------------ ssh command line
    def available(self):
        return bool(shutil.which(self.ssh_binary))

    def _options(self):
        opts = []
        # User options come first: ssh keeps the first value it sees for each option.
        for opt in self.ssh_options:
            opts += ["-o", opt]
        opts += ["-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=%d" % int(math.ceil(self.connect_timeout)),
                 "-o", "ServerAliveInterval=15",
                 "-o", "ServerAliveCountMax=3"]
        if self.identity:
            opts += ["-i", self.identity, "-o", "IdentitiesOnly=yes"]
        if self.port:
            opts += ["-p", str(self.port)]
        if self._control_dir:
            opts += ["-o", "ControlMaster=auto",
                     "-o", "ControlPath=%s" % os.path.join(self._control_dir, "%C"),
                     "-o", "ControlPersist=60"]
        return opts

    def argv(self):
        """ssh command line; the script to run is written on its standard input."""
        return [self.ssh_binary, "-T"] + self._options() + [self.target, "exec %s -s" % self.shell]

    def open(self):
        """Private directory for the multiplexed connection (one TCP/SSH session for the whole plan)."""
        self._control_dir = tempfile.mkdtemp(prefix="runbook-ssh-")

    def close(self):
        if not self._control_dir:
            return
        try:
            subprocess.run([self.ssh_binary] + self._options() + ["-O", "exit", self.target],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        except Exception:
            pass
        shutil.rmtree(self._control_dir, ignore_errors=True)
        self._control_dir = None

    def describe(self):
        return {
            "target": self.target,
            "port": self.port,
            "identity_file": self.identity,
            "shell": self.shell,
            "ssh_options": self.ssh_options,
        }

    # ------------------------------------------------------------------ scripts
    def script(self, cmd, env=None, cwd=None, stdin_data=None, timeout=None):
        """POSIX sh script that runs `cmd` on the target like a local runbook command:
        given environment and working directory, stdin from `stdin_data` (else /dev/null),
        and a remote `timeout` backstop so nothing is left running after a local timeout."""
        if not isinstance(cmd, str):
            cmd = " ".join(_q(part) for part in cmd)
        inner = []
        for key, value in sorted((env or {}).items()):
            if not NAME_RE.match(key):
                raise ValueError("environment variable name %r cannot be exported on the remote host" % key)
            inner.append("export %s=%s" % (key, _q(value)))
        if cwd:
            inner.append("cd %s || exit 1" % _q(cwd))
        inner.append(cmd)
        limit = int(math.ceil(timeout)) + REMOTE_GRACE if timeout else 0

        lines = [
            "RUNBOOK_SCRIPT=%s" % _q("\n".join(inner) + "\n"),
            "RUNBOOK_LIMIT=%d" % limit,
            "runbook_exec() {",
            "  if [ \"$RUNBOOK_LIMIT\" -gt 0 ] && command -v timeout >/dev/null 2>&1; then",
            "    timeout -k 5 \"$RUNBOOK_LIMIT\" %s -c \"$RUNBOOK_SCRIPT\"" % _q(self.shell),
            "  else",
            "    %s -c \"$RUNBOOK_SCRIPT\"" % _q(self.shell),
            "  fi",
            "}",
        ]
        if stdin_data is None:
            lines.append("runbook_exec </dev/null")
        else:
            # A single-quoted string keeps the data byte for byte, final newlines included.
            # No here-document inside $(...): bash 3.2 (macOS /bin/sh) treats a backslash-newline
            # there as a line continuation even when the delimiter is quoted.
            lines += [
                "RUNBOOK_STDIN=%s" % _q(stdin_data),
                "printf '%s' \"$RUNBOOK_STDIN\" | runbook_exec",
            ]
        lines.append("exit $?")
        return "\n".join(lines) + "\n"

    @staticmethod
    def probe_script(binaries=()):
        """Facts about the target, one KEY=VALUE per line, used for 'requires' and the report."""
        lines = [
            "printf 'uid=%s\\n' \"$(id -u)\"",
            "printf 'hostname=%s\\n' \"$(hostname)\"",
            "printf 'kernel=%s\\n' \"$(uname -sr)\"",
            "if [ -r /etc/os-release ]; then",
            "  ( . /etc/os-release; printf 'os=%s\\n' \"$PRETTY_NAME\"; printf 'os_ids=%s %s\\n' \"$ID\" \"$ID_LIKE\" )",
            "else",
            "  printf 'os=%s\\n' \"$(uname -sr)\"; printf 'os_ids=%s\\n' \"$(uname -s | tr A-Z a-z)\"",
            "fi",
        ]
        for binary in binaries:
            lines.append("command -v %s >/dev/null 2>&1 || printf 'missing=%%s\\n' %s" % (_q(binary), _q(binary)))
        lines.append("exit 0")
        return "\n".join(lines) + "\n"

    @staticmethod
    def parse_probe(text):
        facts = {"missing": []}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "missing":
                facts["missing"].append(value)
            else:
                facts[key] = value.strip()
        return facts


def ssh_error_message(stderr):
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no message"


__all__ = ["Remote", "resolve_remote", "render_remote", "validate_remote", "ssh_error_message",
           "SSH_ERROR_RC", "DEFAULT_REMOTE_SHELL", "REMOTE_KEYS"]
