"""Parsing traces.

Accepts JSONL (one object per event) or a JSON array, in two shapes:

  generic   {"id", "name", "start", "end", "parent", "kind", ...}
  otel-ish  {"trace_id", "span_id", "parent_span_id", "name",
             "start_time_unix_nano", "end_time_unix_nano"}

The point is to be feedable from anything without an adapter layer, because a
trace you have to convert before you can read it is a trace you don't read.

Timestamps are normalized to seconds relative to the run start. Absolute
timestamps (unix seconds, milliseconds, or nanoseconds) are accepted and
converted — nanosecond integers are the single most common input, since that is
what OpenTelemetry emits.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from .analyze import Run, Span, build_tree


class ParseError(ValueError):
    """The input could not be interpreted as a trace."""


# Names for the same concept across ecosystems. Checked in order.
_ID_KEYS = ("id", "span_id", "spanId", "event_id")
_PARENT_KEYS = ("parent", "parent_id", "parentId", "parent_span_id", "parentSpanId")
_NAME_KEYS = ("name", "operation", "label", "event", "type")
_START_KEYS = ("start", "start_time", "startTime", "start_time_unix_nano", "timestamp")
_END_KEYS = ("end", "end_time", "endTime", "end_time_unix_nano", "finished_at")
_KIND_KEYS = ("kind", "span_kind", "type")
_STATUS_KEYS = ("status", "level", "outcome")
_IN_TOKENS = ("tokens_in", "input_tokens", "prompt_tokens", "inputTokens")
_OUT_TOKENS = ("tokens_out", "output_tokens", "completion_tokens", "outputTokens")
_COST_KEYS = ("cost", "cost_usd", "total_cost")


def _first(event: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in event and event[key] is not None:
            return event[key]
    return None


def _as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _normalize_time(value: Any) -> Optional[float]:
    """Convert a timestamp to seconds.

    Heuristic by magnitude, which is unambiguous in practice:

        < 1e11   unix seconds            (year 5138)
        < 1e14   unix milliseconds
        < 1e17   unix microseconds
        else     unix nanoseconds

    Relative offsets (small numbers, e.g. 0.0 and 1.5) fall in the first band
    and pass through unchanged, which is what we want.
    """
    number = _as_number(value)
    if number is None:
        return None
    magnitude = abs(number)
    if magnitude >= 1e17:
        return number / 1e9
    if magnitude >= 1e14:
        return number / 1e6
    if magnitude >= 1e11:
        return number / 1e3
    return number


def _to_events(text: str) -> List[Dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []

    # A JSON array is a convenience for tests and for anyone who dumped their
    # span list with json.dump.
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ParseError(f"input looks like a JSON array but does not parse: {exc}") from exc
        if not isinstance(data, list):
            raise ParseError("top-level JSON array expected")
        return [e for e in data if isinstance(e, dict)]

    events: List[Dict[str, Any]] = []
    for number, line in enumerate(stripped.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParseError(f"line {number} is not valid JSON: {exc}") from exc
        if isinstance(event, dict):
            events.append(event)
        elif isinstance(event, list):
            events.extend(e for e in event if isinstance(e, dict))
    return events


def parse_events(text: str) -> Run:
    """Parse trace text into a tree of spans."""
    events = _to_events(text)
    if not events:
        raise ParseError("no events found")

    warnings: List[str] = []
    spans: Dict[str, Span] = {}

    for index, event in enumerate(events):
        raw_id = _first(event, _ID_KEYS)
        span_id = str(raw_id) if raw_id is not None else f"span-{index}"

        name = _first(event, _NAME_KEYS)
        if name is None:
            name = span_id

        start = _normalize_time(_first(event, _START_KEYS))
        end = _normalize_time(_first(event, _END_KEYS))

        kind = _first(event, _KIND_KEYS)
        kind = str(kind).lower() if kind is not None and kind != name else "step"
        # Some formats reuse `type` for the name; in that case kind is the
        # span's own label and is not useful as a category.
        if kind == str(name).lower():
            kind = "step"

        parent = _first(event, _PARENT_KEYS)
        parent_id = str(parent) if parent is not None else None

        status = _first(event, _STATUS_KEYS)
        status = str(status) if status is not None else "ok"

        existing = spans.get(span_id)
        if existing is not None:
            # Start and end arrive as separate events in per-event streams.
            # Merging is the correct reading; overwriting loses the duration.
            if start is not None:
                existing.start = start
            if end is not None:
                existing.end = end
            if parent_id:
                existing.parent = parent_id
            if status != "ok":
                existing.status = status
            existing.tokens_in = max(
                existing.tokens_in, int(_as_number(_first(event, _IN_TOKENS)) or 0)
            )
            existing.tokens_out = max(
                existing.tokens_out, int(_as_number(_first(event, _OUT_TOKENS)) or 0)
            )
            cost = _as_number(_first(event, _COST_KEYS))
            if cost:
                existing.cost = max(existing.cost, cost)
            existing.attributes.update(
                {k: v for k, v in event.items() if k not in _RESERVED}
            )
            continue

        spans[span_id] = Span(
            id=span_id,
            name=str(name),
            kind=kind,
            start=start if start is not None else 0.0,
            end=end,
            parent=parent_id,
            status=status,
            tokens_in=int(_as_number(_first(event, _IN_TOKENS)) or 0),
            tokens_out=int(_as_number(_first(event, _OUT_TOKENS)) or 0),
            cost=_as_number(_first(event, _COST_KEYS)) or 0.0,
            attributes={k: v for k, v in event.items() if k not in _RESERVED},
        )

    ordered = list(spans.values())

    # Anything still open is closed at the latest known time. A crashed agent
    # is exactly the trace worth reading, so it must not be discarded.
    closed = [s.end for s in ordered if s.end is not None]
    latest = max(closed) if closed else 0.0
    for span in ordered:
        if span.end is None:
            warnings.append(
                f"span {span.id!r} has no end event; closed at {latest:.3f}s"
            )
            span.end = latest

    # Rebase so the earliest span starts at zero. Absolute traces otherwise
    # render as a single sliver at the far right of the timeline.
    earliest = min((s.start for s in ordered), default=0.0)
    if earliest:
        for span in ordered:
            span.start -= earliest
            if span.end is not None:
                span.end -= earliest

    roots = build_tree(ordered, warnings)
    return Run(spans=ordered, roots=roots, warnings=warnings)


_RESERVED = set(
    _ID_KEYS
    + _PARENT_KEYS
    + _NAME_KEYS
    + _START_KEYS
    + _END_KEYS
    + _KIND_KEYS
    + _STATUS_KEYS
    + _IN_TOKENS
    + _OUT_TOKENS
    + _COST_KEYS
)
