"""Loads .runbook.ini files.

Lookup order (later files override earlier ones):
    /etc/runbook.ini, ~/.runbook.ini, ./.runbook.ini, then $RUNBOOK_CONFIG or --config.
"""

import configparser
import os
import re
import tempfile

CONFIRM_MODES = ("none", "dangerous", "all")
TRISTATE = ("auto", "true", "false")
COLOR_MODES = ("auto", "always", "never")


def default_lock_file():
    if os.path.isdir("/run/lock") and os.access("/run/lock", os.W_OK):
        return "/run/lock/runbook.lock"
    return os.path.join(tempfile.gettempdir(), "runbook-%d.lock" % os.getuid())


DEFAULTS = {
    "output_dir": ".",
    "max_output": 65536,
    "confirm": "dangerous",
    "timeout": 60,
    "shell": "",          # empty = /bin/bash, then /bin/sh
    "stream": "auto",     # live output: auto = when stderr is a terminal
    "color": "auto",
    "strip_ansi": True,
    "lock_file": None,    # None = default_lock_file(); "" disables locking
    "redact": [],
}

SEARCH_PATHS = ("/etc/runbook.ini", "~/.runbook.ini", "./.runbook.ini")


class ConfigError(Exception):
    pass


class Config(object):
    def __init__(self):
        self.values = dict(DEFAULTS)
        self.values["redact"] = []
        self.vars = {}
        self.files = []

    def __getitem__(self, key):
        return self.values[key]

    def get_lock_file(self):
        lock = self.values["lock_file"]
        return default_lock_file() if lock is None else lock


def _parse_bool(value, key, path):
    low = value.strip().lower()
    if low in ("1", "yes", "true", "on"):
        return True
    if low in ("0", "no", "false", "off"):
        return False
    raise ConfigError("%s: [runbook] %s must be true or false, got %r" % (path, key, value))


def _apply_file(cfg, path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep variable names case-sensitive
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise ConfigError("%s: %s" % (path, exc))

    for section in parser.sections():
        if section not in ("runbook", "vars"):
            raise ConfigError("%s: unknown section [%s] (expected [runbook] and [vars])" % (path, section))

    if parser.has_section("runbook"):
        for key, raw in parser.items("runbook"):
            if key not in DEFAULTS:
                raise ConfigError("%s: unknown key %r in [runbook] (known: %s)"
                                  % (path, key, ", ".join(sorted(DEFAULTS))))
            value = raw.strip()
            if key in ("max_output", "timeout"):
                try:
                    value = int(value)
                except ValueError:
                    raise ConfigError("%s: [runbook] %s must be an integer, got %r" % (path, key, raw))
                if value < 0:
                    raise ConfigError("%s: [runbook] %s must be >= 0" % (path, key))
            elif key == "strip_ansi":
                value = _parse_bool(value, key, path)
            elif key == "confirm" and value not in CONFIRM_MODES:
                raise ConfigError("%s: [runbook] confirm must be one of %s" % (path, ", ".join(CONFIRM_MODES)))
            elif key == "stream" and value.lower() not in TRISTATE:
                raise ConfigError("%s: [runbook] stream must be auto, true or false" % path)
            elif key == "color" and value not in COLOR_MODES:
                raise ConfigError("%s: [runbook] color must be one of %s" % (path, ", ".join(COLOR_MODES)))
            elif key == "output_dir":
                value = os.path.expanduser(value)
            elif key == "redact":
                patterns = [line.strip() for line in raw.splitlines() if line.strip()]
                for pattern in patterns:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise ConfigError("%s: invalid redact regex %r: %s" % (path, pattern, exc))
                cfg.values["redact"].extend(patterns)
                continue
            if key == "stream":
                value = value.lower()
            cfg.values[key] = value

    if parser.has_section("vars"):
        for key, value in parser.items("vars"):
            cfg.vars[key] = value
    cfg.files.append(os.path.abspath(path))


def load_config(explicit=None, search=True):
    cfg = Config()
    if search:
        for candidate in SEARCH_PATHS:
            path = os.path.expanduser(candidate)
            if os.path.isfile(path):
                _apply_file(cfg, path)
    explicit = explicit or os.environ.get("RUNBOOK_CONFIG")
    if explicit:
        if not os.path.isfile(explicit):
            raise ConfigError("config file not found: %s" % explicit)
        _apply_file(cfg, explicit)
    return cfg


SAMPLE = """\
# .runbook.ini - runbook-py configuration
# Looked up in /etc/runbook.ini, ~/.runbook.ini, ./.runbook.ini, then $RUNBOOK_CONFIG / --config.
# Later files override earlier ones; command-line options override everything.

[runbook]
# Where <plan>_<host>_<timestamp>.result.json files are written
output_dir = .
# Bytes kept per stream (stdout, stderr); the head and the tail are kept. 0 = unlimited
max_output = 65536
# Ask before running: none | dangerous | all
confirm = dangerous
# Default timeout per command, in seconds (0 = none)
timeout = 60
# Shell used for string commands (empty = /bin/bash, falling back to /bin/sh)
shell =
# Show command output live: auto (when stderr is a terminal) | true | false
stream = auto
# auto | always | never (NO_COLOR is honoured)
color = auto
# Remove ANSI colour codes and carriage-return progress lines from captured output
strip_ansi = true
# Prevents two runs at once; empty disables locking
# lock_file = /run/lock/runbook.lock
# Regexes masked as [REDACTED] in the report, one per line
redact =
    (?i)(password|passwd|secret|token)\\s*[:=]\\s*\\S+

[vars]
# Host-specific values available as {{name}} in every plan; they override the plan's own "vars".
# mac_nic0 = b8:3f:d2:0d:9f:16
"""
