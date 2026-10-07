import unittest

import helpers


class Approval(helpers.RoundtableTestCase):
    def test_unrelated_answer_does_not_back_approval(self):
        # the example-session scenario: dana approved grace's own question,
        # which is not on ada's thread
        self.team()
        q1 = self.ask(self.a, "ada", "grace")
        q2 = self.ask(self.a, "grace", "dana.lead", "--blocking", "--default", "keep raw_size",
                      question="Approve dropping raw_size?")
        self.answer(self.a, q2, "dana.lead", "Approved.")
        self.answer(self.a, q1, "grace", "Option B.")
        result = self.fails(self.a, "decide", "--as", "ada", "--title", "Export view",
                            "--topics", "api", "--question", q1,
                            "--approved-by", "dana.lead", "--body", "b", code=7)
        self.assertIn("--approval-question", result.err)
        self.assertEqual(self.decision_files(self.a), [])

    def test_approved_by_without_question_refused(self):
        self.team()
        q = self.ask(self.a, "ada", "pedro.baptista")
        self.answer(self.a, q, "pedro.baptista", "Approved.")
        self.fails(self.a, "decide", "--as", "ada", "--title", "Loose claim",
                   "--topics", "api", "--approved-by", "pedro.baptista",
                   "--body", "b", code=7)
        self.assertEqual(self.decision_files(self.a), [])

    def test_approval_question_links_separate_thread(self):
        self.team()
        q1 = self.ask(self.a, "ada", "grace")
        q2 = self.ask(self.a, "grace", "dana.lead", question="Approve the drop?")
        self.answer(self.a, q2, "dana.lead", "Approved.")
        self.answer(self.a, q1, "grace", "Option B.")
        did = self.decide(self.a, "ada", "Export view", "api", "--question", q1,
                          "--approved-by", "dana.lead", "--approval-question", q2)
        meta, _ = self.load(self.dpath(self.a, did))
        self.assertEqual(meta["approved_by_human"], "dana.lead")
        self.assertEqual(meta["approval_question"], q2)
        self.assertIs(meta["approval_verified"], True)

    def test_approval_from_reply_descendant(self):
        self.team()
        q1 = self.ask(self.a, "ada", "grace")
        self.answer(self.a, q1, "grace", "Option B, if pedro agrees.")
        q3 = self.ask(self.a, "ada", "pedro.baptista", "--reply-to", q1,
                      question="grace proposes B — approve?")
        self.answer(self.a, q3, "pedro.baptista", "Approved.")
        did = self.decide(self.a, "ada", "Export view", "api", "--question", q1,
                          "--approved-by", "pedro.baptista")
        meta, _ = self.load(self.dpath(self.a, did))
        self.assertEqual(meta["approval_question"], q3)
        self.assertIs(meta["approval_verified"], True)

    def test_approval_from_reply_ancestor(self):
        self.team()
        # pedro's earlier answer on an unrelated thread must not be picked
        q0 = self.ask(self.a, "ada", "pedro.baptista", question="Unrelated: lunch?")
        self.answer(self.a, q0, "pedro.baptista", "Yes.")
        self.advance(minutes=1)
        q1 = self.ask(self.a, "ada", "pedro.baptista", question="Ship the export?")
        self.answer(self.a, q1, "pedro.baptista", "Approved, align the schema with grace.")
        q2 = self.ask(self.a, "ada", "grace", "--reply-to", q1)
        self.answer(self.a, q2, "grace", "Option B.")
        did = self.decide(self.a, "ada", "Export view", "api", "--question", q2,
                          "--approved-by", "pedro.baptista")
        meta, _ = self.load(self.dpath(self.a, did))
        self.assertEqual(meta["approval_question"], q1)
        self.assertIs(meta["approval_verified"], True)

    def test_out_of_band_still_allowed(self):
        self.team()
        did = self.decide(self.a, "ada", "Hallway call", "api",
                          "--approved-by", "pedro.baptista", "--approval-out-of-band")
        meta, _ = self.load(self.dpath(self.a, did))
        self.assertEqual(meta["approved_by_human"], "pedro.baptista")
        self.assertIs(meta["approval_verified"], False)

    def test_agent_answer_via_approval_question_refused(self):
        self.team()
        q1 = self.ask(self.a, "ada", "grace")
        self.answer(self.a, q1, "grace", "Approved.")
        result = self.fails(self.a, "decide", "--as", "ada", "--title", "Agent ok",
                            "--topics", "api", "--question", q1,
                            "--approval-question", q1, "--body", "b", code=7)
        self.assertIn("an agent's answer is not a human approval", result.err)
        self.assertEqual(self.decision_files(self.a), [])

    def test_approved_by_agent_refused(self):
        self.team()
        q1 = self.ask(self.a, "ada", "grace")
        self.answer(self.a, q1, "grace", "Approved.")
        result = self.fails(self.a, "decide", "--as", "ada", "--title", "Agent ok",
                            "--topics", "api", "--question", q1,
                            "--approved-by", "grace", "--body", "b", code=7)
        self.assertIn("is an agent", result.err)
        self.assertEqual(self.decision_files(self.a), [])

    def test_retired_agent_answer_is_not_human_approval(self):
        self.team()
        q = self.ask(self.a, "ada", "grace")
        self.answer(self.a, q, "grace", "Option B.")
        self.raw_retire(self.a, "grace")
        did = self.decide(self.a, "ada", "Export view", "api", "--question", q)
        meta, _ = self.load(self.dpath(self.a, did))
        self.assertIsNone(meta["approved_by_human"])
        self.assertIsNone(meta["approval_verified"])


