"""An agent brings its human's inbox to them and may submit their answer — in
their words, declared with --relayed-by, and recorded for good."""

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import helpers
from helpers import collab

COLLAB = str(helpers.SCRIPTS / "collab.py")
AGENT_SESSION = {"CLAUDECODE": "1", "ROUNDTABLE_AGENT": "ada"}


class Relay(helpers.RoundtableTestCase):
    def test_relayed_answer_is_the_humans_and_records_the_relay(self):
        self.team()                       # ada's human is pedro.baptista
        qid = self.ask(self.b, "grace", "pedro.baptista", "--blocking",
                       "--default", "hold")
        with mock.patch.dict(os.environ, AGENT_SESSION):
            out = self.ok(self.a, "answer", "--id", qid, "--as", "pedro.baptista",
                          "--relayed-by", "ada", "--answer", "Approved, run it.")
        self.assertIn("relayed by ada", out)
        meta, body = self.load(self.qpath(self.a, qid))
        self.assertEqual((meta["answered_by"], meta["relayed_by"]),
                         ("pedro.baptista", "ada"))
        self.assertIn("Approved, run it.", body)
        log = self.git(self.a, "log", "-1", "--format=%s")
        self.assertIn("(relayed by ada)", log)

    def test_agent_session_cannot_answer_as_a_human_without_declaring_it(self):
        self.team()
        qid = self.ask(self.b, "grace", "pedro.baptista")
        with mock.patch.dict(os.environ, AGENT_SESSION):
            result = self.fails(self.a, "answer", "--id", qid, "--as", "pedro.baptista",
                                "--answer", "Approved.")
        self.assertIn("--relayed-by", result.err)
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["status"], "open")
        # the human, in their own terminal, needs nothing extra
        self.answer(self.a, qid, "pedro.baptista", "Approved.")

    def test_an_agent_relays_only_for_its_own_human(self):
        self.team()                       # grace's human is dana.lead
        qid = self.ask(self.b, "ada", "dana.lead")
        result = self.fails(self.a, "answer", "--id", qid, "--as", "dana.lead",
                            "--relayed-by", "ada", "--answer", "Yes.")
        self.assertIn("only for its own human (pedro.baptista)", result.err)

    def test_relay_must_name_this_sessions_agent_and_an_active_agent(self):
        self.team()
        qid = self.ask(self.b, "ada", "dana.lead")
        with mock.patch.dict(os.environ, AGENT_SESSION):
            result = self.fails(self.a, "answer", "--id", qid, "--as", "dana.lead",
                                "--relayed-by", "grace", "--answer", "Yes.")
        self.assertIn("this session is agent 'ada'", result.err)
        self.raw_retire(self.a, "grace")
        result = self.fails(self.a, "answer", "--id", qid, "--as", "dana.lead",
                            "--relayed-by", "grace", "--answer", "Yes.")
        self.assertIn("not an active agent", result.err)

    def test_relayed_by_is_refused_when_answering_as_an_agent(self):
        self.team()
        qid = self.ask(self.b, "grace", "ada")
        result = self.fails(self.a, "answer", "--id", qid, "--as", "ada",
                            "--relayed-by", "ada", "--answer", "Yes.")
        self.assertIn("agents answer as themselves", result.err)

    def test_decision_records_that_its_approval_was_relayed(self):
        self.team()
        qid = self.ask(self.a, "ada", "pedro.baptista", "--blocking",
                       "--default", "hold", question="Drop the legacy column?")
        with mock.patch.dict(os.environ, AGENT_SESSION):
            self.ok(self.a, "answer", "--id", qid, "--as", "pedro.baptista",
                    "--relayed-by", "ada", "--answer", "Yes, drop it.")
        did = self.decide(self.a, "ada", "Drop the legacy column", "db-schema",
                          "--question", qid)
        meta, _ = self.load(self.dpath(self.a, did))
        self.assertEqual(meta["approved_by_human"], "pedro.baptista")
        self.assertIs(meta["approval_verified"], True)
        self.assertEqual(meta["approval_relayed_by"], "ada")


class HumanInboxDelivery(helpers.RoundtableTestCase):
    def hook(self, clone, event="UserPromptSubmit"):
        result = self.run_cli(clone, "hook", stdin=json.dumps({"hook_event_name": event}))
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out) if result.out.strip() else None

    def test_hook_brings_the_humans_new_question_with_both_ways_to_answer(self):
        self.team()                       # clone a remembers ada
        self.hook(self.a, "SessionStart")
        qid = self.ask(self.b, "grace", "pedro.baptista", "--blocking",
                       "--default", "hold", "--options", "A: view|B: raw columns")
        ctx = self.hook(self.a)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"FOR YOUR HUMAN pedro.baptista: {qid} from grace [BLOCKING", ctx)
        self.assertIn("A: view", ctx)
        self.assertIn(f"answer --id {qid} --as pedro.baptista --relayed-by ada", ctx)
        self.assertIn(f"they run: collab.py answer --id {qid} --as pedro.baptista "
                      "--answer", ctx)
        self.assertIsNone(self.hook(self.a), "delivered twice")

    def test_agents_own_question_to_its_human_is_not_relayed_back(self):
        self.team()
        self.hook(self.a, "SessionStart")
        self.ask(self.a, "ada", "pedro.baptista")
        self.assertIsNone(self.hook(self.a))

    def test_relay_of_the_humans_inbox_can_be_turned_off(self):
        self.team()
        self.hook(self.a, "SessionStart")
        self.ask(self.b, "grace", "pedro.baptista")
        with mock.patch.dict(os.environ, {"ROUNDTABLE_RELAY_HUMAN": "0"}):
            self.assertIsNone(self.hook(self.a))

    def test_watch_with_human_reports_the_humans_inbox(self):
        self.clock = collab.datetime.now(collab.timezone.utc).replace(microsecond=0)
        self.team()
        watcher = subprocess.Popen(
            [sys.executable, COLLAB, "--repo", str(self.a), "watch", "--as", "ada",
             "--with-human", "--interval", "1", "--timeout", "30"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "ROUNDTABLE_NOTIFY_DESKTOP": "0"})
        try:
            time.sleep(2)
            qid = self.ask(self.b, "grace", "pedro.baptista", question="Approve the drop?")
            time.sleep(3)
            self.answer(self.b, qid, "pedro.baptista", "Approved.")
            time.sleep(3)
        finally:
            watcher.terminate()
            out, err = watcher.communicate(timeout=30)
        self.assertIn(f"@ {qid}  for pedro.baptista from grace", out)
        self.assertIn("Approve the drop?", out)
        self.assertIn(f"@- {qid}  answered by pedro.baptista — no longer waiting on "
                      "pedro.baptista", out)


if __name__ == "__main__":
    unittest.main()
