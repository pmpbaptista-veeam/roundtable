"""Shared fixture for the collab.py tests.

Every test gets a throwaway bare "remote" plus two clones (a and b) in the
system temp dir, a frozen clock, and stubs for notifications and sleeping, so
nothing reaches the desktop, a webhook, or the real wall clock.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from collections import namedtuple
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "roundtable" / "skills" / "roundtable" / "scripts"

sys.dont_write_bytecode = True  # keep __pycache__ out of the shipped skill folder
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import collab  # noqa: E402

T0 = "2026-08-20T09:00:00Z"
Result = namedtuple("Result", "code out err")


def git_run(cwd, *args, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args],
                            capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def configure_identity(repo):
    git_run(repo, "config", "user.name", repo.name)
    git_run(repo, "config", "user.email", f"{repo.name}@test.local")
    git_run(repo, "config", "commit.gpgsign", "false")


def _no_sleep(seconds):
    raise AssertionError("collab slept — pass --timeout 0 --interval 0")


class RoundtableTestCase(unittest.TestCase):
    """A remote with one initial commit, and clones self.a and self.b of it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="roundtable-test-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

        env = mock.patch.dict(os.environ, {"HOME": str(self.tmp),
                                           "XDG_CONFIG_HOME": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        # keep the developer's git config, hooks, signing and templates out
        for key in list(os.environ):
            if key.startswith("GIT_") or key in ("ROUNDTABLE_NOTIFY_WEBHOOK",
                                                 "TEAM_MEMORY_REPO",
                                                 # set when run inside Claude Code
                                                 "CLAUDECODE", "ROUNDTABLE_AGENT",
                                                 "ROUNDTABLE_RELAY_HUMAN"):
                os.environ.pop(key)
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ["GIT_TERMINAL_PROMPT"] = "0"

        self.clock = collab.parse_ts(T0)
        self.notified = []
        for target, attr, value in (
                (collab, "now", lambda: self.clock),
                (collab, "_notify", lambda handle, msg: self.notified.append((handle, msg))),
                (collab, "time", types.SimpleNamespace(monotonic=time.monotonic,
                                                       sleep=_no_sleep))):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True, capture_output=True)
        git_run(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True, capture_output=True)
        git_run(seed, "symbolic-ref", "HEAD", "refs/heads/main")
        configure_identity(seed)
        (seed / "README.md").write_text("team memory\n", encoding="utf-8")
        git_run(seed, "add", "README.md")
        git_run(seed, "commit", "-q", "-m", "initial commit")
        git_run(seed, "push", "-q", str(self.remote), "main")
        self.a = self.clone("a")
        self.b = self.clone("b")

    # -- repos
    def clone(self, name):
        path = self.tmp / name
        subprocess.run(["git", "clone", "-q", str(self.remote), str(path)],
                       check=True, capture_output=True)
        configure_identity(path)
        return path

    def git(self, clone, *args):
        return git_run(clone, *args)

    def commit_raw(self, clone, rel, text, push=True):
        """Commit a file exactly as given: fakes v1.0.0 files and hand edits."""
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git_run(clone, "add", rel)
        git_run(clone, "commit", "-q", "-m", f"raw: {rel}")
        if push:
            git_run(clone, "push", "-q")
        return path

    def raw_retire(self, clone, name):
        rel = f"agents/{name}.yaml"
        text = (clone / rel).read_text(encoding="utf-8")
        self.assertIn("status: active", text)
        self.commit_raw(clone, rel, text.replace("status: active", "status: retired"))

    def reject_pushes(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

    def remote_head(self):
        return subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "refs/heads/main"],
            check=True, capture_output=True, text=True).stdout.strip()

    def remote_decisions(self):
        out = subprocess.run(
            ["git", "--git-dir", str(self.remote), "ls-tree", "--name-only",
             "refs/heads/main", "decisions/"],
            check=True, capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if line]

    def assert_team_state(self, clone):
        """The clone holds exactly what the team has: no stray commit or file."""
        git_run(clone, "fetch", "-q")
        self.assertEqual(git_run(clone, "rev-parse", "HEAD").strip(), self.remote_head())
        self.assertEqual(git_run(clone, "status", "--porcelain"), "")

    # -- running the CLI in-process
    def run_cli(self, clone, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        argv = ["collab.py", "--repo", str(clone), *[str(a) for a in argv]]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                collab.main()
            except SystemExit as exc:
                code = 0 if exc.code is None else exc.code
        return Result(code, out.getvalue(), err.getvalue())

    def ok(self, clone, *argv, stdin=""):
        result = self.run_cli(clone, *argv, stdin=stdin)
        self.assertEqual(result.code, 0,
                         f"{' '.join(map(str, argv))} exited {result.code}: {result.err}")
        return result.out

    def fails(self, clone, *argv, code=1, stdin=""):
        result = self.run_cli(clone, *argv, stdin=stdin)
        self.assertEqual(result.code, code,
                         f"{' '.join(map(str, argv))} exited {result.code}, "
                         f"wanted {code}: {result.err}{result.out}")
        return result

    def ask(self, clone, frm, to, *extra, question="Which fields will backups expose?"):
        out = self.ok(clone, "ask", "--as", frm, "--to", to, "--question", question, *extra)
        return out.splitlines()[0]

    def answer(self, clone, qid, as_, text="Option A."):
        return self.ok(clone, "answer", "--id", qid, "--as", as_, "--answer", text)

    def decide(self, clone, as_, title, topics, *extra, body="## Decision\n\nDo it.\n"):
        out = self.ok(clone, "decide", "--as", as_, "--title", title,
                      "--topics", topics, "--body", body, *extra)
        return out.strip().splitlines()[-1]

    def team(self):
        self.ok(self.a, "join", "--name", "ada", "--human", "pedro.baptista",
                "--topics", "api,reports", "--escalation", "dana.lead")
        self.ok(self.a, "join", "--name", "grace", "--human", "dana.lead",
                "--topics", "db-schema,reports", "--escalation", "cto.office,vp.eng")

    @contextmanager
    def stale(self):
        """Skip the start-of-command pull: the clone acts on an outdated view,
        as it would when a teammate pushed a moment after it synced."""
        with mock.patch.object(collab.Repo, "sync", lambda self: None):
            yield

    # -- clock
    def advance(self, **delta):
        self.clock = self.clock + timedelta(**delta)

    def set_time(self, iso):
        self.clock = collab.parse_ts(iso)

    # -- files
    def load(self, path):
        return collab.load_doc(Path(path))

    def qpath(self, clone, qid):
        matches = list((clone / "inbox").glob(f"*/{qid}.md"))
        self.assertEqual(len(matches), 1, f"{qid} not found once in {clone}")
        return matches[0]

    def dpath(self, clone, did):
        return clone / "decisions" / f"{did}.md"

    def question_files(self, clone):
        return sorted((clone / "inbox").glob("*/Q-*.md"))

    def decision_files(self, clone):
        return sorted((clone / "decisions").glob("D-*.md"))
