"""Self-contained HTML waterfall.

No CDN, no build step, no server. The output is one file you can email, attach
to a bug report, or open from a `file://` URL — which matters, because a trace
viewer that needs infrastructure never gets used during an incident.

Everything user-supplied is HTML-escaped. A span name comes from application
code and often contains model output, which often contains whatever a user
typed; treating that as markup would be an injection bug in a debugging tool.
"""

from __future__ import annotations

import html
import json
from typing import Dict, List

from .analyze import Run, Span, summarize

_CSS = """
:root {
  --bg: #0e1117; --panel: #161b24; --line: #222a36;
  --text: #d7dde8; --dim: #7f8b9e;
  --ok: #3fb950; --warn: #d29922; --fail: #f85149;
  --bar: #2f81f7; --bar-alt: #8957e5; --bar-llm: #3fb950;
  --bar-tool: #d29922; --bar-fail: #f85149;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--bg); color: var(--text);
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
h1 { font-size: 16px; margin: 0 0 4px; font-weight: 600; letter-spacing: .01em; }
.sub { color: var(--dim); margin-bottom: 20px; font-size: 12px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
         gap: 10px; margin-bottom: 22px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
        padding: 12px 14px; }
.card .k { color: var(--dim); font-size: 10px; text-transform: uppercase;
           letter-spacing: .07em; }
.card .v { font-size: 19px; font-weight: 600; margin-top: 4px; }
.card.fail .v { color: var(--fail); }
.card.ok .v { color: var(--ok); }
.legend { display: flex; gap: 16px; flex-wrap: wrap; color: var(--dim);
          font-size: 11px; margin-bottom: 12px; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px;
            margin-right: 5px; vertical-align: middle; }
.wrap { background: var(--panel); border: 1px solid var(--line);
        border-radius: 8px; overflow: hidden; }
.head, .row { display: grid; grid-template-columns: minmax(200px, 32%) 1fr 78px;
              align-items: center; }
.head { border-bottom: 1px solid var(--line); color: var(--dim); font-size: 10px;
        text-transform: uppercase; letter-spacing: .07em; }
.head > div, .row > div { padding: 7px 12px; }
.row { border-bottom: 1px solid rgba(34,42,54,.55); }
.row:last-child { border-bottom: 0; }
.row:hover { background: rgba(47,129,247,.06); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.name .kind { color: var(--dim); font-size: 10px; margin-left: 6px; }
.track { position: relative; height: 18px; }
.bar { position: absolute; top: 4px; height: 10px; border-radius: 3px;
       background: var(--bar); min-width: 2px; }
.bar.kind-llm { background: var(--bar-llm); }
.bar.kind-tool { background: var(--bar-tool); }
.bar.kind-agent, .bar.kind-chain { background: var(--bar-alt); }
.bar.failed { background: var(--bar-fail); }
.dur { text-align: right; color: var(--dim); font-size: 12px; }
.fail .name { color: var(--fail); }
details { margin-top: 18px; background: var(--panel); border: 1px solid var(--line);
          border-radius: 8px; padding: 10px 14px; }
summary { cursor: pointer; color: var(--dim); font-size: 11px;
          text-transform: uppercase; letter-spacing: .07em; }
pre { overflow-x: auto; color: var(--dim); font-size: 11px; margin: 10px 0 0; }
.warn { border-left: 3px solid var(--warn); padding: 6px 10px; margin: 4px 0;
        color: var(--warn); font-size: 11px; background: rgba(210,153,34,.07); }
footer { color: var(--dim); font-size: 11px; margin-top: 20px; }
"""

# Colour by kind: a bar for an LLM call should not look like a bar for a tool.
_KIND_CLASS = {
    "llm": "kind-llm",
    "model": "kind-llm",
    "completion": "kind-llm",
    "tool": "kind-tool",
    "function": "kind-tool",
    "agent": "kind-agent",
    "chain": "kind-chain",
}


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m{rest:.0f}s"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _rows(spans: List[Span], total: float, depth: int = 0) -> str:
    out = []
    for span in spans:
        percent = (span.duration / total * 100) if total else 0.0
        offset = (span.start / total * 100) if total else 0.0
        kind_class = _KIND_CLASS.get(span.kind, "")
        failed = " failed" if span.failed else ""
        row_class = "row failed" if span.failed else "row"

        # A span that starts before the window (clock skew) would render off the
        # left edge; clamping keeps it visible instead of invisible.
        offset = max(0.0, min(offset, 100.0))
        width = max(0.15, min(percent, 100.0 - offset))

        indent = f'<span style="opacity:{max(0.35, 1 - depth * 0.16)}">{"│  " * depth}</span>' if depth else ""
        kind = f'<span class="kind">{_esc(span.kind)}</span>' if span.kind != "step" else ""

        title = _esc(
            f"{span.name} · {_fmt_duration(span.duration)} · "
            f"{span.tokens} tokens · ${span.cost:.4f}"
        )
        out.append(
            f'<div class="{row_class}">'
            f'<div class="name" title="{title}">{indent}{_esc(span.name)}{kind}</div>'
            f'<div class="track">'
            f'<div class="bar {kind_class}{failed}" style="left:{offset:.3f}%;'
            f'width:{width:.3f}%" title="{title}"></div>'
            f"</div>"
            f'<div class="dur">{_fmt_duration(span.duration)}</div>'
            f"</div>"
        )
        if span.children:
            out.append(_rows(span.children, total, depth + 1))
    return "".join(out)


