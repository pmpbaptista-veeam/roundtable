import unittest

import helpers
from helpers import collab


class GoldenPath(helpers.RoundtableTestCase):
    def test_golden_path(self):
        self.team()
        q1 = self.ask(self.a, "ada", "grace", "--blocking", "--default", "proceed with A")
        self.assertIn(q1, self.ok(self.b, "inbox", "--as", "grace"))
        self.answer(self.b, q1, "grace", "Option B. View backups_report_v1.")
        self.assertIn("Option B", self.ok(self.a, "wait", "--id", q1,
                                          "--timeout", "0", "--interval", "0"))
        d1 = self.decide(self.a, "ada", "Serializer contract", "api",
                         "--question", q1, "--agreed-with", "grace")
        meta, _ = self.load(self.dpath(self.a, d1))
        self.assertEqual(meta["agreed_by"], ["grace"])
        self.assertIsNone(meta["approved_by_human"])
        self.assertIsNone(meta["approval_verified"])

        q2 = self.ask(self.a, "ada", "pedro.baptista", question="Purge exports after 30 days?")
        self.answer(self.b, q2, "pedro.baptista", "Approved.")
        d2 = self.decide(self.a, "ada", "Purge old exports", "reports", "--question", q2)
        meta, _ = self.load(self.dpath(self.a, d2))
        self.assertEqual(meta["approved_by_human"], "pedro.baptista")
        self.assertEqual(meta["approval_question"], q2)
        self.assertIs(meta["approval_verified"], True)

        self.assertIn(d2, self.ok(self.b, "recall", "purge"))
        d3 = self.decide(self.b, "grace", "Purge exports after 90 days", "reports",
                         "--supersedes", d2)
        out = self.ok(self.b, "recall", "purge")
        self.assertIn(d3, out)
        self.assertNotIn(d2, out)
        self.assertIn(f"[superseded] {d2}", self.ok(self.b, "recall", "purge", "--all"))
        self.assertIn(("ada", f"[roundtable] {q1} was answered by grace"), self.notified)
        self.assert_team_state(self.b)


class Retract(helpers.RoundtableTestCase):
    def approved_decision(self, title="Retractable", topics="api"):
        self.team()
        q = self.ask(self.a, "ada", "pedro.baptista", question="Ship it?")
        self.answer(self.a, q, "pedro.baptista", "Approved.")
        return self.decide(self.a, "ada", title, topics, "--question", q,
                           "--agreed-with", "grace", body="## Decision\n\nOriginal body.\n")

    def test_retract_by_participant(self):
        did = self.approved_decision()
        del self.notified[:]
        self.advance(minutes=5)
        self.assertIn(f"retracted {did}", self.ok(
            self.a, "retract", "--id", did, "--as", "grace", "--reason", "turned out wrong"))
        meta, body = self.load(self.dpath(self.a, did))
        self.assertEqual(meta["status"], "retracted")
        self.assertEqual(meta["retracted_by"], "grace")
        self.assertEqual(meta["retracted_at"], collab.ts(self.clock))
        self.assertTrue(body.startswith("## Decision\n\nOriginal body."), body)
        self.assertTrue(body.endswith("## Retracted\n\nturned out wrong"), body)
        self.assertEqual(sorted(h for h, _ in self.notified), ["ada", "pedro.baptista"])
        self.assertNotIn(did, self.ok(self.a, "recall", ""))
        self.assertIn(f"[retracted] {did}", self.ok(self.a, "recall", "", "--all"))
        self.assert_team_state(self.a)

    def test_retract_refusals(self):
        did = self.approved_decision()
        for argv in ((did, "vp.eng", "not mine"),
                     (did, "grace", "  "),
                     ("D-20260101T000000Z-nope", "ada", "gone")):
            self.fails(self.a, "retract", "--id", argv[0], "--as", argv[1],
                       "--reason", argv[2])
        replacement = self.decide(self.a, "ada", "Replacement", "api", "--supersedes", did)
        result = self.fails(self.a, "retract", "--id", did, "--as", "ada",
                            "--reason", "already replaced")
        self.assertIn("only an active decision", result.err)
        self.assertEqual(self.load(self.dpath(self.a, did))[0]["superseded_by"], replacement)
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

    def test_retract_frees_topics(self):
        did = self.approved_decision()
        self.ok(self.a, "retract", "--id", did, "--as", "ada", "--reason", "wrong")
        self.decide(self.a, "ada", "Fresh start", "api")


