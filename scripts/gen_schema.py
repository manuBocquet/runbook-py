"""Regenerates schema/runbook-plan.schema.json from runbook_py.schema (run before a release)."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from runbook_py.schema import PLAN_SCHEMA  # noqa: E402

with open(os.path.join(ROOT, "schema", "runbook-plan.schema.json"), "w") as handle:
    json.dump(PLAN_SCHEMA, handle, indent=2)
    handle.write("\n")