def render_html(run: Run, *, title: str = "Agent trace") -> str:
    stats = summarize(run)
    total = run.duration or 1.0

    cards = [
        ("duration", _fmt_duration(stats["duration_s"]), ""),
        ("spans", str(stats["spans"]), ""),
        ("tokens", f"{stats['tokens']:,}", ""),
        ("cost", f"${stats['cost']:.4f}", ""),
        (
            "failed",
            str(stats["failed"]),
            "fail" if stats["failed"] else "ok",
        ),
        ("parallelism", f"{stats['parallelism']}x", ""),
    ]
    cards_html = "".join(
        f'<div class="card {cls}"><div class="k">{_esc(k)}</div>'
        f'<div class="v">{_esc(v)}</div></div>'
        for k, v, cls in cards
    )

    legend = (
        '<div class="legend">'
        '<span><i style="background:var(--bar)"></i>step</span>'
        '<span><i style="background:var(--bar-llm)"></i>llm</span>'
        '<span><i style="background:var(--bar-tool)"></i>tool</span>'
        '<span><i style="background:var(--bar-alt)"></i>agent</span>'
        '<span><i style="background:var(--bar-fail)"></i>failed</span>'
        "</div>"
    )

    warnings = ""
    if run.warnings:
        body = "".join(f'<div class="warn">{_esc(w)}</div>' for w in run.warnings)
        warnings = f"<details><summary>{len(run.warnings)} parser note(s)</summary>{body}</details>"

    slowest = (
        f"<p class=\"sub\">slowest: {_esc(stats['slowest'])} "
        f"({_fmt_duration(stats['slowest_s'])})</p>"
        if stats["slowest"]
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{_esc(title)}</h1>
{slowest}
<div class="cards">{cards_html}</div>
{legend}
<div class="wrap">
  <div class="head"><div>span</div><div>timeline</div><div class="dur">dur</div></div>
  {_rows(run.roots, total)}
</div>
{warnings}
<footer>agent-trace · {stats['spans']} spans · {_fmt_duration(stats['duration_s'])} wall clock ·
{stats['leaf_time_s']}s in leaves</footer>
</body>
</html>
"""


def render_json(run: Run) -> str:
    return json.dumps(
        {
            "summary": summarize(run),
            "warnings": run.warnings,
            "spans": [
                {
                    "id": s.id,
                    "name": s.name,
                    "kind": s.kind,
                    "parent": s.parent,
                    "start": round(s.start, 4),
                    "duration": round(s.duration, 4),
                    "status": s.status,
                    "tokens_in": s.tokens_in,
                    "tokens_out": s.tokens_out,
                    "cost": s.cost,
                }
                for s in run.all_spans
            ],
        },
        indent=2,
    )


def render_text(run: Run) -> str:
    """Plain tree for a terminal or a CI log."""
    stats = summarize(run)
    lines = []
    total = run.duration or 1.0

    def walk(spans: List[Span], depth: int = 0) -> None:
        for span in spans:
            mark = "!" if span.failed else " "
            pad = "  " * depth
            share = span.duration / total * 100 if total else 0
            lines.append(
                f"{mark} {pad}{span.name}  {_fmt_duration(span.duration)}  "
                f"{share:4.1f}%  {span.kind}"
                + (f"  {span.tokens} tok" if span.tokens else "")
            )
            walk(span.children, depth + 1)

    walk(run.roots)
    lines.append("")
    lines.append(
        f"{stats['spans']} spans · {_fmt_duration(stats['duration_s'])} · "
        f"{stats['tokens']:,} tokens · ${stats['cost']:.4f}"
        + (f" · {stats['failed']} failed" if stats["failed"] else "")
    )
    return "\n".join(lines)
