import io
import os
import re
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest import mock

import helpers
from helpers import collab


def git_version():
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


class ConcurrentDecide(helpers.RoundtableTestCase):
    def test_concurrent_overlapping_decide_undone_exit_4(self):
        self.team()
        theirs = self.decide(self.b, "grace", "B reports rule", "reports")
        with self.stale():
            result = self.fails(self.a, "decide", "--as", "ada", "--title", "A reports rule",
                                "--topics", "reports", "--body", "b", code=4)
        self.assertIn(theirs, result.err)
        self.assertIn("NOT recorded", result.err)
        self.assert_team_state(self.a)
        self.assertEqual([p.stem for p in self.decision_files(self.a)], [theirs])
        self.assertEqual(len(self.remote_decisions()), 1)

    def test_coexists_does_not_cover_unseen_concurrent_decision(self):
        # --coexists speaks only for the decisions the pre-check listed
        self.team()
        theirs = self.decide(self.b, "grace", "B reports rule", "reports")
        with self.stale():
            result = self.fails(self.a, "decide", "--as", "ada", "--title", "A reports rule",
                                "--topics", "reports", "--body", "b", "--coexists", code=4)
        self.assertIn(theirs, result.err)
        self.assert_team_state(self.a)
        self.assertEqual(len(self.remote_decisions()), 1)

    def test_coexists_with_listed_decision_lands_after_rebase(self):
        self.team()
        listed = self.decide(self.a, "ada", "Listed reports rule", "reports")
        self.ok(self.b, "sync")
        self.decide(self.b, "grace", "Unrelated schema rule", "db-schema")
        with self.stale():
            did = self.decide(self.a, "ada", "A reports rule", "reports", "--coexists")
        self.assert_team_state(self.a)
        self.assertEqual(self.load(self.dpath(self.a, listed))[0]["status"], "active")
        self.assertIn(f"decisions/{did}.md", self.remote_decisions())
        self.assertEqual(len(self.remote_decisions()), 3)

    def test_undo_keeps_other_unpushed_commits(self):
        self.team()
        self.commit_raw(self.a, "notes.txt", "local note\n", push=False)
        self.decide(self.b, "grace", "B reports rule", "reports")
        with self.stale():
            self.fails(self.a, "decide", "--as", "ada", "--title", "A reports rule",
                       "--topics", "reports", "--body", "b", code=4)
        self.assertTrue((self.a / "notes.txt").exists())
        self.assertEqual(self.git(self.a, "log", "--format=%s", "@{u}..HEAD").strip(),
                         "raw: notes.txt")
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")

    def test_recheck_undo_restores_supersede_target(self):
        self.team()
        d1 = self.decide(self.a, "ada", "Alpha", "reports")
        self.ok(self.b, "sync")
        self.advance(seconds=1)
        self.decide(self.b, "grace", "Bravo", "reports", "--coexists")
        self.advance(seconds=1)
        with self.stale():
            self.fails(self.a, "decide", "--as", "ada", "--title", "Charlie",
                       "--topics", "reports", "--supersedes", d1, "--body", "b", code=4)
        self.assertEqual(self.load(self.dpath(self.a, d1))[0]["status"], "active")
        self.assert_team_state(self.a)


