"""Tests for trace parsing and analysis.

The cases that matter are the ugly traces: a crashed run with no end event, a
missing parent, a cycle. Those are the ones a debugging tool is actually opened
for, and they are the ones a naive implementation throws away.
"""

import json
import unittest

from agenttrace.analyze import Span, build_tree, summarize
from agenttrace.parse import ParseError, _normalize_time, parse_events


def jsonl(*events):
    return "\n".join(json.dumps(e) for e in events)


class TestTimeNormalization(unittest.TestCase):
    def test_relative_seconds_pass_through(self):
        self.assertEqual(_normalize_time(1.5), 1.5)
        self.assertEqual(_normalize_time(0), 0.0)

    def test_unix_seconds(self):
        self.assertAlmostEqual(_normalize_time(1_700_000_000), 1_700_000_000)

    def test_milliseconds(self):
        self.assertAlmostEqual(_normalize_time(1_700_000_000_000), 1_700_000_000)

    def test_microseconds(self):
        self.assertAlmostEqual(_normalize_time(1_700_000_000_000_000), 1_700_000_000)

    def test_nanoseconds(self):
        self.assertAlmostEqual(_normalize_time(1_700_000_000_000_000_000), 1_700_000_000)

    def test_numeric_string(self):
        self.assertAlmostEqual(_normalize_time("1700000000000000000"), 1_700_000_000)

    def test_none_and_garbage(self):
        self.assertIsNone(_normalize_time(None))
        self.assertIsNone(_normalize_time("not a number"))
        self.assertIsNone(_normalize_time(True))


class TestParsing(unittest.TestCase):
    def test_parses_jsonl(self):
        run = parse_events(
            jsonl(
                {"id": "a", "name": "agent", "start": 0, "end": 2},
                {"id": "b", "name": "llm", "start": 0.2, "end": 1.8, "parent": "a"},
            )
        )
        self.assertEqual(len(run.all_spans), 2)
        self.assertEqual(len(run.roots), 1)
        self.assertEqual(run.roots[0].children[0].id, "b")

    def test_parses_json_array(self):
        run = parse_events(json.dumps([{"id": "a", "name": "x", "start": 0, "end": 1}]))
        self.assertEqual(len(run.all_spans), 1)

    def test_skips_blank_lines(self):
        run = parse_events('\n\n{"id":"a","name":"x","start":0,"end":1}\n\n')
        self.assertEqual(len(run.all_spans), 1)

    def test_rejects_garbage(self):
        with self.assertRaises(ParseError):
            parse_events("{not json")

    def test_rejects_empty(self):
        with self.assertRaises(ParseError):
            parse_events("   ")

    def test_defaults_missing_id(self):
        run = parse_events(jsonl({"name": "orphan", "start": 0, "end": 1}))
        self.assertEqual(run.all_spans[0].id, "span-0")

    def test_merges_split_start_and_end_events(self):
        # Per-event streams emit the start and end separately. Overwriting would
        # lose the duration entirely.
        run = parse_events(
            jsonl(
                {"id": "a", "name": "x", "start": 0},
                {"id": "a", "end": 3},
            )
        )
        self.assertEqual(len(run.all_spans), 1)
        self.assertEqual(run.all_spans[0].duration, 3.0)

    def test_reads_otel_style_keys(self):
        run = parse_events(
            jsonl(
                {
                    "span_id": "s1",
                    "name": "llm",
                    "start_time_unix_nano": 1_700_000_000_000_000_000,
                    "end_time_unix_nano": 1_700_000_002_000_000_000,
                }
            )
        )
        self.assertAlmostEqual(run.all_spans[0].duration, 2.0)

    def test_reads_token_keys(self):
        run = parse_events(
            jsonl(
                {
                    "id": "a",
                    "name": "x",
                    "start": 0,
                    "end": 1,
                    "input_tokens": 100,
                    "completion_tokens": 20,
                }
            )
        )
        self.assertEqual(run.all_spans[0].tokens, 120)

    def test_unterminated_span_is_closed_not_dropped(self):
        # A crashed agent is the trace you most want to read.
        run = parse_events(
            jsonl(
                {"id": "a", "name": "agent", "start": 0},
                {"id": "b", "name": "llm", "start": 1, "end": 4, "parent": "a"},
            )
        )
        self.assertEqual(len(run.all_spans), 2)
        self.assertEqual(run.all_spans[0].end, 4.0)
        self.assertTrue(any("no end event" in w for w in run.warnings))

    def test_rebases_to_zero(self):
        run = parse_events(
            jsonl(
                {"id": "a", "name": "x", "start": 1_700_000_000, "end": 1_700_000_002}
            )
        )
        self.assertAlmostEqual(run.all_spans[0].start, 0.0)
        self.assertAlmostEqual(run.all_spans[0].duration, 2.0)

    def test_orphan_parent_reattached(self):
        run = parse_events(
            jsonl({"id": "b", "name": "child", "start": 0, "end": 1, "parent": "ghost"})
        )
        self.assertIsNone(run.all_spans[0].parent)
        self.assertTrue(any("unknown parent" in w for w in run.warnings))

    def test_self_parent_is_detached(self):
        run = parse_events(
            jsonl({"id": "a", "name": "x", "start": 0, "end": 1, "parent": "a"})
        )
        self.assertIsNone(run.all_spans[0].parent)

    def test_extra_fields_kept_as_attributes(self):
        run = parse_events(
            jsonl({"id": "a", "name": "x", "start": 0, "end": 1, "model": "gpt-4o"})
        )
        self.assertEqual(run.all_spans[0].attributes["model"], "gpt-4o")


