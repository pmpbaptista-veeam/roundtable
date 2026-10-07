import unittest

import helpers


class Answering(helpers.RoundtableTestCase):
    def test_empty_answer_refused(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        for empty in ("", "  \n"):
            result = self.fails(self.a, "answer", "--id", qid, "--as", "grace",
                                "--answer", empty)
            self.assertIn("--answer is empty", result.err)
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["status"], "open")

    def test_retired_agent_cannot_answer(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        self.raw_retire(self.a, "grace")
        result = self.fails(self.a, "answer", "--id", qid, "--as", "grace",
                            "--answer", "Option A.")
        self.assertIn("only an active agent may answer", result.err)
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["status"], "open")


class Waiting(helpers.RoundtableTestCase):
    def test_wait_on_withdrawn_exits_8(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace", "--blocking", "--default", "hold")
        self.ok(self.a, "withdraw", "--id", qid, "--as", "ada", "--reason", "moot now")
        result = self.fails(self.a, "wait", "--id", qid, "--timeout", "0",
                            "--interval", "0", code=8)
        self.assertIn("WITHDRAWN", result.out)
        self.assertIn("moot now", result.out)


class Deadlines(helpers.RoundtableTestCase):
    def test_bad_deadline_format_is_clear_error(self):
        self.team()
        for bad in ("2h30m", "tomorrow", "2026-08-20 13:30"):
            result = self.fails(self.a, "ask", "--as", "ada", "--to", "grace",
                                "--question", "when?", "--deadline", bad)
            self.assertIn("45m", result.err)
            self.assertIn("ISO-8601", result.err)
        self.assertEqual(self.question_files(self.a), [])
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

    def test_escalation_windows_shrink(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace", "--blocking", "--default", "hold",
                       "--deadline", "2h")
        self.set_time("2026-08-20T11:00:01Z")
        self.ok(self.a, "sweep")
        meta, _ = self.load(self.qpath(self.a, qid))
        self.assertEqual(meta["escalated_to"], ["dana.lead"])
        self.assertEqual(meta["deadline"], "2026-08-20T12:00:01Z")
        self.assertEqual(meta["escalated_at"], "2026-08-20T11:00:01Z")
        self.set_time("2026-08-20T12:00:02Z")
        self.ok(self.a, "sweep")
        meta, _ = self.load(self.qpath(self.a, qid))
        self.assertEqual(meta["escalated_to"], ["dana.lead", "cto.office"])
        self.assertEqual(meta["deadline"], "2026-08-20T12:30:02Z")
        self.assertEqual(meta["escalated_at"], "2026-08-20T12:00:02Z")

    def test_escalation_window_floor_5m(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace", "--blocking", "--default", "hold",
                       "--deadline", "6m")
        self.set_time("2026-08-20T09:06:01Z")
        self.ok(self.a, "sweep")
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["deadline"],
                         "2026-08-20T09:11:01Z")
        self.set_time("2026-08-20T09:11:02Z")
        self.ok(self.a, "sweep")
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["deadline"],
                         "2026-08-20T09:16:02Z")

    def test_v1_escalated_file_without_escalated_at(self):
        self.team()
        qid = "Q-20260820T090000Z-ada-v1e1"
        self.commit_raw(self.a, f"inbox/grace/{qid}.md", "\n".join([
            "---", f"id: {qid}", "from: ada", "to: grace", "to_kind: agent",
            "blocking: true", "urgency: normal", "deadline: 2026-08-20T10:00:00Z",
            "default: hold", "status: escalated", "created: 2026-08-20T09:00:00Z",
            "context_decisions: []", "in_reply_to: null",
            'escalated_to: ["dana.lead"]', "---", "", "## Question", "",
            "Old escalated one?", ""]))
        self.set_time("2026-08-20T10:00:01Z")
        self.ok(self.a, "sweep")
        meta, _ = self.load(self.qpath(self.a, qid))
        self.assertEqual(meta["escalated_to"], ["dana.lead", "cto.office"])
        self.assertEqual(meta["deadline"], "2026-08-20T10:30:01Z")
        self.assertEqual(meta["escalated_at"], "2026-08-20T10:00:01Z")


if __name__ == "__main__":
    unittest.main()