class CommitPush(helpers.RoundtableTestCase):
    def test_recheck_not_called_when_first_push_succeeds(self):
        path = self.a / "notes.txt"
        path.write_text("x\n", encoding="utf-8")
        recheck = mock.Mock(return_value=None)
        collab.Repo(self.a).commit_push([path], "raw: x", "tester", recheck=recheck)
        recheck.assert_not_called()
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD").strip(), self.remote_head())

    def test_recheck_called_after_rebase(self):
        self.commit_raw(self.b, "theirs.txt", "b\n")
        path = self.a / "mine.txt"
        path.write_text("a\n", encoding="utf-8")
        recheck = mock.Mock(return_value=None)
        collab.Repo(self.a).commit_push([path], "raw: mine", "tester", recheck=recheck)
        recheck.assert_called_once_with()
        self.assert_team_state(self.a)
        self.assertTrue((self.a / "theirs.txt").exists())

    def test_commit_holds_only_the_named_paths(self):
        (self.a / "README.md").write_text("team memory\nstaged by a human\n", encoding="utf-8")
        self.git(self.a, "add", "README.md")
        path = self.a / "mine.txt"
        path.write_text("a\n", encoding="utf-8")
        collab.Repo(self.a).commit_push([path], "raw: mine", "tester")
        self.assertEqual(self.git(self.a, "show", "--name-only", "--format=", "HEAD").split(),
                         ["mine.txt"])
        self.assertEqual(self.git(self.a, "diff", "--cached", "--name-only").split(),
                         ["README.md"])

    def test_undo_refuses_a_commit_with_foreign_files(self):
        # a commit that touches files the command did not write is left alone
        for name in ("mine.txt", "human.txt"):
            (self.a / name).write_text("x\n", encoding="utf-8")
            self.git(self.a, "add", name)
        env = dict(os.environ, GIT_AUTHOR_NAME="roundtable:tester",
                   GIT_AUTHOR_EMAIL="tester@roundtable.local")
        subprocess.run(["git", "-C", str(self.a), "commit", "-q", "-m", "raw: mine"],
                       check=True, capture_output=True, env=env)
        head = self.git(self.a, "rev-parse", "HEAD")
        repo = collab.Repo(self.a)
        with redirect_stderr(io.StringIO()) as err:
            undone = repo._drop_own_commit("tester", [self.a / "mine.txt"], head.strip())
        self.assertFalse(undone)
        self.assertIn("human.txt", err.getvalue())
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD"), head)
        self.assertTrue((self.a / "human.txt").exists())

    def test_undo_accepts_a_commit_the_rebase_dropped(self):
        # an identical change already upstream: the rebase drops ours, nothing to undo
        self.commit_raw(self.b, "same.txt", "x\n")
        self.commit_raw(self.a, "notes.txt", "local note\n", push=False)
        path = self.a / "same.txt"
        path.write_text("x\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
            collab.Repo(self.a).commit_push([path], "raw: same", "tester",
                                            recheck=lambda: ("stop", "", 4))
        self.assertEqual(stop.exception.code, 4)
        self.assertNotIn("warning", err.getvalue())
        self.assertEqual(self.git(self.a, "log", "--format=%s", "@{u}..HEAD").strip(),
                         "raw: notes.txt")

    def test_push_rejected_by_remote_is_undone(self):
        self.team()
        self.reject_pushes()
        result = self.fails(self.a, "ask", "--as", "ada", "--to", "grace",
                            "--question", "hello?")
        self.assertIn("undone", result.err)
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD").strip(), self.remote_head())
        self.assertEqual(self.question_files(self.a), [])
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")


class HumanWorkSurvivesUndo(helpers.RoundtableTestCase):
    """A clone can hold a human's uncommitted work (SKILL.md lets humans edit
    question files directly); undoing a failed command must never touch it."""

    def test_staged_human_edit_survives_undo(self):
        self.team()
        (self.a / "README.md").write_text("team memory\nHUMAN EDIT\n", encoding="utf-8")
        self.git(self.a, "add", "README.md")
        self.reject_pushes()
        self.fails(self.a, "ask", "--as", "ada", "--to", "grace", "--question", "q?")
        self.assertIn("HUMAN EDIT", (self.a / "README.md").read_text(encoding="utf-8"))
        # reset --keep keeps the content but not the staging
        self.assertEqual(self.git(self.a, "status", "--porcelain").splitlines(),
                         [" M README.md"])
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD").strip(), self.remote_head())
        self.assertEqual(self.question_files(self.a), [])

    def test_untracked_inbox_file_survives_failed_join(self):
        self.team()
        note = self.a / "inbox" / "pedro.baptista" / "my-notes.txt"
        note.write_text("draft thoughts\n", encoding="utf-8")
        self.reject_pushes()
        self.fails(self.a, "join", "--name", "eve", "--human", "eve.h")
        self.assertEqual(note.read_text(encoding="utf-8"), "draft thoughts\n")
        self.assertFalse((self.a / "agents" / "eve.yaml").exists())
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD").strip(), self.remote_head())

    def test_hand_edited_agent_file_survives_failed_join(self):
        self.team()
        grace = self.a / "agents" / "grace.yaml"
        grace.write_text(grace.read_text(encoding="utf-8") + "# hand note\n", encoding="utf-8")
        self.reject_pushes()
        self.fails(self.a, "join", "--name", "eve", "--human", "eve.h")
        self.assertIn("# hand note", grace.read_text(encoding="utf-8"))
        self.assertFalse((self.a / "agents" / "eve.yaml").exists())
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD").strip(), self.remote_head())

    def test_join_commits_only_its_own_files(self):
        self.team()
        (self.a / "inbox" / "pedro.baptista" / "my-notes.txt").write_text("x\n", encoding="utf-8")
        self.ok(self.a, "join", "--name", "eve", "--human", "eve.h")
        self.assertEqual(
            sorted(self.git(self.a, "show", "--name-only", "--format=", "HEAD").split()),
            ["agents/eve.yaml", "inbox/eve.h/.gitkeep", "inbox/eve/.gitkeep"])

    def test_unstaged_unrelated_edit_survives_lost_race(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        (self.a / "README.md").write_text("team memory\nlocal edit\n", encoding="utf-8")
        self.answer(self.b, qid, "grace", "B wins")
        self.fails(self.a, "answer", "--id", qid, "--as", "grace", "--answer", "A loses")
        self.assertIn("local edit", (self.a / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(self.git(self.a, "log", "--format=%s", "@{u}..HEAD"), "")


def install_hook(clone, name, script):
    hook = clone / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    hook.chmod(0o755)


PREFIX_SUBJECT = 'printf "[team] %s" "$(cat "$1")" > "$1"\n'


class SubjectRewritingHook(helpers.RoundtableTestCase):
    """A prepare-commit-msg hook that prefixes every subject (a common corporate
    hook): the undo must identify our commit without trusting its subject."""

    def stale_overlapping_decide(self, code=4):
        theirs = self.decide(self.b, "grace", "B reports rule", "reports")
        with self.stale():
            result = self.fails(self.a, "decide", "--as", "ada", "--title", "A reports rule",
                                "--topics", "reports", "--body", "b", code=code)
        return theirs, result

    def test_stale_overlapping_decide_is_undone(self):
        self.team()
        install_hook(self.a, "prepare-commit-msg", PREFIX_SUBJECT)
        theirs, result = self.stale_overlapping_decide()
        self.assertIn("NOT recorded", result.err)
        self.assertNotIn("warning", result.err)
        self.assert_team_state(self.a)
        self.ask(self.a, "ada", "grace")
        self.assertEqual(self.remote_decisions(), [f"decisions/{theirs}.md"])

    def test_lost_answer_race_is_undone(self):
        self.team()
        install_hook(self.a, "prepare-commit-msg", PREFIX_SUBJECT)
        qid = self.ask(self.a, "ada", "grace")
        self.answer(self.b, qid, "grace", "B wins")
        with self.stale():
            result = self.fails(self.a, "answer", "--id", qid, "--as", "grace",
                                "--answer", "A loses")
        self.assertIn("was undone", result.err)
        self.assert_team_state(self.a)
        self.assertNotIn("warning", self.run_cli(self.a, "sync").err)

    def test_undo_falls_back_to_the_path_check_without_patch_id(self):
        self.team()
        install_hook(self.a, "prepare-commit-msg", PREFIX_SUBJECT)
        with mock.patch.object(collab.Repo, "_patch_id", new=lambda self, base, tip: ""):
            self.stale_overlapping_decide()
        self.assert_team_state(self.a)

    def test_unproven_ownership_warns_and_keeps_the_commit(self):
        # different patch ids: the rebased top commit cannot be proven ours
        self.team()
        with mock.patch.object(collab.Repo, "_patch_id", new=lambda self, base, tip: base):
            _, result = self.stale_overlapping_decide()
        self.assertIn("warning: could not undo", result.err)
        self.assertIn("still committed locally", result.err)
        self.assertNotIn("NOT recorded", result.err)
        self.assertEqual(len(self.git(self.a, "log", "--format=%s", "@{u}..HEAD").splitlines()), 1)

    def test_recheck_wording_when_the_undo_fails(self):
        self.team()
        with mock.patch.object(collab.Repo, "_drop_own_commit", return_value=False):
            _, result = self.stale_overlapping_decide()
        self.assertIn("rejected, but is still committed locally", result.err)
        self.assertNotIn("NOT recorded", result.err)

    def test_hook_added_file_blocks_the_undo(self):
        # a pre-commit hook adds a file this command did not write
        self.team()
        install_hook(self.a, "pre-commit", "echo hooked >> hooked.txt\ngit add hooked.txt\n")
        _, result = self.stale_overlapping_decide()
        self.assertIn("it also touches hooked.txt", result.err)
        self.assertIn("still committed locally", result.err)
        self.assertNotIn("was undone", result.err)
        self.assertTrue((self.a / "hooked.txt").exists())
        self.assertEqual(len(self.git(self.a, "log", "--format=%s", "@{u}..HEAD").splitlines()), 1)


class ContentRewritingHook(helpers.RoundtableTestCase):
    def test_restaging_precommit_hook_leaves_clone_clean(self):
        # lint-staged style: the hook rewrites staged files and re-adds them
        self.team()
        install_hook(self.a, "pre-commit", 'for f in $(git diff --cached --name-only); do '
                                           'echo "# lint" >> "$f"; git add "$f"; done\n')
        qid = self.ask(self.a, "ada", "grace", "--blocking", "--default", "hold")
        rel = self.qpath(self.a, qid).relative_to(self.a).as_posix()
        self.assertIn("# lint", self.git(self.a, "show", f"HEAD:{rel}"))
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")
        self.assertNotIn("warning", self.run_cli(self.a, "sync").err)
        self.answer(self.b, qid, "grace", "Option B.")
        out = self.ok(self.a, "wait", "--id", qid, "--timeout", "0", "--interval", "0")
        self.assertIn("Option B.", out)

    def test_foreign_file_staging_precommit_hook_leaves_clone_clean(self):
        # the hook adds a file the command did not write (generated/version files)
        self.team()
        install_hook(self.a, "pre-commit",
                     "git rev-parse HEAD > .last-commit; git add .last-commit\n")
        qid = self.ask(self.a, "ada", "grace", "--blocking", "--default", "hold")
        self.assertIn(".last-commit", self.git(self.a, "show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(self.git(self.a, "status", "--porcelain"), "")
        self.assertNotIn("warning", self.run_cli(self.a, "sync").err)
        self.answer(self.b, qid, "grace", "Option B.")
        out = self.ok(self.a, "wait", "--id", qid, "--timeout", "0", "--interval", "0")
        self.assertIn("Option B.", out)


@unittest.skipUnless(shutil.which("ssh-keygen") and git_version() >= (2, 34),
                     "needs ssh-keygen and git >= 2.34 for SSH commit signing")
class SignedClone(helpers.RoundtableTestCase):
    def sign(self, clone):
        key = self.tmp / "signing-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       check=True, capture_output=True)
        allowed = self.tmp / "allowed-signers"
        allowed.write_text(f"tester@test.local {(self.tmp / 'signing-key.pub').read_text()}",
                           encoding="utf-8")
        for key_name, value in (("gpg.format", "ssh"), ("user.signingkey", str(key)),
                                ("commit.gpgsign", "true"),
                                ("gpg.ssh.allowedSignersFile", str(allowed)),
                                ("log.showSignature", "true")):
            self.git(clone, "config", key_name, value)

    def test_undo_works_with_show_signature(self):
        self.team()
        self.sign(self.a)
        theirs = self.decide(self.b, "grace", "B reports rule", "reports")
        with self.stale():
            self.fails(self.a, "decide", "--as", "ada", "--title", "A reports rule",
                       "--topics", "reports", "--body", "b", code=4)
        self.assert_team_state(self.a)
        # the next ordinary command must not push the refused decision
        self.ask(self.a, "ada", "grace")
        self.assertEqual(self.remote_decisions(), [f"decisions/{theirs}.md"])
        self.assertIn("Good", self.git(self.a, "log", "-1", "--show-signature"))


class ConcurrentAnswer(helpers.RoundtableTestCase):
    def lose_answer_race(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        self.answer(self.b, qid, "grace", "B wins")
        with self.stale():
            result = self.fails(self.a, "answer", "--id", qid, "--as", "grace",
                                "--answer", "A loses")
        self.assertIn("won the race", result.err)
        return qid

    def test_lost_answer_race_returns_clone_to_team_state(self):
        qid = self.lose_answer_race()
        self.assert_team_state(self.a)
        _, body = self.load(self.qpath(self.a, qid))
        self.assertIn("B wins", body)
        self.assertNotIn("A loses", body)

    def test_sync_clean_after_lost_race(self):
        self.lose_answer_race()
        result = self.run_cli(self.a, "sync")
        self.assertEqual(result.code, 0)
        self.assertNotIn("warning", result.err)

    def test_withdraw_vs_answer_race(self):
        self.team()
        qid = self.ask(self.a, "ada", "grace")
        self.answer(self.b, qid, "grace", "B wins")
        with self.stale():
            self.fails(self.a, "withdraw", "--id", qid, "--as", "ada", "--reason", "moot")
        self.assert_team_state(self.a)
        self.assertEqual(self.load(self.qpath(self.a, qid))[0]["status"], "answered")


class NoRemote(helpers.RoundtableTestCase):
    def test_no_remote_mode(self):
        solo = self.tmp / "solo"
        subprocess.run(["git", "init", "-q", str(solo)], check=True, capture_output=True)
        helpers.configure_identity(solo)
        (solo / "README.md").write_text("solo\n", encoding="utf-8")
        self.git(solo, "add", "README.md")
        self.git(solo, "commit", "-q", "-m", "initial commit")
        self.ok(solo, "join", "--name", "ada", "--human", "pedro.baptista")
        self.ok(solo, "join", "--name", "grace", "--human", "dana.lead")
        qid = self.ask(solo, "ada", "grace")
        self.answer(solo, qid, "grace", "Option A.")
        did = self.decide(solo, "ada", "Solo rule", "api", "--question", qid)
        self.assertTrue(self.dpath(solo, did).exists())
        subjects = self.git(solo, "log", "--format=%s").splitlines()
        self.assertEqual(len(subjects), 6, subjects)
        self.assertEqual(self.git(solo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
