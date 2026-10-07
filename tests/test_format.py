import io
import json
import unittest
from contextlib import redirect_stderr

import helpers
from helpers import collab


def v1_parse(raw):
    """The v1.0.0 value reader, verbatim: teammates on v1.0.0 read v1.1 files."""
    raw = raw.strip()
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


class ValueRoundTrip(unittest.TestCase):
    def test_numeric_and_keyword_strings_round_trip(self):
        for value in ("2026", "1.0", "-5", "1e5", "NaN", "Infinity", "true",
                      "false", "null", "01", "abc"):
            dumped = collab.fm_dump_value(value)
            self.assertEqual(collab.fm_parse_value(dumped), value, dumped)
            self.assertIsInstance(collab.fm_parse_value(dumped), str, dumped)
            self.assertEqual(v1_parse(dumped), value, f"v1.0.0 reader on {dumped}")

    def test_plain_scalars_stay_bare(self):
        for value in ("Q-20260820T093000Z-ada-k3f9",
                      "D-20260820T104500Z-report-export-schema",
                      "2026-08-20T09:30:00Z", "pedro.baptista", "ada", "open"):
            self.assertEqual(collab.fm_dump_value(value), value)


class FrontmatterBlock(helpers.RoundtableTestCase):
    def test_recall_survives_v1_numeric_title_on_disk(self):
        did = "D-20260812T140000Z-2026"
        self.commit_raw(self.a, f"decisions/{did}.md", "\n".join([
            "---", f"id: {did}", "title: 2026", 'topics: ["planning"]',
            "status: active", "supersedes: null", "proposed_by: ada",
            "agreed_by: []", "approved_by_human: null", "source_question: null",
            "created: 2026-08-12T14:00:00Z", "---", "", "## Decision", "",
            "The roadmap year.", ""]))
        self.assertIn(did, self.ok(self.a, "recall", ""))
        out = self.ok(self.a, "recall", "2026")
        self.assertIn(did, out)
        self.assertIn("title:   2026", out)

    def test_decide_quotes_numeric_title(self):
        did = self.decide(self.a, "ada", "2026", "planning")
        text = self.dpath(self.a, did).read_text(encoding="utf-8")
        self.assertIn('title: "2026"', text)
        self.assertEqual(self.load(self.dpath(self.a, did))[0]["title"], "2026")

    def test_default_true_stays_a_string(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace", "--default", "true")
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["default"], "true")

    def test_triple_dash_inside_value_keeps_frontmatter(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace", "--blocking",
                       "--default", "hold --- do not ship")
        meta, body = self.load(self.qpath(self.a, qid))
        self.assertEqual(meta["default"], "hold --- do not ship")
        self.assertEqual(meta["status"], "open")
        self.assertIn("## Question", body)

    def test_body_rule_line_still_loads(self):
        did = self.decide(self.a, "ada", "Rules", "style",
                          body="## Decision\n\nabove\n\n---\n\nbelow\n")
        meta, body = self.load(self.dpath(self.a, did))
        self.assertEqual(meta["status"], "active")
        self.assertIn("\n---\n", body)
        self.assertTrue(body.endswith("below"))

    def test_unterminated_frontmatter_is_refused(self):
        path = self.tmp / "broken.md"
        path.write_text("---\nid: x\n", encoding="utf-8")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()) as err:
            collab.load_doc(path)
        self.assertIn("unterminated", err.getvalue())


if __name__ == "__main__":
    unittest.main()
