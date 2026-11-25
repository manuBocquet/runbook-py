# Generating plans with an AI assistant

runbook-py started as the glue between a sysadmin and an AI assistant: instead of "run these
twelve commands and paste me the output", the assistant writes a plan, the human reviews and runs
it, and the assistant reads one `.result.json` file. The loop is short, nothing gets lost in
copy-paste, and every exit code is visible.

## The loop

1. Describe the goal and the server to the assistant.
2. It answers with a plan file. Read it, or at least `runbook validate plan.json` and
   `runbook plan.json --dry-run`.
3. Run it: `runbook plan.json`. Answer the confirmations for `dangerous` commands.
4. Give the `.result.json` file back to the assistant. It reads the results and proposes the next plan.

Start with a read-only inventory plan: the assistant then writes the following plans against the
real state of the machine instead of guessing.

## Instructions to paste into your assistant

Put this in your assistant's custom instructions, project instructions or memory:

```text
When I need to run commands on one of my machines, do not give me commands to copy and paste.
Write a runbook-py plan (JSON) that I will run with `runbook plan.json`; I will send you back
the .result.json file it produces.

Rules for plans:
- Top level: "name", "description", optional "defaults", "vars", "redact", "requires", "remote", and "commands".
- For a host without Python (firewall, appliance), add "remote": {"host", "user", "identity_file"}
  and I run the plan from another machine; commands then run with /bin/sh on that host, not its login shell.
- Each command: "id" (short, unique), "cmd", and when useful "description", "timeout",
  "stop_on_error", "expect_rc", "only_if", "skip_if", "capture", "retries", "env", "cwd", "stdin".
- Mark "dangerous": true on every command that changes the system (writes files, installs,
  restarts services, reboots, deletes). Read-only commands are never dangerous.
- Stdin is /dev/null: never use interactive commands; add -y / --non-interactive flags.
- Use "expect_rc" when a non-zero exit code is a normal answer (grep: [0, 1]; systemctl is-active: [0, 3]).
- Use "stop_on_error": false for independent read-only checks, true before anything that depends on them.
- Use "only_if" to chain a step to the success of an earlier one, "skip_if" to make a step idempotent,
  "capture" to reuse an output in later steps ({{NAME}} or $NAME), instead of repeating a pipeline.
- Put host-specific values in "vars" with {{name}} so the same plan works on several servers.
- Long or risky operations (dist-upgrade, migrations) go in their own plan, with a preflight plan
  before them and a verification plan after them.
- Never put secrets in a plan; add "redact" regexes when an output may contain one.
- Reboots are left to the human, outside the plan.
```

## Things worth asking the assistant to check in a result

- `summary` first, then every entry whose `status` is not `ok`.
- `stderr` of commands that ended `ok`: warnings often hide there.
- `truncated: true`: the output was cut to head + tail; ask for a narrower command if the middle matters.
- `plan_sha256`: proves which version of the plan produced the result.

## Validating generated plans

`runbook schema` prints the JSON Schema (draft-07). Editors such as VS Code validate and
autocomplete a plan that starts with:

```json
{ "$schema": "https://raw.githubusercontent.com/manuBocquet/runbook-py/main/schema/runbook-plan.schema.json" }
```

`runbook validate` goes further than the schema: it checks that `only_if` names earlier commands
and that every `{{variable}}` is defined, by the plan, `.runbook.ini`, `--var` or an earlier `capture`.
