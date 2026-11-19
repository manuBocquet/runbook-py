# runbook-py

**Run a JSON plan of shell commands. Get one JSON report back.**

`runbook` executes the commands of a plan in order and records, for each one, the exit code, stdout,
stderr, duration and status. It was born from a simple workflow: an AI assistant (or a colleague)
writes the plan, you review and run it on the server, and you hand back a single
`.result.json` file instead of copy-pasting terminal output.

```console
$ runbook hello.json
▶ hello — 5 command(s) on web01
[1/5] whoami $ id; hostname; uptime
    │ uid=0(root) gid=0(root) groups=0(root)
    │ web01
    → ok rc=0 0.01s
[3/5] kernel $ uname -r
    │ 6.8.12-4-pve
    → ok rc=0 0.00s
...
■ Done in 0.2s — ok=5
/root/hello_web01_20261119-091500.result.json
```

- **No dependencies**, one command, Python 3.6+ (Debian, Ubuntu, Proxmox, RHEL/Rocky 8+, macOS).
- **Safe by default**: commands marked `"dangerous": true` ask for confirmation; stdin is closed, so
  nothing hangs on a prompt; every command has a timeout that kills its whole process group.
- **Complete, honest report**: exit codes, both streams, durations, the host, the OS, the plan's
  SHA-256. It is rewritten after every command, so even a Ctrl-C leaves a usable partial report.
- **Built for review**: secrets masked by regex, ANSI codes and progress bars cleaned up, huge outputs
  kept as head + tail.

## Install

```bash
pipx install runbook-py          # recommended: isolated, puts `runbook` in your PATH
# or
python3 -m pip install --user runbook-py
```

On Debian 12+ / Ubuntu 23.04+, plain `pip install` outside a virtualenv is refused (PEP 668): use
`pipx` (`apt install pipx`). On RHEL/Rocky 8 (Python 3.6, pip 9), upgrade pip first:
`python3 -m pip install --user --upgrade pip`.

## A plan

```json
{
  "name": "nginx-check",
  "description": "Check nginx and reload it if the configuration is valid",
  "requires": { "root": true, "commands": ["nginx"] },
  "defaults": { "timeout": 30, "stop_on_error": true },
  "commands": [
    { "id": "status", "cmd": "systemctl is-active nginx", "expect_rc": [0, 3] },
    { "id": "test",   "cmd": "nginx -t" },
    { "id": "reload", "cmd": "systemctl reload nginx", "dangerous": true, "only_if": "test" }
  ]
}
```

```bash
runbook validate plan.json     # structure, ids, variables; runs nothing
runbook plan.json --dry-run    # shows what would run
runbook plan.json              # runs it; prints the path of the result file on stdout
runbook report plan_web01_20261119-091500.result.json
```

### Command fields

| Field | Default | Meaning |
| --- | --- | --- |
| `id` | `cmdN` | Unique name, used by `only_if`, `--from`, `--only`, `--skip`, the report |
| `cmd` | required | String run with `bash -c` (or `sh -c`), or an argv list when `"shell": false` |
| `description` | | Shown at confirmation and in the report |
| `timeout` | `60` | Seconds; `0` or `null` = no limit. The process group is killed (SIGTERM, then SIGKILL) |
| `stop_on_error` | `true` | A failure, timeout or start error skips the rest of the plan |
| `expect_rc` | `[0]` | Exit codes that count as success |
| `dangerous` | `false` | Changes the system: asks first (with the default `--confirm dangerous`) |
| `only_if` | | Id(s) of earlier commands that must have ended `ok` |
| `skip_if` | | Shell test run first; exit code 0 = already done, the command is skipped |
| `capture` | | Saves stdout as `{{NAME}}` and `$NAME` for later commands |
| `retries`, `retry_delay` | `0`, `5` | Extra attempts after a failure or a timeout |
| `stdin` | | Text sent to the command's standard input (otherwise `/dev/null`) |
| `cwd`, `env` | | Working directory, extra environment variables |
| `shell` | `true` | `false` runs `cmd` as an argv list without a shell |
| `stream` | | Force live output on or off for this command |

Every field except `id`, `cmd`, `description`, `only_if`, `skip_if`, `capture` and `stdin` can be set
once in `"defaults"`. Plan-level keys: `name`, `description`, `defaults`, `vars`, `redact`,
`requires` (`root`, `os`, `commands`), `commands`. The full JSON Schema: `runbook schema`, also in
[`schema/runbook-plan.schema.json`](schema/runbook-plan.schema.json).

