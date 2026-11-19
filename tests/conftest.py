import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


class Runbook(object):
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = SRC + os.pathsep + self.env.get("PYTHONPATH", "")
        self.env["HOME"] = str(tmp_path / "home")
        self.env.pop("RUNBOOK_CONFIG", None)
        os.makedirs(self.env["HOME"], exist_ok=True)

    def plan(self, data, name="plan.json"):
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return str(path)

    def __call__(self, *args, **kw):
        cmd = [sys.executable, "-m", "runbook_py"] + [str(a) for a in args]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                              cwd=str(self.tmp), universal_newlines=True, timeout=kw.get("timeout", 60))
        return proc

    def run(self, data, *extra):
        out = self.tmp / "result.json"
        if out.exists():
            out.unlink()
        proc = self(self.plan(data), "--no-lock", "-y", "--no-stream", "-o", out, *extra)
        result = json.loads(out.read_text()) if out.exists() else None
        return proc, result


@pytest.fixture
def rb(tmp_path):
    return Runbook(tmp_path)


def by_id(result):
    return {e["id"]: e for e in result["results"]}
