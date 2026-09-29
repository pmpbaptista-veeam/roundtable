import io
import unittest
from contextlib import redirect_stderr

import helpers
from helpers import collab


class HandleRule(unittest.TestCase):
    def test_handle_rule_unit(self):
        for handle in ("pedro.baptista", "dana_lead", "a-1", "2026", "Pedro.Baptista"):
            self.assertEqual(collab.norm_handle(handle), handle.lower())
        for bad in ("../x", "a..b", ".x", "-x", "a/b", "a b", "", "p@x.com"):
            with self.assertRaises(SystemExit, msg=bad), redirect_stderr(io.StringIO()):
                collab.norm_handle(bad)


class PathEscape(helpers.RoundtableTestCase):
    def test_ask_to_path_escape_writes_nothing(self):
        self.team()
        result = self.fails(self.a, "ask", "--as", "ada", "--to", "../../x.y",
                            "--question", "hello?")
        self.assertIn("is not a valid handle", result.err)
        self.assertFalse((self.tmp / "x.y").exists())
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

    def test_join_human_path_escape_writes_nothing(self):
        self.fails(self.a, "join", "--name", "eve", "--human", "../../evil.h")
        self.assertFalse((self.tmp / "evil.h").exists())
        self.assertFalse((self.a / "agents" / "eve.yaml").exists())

    def test_join_escalation_path_escape_refused(self):
        self.fails(self.a, "join", "--name", "eve", "--human", "eve.h",
                   "--escalation", "dana.lead,../../bad.h")
        self.assertFalse((self.tmp / "bad.h").exists())
        self.assertFalse((self.a / "agents" / "eve.yaml").exists())

    def test_question_id_path_escape_refused(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        outside = self.tmp / "x" / "Q-y.md"
        outside.parent.mkdir()
        text = self.qpath(self.a, qid).read_text(encoding="utf-8")
        outside.write_text(text, encoding="utf-8")
        for argv in (("answer", "--id", "../../../x/Q-y", "--as", "grace", "--answer", "hi"),
                     ("answer", "--id", "Q-*", "--as", "grace", "--answer", "hi"),
                     ("withdraw", "--id", "../../../x/Q-y", "--as", "ada", "--reason", "r"),
                     ("wait", "--id", "../../../x/Q-y", "--timeout", "0", "--interval", "0"),
                     ("decide", "--as", "ada", "--title", "t", "--topics", "x",
                      "--question", "../../../x/Q-y", "--body", "b")):
            result = self.fails(self.a, *argv)
            self.assertIn("is not a question id", result.err)
        self.assertEqual(outside.read_text(encoding="utf-8"), text)
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["status"], "open")
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

    def test_question_id_in_two_inboxes_refused(self):
        # v1.0.0 on Linux could leave the same id in inbox/grace and inbox/Grace
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        text = self.qpath(self.a, qid).read_text(encoding="utf-8")
        self.commit_raw(self.a, f"inbox/dana.lead/{qid}.md", text)
        result = self.fails(self.a, "answer", "--id", qid, "--as", "grace", "--answer", "hi")
        self.assertIn(f"inbox/grace/{qid}.md", result.err)
        self.assertIn(f"inbox/dana.lead/{qid}.md", result.err)
        for path in self.question_files(self.a):
            self.assertEqual(self.load(path)[0]["status"], "open")

    def test_quickstart_handle_error_names_the_positional(self):
        result = self.fails(self.a, "quickstart", "../x", "--human", "pedro.baptista")
        self.assertIn("name '../x' is not a valid handle", result.err)
        self.assertNotIn("--name", result.err)

    def test_invalid_identity_flags_refused(self):
        self.team()
        for argv in (("inbox", "--as", "../x"),
                     ("decide", "--as", "ada", "--title", "t1", "--topics", "x",
                      "--approved-by", "a/b", "--body", "b"),
                     ("decide", "--as", "ada", "--title", "t2", "--topics", "x",
                      "--agreed-with", "grace,../y", "--body", "b")):
            result = self.fails(self.a, *argv)
            self.assertIn("is not a valid handle", result.err)
        self.assertEqual(self.decision_files(self.a), [])


class CaseInsensitiveHandles(helpers.RoundtableTestCase):
    def test_join_lowercases_human_and_escalation(self):
        self.ok(self.a, "join", "--name", "eve", "--human", "Pedro.Baptista",
                "--escalation", "Dana.Lead")
        text = (self.a / "agents" / "eve.yaml").read_text(encoding="utf-8")
        self.assertIn("human: pedro.baptista", text)
        self.assertIn('escalation: ["dana.lead"]', text)
        self.assertIn("inbox/pedro.baptista/.gitkeep",
                      self.git(self.a, "ls-files").splitlines())

    def test_as_flag_is_case_insensitive(self):
        self.team()
        qid = self.ask(self.a, "ADA", "Pedro.Baptista")
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["from"], "ada")
        self.assertIn(qid, self.ok(self.a, "inbox", "--as", "Pedro.Baptista"))

    def test_v1_mixed_case_inbox_dir_still_works(self):
        old = "Q-20260820T085000Z-ada-v1c1"
        self.commit_raw(self.a, f"inbox/Pedro.Baptista/{old}.md", "\n".join([
            "---", f"id: {old}", "from: ada", "to: Pedro.Baptista",
            "to_kind: human", "blocking: false", "urgency: normal",
            "deadline: null", "default: null", "status: open",
            "created: 2026-08-20T08:50:00Z", "context_decisions: []",
            "in_reply_to: null", "---", "", "## Question", "", "Old question?", ""]))
        self.team()
        self.assertIn(old, self.ok(self.a, "inbox", "--as", "pedro.baptista"))
        self.answer(self.a, old, "pedro.baptista", "Yes.")
        self.assertEqual(self.load(self.qpath(self.a, old))[0]["status"], "answered")
        new = self.ask(self.a, "ada", "pedro.baptista")
        files = self.git(self.a, "ls-files", "inbox").splitlines()
        self.assertIn(f"inbox/Pedro.Baptista/{new}.md", files)
        self.assertFalse([f for f in files if f.startswith("inbox/pedro.baptista/")], files)


if __name__ == "__main__":
    unittest.main()
