#!/usr/bin/env python3
"""Agent 1 entry point (the scheduled Cloud Run job runs this).

All logic lives in workflow.py: code makes every decision (freshness rule, Layer 2 gate, verification) and
the LLM explains each step and writes the report. See workflow.py and the README.

Run:  python3 -I agent1.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow  # noqa: E402

if __name__ == "__main__":
    run = workflow.run_auto()
    sys.exit(0 if run.status in ("done", "attention") else 1)
