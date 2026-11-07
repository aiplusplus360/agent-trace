"""Trace analysis for agent runs.

An agent that takes 40 seconds and costs 9 cents per task is a different
product from one that takes 4 seconds and costs 0.4 cents — and the trace is
where that difference is visible. This package makes the trace readable.
"""

__version__ = "0.1.0"

from .analyze import Run, Span, summarize
from .parse import ParseError, parse_events

__all__ = ["ParseError", "Run", "Span", "parse_events", "summarize"]