class TestTree(unittest.TestCase):
    def test_cycle_is_broken(self):
        # a -> b -> a would make walk() infinite.
        spans = [
            Span(id="a", name="a", start=0, end=1, parent="b"),
            Span(id="b", name="b", start=0, end=1, parent="a"),
        ]
        warnings = []
        roots = build_tree(spans, warnings)
        self.assertTrue(roots)
        # Must terminate and reach both.
        reached = [s.id for r in roots for s in r.walk()]
        self.assertIn("a", reached)
        self.assertIn("b", reached)
        self.assertTrue(any("cycle" in w for w in warnings))

    def test_children_sorted_by_start(self):
        spans = [
            Span(id="p", name="p", start=0, end=10),
            Span(id="late", name="late", start=5, end=6, parent="p"),
            Span(id="early", name="early", start=1, end=2, parent="p"),
        ]
        roots = build_tree(spans, [])
        self.assertEqual([c.id for c in roots[0].children], ["early", "late"])


class TestSummary(unittest.TestCase):
    def _run(self):
        return parse_events(
            jsonl(
                {"id": "a", "name": "agent", "start": 0, "end": 4, "kind": "agent"},
                {"id": "b", "name": "llm1", "start": 0, "end": 2, "parent": "a",
                 "kind": "llm", "tokens_in": 100, "tokens_out": 50, "cost": 0.01},
                {"id": "c", "name": "llm2", "start": 2, "end": 4, "parent": "a",
                 "kind": "llm", "tokens_in": 80, "tokens_out": 20, "cost": 0.02},
            )
        )

    def test_rollups(self):
        stats = summarize(self._run())
        self.assertEqual(stats["spans"], 3)
        self.assertEqual(stats["duration_s"], 4.0)
        self.assertAlmostEqual(stats["cost"], 0.03)
        # Token totals must not double count: the root has none here.
        self.assertEqual(stats["tokens"], 250)

    def test_leaf_time_excludes_parents(self):
        stats = summarize(self._run())
        # Only the two leaves contribute; the parent's 4s would double count.
        self.assertAlmostEqual(stats["leaf_time_s"], 4.0)

    def test_parallelism_detects_overlap(self):
        run = parse_events(
            jsonl(
                {"id": "a", "name": "root", "start": 0, "end": 2},
                {"id": "b", "name": "p1", "start": 0, "end": 2, "parent": "a"},
                {"id": "c", "name": "p2", "start": 0, "end": 2, "parent": "a"},
            )
        )
        stats = summarize(run)
        self.assertGreater(stats["parallelism"], 1.5)

    def test_failures_surface(self):
        run = parse_events(
            jsonl({"id": "a", "name": "x", "start": 0, "end": 1, "status": "error"})
        )
        stats = summarize(run)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["failed_ids"], ["a"])

    def test_slowest_and_costliest(self):
        stats = summarize(self._run())
        self.assertEqual(stats["costliest"], "llm2")
        self.assertEqual(stats["slowest"], "agent")

    def test_by_kind_groups(self):
        stats = summarize(self._run())
        self.assertEqual(stats["by_kind"]["llm"]["count"], 2)


class TestSpan(unittest.TestCase):
    def test_failed_status_variants(self):
        for status in ("error", "failed", "FAIL", "timeout"):
            self.assertTrue(Span(id="a", name="a", status=status).failed)

    def test_ok_status_variants(self):
        for status in ("ok", "success", "completed", "done"):
            self.assertFalse(Span(id="a", name="a", status=status).failed)

    def test_duration_never_negative(self):
        self.assertEqual(Span(id="a", name="a", start=5, end=2).duration, 0.0)


if __name__ == "__main__":
    unittest.main()
