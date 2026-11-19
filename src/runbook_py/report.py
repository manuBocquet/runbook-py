"""Human-readable summary of a .result.json file (text or Markdown)."""

import json

BAD = ("failed", "timeout", "error")


def _tail(text, lines):
    parts = (text or "").rstrip("\n").splitlines()
    if len(parts) > lines:
        return ["[... %d lines above ...]" % (len(parts) - lines)] + parts[-lines:]
    return parts


def load_result(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or "results" not in data:
        raise ValueError("%s is not a runbook result file" % path)
    return data


def render_report(data, fmt="text", lines=20, show_all=False):
    summary = data.get("summary", {})
    counts = ", ".join("%s=%s" % (k, v) for k, v in sorted(summary.items()) if k != "total")
    head = [
        ("Plan", data.get("plan_name")),
        ("Host", "%s (%s, kernel %s)" % (data.get("host"), data.get("os"), data.get("kernel"))),
        ("Started", data.get("started")),
        ("Duration", "%ss" % data.get("duration_s")),
        ("Summary", "%s of %s commands" % (counts, summary.get("total"))),
    ]
    if data.get("stopped_at"):
        head.append(("Stopped at", data["stopped_at"]))
    if data.get("interrupted"):
        head.append(("Interrupted", "yes"))
    if data.get("aborted"):
        head.append(("Aborted", data["aborted"]))

    rows = []
    for entry in data.get("results", []):
        rc = entry.get("rc")
        note = entry.get("reason") or entry.get("error") or ""
        rows.append((entry.get("id", ""), entry.get("status", ""), "" if rc is None else str(rc),
                     "" if entry.get("duration_s") is None else "%.2f" % entry["duration_s"], note))

    details = [e for e in data.get("results", []) if show_all or e.get("status") in BAD]
    out = []
    if fmt == "markdown":
        out.append("# Runbook report: %s\n" % data.get("plan_name"))
        out.extend("- **%s:** %s" % (k, v) for k, v in head)
        out.append("\n| id | status | rc | seconds | note |\n| --- | --- | --- | --- | --- |")
        out.extend("| `%s` | %s | %s | %s | %s |" % (r[0], r[1], r[2], r[3], r[4].replace("|", "\\|")) for r in rows)
        for entry in details:
            out.append("\n## `%s` — %s\n" % (entry["id"], entry.get("status")))
            out.append("```bash\n%s\n```" % entry.get("cmd", ""))
            for stream in ("stdout", "stderr"):
                text = entry.get(stream)
                if text:
                    out.append("%s:\n\n```\n%s\n```" % (stream, "\n".join(_tail(text, lines))))
        return "\n".join(out) + "\n"

    width = max([len(r[0]) for r in rows] + [2])
    out.extend("%-11s %s" % (k + ":", v) for k, v in head)
    out.append("")
    out.append("%-*s  %-9s %4s %8s  %s" % (width, "ID", "STATUS", "RC", "SECONDS", "NOTE"))
    out.extend("%-*s  %-9s %4s %8s  %s" % (width, r[0], r[1], r[2], r[3], r[4]) for r in rows)
    for entry in details:
        out.append("")
        out.append("=== %s (%s) $ %s" % (entry["id"], entry.get("status"), entry.get("cmd", "")))
        for stream in ("stdout", "stderr"):
            text = entry.get(stream)
            if text:
                out.append("--- %s" % stream)
                out.extend(_tail(text, lines))
    return "\n".join(out) + "\n"
