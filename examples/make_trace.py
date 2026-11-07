"""Generate a realistic example trace.

Handy for trying the viewer without wiring up a real agent, and used by CI to
prove the CLI works end to end. The shape mirrors a multi-step agent: plan,
then a parallel tool fan-out, then a final synthesis, with one tool failing and
being retried — the exact pattern a waterfall is good at showing.
"""

from __future__ import annotations

import json
from pathlib import Path

SPANS = [
    # id, name, start, end, parent, kind, status, in, out, cost
    ("run", "research-agent", 0.0, 7.4, None, "agent", "ok", 0, 0, 0.0),
    ("plan", "plan (deepseek-v4-pro)", 0.0, 1.2, "run", "llm", "ok", 820, 240, 0.0082),
    ("fan", "tool fan-out", 1.2, 4.1, "run", "step", "ok", 0, 0, 0.0),
    ("t1", "search: pricing 2026", 1.2, 2.0, "fan", "tool", "ok", 0, 0, 0.0),
    ("t2", "search: competitor docs", 1.2, 1.8, "fan", "tool", "ok", 0, 0, 0.0),
    ("t3", "fetch: example.com/pricing", 1.2, 2.6, "fan", "tool", "error", 0, 0, 0.0),
    ("t3r", "fetch: retry (2/3)", 2.6, 3.4, "fan", "tool", "ok", 0, 0, 0.0),
    ("t4", "parse: html -> text", 3.4, 3.7, "fan", "tool", "ok", 0, 0, 0.0),
    ("ctx", "context assembly", 4.1, 4.4, "run", "step", "ok", 0, 0, 0.0),
    ("syn", "synthesise (deepseek-v4-flash)", 4.4, 7.4, "run", "llm", "ok", 4100, 610, 0.0210),
]


def main() -> None:
    lines = []
    for span_id, name, start, end, parent, kind, status, tin, tout, cost in SPANS:
        event = {
            "id": span_id,
            "name": name,
            "start": start,
            "end": end,
            "kind": kind,
            "status": status,
            "tokens_in": tin,
            "tokens_out": tout,
            "cost": cost,
        }
        if parent:
            event["parent"] = parent
        lines.append(json.dumps(event))

    out = Path(__file__).parent / "example-trace.jsonl"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
