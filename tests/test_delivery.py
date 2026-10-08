"""Automatic delivery (`collab.py hook`, `watch` answer lines) and the clone
lock that lets a watcher, the hook and the agent share one clone."""

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

# Holds the clone lock from another process until stdin closes.
HOLDER = """
import fcntl, sys
fh = open(sys.argv[1], "a+")
fcntl.flock(fh, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""


class Delivery(helpers.RoundtableTestCase):
    def hook(self, clone, event, *extra):
        result = self.run_cli(clone, "hook", *extra,
                              stdin=json.dumps({"hook_event_name": event}))
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out) if result.out.strip() else None

    def context(self, out):
        return out["hookSpecificOutput"]["additionalContext"]

    def test_new_question_is_delivered_into_context_once(self):
        self.team()                                   # clone a remembers ada
        self.hook(self.b, "SessionStart", "--as", "grace")
        qid = self.ask(self.a, "ada", "grace", "--blocking", "--default", "hold")
        out = self.hook(self.b, "UserPromptSubmit", "--as", "grace")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn(qid, self.context(out))
        self.assertIn("BLOCKING", self.context(out))
        self.assertIn(f"answer --id {qid} --as grace", self.context(out))
        self.assertIsNone(self.hook(self.b, "UserPromptSubmit", "--as", "grace"))

    def test_stop_hook_keeps_the_agent_going_when_its_answer_arrives(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        self.assertIsNone(self.hook(self.a, "Stop"))  # still pending: let it stop
        self.answer(self.b, qid, "grace", "materialized view backups_report_v1")
        out = self.hook(self.a, "Stop")
        self.assertEqual(out["decision"], "block")
        self.assertIn(f"ANSWERED: your {qid}", out["reason"])
        self.assertIn("backups_report_v1", out["reason"])
        self.assertIsNone(self.hook(self.a, "Stop"), "a second stop must not loop")

    def test_first_run_does_not_replay_old_answers(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        self.answer(self.b, qid, "grace")
        self.advance(hours=1)
        self.assertIsNone(self.hook(self.a, "SessionStart"))

    def test_answer_filed_and_answered_between_checks_is_reported(self):
        self.team()
        self.hook(self.a, "SessionStart")
        self.advance(minutes=1)
        qid = self.ask(self.a, "ada", "grace")
        self.answer(self.b, qid, "grace")
        self.assertIn(f"ANSWERED: your {qid}",
                      self.context(self.hook(self.a, "UserPromptSubmit")))

    def test_expiry_tells_the_agent_to_apply_its_default(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace", "--deadline", "30m",
                       "--default", "proceed with A")
        self.hook(self.a, "SessionStart")
        self.advance(hours=1)
        self.ok(self.b, "sweep")
        self.assertIn("apply your declared default: proceed with A",
                      self.context(self.hook(self.a, "UserPromptSubmit")))
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["status"], "expired")

    def test_post_tool_use_is_throttled(self):
        self.team()
        self.hook(self.b, "PostToolUse", "--as", "grace")   # pulls, records the time
        qid = self.ask(self.a, "ada", "grace")
        self.assertIsNone(self.hook(self.b, "PostToolUse", "--as", "grace"))
        self.advance(seconds=31)
        self.assertIn(qid, self.context(self.hook(self.b, "PostToolUse", "--as", "grace")))

    def test_identity_comes_from_env_then_join_and_missing_is_silent(self):
        self.team()
        qid = self.ask(self.b, "ada", "grace")
        with mock.patch.dict(os.environ, {"ROUNDTABLE_AGENT": "Grace"}):
            self.assertIn(qid, self.context(self.hook(self.a, "SessionStart")))
        self.assertIsNone(self.hook(self.b, "SessionStart"))  # nobody joined from b
        result = self.run_cli(self.a, "hook", "--as", "ada", stdin="not json")
        self.assertEqual((result.code, result.out), (0, ""))

    def test_hook_without_a_repo_never_fails_the_session(self):
        with mock.patch.object(collab.sys, "argv", ["collab.py", "hook"]):
            self.assertIsNone(collab.cmd_hook(collab.argparse.Namespace(
                repo=None, as_name=None, min_interval=30, command="hook")))


class CloneLock(helpers.RoundtableTestCase):
    def hold(self, clone):
        lock = clone / ".git" / "roundtable.lock"
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.stdin.close)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_commands_release_the_lock_when_they_return(self):
        # in-process callers (and these tests) run many commands in one process
        self.team()
        self.ask(self.a, "ada", "grace")
        self.assertEqual(collab._held_locks, {})
        self.hold(self.a).stdin.close()

    def test_hook_stays_silent_while_another_process_holds_the_clone(self):
        self.team()
        self.ask(self.b, "grace", "ada")
        holder = self.hold(self.a)
        started = time.monotonic()
        result = self.run_cli(self.a, "hook", stdin='{"hook_event_name": "Stop"}')
        self.assertEqual((result.code, result.out), (0, ""))
        self.assertLess(time.monotonic() - started, 10)
        holder.stdin.close()
        holder.wait()
        result = self.run_cli(self.a, "hook", stdin='{"hook_event_name": "Stop"}')
        self.assertIn("decision", result.out)       # delivered on the next event

    def test_watcher_and_agent_can_share_one_clone(self):
        """A human ran `watch` in the agent's own clone: concurrent pulls broke
        FETCH_HEAD ("Cannot rebase onto multiple branches") and a pull landed on
        an uncommitted question file."""
        # the watcher is a real process on the real clock; keep ours in step
        self.clock = collab.datetime.now(collab.timezone.utc).replace(microsecond=0)
        self.team()
        watcher = subprocess.Popen(
            [sys.executable, COLLAB, "--repo", str(self.a), "watch", "--as", "ada",
             "--interval", "1", "--timeout", "40"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            qids = [self.ask(self.a, "ada", "grace", question=f"q{i}?") for i in range(5)]
            for qid in qids:
                self.ok(self.b, "sync")
                self.answer(self.b, qid, "grace", "use the view")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                self.ok(self.a, "sync")
                if all(self.load(self.qpath(self.a, q))[0]["status"] == "answered"
                       for q in qids):
                    break
            time.sleep(3)                        # two more passes to report them
        finally:
            watcher.terminate()
            out, err = watcher.communicate(timeout=30)
        self.assertNotIn("sync failed", err)
        self.assertNotIn("multiple branches", err)
        self.assert_team_state(self.a)
        for qid in qids:
            self.assertIn(f"= {qid}  answered by grace: use the view", out)


if __name__ == "__main__":
    unittest.main()
