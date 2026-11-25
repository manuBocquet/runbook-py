# Changelog

## Unreleased

- Add remote execution over SSH

## 1.1.0 — 2026-11-25

- Remote execution over SSH: a plan-level `"remote"` block (`host`, `user`, `port`, `identity_file`,
  `ssh_options`, `shell`, `connect_timeout`) and the `--remote [USER@]HOST`, `--port`, `-i/--identity`,
  `--ssh-option`, `--local` options. Python is not needed on the target: each command is sent as a
  POSIX sh script on the standard input of `ssh … exec /bin/sh -s`, so tcsh/csh login shells (pfSense,
  FreeBSD) are bypassed. One multiplexed connection per run, `BatchMode=yes`, remote `timeout` backstop,
  rc 255 reported as an ssh `error`, `requires` checked on the target, `remote` and `runner` blocks in the report.
- New built-in variable `{{runner_host}}`; with a remote target `{{host}}` and `{{fqdn}}` name the remote host.
- `.runbook.ini`: new `ssh` key (ssh client path).
- Example: `examples/remote-pfsense-check.json`.

## 1.0.3 — 2026-11-19

- Tests: runbook subprocesses run without a controlling terminal, so `make test` from a terminal no longer waits at a confirmation prompt.
- Correction du processus de release

## 1.0.2 — 2026-11-19

- Development: Makefile (test, build, bump-*, release) and bump-my-version configuration.

## 1.0.1 — 2026-11-19

- The plan schema no longer embeds the package version; tests always import src/.
- Project links (PyPI, schema $id, examples) point to https://github.com/manuBocquet/runbook-py.

## 1.0.0 — 2026-11-19

First public release, from the internal `runbook.py` script used for a Proxmox VE 8 → 9 migration.

- Plans in JSON, one JSON report per run, rewritten after each command.
- English console and prompts: `Run? [y/N/a/q]`.
- `.runbook.ini` configuration (`/etc`, `~`, `./`, `$RUNBOOK_CONFIG`, `--config`) with a `[vars]` section.
- `{{variables}}`: built-ins, plan `vars`, config `[vars]`, `--var`.
- `capture`, `skip_if`, `only_if` with several ids, `retries`, `stdin`, `requires`.
- `--from`, `--only`, `--skip`, `--resume previous.result.json`.
- Live output while capturing, ANSI and carriage-return clean-up, head + tail truncation.
- Lock file against parallel runs; SIGTERM handled like Ctrl-C.
- Subcommands: `run` (default), `validate`, `report` (text / Markdown), `schema`, `init-config`.
- Exit codes: 0, 1, 2, 3 (requirements), 4 (locked), 130 (interrupted).
