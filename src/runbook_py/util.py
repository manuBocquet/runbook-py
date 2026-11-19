"""Small helpers shared by the other modules (no third-party dependencies)."""

import datetime
import json
import os
import re
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def slug(text, maxlen=60):
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-")
    return text[:maxlen] or "runbook"


def sanitize(text):
    """Remove ANSI escape codes and collapse carriage-return progress lines."""
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        text = "\n".join(line.rsplit("\r", 1)[-1] if "\r" in line else line for line in text.split("\n"))
    return text


def redact(text, patterns):
    if not text:
        return text
    for pattern in patterns:
        text = pattern.sub("[REDACTED]", text)
    return text


def write_json_atomic(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


class OutputBuffer(object):
    """Keeps the head and the tail of a stream when it grows past ``limit`` bytes."""

    def __init__(self, limit):
        self.limit = int(limit or 0)
        self.half = self.limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk):
        self.total += len(chunk)
        if self.limit <= 0:
            self.head += chunk
            return
        if len(self.head) < self.half:
            take = self.half - len(self.head)
            self.head += chunk[:take]
            chunk = chunk[take:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.limit - self.half:
                del self.tail[: len(self.tail) - (self.limit - self.half)]

    @property
    def truncated(self):
        return self.total > len(self.head) + len(self.tail)

    def text(self):
        head = bytes(self.head).decode("utf-8", "replace")
        tail = bytes(self.tail).decode("utf-8", "replace")
        if not self.truncated:
            return head + tail
        dropped = self.total - len(self.head) - len(self.tail)
        return head + "\n\n[... %d bytes truncated by runbook ...]\n\n" % dropped + tail


class Console(object):
    """Human-facing output on stderr; stdout is kept for the result file path."""

    def __init__(self, color="auto", quiet=False, stream=sys.stderr):
        self.stream = stream
        self.quiet = quiet
        if color == "always":
            self.color = True
        elif color == "never":
            self.color = False
        else:
            self.color = hasattr(stream, "isatty") and stream.isatty() and not os.environ.get("NO_COLOR")

    def paint(self, text, code):
        if self.color and code:
            return "\033[%sm%s\033[0m" % (code, text)
        return text

    def write(self, text="", code=None, force=False):
        if self.quiet and not force:
            return
        self.stream.write(self.paint(text, code) + "\n")
        self.stream.flush()

    def error(self, text):
        self.write(self.paint("error: ", "31") + text, force=True)

    def warn(self, text):
        self.write(self.paint("warning: ", "33") + text, force=True)
