"""Plan loading, validation and {{variable}} templating."""

import datetime
import getpass
import hashlib
import json
import os
import re
import socket

COMMAND_KEYS = {
    "id", "cmd", "description", "timeout", "stop_on_error", "expect_rc", "shell", "cwd", "env",
    "dangerous", "only_if", "skip_if", "capture", "retries", "retry_delay", "stdin", "stream",
}
DEFAULT_KEYS = {
    "timeout", "stop_on_error", "expect_rc", "shell", "cwd", "env", "dangerous",
    "retries", "retry_delay", "stream",
}
TOP_KEYS = {"$schema", "schema_version", "name", "description", "defaults", "vars", "redact", "requires", "remote",
            "commands"}
REQUIRES_KEYS = {"root", "os", "commands"}

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RESERVED_ENV = {"PATH", "HOME", "SHELL", "USER", "LOGNAME", "PWD", "OLDPWD", "IFS", "TERM", "LANG", "TZ"}


class PlanError(Exception):
    pass


class UndefinedVariable(Exception):
    def __init__(self, name):
        Exception.__init__(self, name)
        self.name = name


def builtin_vars(plan_name):
    now = datetime.datetime.now()
    try:
        user = getpass.getuser()
    except Exception:
        user = str(os.getuid())
    return {
        "host": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "user": user,
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "timestamp": now.strftime("%Y%m%d-%H%M%S"),
        "plan_name": plan_name,
        "runner_host": socket.gethostname(),
    }


def referenced(value):
    """Variable names used in a string, a list or a dict of strings."""
    names = set()
    if isinstance(value, str):
        names.update(VAR_RE.findall(value))
    elif isinstance(value, list):
        for item in value:
            names |= referenced(item)
    elif isinstance(value, dict):
        for item in value.values():
            names |= referenced(item)
    return names


def render(value, variables):
    if isinstance(value, str):
        def repl(match):
            name = match.group(1)
            if name not in variables:
                raise UndefinedVariable(name)
            return str(variables[name])
        return VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: render(item, variables) for key, item in value.items()}
    return value


def _check_type(where, key, value, types, label):
    if not isinstance(value, types) or (isinstance(value, bool) and bool not in _as_tuple(types)):
        raise PlanError("%s: %r must be %s" % (where, key, label))


def _as_tuple(types):
    return types if isinstance(types, tuple) else (types,)


def _validate_fields(where, c):
    if "timeout" in c and c["timeout"] is not None:
        _check_type(where, "timeout", c["timeout"], (int, float), "a number of seconds (0 or null = none)")
    for key in ("stop_on_error", "dangerous"):
        if key in c:
            _check_type(where, key, c[key], bool, "true or false")
    if "shell" in c:
        _check_type(where, "shell", c["shell"], bool, "true or false")
    if "stream" in c and c["stream"] is not None:
        _check_type(where, "stream", c["stream"], bool, "true, false or null")
    if "retries" in c:
        _check_type(where, "retries", c["retries"], int, "an integer >= 0")
        if c["retries"] < 0:
            raise PlanError("%s: 'retries' must be >= 0" % where)
    if "retry_delay" in c:
        _check_type(where, "retry_delay", c["retry_delay"], (int, float), "a number of seconds")
    if "expect_rc" in c:
        rc = c["expect_rc"]
        if isinstance(rc, int) and not isinstance(rc, bool):
            rc = [rc]
        if not isinstance(rc, list) or not rc or not all(isinstance(x, int) and not isinstance(x, bool) for x in rc):
            raise PlanError("%s: 'expect_rc' must be an integer or a non-empty list of integers" % where)
        c["expect_rc"] = rc
    if "cwd" in c and c["cwd"] is not None:
        _check_type(where, "cwd", c["cwd"], str, "a string")
    if "env" in c:
        env = c["env"]
        if not isinstance(env, dict) or not all(isinstance(v, (str, int, float)) for v in env.values()):
            raise PlanError("%s: 'env' must be an object of string values" % where)
        c["env"] = {str(k): str(v) for k, v in env.items()}
    for key in ("description", "skip_if", "stdin"):
        if key in c and c[key] is not None:
            _check_type(where, key, c[key], str, "a string")