### Statuses

`ok`, `failed` (exit code not in `expect_rc`), `timeout`, `error` (could not start, undefined
variable), `skipped` (with a `reason`), `declined` (answered *no* at the confirmation).

## Variables and captures

`{{name}}` is replaced in `cmd`, `cwd`, `env`, `skip_if` and `stdin`. Sources, lowest priority first:

1. built-ins: `host`, `fqdn`, `user`, `date` (YYYYMMDD), `time`, `timestamp`, `plan_name`
2. the plan's `"vars"`
3. `[vars]` in `.runbook.ini`: host-specific values, so one plan serves many servers
4. `--var NAME=VALUE`

A command with `"capture": "NAME"` stores its stdout (trailing newlines removed) for the commands
after it, both as `{{NAME}}` and as the environment variable `$NAME`:

```json
{ "id": "esp", "cmd": "findmnt -no SOURCE /boot/efi", "capture": "ESP" },
{ "id": "show", "cmd": "lsblk {{ESP}}", "only_if": "esp" }
```

`runbook validate` rejects a variable that no source defines, including one captured only *later*.

## Re-running part of a plan

```bash
runbook plan.json --from upgrade            # start at a command
runbook plan.json --only update,pending     # run only these
runbook plan.json --skip reboot             # everything but these
runbook plan.json --resume previous.result.json   # skip what was ok, rerun the rest
```

`--resume` also restores the values captured in the previous run. Plans written with `skip_if` can
simply be run again.

## Confirmation

With `--confirm dangerous` (default), each `"dangerous": true` command shows its description and
command line and asks `Run? [y/N/a/q]`: **y** runs it, **N** (or Enter) skips it (`declined`),
**a** runs it and every following command without asking, **q** stops the plan. `--confirm all` asks
for every command; `-y` / `--confirm none` never asks. Without a terminal, dangerous commands are
declined, never run silently.

## Configuration: `.runbook.ini`

Read in this order, later files overriding earlier ones: `/etc/runbook.ini`, `~/.runbook.ini`,
`./.runbook.ini`, then `$RUNBOOK_CONFIG` or `--config FILE`. Command-line options override everything.
`runbook init-config ~/.runbook.ini` writes a commented template.

```ini
[runbook]
output_dir = /root/runbook-results
max_output = 65536        ; bytes kept per stream (head + tail), 0 = all
confirm = dangerous       ; none | dangerous | all
timeout = 60
stream = auto             ; live output: auto | true | false
color = auto              ; auto | always | never (NO_COLOR honoured)
strip_ansi = true
redact =
    (?i)(password|passwd|secret|token)\s*[:=]\s*\S+

[vars]
backup_dir = /srv/backups
```

Unknown keys are errors, so a typo never goes unnoticed.

## The report

```json
{
  "runbook_version": "1.0.0", "schema_version": 1,
  "plan_name": "nginx-check", "plan_sha256": "…", "host": "web01", "os": "Debian GNU/Linux 13 (trixie)",
  "started": "2026-11-19T09:15:00-05:00", "duration_s": 0.4, "stopped_at": null, "interrupted": false,
  "summary": { "ok": 3, "total": 3 },
  "results": [
    { "id": "test", "cmd": "nginx -t", "status": "ok", "rc": 0, "duration_s": 0.05, "attempts": 1,
      "stdout": "", "stderr": "nginx: configuration file /etc/nginx/nginx.conf test is successful\n",
      "stdout_bytes": 0, "stderr_bytes": 64, "truncated": false }
  ]
}
```

`runbook report FILE` prints a summary table with the output of failed commands;
`--format markdown` gives the same for a ticket or a chat.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every command ended `ok` (or was skipped) |
| 1 | at least one command `failed`, timed out or could not start |
| 2 | invalid plan, configuration or arguments |
| 3 | `requires` not met: nothing was run |
| 4 | another run holds the lock (`--no-lock` to bypass) |
| 130 | interrupted (Ctrl-C or SIGTERM); the partial report is written |

## Working with an AI assistant

The format is meant to be written by a language model and reviewed by a human. See
[docs/ai-assistants.md](docs/ai-assistants.md) for a ready-to-paste instruction block and the rules
that make generated plans safe to run.

## License

MIT, © Emmanuel Bocquet.