class Retire(helpers.RoundtableTestCase):
    def test_retire_by_self_or_human(self):
        self.team()
        self.assertIn("retired 'ada'", self.ok(self.a, "retire", "--name", "ada", "--as", "ada"))
        self.ok(self.a, "retire", "--name", "grace", "--as", "Dana.Lead")
        for name in ("ada", "grace"):
            agent = collab.Repo(self.a).load_agent(name)
            self.assertEqual(agent["status"], "retired")
            self.assertEqual(agent["retired_at"], collab.ts(self.clock))
        self.assert_team_state(self.a)

    def test_retire_refusals(self):
        self.team()
        self.fails(self.a, "retire", "--name", "ada", "--as", "grace")
        self.fails(self.a, "retire", "--name", "nobody", "--as", "nobody")
        self.ok(self.a, "retire", "--name", "ada", "--as", "pedro.baptista")
        result = self.fails(self.a, "retire", "--name", "ada", "--as", "ada")
        self.assertIn("already 'retired'", result.err)

    def test_retire_lists_open_questions_untouched(self):
        self.team()
        incoming = self.ask(self.a, "ada", "grace", "--blocking", "--default", "hold")
        asked = self.ask(self.a, "grace", "dana.lead")
        out = self.ok(self.a, "retire", "--name", "grace", "--as", "dana.lead")
        self.assertIn(incoming, out)
        self.assertIn(asked, out)
        for qid in (incoming, asked):
            self.assertEqual(self.load(self.qpath(self.a, qid))[0]["status"], "open")
        self.assertTrue(collab.Repo(self.a).load_agent("grace")["retired_at"])
        result = self.fails(self.a, "ask", "--as", "ada", "--to", "grace",
                            "--question", "still there?")
        self.assertIn("not active", result.err)


    def test_update_escalation_by_self_or_human(self):
        self.team()
        out = self.ok(self.a, "update", "--name", "ada", "--as", "ada",
                      "--escalation", "Dana.Lead,cto.office,dana.lead")
        self.assertIn("['dana.lead'] -> ['dana.lead', 'cto.office']", out)
        self.ok(self.a, "update", "--name", "grace", "--as", "dana.lead",
                "--escalation", "vp.eng")
        repo = collab.Repo(self.a)
        self.assertEqual(repo.load_agent("ada")["escalation"], ["dana.lead", "cto.office"])
        self.assertEqual(repo.load_agent("grace")["escalation"], ["vp.eng"])
        self.assertIn("roundtable:dana.lead",
                      self.git(self.a, "log", "-1", "--format=%an"))
        self.assert_team_state(self.a)

    def test_update_clears_and_skips_unchanged(self):
        self.team()
        self.ok(self.a, "update", "--name", "ada", "--as", "ada", "--escalation", "")
        self.assertEqual(collab.Repo(self.a).load_agent("ada")["escalation"], [])
        head = self.remote_head()
        self.assertIn("unchanged", self.ok(self.a, "update", "--name", "ada", "--as", "ada",
                                           "--escalation", ""))
        self.assertEqual(self.remote_head(), head)

    def test_update_refusals(self):
        self.team()
        result = self.fails(self.a, "update", "--name", "ada", "--as", "grace",
                            "--escalation", "vp.eng")
        self.assertIn("only ada itself", result.err)
        self.fails(self.a, "update", "--name", "nobody", "--as", "nobody", "--escalation", "x.y")
        result = self.fails(self.a, "update", "--name", "ada", "--as", "ada",
                            "--escalation", "grace")
        self.assertIn("'grace' is an agent", result.err)
        self.fails(self.a, "update", "--name", "ada", "--as", "ada", "--escalation", "../x")
        self.ok(self.a, "retire", "--name", "ada", "--as", "ada")
        result = self.fails(self.a, "update", "--name", "ada", "--as", "ada",
                            "--escalation", "vp.eng")
        self.assertIn("'retired'", result.err)

    def test_update_takes_effect_on_the_next_breach(self):
        self.team()
        qid = self.ask(self.a, "ada", "dana.lead", "--blocking", "--default", "hold",
                       "--deadline", "1h")
        self.ok(self.a, "update", "--name", "ada", "--as", "pedro.baptista",
                "--escalation", "vp.eng")
        self.advance(hours=2)
        self.ok(self.a, "sweep")
        meta, _ = self.load(self.qpath(self.a, qid))
        self.assertEqual(meta["status"], "escalated")
        self.assertEqual(meta["escalated_to"], ["vp.eng"])

    def test_quickstart_points_a_joined_agent_at_update(self):
        self.team()
        result = self.run_cli(self.a, "quickstart", "ada", "--escalation", "vp.eng")
        self.assertEqual(result.code, 0)
        self.assertIn("collab.py update --name ada", result.err)
        self.assertEqual(collab.Repo(self.a).load_agent("ada")["escalation"], ["dana.lead"])


if __name__ == "__main__":
    unittest.main()
