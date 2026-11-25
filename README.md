# runbook-py

[![Python](https://img.shields.io/badge/Python-v3-lightgrey?logo=python&logoColor=fff&labelColor=3776AB)](#)

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
- **Remote targets over SSH**: with a `"remote"` block (or `--remote`), the commands run on another
  host through `ssh`. Python is only needed where `runbook` runs: firewalls (pfSense, OPNsense),
  appliances and minimal hosts only need `sshd` and `/bin/sh`.
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
`requires` (`root`, `os`, `commands`), `remote`, `commands`. The full JSON Schema: `runbook schema`, also in
[`schema/runbook-plan.schema.json`](schema/runbook-plan.schema.json).

### Statuses

`ok`, `failed` (exit code not in `expect_rc`), `timeout`, `error` (could not start, undefined
variable), `skipped` (with a `reason`), `declined` (answered *no* at the confirmation).

## Variables and captures

`{{name}}` is replaced in `cmd`, `cwd`, `env`, `skip_if` and `stdin`. Sources, lowest priority first:

1. built-ins: `host`, `fqdn`, `user`, `date` (YYYYMMDD), `time`, `timestamp`, `plan_name`, `runner_host` (with a remote target, `host` and `fqdn` name the remote host)
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

## Remote execution over SSH

Add a `"remote"` block and every command, `skip_if` test and `requires` check runs on that host
through `ssh`, while the plan, the confirmations and the `.result.json` stay on the machine where you
run `runbook`. Python is not needed on the target.

```json
{
  "name": "pfsense-check",
  "remote": {
    "host": "{{fw_host}}",
    "user": "admin",
    "port": 22,
    "identity_file": "~/.ssh/id_ed25519_runbook",
    "ssh_options": ["StrictHostKeyChecking=yes"],
    "shell": "/bin/sh",
    "connect_timeout": 10
  },
  "vars": { "fw_host": "192.168.1.1" },
  "commands": [ { "id": "version", "cmd": "cat /etc/version" } ]
}
```

Only `host` is required. The same plan can target another host, or run locally, from the command line:

```bash
runbook plan.json --remote admin@fw2.example --port 2222 -i ~/.ssh/id_fw --ssh-option ProxyJump=bastion
runbook plan.json --local                    # ignore the plan's remote block
runbook plan.json --var fw_host=10.0.0.1     # variables work in every remote field
```

How it works:

- Each command becomes a small POSIX `sh` script written on the standard input of
  `ssh <target> exec /bin/sh -s`. The remote login shell only parses that fixed line, so a tcsh or
  csh login shell (pfSense, FreeBSD root) or a menu shell never misreads `2>/dev/null`, `[x]`, `!` or quotes.
  `remote.shell` picks the shell that runs the commands (`/bin/sh` by default; `/bin/bash` where available).
- Commands behave as locally: `env` and captures are exported, `cwd` is applied, stdin is `/dev/null`
  unless `stdin` is set (sent byte for byte), argv commands (`"shell": false`) are quoted for you.
- One SSH connection is shared by the whole plan (`ControlMaster`, private socket directory, closed at
  the end). ssh runs with `BatchMode=yes`: no password or host-key prompt can block a run, so use a
  key and a host already in `known_hosts`.
- On a timeout the local ssh is killed, and the remote command is also wrapped in `timeout` (when the
  target has it) a few seconds longer, so nothing is left running there.
- Exit code 255 is ssh's own error code (connection refused, authentication, host key): the command
  ends `error` with ssh's message, unless 255 is in its `expect_rc`. `retries` apply.
- Before the first command, runbook connects once and reads the target's uid, OS, hostname and
  missing `requires.commands`; if the host cannot be reached, nothing runs (exit code 3).
- `{{host}}` and `{{fqdn}}` name the remote host; `{{runner_host}}` is the machine running runbook.
  The report gains `remote` (target, port, key, shell, options) and `runner` blocks, and its `host`,
  `os`, `kernel` and `uid` describe the target.
- `ssh_options` and `--ssh-option` are passed as `-o` before runbook's own options, so they win
  (for ssh the first value of an option is used), e.g. `ControlMaster=no` or `ProxyJump=bastion`.
- The ssh client is `ssh` from `PATH`, or `ssh = /path/to/ssh` in `.runbook.ini`.

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
ssh = ssh                 ; ssh client for remote targets
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