class Validation(helpers.RoundtableTestCase):
    def test_empty_topics_refused(self):
        for topics in ("", " ", ","):
            result = self.fails(self.a, "decide", "--as", "ada", "--title", "No topic",
                                "--topics", topics, "--body", "b")
            self.assertIn("--topics", result.err)
        self.assertEqual(self.decision_files(self.a), [])

    def test_failed_decide_leaves_old_decision_untouched(self):
        d1 = self.decide(self.a, "ada", "Use cursors", "x")
        before = self.dpath(self.a, d1).read_bytes()
        for supersedes, body in ((d1, ""), ("D-20260101T000000Z-nope", "b")):
            self.fails(self.a, "decide", "--as", "ada", "--title", "Replace cursors",
                       "--topics", "x", "--supersedes", supersedes, "--body", body)
            self.assertEqual(self.dpath(self.a, d1).read_bytes(), before)
            self.assertEqual(self.git(self.a, "status", "--porcelain"), "")
        self.assertEqual(len(self.decision_files(self.a)), 1)


class Supersede(helpers.RoundtableTestCase):
    def three_on_x(self):
        return (self.decide(self.a, "ada", "Alpha", "x"),
                self.decide(self.a, "ada", "Bravo", "x", "--coexists"),
                self.decide(self.a, "ada", "Charlie", "x", "--coexists"))

    def test_supersede_one_of_many_requires_the_rest(self):
        a, b, c = self.three_on_x()
        result = self.fails(self.a, "decide", "--as", "ada", "--title", "Delta",
                            "--topics", "x", "--supersedes", a, "--body", "b", code=4)
        self.assertIn(b, result.err)
        self.assertIn(c, result.err)
        self.assertNotIn(a, result.err)
        self.assertEqual(self.load(self.dpath(self.a, a))[0]["status"], "active")
        self.assertEqual(len(self.decision_files(self.a)), 3)
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

    def test_supersede_list_replaces_all(self):
        ids = self.three_on_x()
        did = self.decide(self.a, "ada", "Delta", "x", "--supersedes", ",".join(ids))
        for old in ids:
            meta, _ = self.load(self.dpath(self.a, old))
            self.assertEqual(meta["status"], "superseded")
            self.assertEqual(meta["superseded_by"], did)
        self.assertEqual(self.load(self.dpath(self.a, did))[0]["supersedes"], list(ids))
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

    def test_supersede_plus_coexists(self):
        a, b, c = self.three_on_x()
        did = self.decide(self.a, "ada", "Delta", "x", "--supersedes", a, "--coexists")
        self.assertEqual(self.load(self.dpath(self.a, a))[0]["superseded_by"], did)
        for other in (b, c):
            self.assertEqual(self.load(self.dpath(self.a, other))[0]["status"], "active")
        self.assertEqual(self.load(self.dpath(self.a, did))[0]["supersedes"], a)
        # replace some, coexist with the rest
        echo = self.decide(self.a, "ada", "Echo", "x", "--supersedes", f"{did},{b}",
                           "--coexists")
        for old in (did, b):
            self.assertEqual(self.load(self.dpath(self.a, old))[0]["superseded_by"], echo)
        self.assertEqual(self.load(self.dpath(self.a, c))[0]["status"], "active")

    def test_cannot_supersede_non_active(self):
        a = self.decide(self.a, "ada", "Alpha", "x")
        d2 = self.decide(self.a, "ada", "Bravo", "x", "--supersedes", a)
        result = self.fails(self.a, "decide", "--as", "ada", "--title", "Charlie",
                            "--topics", "x", "--supersedes", a, "--body", "b")
        self.assertIn("superseded", result.err)
        self.assertEqual(self.load(self.dpath(self.a, a))[0]["superseded_by"], d2)
        retracted = "D-20260801T000000Z-old-rule"
        self.commit_raw(self.a, f"decisions/{retracted}.md", "\n".join([
            "---", f"id: {retracted}", "title: Old rule", 'topics: ["y"]',
            "status: retracted", "supersedes: null", "proposed_by: ada",
            "agreed_by: []", "created: 2026-08-01T00:00:00Z", "---", "",
            "## Decision", "", "Old.", ""]))
        result = self.fails(self.a, "decide", "--as", "ada", "--title", "Echo",
                            "--topics", "y", "--supersedes", retracted, "--body", "b")
        self.assertIn("retracted", result.err)
        self.assertEqual(self.load(self.dpath(self.a, retracted))[0]["status"], "retracted")
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
