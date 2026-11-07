"""CLI.

    agent-trace render run.jsonl -o trace.html
    agent-trace render run.jsonl --format text
    agent-trace summarize run.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .analyze import summarize
from .parse import ParseError, parse_events
from .render import render_html, render_json, render_text

EXIT_OK = 0
EXIT_PARSE = 1
EXIT_USAGE = 2


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ParseError(f"cannot read {path}: {exc}") from exc


def cmd_render(args: argparse.Namespace) -> int:
    run = parse_events(_read(args.trace))

    if args.format == "html":
        output = render_html(run, title=args.title or f"Trace — {Path(args.trace).name}")
    elif args.format == "json":
        output = render_json(run)
    else:
        output = render_text(run)

    if args.output and args.format == "html":
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"wrote {args.output}  ({len(output):,} bytes)")
    else:
        print(output)

    for warning in run.warnings:
        print(f"note: {warning}", file=sys.stderr)

    # A trace with a failed span is a failed run. Surfacing that in the exit
    # code lets CI assert on a recorded trace, which is the whole point of
    # keeping one.
    if args.fail_on_error and any(s.failed for s in run.all_spans):
        return EXIT_PARSE
    return EXIT_OK


def cmd_summarize(args: argparse.Namespace) -> int:
    run = parse_events(_read(args.trace))
    stats = summarize(run)

    if args.json:
        import json

        print(json.dumps(stats, indent=2))
        return EXIT_OK

    print(f"duration     {stats['duration_s']}s")
    print(f"spans        {stats['spans']}")
    print(f"leaf time    {stats['leaf_time_s']}s  ({stats['parallelism']}x parallel)")
    print(f"tokens       {stats['tokens']:,}  ({stats['tokens_in']:,} in / {stats['tokens_out']:,} out)")
    print(f"cost         ${stats['cost']:.4f}")
    if stats["failed"]:
        print(f"failed       {stats['failed']}  ({', '.join(stats['failed_ids'])})")
    if stats["slowest"]:
        print(f"slowest      {stats['slowest']}  {stats['slowest_s']}s")

    by_kind = stats.get("by_kind") or {}
    if by_kind:
        print("\nby kind")
        for kind, data in sorted(by_kind.items(), key=lambda kv: -kv[1]["duration_s"]):
            print(
                f"  {kind:<10} {data['count']:>3} spans  "
                f"{data['duration_s']:>8.3f}s  {data['tokens']:>7} tokens"
            )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-trace",
        description="Read an agent run: waterfall, rollups, and cost.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="render a trace")
    render.add_argument("trace", help="trace file (JSONL or JSON), or - for stdin")
    render.add_argument("-o", "--output", help="write HTML here instead of stdout")
    render.add_argument(
        "--format", choices=["html", "text", "json"], default="html"
    )
    render.add_argument("--title", help="report title")
    render.add_argument(
        "--fail-on-error",
        action="store_true",
        help="exit 1 if any span failed (for CI assertions)",
    )
    render.set_defaults(func=cmd_render)

    summarize_cmd = sub.add_parser("summarize", help="print rollups only")
    summarize_cmd.add_argument("trace")
    summarize_cmd.add_argument("--json", action="store_true")
    summarize_cmd.set_defaults(func=cmd_summarize)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
