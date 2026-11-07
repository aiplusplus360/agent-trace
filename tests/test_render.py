"""Tests for rendering.

The security case is the important one: span names come from application code
and frequently contain model output, which frequently contains whatever a user
typed. Rendering that as markup would be an injection bug inside a debugging
tool — the worst place for one.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agenttrace.analyze import summarize
from agenttrace.cli import EXIT_OK, EXIT_PARSE, EXIT_USAGE, main
from agenttrace.parse import parse_events
from agenttrace.render import render_html, render_json, render_text


def trace_jsonl():
    events = [
        {"id": "run", "name": "agent run", "start": 0, "end": 5, "kind": "agent"},
        {"id": "llm1", "name": "plan", "start": 0, "end": 2, "parent": "run",
         "kind": "llm", "tokens_in": 500, "tokens_out": 120, "cost": 0.004},
        {"id": "tool", "name": "search", "start": 2, "end": 3, "parent": "run",
         "kind": "tool", "status": "error"},
        {"id": "llm2", "name": "summarise", "start": 3, "end": 5, "parent": "run",
         "kind": "llm", "tokens_in": 800, "tokens_out": 90, "cost": 0.006},
    ]
    return "\n".join(json.dumps(e) for e in events)


class TestHtml(unittest.TestCase):
    def setUp(self):
        self.run = parse_events(trace_jsonl())
        self.html = render_html(self.run)

    def test_is_a_complete_document(self):
        self.assertTrue(self.html.startswith("<!doctype html>"))
        self.assertIn("</html>", self.html)

    def test_is_self_contained(self):
        # No CDN: the report must open from a file:// URL, offline.
        self.assertNotIn("<script src=", self.html)
        self.assertNotIn("<link rel=\"stylesheet\"", self.html)

    def test_shows_the_spans(self):
        for name in ("agent run", "plan", "search", "summarise"):
            self.assertIn(name, self.html)

    def test_shows_rollups(self):
        stats = summarize(self.run)
        self.assertIn(str(stats["spans"]), self.html)
        self.assertIn(f"{stats['tokens']:,}", self.html)

    def test_escapes_span_names(self):
        run = parse_events(
            json.dumps(
                [{"id": "a", "name": "<script>alert(1)</script>", "start": 0, "end": 1}]
            )
        )
        html = render_html(run)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_escapes_quotes_in_title_attribute(self):
        run = parse_events(
            json.dumps([{"id": "a", "name": 'x" onmouseover="evil()', "start": 0, "end": 1}])
        )
        html = render_html(run)
        self.assertNotIn('onmouseover="evil()"', html)

    def test_bar_width_stays_in_bounds(self):
        # Clock skew can put a span outside the window; an out-of-range percent
        # would push the bar off the panel.
        run = parse_events(
            json.dumps(
                [
                    {"id": "a", "name": "root", "start": 0, "end": 1},
                    {"id": "b", "name": "skewed", "start": 5, "end": 5.5, "parent": "a"},
                ]
            )
        )
        html = render_html(run)
        # Only inspect the inline bar styles, not the stylesheet.
        for fragment in html.split('style="left:')[1:]:
            left = float(fragment.split("%")[0])
            width = float(fragment.split("width:")[1].split("%")[0])
            self.assertGreaterEqual(left, 0.0)
            self.assertLessEqual(left, 100.0)
            self.assertGreater(width, 0.0)
            self.assertLessEqual(left + width, 100.01)

    def test_failed_span_marked(self):
        self.assertIn("failed", self.html)

    def test_warnings_rendered(self):
        run = parse_events(json.dumps([{"id": "a", "name": "x", "start": 0}]))
        self.assertIn("parser note", render_html(run))


class TestText(unittest.TestCase):
    def test_tree_indentation(self):
        text = render_text(parse_events(trace_jsonl()))
        self.assertIn("agent run", text)
        self.assertIn("  plan", text)  # children are indented

    def test_marks_failures(self):
        text = render_text(parse_events(trace_jsonl()))
        self.assertTrue(any(line.startswith("!") for line in text.splitlines()))

    def test_duration_formatting(self):
        run = parse_events(
            json.dumps([{"id": "a", "name": "fast", "start": 0, "end": 0.05}])
        )
        self.assertIn("50ms", render_text(run))

    def test_minutes_formatting(self):
        run = parse_events(
            json.dumps([{"id": "a", "name": "slow", "start": 0, "end": 125}])
        )
        self.assertIn("2m5s", render_text(run))


class TestJson(unittest.TestCase):
    def test_round_trips(self):
        data = json.loads(render_json(parse_events(trace_jsonl())))
        self.assertEqual(len(data["spans"]), 4)
        self.assertEqual(data["summary"]["spans"], 4)

    def test_includes_warnings(self):
        data = json.loads(
            render_json(parse_events(json.dumps([{"id": "a", "name": "x", "start": 0}])))
        )
        self.assertTrue(data["warnings"])


class TestCli(unittest.TestCase):
    def _file(self, content):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_render_text(self):
        path = self._file(trace_jsonl())
        code, out, _ = self._run(["render", path, "--format", "text"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("agent run", out)

    def test_render_html_to_file(self):
        path = self._file(trace_jsonl())
        out_file = tempfile.NamedTemporaryFile(suffix=".html", delete=False)
        out_file.close()
        self.addCleanup(os.unlink, out_file.name)
        code, out, _ = self._run(["render", path, "-o", out_file.name])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("wrote", out)
        with open(out_file.name, encoding="utf-8") as handle:
            self.assertIn("<!doctype html>", handle.read())

    def test_fail_on_error(self):
        path = self._file(trace_jsonl())  # contains a failed span
        code, _, _ = self._run(["render", path, "--format", "text", "--fail-on-error"])
        self.assertEqual(code, EXIT_PARSE)

    def test_fail_on_error_clean_trace(self):
        path = self._file(json.dumps([{"id": "a", "name": "x", "start": 0, "end": 1}]))
        code, _, _ = self._run(["render", path, "--format", "text", "--fail-on-error"])
        self.assertEqual(code, EXIT_OK)

    def test_bad_trace_exits_two(self):
        path = self._file("{nope")
        code, _, err = self._run(["render", path])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("error", err)

    def test_missing_file_is_a_clean_error(self):
        code, _, err = self._run(["render", "no-such-trace.jsonl"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot read", err)

    def test_summarize_json(self):
        path = self._file(trace_jsonl())
        code, out, _ = self._run(["summarize", path, "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["spans"], 4)

    def test_summarize_text(self):
        path = self._file(trace_jsonl())
        code, out, _ = self._run(["summarize", path])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("by kind", out)


if __name__ == "__main__":
    unittest.main()
