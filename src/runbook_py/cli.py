"""Command-line interface: runbook [run] plan.json | validate | report | schema | init-config."""

import argparse
import json
import os
import re
import signal
import sys

from . import __version__
from .config import COLOR_MODES, CONFIRM_MODES, SAMPLE, ConfigError, load_config
from .plan import NAME_RE, PlanError, builtin_vars, check_variables, load_plan, parse_cli_vars, render
from .remote import render_remote, resolve_remote
from .report import load_result, render_report
from .runner import EXIT_INTERRUPTED, EXIT_INVALID, EXIT_LOCKED, Lock, Runner
from .schema import PLAN_SCHEMA
from .util import Console, redact, slug

SUBCOMMANDS = ("run", "validate", "report", "schema", "init-config")


def _csv(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def _add_plan_options(parser):
    parser.add_argument("plan", help="plan file (JSON)")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                        help="set a {{variable}} (repeatable; overrides the plan and .runbook.ini)")
    parser.add_argument("--config", metavar="FILE", help="extra .runbook.ini read last (also $RUNBOOK_CONFIG)")
    parser.add_argument("--no-config", action="store_true", help="ignore /etc, ~ and ./ .runbook.ini files")
    remote = parser.add_argument_group("remote execution over SSH (Python is not needed on the target)")
    remote.add_argument("--remote", metavar="[USER@]HOST", help="run the commands on HOST through ssh "
                                                               "(overrides the plan's remote.host / remote.user)")
    remote.add_argument("--port", type=int, metavar="PORT", help="ssh port (remote.port)")
    remote.add_argument("-i", "--identity", metavar="FILE", help="ssh private key (remote.identity_file)")
    remote.add_argument("--ssh-option", action="append", default=[], metavar="KEY=VALUE",
                        help="extra ssh -o option, repeatable (added before remote.ssh_options)")
    remote.add_argument("--local", action="store_true", help="ignore the plan's remote target and run locally")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="runbook",
        description="Run a JSON plan of shell commands and write a complete JSON report "
                    "(rc, stdout, stderr, duration) for each command.",
        epilog="Exit codes: 0 all ok, 1 a command failed, 2 invalid plan or config, "
               "3 requirements not met, 4 another run holds the lock, 130 interrupted.")
    parser.add_argument("--version", action="version", version="runbook-py " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = sub.add_parser("run", help="run a plan (default when a plan file is given directly)")
    _add_plan_options(run)
    run.add_argument("-o", "--output", metavar="FILE", help="result file (default: <plan>_<host>_<timestamp>.result.json)")
    run.add_argument("--output-dir", metavar="DIR", help="directory for the result file ([runbook] output_dir)")
    run.add_argument("--dry-run", action="store_true", help="show what would run, execute nothing")
    run.add_argument("--confirm", choices=CONFIRM_MODES, help="ask before: none, dangerous (default) or all commands")
    run.add_argument("-y", "--yes", action="store_true", help="never ask (same as --confirm none)")
    run.add_argument("--from", dest="start_from", metavar="ID", help="start at this command; earlier ones are skipped")
    run.add_argument("--only", type=_csv, metavar="ID[,ID...]", help="run only these commands")
    run.add_argument("--skip", type=_csv, metavar="ID[,ID...]", help="skip these commands")
    run.add_argument("--resume", metavar="RESULT", help="previous .result.json: commands that were ok are skipped, "
                                                        "failed and skipped ones run again")
    run.add_argument("--max-output", type=int, metavar="BYTES", help="bytes kept per stream, head + tail (0 = all)")
    run.add_argument("--redact", action="append", default=[], metavar="REGEX", help="mask matches as [REDACTED] (repeatable)")
    group = run.add_mutually_exclusive_group()
    group.add_argument("--stream", dest="stream", action="store_const", const="true", help="show output live")
    group.add_argument("--no-stream", dest="stream", action="store_const", const="false", help="do not show output live")
    run.add_argument("--color", choices=COLOR_MODES, help="console colours")
    run.add_argument("--no-lock", action="store_true", help="do not take the lock file")
    run.add_argument("-q", "--quiet", action="store_true", help="only print the result path and errors")

    val = sub.add_parser("validate", help="check a plan (structure, ids, variables) without running it")
    _add_plan_options(val)

    rep = sub.add_parser("report", help="summarize a .result.json file")
    rep.add_argument("result", help="result file")
    rep.add_argument("--format", choices=("text", "markdown"), default="text")
    rep.add_argument("--lines", type=int, default=20, help="output lines shown per failed command (default 20)")
    rep.add_argument("--all", action="store_true", help="show the output of every command, not only failures")

    sub.add_parser("schema", help="print the JSON Schema of a plan")

    init = sub.add_parser("init-config", help="print a commented .runbook.ini (or write it to FILE)")
    init.add_argument("file", nargs="?", help="where to write it (refuses to overwrite)")
    return parser


def normalize_argv(argv):
    """`runbook plan.json ...` is accepted as a shorthand for `runbook run plan.json ...`."""
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        return ["run"] + list(argv)
    return list(argv)


def _prepare(args, console):
    cfg = load_config(args.config, search=not args.no_config)
    plan = load_plan(args.plan)
    variables = builtin_vars(plan["name"])
    variables.update(plan["vars"])
    for key in cfg.vars:
        if not NAME_RE.match(key):
            raise ConfigError("invalid variable name %r in [vars]" % key)
    variables.update(cfg.vars)
    cli_vars = parse_cli_vars(args.var)
    variables.update(cli_vars)
    remote = render_remote(resolve_remote(plan["remote"], args), variables)
    plan["remote"] = remote
    if remote:
        explicit = set(plan["vars"]) | set(cfg.vars) | set(cli_vars)
        # {{host}} and {{fqdn}} name the machine the commands run on; {{runner_host}} stays local.
        for name in ("host", "fqdn"):
            if name not in explicit:
                variables[name] = remote["host"]
    check_variables(plan, variables)
    return cfg, plan, variables


def _target(plan):
    remote = plan.get("remote")
    if not remote:
        return ""
    target = "%s@%s" % (remote["user"], remote["host"]) if remote.get("user") else remote["host"]
    return " via ssh %s%s" % (target, (":%s" % remote["port"]) if remote.get("port") else "")


def _compile(patterns):
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise PlanError("invalid redact regex %r: %s" % (pattern, exc))
    return compiled


def cmd_validate(args, console):
    cfg, plan, variables = _prepare(args, console)
    cmds = plan["commands"]
    dangerous = sum(1 for c in cmds if c["dangerous"])
    captures = [c["capture"] for c in cmds if c["capture"]]
    console.write("OK: %s — %d command(s), %d dangerous%s%s" % (
        plan["name"], len(cmds), dangerous, (", captures: " + ", ".join(captures)) if captures else "",
        _target(plan)), force=True)
    if cfg.files:
        console.write("config: " + ", ".join(cfg.files), force=True)
    return 0


def _dry_run(plan, variables, console, opts, patterns):
    placeholders = dict(variables)
    for c in plan["commands"]:
        if c["capture"]:
            placeholders.setdefault(c["capture"], "{{%s}}" % c["capture"])
    console.write(console.paint("DRY RUN: %s — %d command(s) on %s%s" % (
        plan["name"], len(plan["commands"]), variables.get("host"), _target(plan)), "36;1"), force=True)
    ids = [c["id"] for c in plan["commands"]]
    start = ids.index(opts.start_from) if opts.start_from else 0
    for index, c in enumerate(plan["commands"]):
        selected = index >= start and (not opts.only or c["id"] in opts.only) and not (opts.skip and c["id"] in opts.skip)
        cmd = render(c["cmd"], placeholders)
        shown = redact(cmd if isinstance(cmd, str) else json.dumps(cmd), patterns)
        flags = []
        if c["dangerous"]:
            flags.append("DANGEROUS")
        if not c["stop_on_error"]:
            flags.append("continue-on-error")
        if c["only_if"]:
            flags.append("only_if=" + ",".join(c["only_if"]))
        if c["skip_if"]:
            flags.append("skip_if")
        if c["capture"]:
            flags.append("capture=" + c["capture"])
        if c["retries"]:
            flags.append("retries=%d" % c["retries"])
        if not selected:
            flags.append("NOT SELECTED")
        line = "  [%s] %s" % (c["id"], shown)
        console.write(line + console.paint("   (%s)" % ", ".join(flags), "90") if flags else line, force=True)
    return 0


def _load_resume(path, plan, console):
    data = load_result(path)
    if data.get("plan_sha256") and plan.get("_sha256") and data["plan_sha256"] != plan["_sha256"]:
        console.warn("the plan changed since %s; resuming by command id" % path)
    ok = {e["id"] for e in data.get("results", []) if e.get("status") == "ok"}
    known = {c["id"] for c in plan["commands"]}
    return ok & known, data.get("captures") or {}


def cmd_run(args, console):
    cfg, plan, variables = _prepare(args, console)
    ids = {c["id"] for c in plan["commands"]}
    for opt_name, values in (("--only", args.only or []), ("--skip", args.skip or []),
                             ("--from", [args.start_from] if args.start_from else [])):
        unknown = [v for v in values if v not in ids]
        if unknown:
            raise PlanError("%s: unknown command id(s): %s" % (opt_name, ", ".join(unknown)))

    if args.yes:
        args.confirm = "none"
    if args.color:
        console = Console(args.color, quiet=args.quiet)
    elif cfg["color"] != "auto":
        console = Console(cfg["color"], quiet=args.quiet)
    args.resume_path = os.path.abspath(args.resume) if args.resume else None
    args.resume_ok, args.resume_captures = set(), {}
    if args.resume:
        args.resume_ok, args.resume_captures = _load_resume(args.resume, plan, console)

    patterns = _compile(cfg["redact"] + plan["redact"] + args.redact)
    if args.dry_run:
        return _dry_run(plan, variables, console, args, patterns)

    if args.output:
        out_path = args.output
    else:
        out_dir = os.path.expanduser(args.output_dir or cfg["output_dir"])
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        out_path = os.path.join(out_dir, "%s_%s_%s.result.json" % (
            slug(plan["name"]), slug(variables["host"]), variables["timestamp"]))
    out_path = os.path.abspath(out_path)

    lock = Lock(None if args.no_lock else cfg.get_lock_file())
    if not lock.acquire():
        console.error("another runbook is running (lock %s); use --no-lock to bypass" % lock.path)
        return EXIT_LOCKED
    try:
        runner = Runner(plan, cfg, args, console, variables, patterns)
        code = runner.run(out_path)
    finally:
        lock.release()
    print(out_path)
    return code


def cmd_report(args, console):
    data = load_result(args.result)
    sys.stdout.write(render_report(data, args.format, args.lines, args.all))
    return 0


def cmd_init_config(args, console):
    if not args.file:
        sys.stdout.write(SAMPLE)
        return 0
    if os.path.exists(args.file):
        console.error("%s already exists" % args.file)
        return EXIT_INVALID
    with open(args.file, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE)
    console.write("wrote %s" % args.file, force=True)
    return 0


def _sigterm(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    argv = normalize_argv(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console("auto", quiet=getattr(args, "quiet", False))
    if not args.command:
        parser.print_help()
        return EXIT_INVALID
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except ValueError:
        pass
    try:
        if args.command == "schema":
            sys.stdout.write(json.dumps(PLAN_SCHEMA, indent=2) + "\n")
            return 0
        if args.command == "init-config":
            return cmd_init_config(args, console)
        if args.command == "report":
            return cmd_report(args, console)
        if args.command == "validate":
            return cmd_validate(args, console)
        return cmd_run(args, console)
    except (PlanError, ConfigError) as exc:
        console.error(str(exc))
        return EXIT_INVALID
    except (OSError, ValueError) as exc:
        console.error(str(exc))
        return EXIT_INVALID
    except KeyboardInterrupt:
        console.error("interrupted")
        return EXIT_INTERRUPTED