def load_plan(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise PlanError("cannot read %s: %s" % (path, exc))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlanError("invalid JSON in %s: %s" % (path, exc))
    plan = normalize_plan(data, default_name=os.path.splitext(os.path.basename(path))[0])
    plan["_sha256"] = hashlib.sha256(raw).hexdigest()
    plan["_path"] = os.path.abspath(path)
    return plan


def normalize_plan(data, default_name="runbook"):
    """Validates the plan structure and fills each command with its defaults (no variables yet)."""
    if isinstance(data, list):
        data = {"commands": data}
    if not isinstance(data, dict):
        raise PlanError("a plan must be a JSON object with a 'commands' list")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise PlanError("unknown top-level key(s): %s" % ", ".join(sorted(unknown)))
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        raise PlanError("'commands' must be a non-empty list")
    if data.get("schema_version", 1) != 1:
        raise PlanError("unsupported schema_version %r (this runbook supports 1)" % data.get("schema_version"))

    name = data.get("name") or default_name
    if not isinstance(name, str):
        raise PlanError("'name' must be a string")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise PlanError("'defaults' must be an object")
    unknown = set(defaults) - DEFAULT_KEYS
    if unknown:
        raise PlanError("unknown key(s) in defaults: %s (allowed: %s)"
                        % (", ".join(sorted(unknown)), ", ".join(sorted(DEFAULT_KEYS))))
    defaults = dict(defaults)
    _validate_fields("defaults", defaults)

    plan_vars = data.get("vars") or {}
    if not isinstance(plan_vars, dict):
        raise PlanError("'vars' must be an object")
    for key, value in plan_vars.items():
        if not NAME_RE.match(key):
            raise PlanError("invalid variable name %r (letters, digits, underscore)" % key)
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise PlanError("variable %r must be a string or a number" % key)

    redact = data.get("redact") or []
    if not isinstance(redact, list) or not all(isinstance(r, str) for r in redact):
        raise PlanError("'redact' must be a list of regex strings")
    for pattern in redact:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PlanError("invalid redact regex %r: %s" % (pattern, exc))

    requires = data.get("requires") or {}
    if not isinstance(requires, dict) or set(requires) - REQUIRES_KEYS:
        raise PlanError("'requires' accepts only: %s" % ", ".join(sorted(REQUIRES_KEYS)))
    if "root" in requires and not isinstance(requires["root"], bool):
        raise PlanError("requires.root must be true or false")
    for key in ("os", "commands"):
        if key in requires:
            val = requires[key]
            if isinstance(val, str):
                val = [val]
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise PlanError("requires.%s must be a string or a list of strings" % key)
            requires[key] = val

    from .remote import validate_remote  # late import: remote.py imports this module
    remote = validate_remote(data.get("remote"))

    seen = []
    captures = set()
    cmds = []
    for index, item in enumerate(commands, 1):
        if isinstance(item, str):
            item = {"cmd": item}
        where = "command #%d" % index
        if not isinstance(item, dict) or "cmd" not in item:
            raise PlanError("%s: missing 'cmd'" % where)
        unknown = set(item) - COMMAND_KEYS
        if unknown:
            raise PlanError("%s: unknown key(s): %s" % (where, ", ".join(sorted(unknown))))
        c = {"timeout": 60, "stop_on_error": True, "expect_rc": [0], "shell": True, "cwd": None, "env": {},
             "dangerous": False, "retries": 0, "retry_delay": 5, "stream": None, "description": "",
             "only_if": [], "skip_if": None, "capture": None, "stdin": None}
        c["_explicit_timeout"] = "timeout" in item or "timeout" in defaults
        for key, value in defaults.items():
            c[key] = dict(value) if isinstance(value, dict) else value
        if "env" in item and "env" in defaults:
            merged = dict(defaults["env"])
            merged.update(item["env"])
            item = dict(item, env=merged)
        c.update(item)
        cid = str(c.get("id") or "cmd%d" % index)
        if not ID_RE.match(cid):
            raise PlanError("%s: invalid id %r (letters, digits, '.', '_', '-')" % (where, cid))
        if cid in seen:
            raise PlanError("duplicate id: %s" % cid)
        c["id"] = cid
        where = "command %s" % cid
        _validate_fields(where, c)

        if c["shell"]:
            if isinstance(c["cmd"], list):
                c["cmd"] = " ".join(str(x) for x in c["cmd"])
            if not isinstance(c["cmd"], str) or not c["cmd"].strip():
                raise PlanError("%s: 'cmd' must be a non-empty string" % where)
        else:
            if not isinstance(c["cmd"], list) or not c["cmd"] or not all(isinstance(x, (str, int, float)) for x in c["cmd"]):
                raise PlanError("%s: shell=false needs 'cmd' as a non-empty list (argv)" % where)
            c["cmd"] = [str(x) for x in c["cmd"]]

        only_if = c["only_if"] or []
        if isinstance(only_if, str):
            only_if = [only_if]
        if not isinstance(only_if, list) or not all(isinstance(x, str) for x in only_if):
            raise PlanError("%s: 'only_if' must be an id or a list of ids" % where)
        for dep in only_if:
            if dep not in seen:
                raise PlanError("%s: only_if=%s must name an earlier command" % (where, dep))
        c["only_if"] = only_if

        if c["capture"] is not None:
            cap = c["capture"]
            if not isinstance(cap, str) or not NAME_RE.match(cap):
                raise PlanError("%s: 'capture' must be a variable name (letters, digits, underscore)" % where)
            if cap in RESERVED_ENV or cap.startswith("LC_") or cap.startswith("RUNBOOK_"):
                raise PlanError("%s: capture name %r is reserved" % (where, cap))
            captures.add(cap)
        c["_captures_before"] = set(captures) - ({c["capture"]} if c["capture"] else set())
        seen.append(cid)
        cmds.append(c)

    for key in plan_vars:
        if key in RESERVED_ENV or key.startswith("LC_") or key.startswith("RUNBOOK_"):
            raise PlanError("variable name %r is reserved" % key)

    return {
        "name": name,
        "description": data.get("description", ""),
        "vars": {k: str(v) for k, v in plan_vars.items()},
        "redact": redact,
        "requires": requires,
        "remote": remote,
        "commands": cmds,
    }


def check_variables(plan, variables):
    """Every {{name}} must be known: builtin, plan, config, --var, or captured by an EARLIER command."""
    problems = []
    for c in plan["commands"]:
        known = set(variables) | c["_captures_before"]
        for field in ("cmd", "cwd", "env", "skip_if", "stdin"):
            for name in sorted(referenced(c.get(field))):
                if name not in known:
                    later = any(o.get("capture") == name for o in plan["commands"])
                    hint = " (captured by a later command)" if later else ""
                    problems.append("command %s: {{%s}} in %s is not defined%s" % (c["id"], name, field, hint))
    if problems:
        raise PlanError("undefined variable(s):\n  " + "\n  ".join(problems))


def parse_cli_vars(items):
    result = {}
    for item in items or []:
        if "=" not in item:
            raise PlanError("--var expects NAME=VALUE, got %r" % item)
        key, value = item.split("=", 1)
        key = key.strip()
        if not NAME_RE.match(key):
            raise PlanError("invalid variable name in --var: %r" % key)
        result[key] = value
    return result
