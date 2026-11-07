"""Span model and rollups.

A trace is a tree, but it arrives flat: events stream out in the order they
happen, and a span's parent may only be known from an id. Building the tree is
the interesting part, because traces are frequently *incomplete* — a crashed
agent never writes its closing event.

So the parser is deliberately tolerant: an unterminated span is closed at the
last known timestamp rather than dropped, and an orphan parent is reattached to
the root with a warning. A debugging tool that refuses to open a broken trace
is useless at exactly the moment you need it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Span:
    id: str
    name: str
    kind: str = "step"
    start: float = 0.0
    end: Optional[float] = None
    parent: Optional[str] = None
    status: str = "ok"
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    attributes: Dict[str, object] = field(default_factory=dict)
    children: List["Span"] = field(default_factory=list)

    @property
    def duration(self) -> float:
        if self.end is None:
            return 0.0
        return max(0.0, self.end - self.start)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def failed(self) -> bool:
        return self.status.lower() not in {"ok", "success", "completed", "done"}

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def depth(self) -> int:
        node, depth = self, 0
        while node.parent is not None:
            depth += 1
            node = node.parent  # type: ignore[assignment]
        return depth


@dataclass
class Run:
    spans: List[Span]
    roots: List[Span]
    warnings: List[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.roots)

    @property
    def all_spans(self) -> List[Span]:
        out: List[Span] = []
        for root in self.roots:
            out.extend(root.walk())
        return out

    @property
    def start(self) -> float:
        return min((s.start for s in self.all_spans), default=0.0)

    @property
    def end(self) -> float:
        return max((s.end for s in self.all_spans if s.end is not None), default=self.start)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def by_kind(self) -> Dict[str, List[Span]]:
        grouped: Dict[str, List[Span]] = {}
        for span in self.all_spans:
            grouped.setdefault(span.kind, []).append(span)
        return grouped


def _rollup(spans: List[Span], attribute: str) -> float:
    """Sum a metric without double counting.

    Traces come in two shapes and both are common:

      leaf-only   cost/tokens recorded on each LLM call
      aggregate   cost/tokens recorded on a parent alongside its children

    Summing every span double counts the aggregate shape. Summing only roots
    reports zero for the leaf-only shape — which is the majority, and was
    exactly the bug this function exists to fix.

    So: sum the leaves. If that is zero, the tracer is aggregate-only and the
    roots hold the total.
    """
    leaves = [s for s in spans if not s.children]
    total = sum(getattr(s, attribute) for s in leaves)
    if total:
        return total
    return sum(getattr(s, attribute) for s in spans if s.parent is None)


def summarize(run: Run) -> Dict[str, object]:
    """Rollups that answer the questions people actually ask about a run."""
    spans = run.all_spans
    failed = [s for s in spans if s.failed]

    # Wall-clock span time is not the same as run time: parallel branches
    # overlap. Reporting both stops the "why is the sum bigger than the run"
    # question before it is asked.
    busy = sum(s.duration for s in spans if not s.children)
    tokens_in = int(_rollup(spans, "tokens_in"))
    tokens_out = int(_rollup(spans, "tokens_out"))
    cost = _rollup(spans, "cost")

    slowest = max(spans, key=lambda s: s.duration, default=None)
    costliest = max(spans, key=lambda s: s.cost, default=None)

    return {
        "spans": len(spans),
        "roots": len(run.roots),
        "duration_s": round(run.duration, 3),
        "leaf_time_s": round(busy, 3),
        "parallelism": round(busy / run.duration, 2) if run.duration else 0.0,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens": tokens_in + tokens_out,
        "cost": round(cost, 6),
        "failed": len(failed),
        "failed_ids": [s.id for s in failed],
        "slowest": slowest.name if slowest else "",
        "slowest_s": round(slowest.duration, 3) if slowest else 0.0,
        "costliest": costliest.name if costliest else "",
        "by_kind": {
            kind: {
                "count": len(items),
                "duration_s": round(sum(i.duration for i in items), 3),
                "tokens": sum(i.tokens for i in items),
            }
            for kind, items in run.by_kind().items()
        },
    }


def build_tree(spans: List[Span], warnings: List[str]) -> List[Span]:
    """Attach children to parents and return the roots, in start order."""
    index = {s.id: s for s in spans}

    for span in spans:
        if span.parent is None:
            continue
        parent = index.get(span.parent)
        if parent is None:
            # The parent event was lost (crash, sampling, truncated file).
            # Reattaching keeps the child visible; dropping it would hide the
            # most interesting part of a failed run.
            warnings.append(
                f"span {span.id!r} references unknown parent {span.parent!r}; "
                "reattached to root"
            )
            span.parent = None
            continue
        if parent is span:
            warnings.append(f"span {span.id!r} is its own parent; treated as root")
            span.parent = None
            continue
        parent.children.append(span)

    # A cycle would make walk() infinite. Detect by walking with a visited set
    # and detach anything that cannot be reached from a root.
    roots = [s for s in spans if s.parent is None]
    reachable = set()
    for root in roots:
        for span in root.walk():
            reachable.add(span.id)

    orphans = [s for s in spans if s.id not in reachable]
    if orphans:
        warnings.append(
            f"{len(orphans)} span(s) formed a cycle; detached to root"
        )
        for span in orphans:
            if span.parent is not None:
                parent = index.get(span.parent)
                if parent and span in parent.children:
                    parent.children.remove(span)
            span.parent = None
            roots.append(span)

    roots.sort(key=lambda s: s.start)
    for root in roots:
        _sort_children(root)
    return roots


def _sort_children(span: Span) -> None:
    span.children.sort(key=lambda s: s.start)
    for child in span.children:
        _sort_children(child)
