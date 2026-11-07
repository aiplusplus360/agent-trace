# agent-trace

**Turn an agent run into a readable waterfall. Parse spans, roll up tokens and cost, and render a self-contained HTML report — no server, no CDN, no account.**

[![CI](https://github.com/aiplusplus360/agent-trace/actions/workflows/ci.yml/badge.svg)](https://github.com/aiplusplus360/agent-trace/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

```console
$ agent-trace render examples/example-trace.jsonl --format text

  research-agent  7.40s  100.0%  agent
    plan (deepseek-v4-pro)  1.20s  16.2%  llm  1060 tok
    tool fan-out  2.90s  39.2%  step
      search: pricing 2026  800ms  10.8%  tool
      search: competitor docs  600ms   8.1%  tool
!     fetch: example.com/pricing  1.40s  18.9%  tool
      fetch: retry (2/3)  800ms  10.8%  tool
      parse: html -> text  300ms   4.1%  tool
    context assembly  300ms   4.1%  step
    synthesise (deepseek-v4-flash)  3.00s  40.5%  llm  4710 tok

10 spans · 7.40s · 5,770 tokens · $0.0292 · 1 failed
```

---

## Why this exists

An agent that finishes in 7 seconds and costs 3 cents is a different product from one that takes 40 seconds and costs 9 cents. The difference is always visible in the trace, and almost never looked at — because the traces are 8,000-line JSONL files.

The questions that matter are visual: **where did the time go, what ran in parallel, which call failed, and what did it cost.** A waterfall answers all four in one glance.

Existing APM tools answer this well, and they all want an account, an API key, and a network egress rule. This is the version you can run on a recorded file during an incident, offline, in one command.

## What it does

- **Parses what you already have.** JSONL or a JSON array. Generic spans, or OpenTelemetry-shaped spans with `span_id` / `start_time_unix_nano`. No adapter needed.
- **Normalizes time automatically.** Unix nanoseconds, microseconds, milliseconds and seconds are distinguished by magnitude, then rebased so the run starts at zero.
- **Survives broken traces.** A crashed agent never writes its closing event — so unterminated spans are closed at the last known timestamp instead of dropped, and orphans are reattached with a note. The trace you most need is the one that failed.
- **Rolls up cost and tokens without double counting.** Handles both tracer shapes: per-call metrics on leaves, and aggregates on the parent.
- **Renders one HTML file.** Inline CSS, no JavaScript, no external requests. Attach it to a bug report.

## Install

No dependencies — `json`, `pathlib`, `html`.

```bash
pip install git+https://github.com/aiplusplus360/agent-trace
```

Or run from a checkout: `python -m agenttrace render trace.jsonl`

## Usage

```bash
agent-trace render run.jsonl -o trace.html     # self-contained waterfall
agent-trace render run.jsonl --format text     # terminal tree
agent-trace render run.jsonl --format json     # normalized spans
agent-trace summarize run.jsonl                # rollups only
agent-trace render run.jsonl --fail-on-error   # exit 1 if any span failed
```

`-` reads stdin, so it composes:

```bash
cat run.jsonl | agent-trace render - --format text
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | parsed and rendered |
| `1` | `--fail-on-error` set and a span failed |
| `2` | unreadable or unparseable trace |

`--fail-on-error` is the useful one in CI: record a trace from a test run, then assert it had no failures.

## Input formats

### Generic

```json
{"id": "run", "name": "agent", "start": 0.0, "end": 7.4, "kind": "agent"}
{"id": "plan", "name": "plan", "start": 0.0, "end": 1.2, "parent": "run", "kind": "llm",
 "tokens_in": 820, "tokens_out": 240, "cost": 0.0082}
```

Only `id` and `name` are really required. Missing timestamps default to zero.

### OpenTelemetry-shaped

```json
{"span_id": "a1b2", "parent_span_id": "f0", "name": "llm.complete",
 "start_time_unix_nano": 1700000000000000000,
 "end_time_unix_nano":   1700000002000000000}
```

### Accepted key aliases

| Concept | Keys tried |
|---|---|
| id | `id`, `span_id`, `spanId`, `event_id` |
| parent | `parent`, `parent_id`, `parentId`, `parent_span_id`, `parentSpanId` |
| name | `name`, `operation`, `label`, `event`, `type` |
| start / end | `start`, `start_time`, `start_time_unix_nano`, `timestamp` / `end`, `end_time`, `end_time_unix_nano` |
| tokens | `tokens_in`, `input_tokens`, `prompt_tokens` / `tokens_out`, `output_tokens`, `completion_tokens` |
| cost | `cost`, `cost_usd`, `total_cost` |

Anything unrecognised is kept as a span attribute rather than discarded.

## Design notes

**Unterminated spans are closed, not dropped.** The common instinct is to discard incomplete spans. That is exactly backwards: a trace with a missing end event is a crashed run, and the crash is what you're investigating.

**Cycles are detected.** `a → b → a` would make a recursive walk infinite. Orphans of a cycle are detached to the root rather than deleted, so nothing disappears.

**Cost uses leaves, with an aggregate fallback.** Summing every span double counts a tracer that records cost on both the parent and its children; summing only roots reports `$0.00` for the far more common leaf-only shape. Summing leaves, with a root fallback, handles both — and the first version of this tool got it wrong, which is now a test.

**Parallelism is reported next to wall clock.** Leaf time divided by wall time. Without it, "the spans sum to 8.4s but the run was 7.4s" looks like a bug rather than the parallel fan-out it is.

**Every user-supplied string is HTML-escaped.** Span names come from application code and routinely contain model output, which routinely contains whatever a user typed. A debugging tool that renders that as markup is an injection bug in the worst possible place.

**Bar geometry is clamped to the viewport.** Clock skew between workers can place a span outside the run window; unclamped percentages push bars off the panel and they silently vanish.

## Development

```bash
python -m unittest discover -s tests -v   # 55 tests
python examples/make_trace.py             # regenerate the example
```

Tested on Python 3.9, 3.11 and 3.13 in CI, which also renders the example trace and fails if the report does not contain the expected spans.

## License

[MIT](LICENSE)
