#!/usr/bin/env python3
"""Roundtable — team coordination CLI over a shared git repo.

Stdlib only. See references/protocol.md for the file formats and rules
this tool implements. Subcommands:

  join     register this agent in the team memory
  agents   list the agent roster
  retire   move an agent to retired (agents are never deleted)
  ask      file a question to another agent or a human
  wait     block on a question until answered (handles escalation)
  inbox    list open questions addressed to me (or escalated to me)
  answer   answer a question
  decide   record a decision with provenance
  retract  withdraw an active decision that nothing replaces
  recall   search past decisions
  status   my registration, my inbox, and my open asks
  show     print a question or decision in full
  withdraw take back a question that became moot
  sweep    apply overdue deadlines without anyone waiting
  watch    stay running and report arrivals and answers to my asks
  hook     Claude Code hook: deliver new questions and answers into a session
  quickstart  join if needed and print the cheat sheet
  sync     pull latest team memory
"""

import argparse
import json
import os
import random
import re
import string
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- utilities

UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
ID_FMT = "%Y%m%dT%H%M%SZ"
# One `wait` call polls for at most this long: agent harnesses cap how long a
# single command may run, and a killed wait is indistinguishable from a wait
# that found nothing. Re-enter it (or wait on the check-in) to keep waiting.
MAX_WAIT_SECONDS = 570


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ts(dt: datetime) -> str:
    return dt.strftime(UTC_FMT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, UTC_FMT).replace(tzinfo=timezone.utc)


def parse_deadline(value: str, base: datetime) -> datetime:
    """Accept ISO-8601 UTC or shorthand like 45m / 2h / 1d."""
    m = re.fullmatch(r"(\d+)([mhd])", value.strip())
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=amount),
                 "h": timedelta(hours=amount),
                 "d": timedelta(days=amount)}[unit]
        return base + delta
    try:
        return parse_ts(value.strip())
    except ValueError:
        die(f"--deadline '{value}' is not a valid deadline — use ISO-8601 UTC "
            "like 2026-08-20T13:30:00Z, or 45m / 2h / 1d")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > 60:
        # cut on a word boundary: ids are read aloud and shown on screen
        slug = slug[:60].rsplit("-", 1)[0] or slug[:60]
    return slug.strip("-") or "untitled"


def rand_suffix(n: int = 4) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def die(message: str, code: int = 1):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


HANDLE_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
QID_RE = re.compile(r"Q-[A-Za-z0-9-]+")


def norm_handle(value, flag: str = "handle") -> str:
    """Lowercase a handle and refuse anything that is unsafe as a path component."""
    handle = str(value).strip().lower()
    if not HANDLE_RE.fullmatch(handle) or ".." in handle:
        die(f"{flag} '{value}' is not a valid handle — use lowercase letters, digits, "
            "'.', '_' or '-', starting with a letter or digit (e.g. pedro.baptista)")
    return handle


def _lower(value) -> str:
    """A handle read from disk, for comparison: v1.0.0 stored some verbatim."""
    return "" if value is None else str(value).lower()


def _lowers(values) -> set:
    return {str(v).lower() for v in values or [] if v}


# ------------------------------------------------------- frontmatter format

def _bare_safe(value: str) -> bool:
    """Bare only if it reads back as this same string, including under v1.0.0's
    reader, which json-decodes every value ("2026" would come back an int)."""
    if value in ("null", "true", "false") or not re.fullmatch(r"[A-Za-z0-9._:@/-]+", value):
        return False
    try:
        json.loads(value)
    except ValueError:
        return True
    return False


def fm_dump_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, str) and _bare_safe(value):
        return value
    if isinstance(value, str):
        return json.dumps(value)
    return str(value)


def fm_parse_value(raw: str):
    raw = raw.strip()
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    # no field is numeric: a number here is a v1.0.0 bare string (title: 2026)
    return raw if isinstance(value, (int, float)) else value


def dump_doc(meta: dict, body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {fm_dump_value(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def load_doc(path: Path):
    # split on "\n" only, so the body keeps any other line-separator characters
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines[0].rstrip() != "---":
        die(f"{path} has no frontmatter block")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        die(f"{path} has an unterminated frontmatter block")
    meta = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        meta[key.strip()] = fm_parse_value(raw)
    return meta, "\n".join(lines[end + 1:]).strip()


# ----------------------------------------------------------------- git layer

# ---------------------------------------------------------------- clone lock
#
# Git is not safe for two processes pulling, rebasing and committing in one
# working tree: concurrent fetches corrupt FETCH_HEAD ("Cannot rebase onto
# multiple branches"), a pull lands while another process has a written but
# uncommitted question file, and a failed pull's `rebase --abort` can kill a
# rebase that belongs to someone else. A human's watcher, an agent's monitor,
# the delivery hook and the agent's own commands all share a clone in
# practice, so every roundtable process takes this lock before touching it.

LOCK_WAIT_SECONDS = 60
EXIT_CLONE_BUSY = 9
_lock_sleep = time.sleep  # real backoff, whatever a caller does to `time`
_held_locks = {}          # lock path -> [file handle, depth]; reentrant per process
_process_holds = []       # locks held for a whole command, released by main()


class CloneBusy(Exception):
    pass


def _try_lock(fh):
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:   # Windows
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)


class clone_lock:
    """Exclusive, reentrant lock on a clone. Raises CloneBusy after `wait`s."""

    def __init__(self, git_dir: Path, wait: float = LOCK_WAIT_SECONDS):
        self.path = str(git_dir / "roundtable.lock")
        self.wait = wait

    def __enter__(self):
        if self.path in _held_locks:
            _held_locks[self.path][1] += 1
            return self
        fh = open(self.path, "a+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                _try_lock(fh)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise CloneBusy(self.path)
                _lock_sleep(0.2)
        _held_locks[self.path] = [fh, 1]
        return self

    def __exit__(self, *exc):
        entry = _held_locks[self.path]
        entry[1] -= 1
        if entry[1] == 0:
            del _held_locks[self.path]
            entry[0].close()            # closing the handle releases the lock
        return False


def _release_process_holds():
    while _process_holds:
        _process_holds.pop().__exit__(None, None, None)


class Repo:
    def __init__(self, root: Path):
        self.root = root
        if not (root / ".git").exists():
            die(f"{root} is not a git repository (set TEAM_MEMORY_REPO or --repo)")
        # .git is a file in a linked worktree; ask git where the real dir is
        self.git_dir = Path(self._git("rev-parse", "--absolute-git-dir").strip())
        self._has_remote = bool(self._git("remote").strip())

    def lock(self, wait: float = LOCK_WAIT_SECONDS):
        return clone_lock(self.git_dir, wait)

    def _git(self, *args, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True,
        )
        if check and result.returncode != 0:
            die(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def _ok(self, *args) -> bool:
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True).returncode == 0

    def _pull_rebase(self):
        """pull --rebase, never leaving the clone mid-rebase; git's error text or None."""
        result = subprocess.run(
            ["git", "-C", str(self.root), "pull", "--rebase", "--quiet"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return None
        subprocess.run(["git", "-C", str(self.root), "rebase", "--abort"],
                       capture_output=True)
        return result.stderr.strip() or "git pull --rebase failed"

    def sync(self):
        if not self._has_remote:
            return
        with self.lock():
            self._sync_locked()

    def _sync_locked(self):
        error = self._pull_rebase()
        if error:
            print(f"warning: sync failed ({error}) — working from local state",
                  file=sys.stderr)

    def commit_push(self, paths, message: str, actor: str, recheck=None):
        """Commit and push; on any failure after the commit, undo it. `recheck` runs
        after each rebase and returns None, or (reason, details, exit code) to abort."""
        with self.lock():
            self._commit_push_locked(paths, message, actor, recheck)

    def _commit_push_locked(self, paths, message: str, actor: str, recheck):
        names = [str(p) for p in paths]
        for name in names:
            self._git("add", name)
        # Guard on the INDEX, not on `status --porcelain`: the latter also
        # reports untracked files a teammate's editor left in the clone, which
        # would send an idempotent write into `git commit` with nothing staged.
        # Guard and commit only our paths: the rest of the index is a human's.
        nothing_staged = subprocess.run(
            ["git", "-C", str(self.root), "diff", "--cached", "--quiet", "--", *names],
            capture_output=True,
        ).returncode == 0
        if nothing_staged:
            return
        # The agent is the AUTHOR (audit trail); the committer — and any
        # commit signature (GPG) — stays the machine's real git identity.
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=f"roundtable:{actor}",
                   GIT_AUTHOR_EMAIL=f"{actor}@roundtable.local")
        result = subprocess.run(
            ["git", "-C", str(self.root), "commit", "--quiet", "-m", message, "--", *names],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            die(f"git commit failed: {result.stderr.strip()}")
        # a pre-commit hook's `git add` only reaches the commit's temporary index:
        # point the real index entries of every path the commit holds at HEAD again
        committed = self._git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r",
                              "-z", "--no-renames", "HEAD", check=False).split("\0")
        self._git("reset", "-q", "--", *names, *filter(None, committed), check=False)
        if not self._has_remote:
            return
        sha = self._git("rev-parse", "HEAD").strip()
        for attempt in range(3):
            push = subprocess.run(
                ["git", "-C", str(self.root), "push", "--quiet"],
                capture_output=True, text=True,
            )
            if push.returncode == 0:
                return
            error = self._pull_rebase()
            if error:
                outcome = self._undo_outcome(actor, names, sha, "your change was undone")
                die("a concurrent write to the same file won the race "
                    f"({error}) — {outcome}; sync and re-check before retrying")
            # the rebase can bring in state the caller's checks never saw
            problem = recheck() if recheck else None
            if problem:
                reason, details, code = problem
                outcome = self._undo_outcome(actor, names, sha, "your change was NOT recorded")
                die(f"{reason} — {outcome}." + (f"\n{details}" if details else ""), code)
        outcome = self._undo_outcome(actor, names, sha, "your change was undone")
        die(f"push failed after 3 rebase retries — {outcome}; report this to your human")

    def _undo_outcome(self, actor: str, paths, sha: str, done: str) -> str:
        if self._drop_own_commit(actor, paths, sha):
            return done
        return "your change was rejected, but is still committed locally (see the warning above)"

    def _drop_own_commit(self, actor: str, paths, sha: str) -> bool:
        """Remove our unpushed commit `sha`, or its rebased copy, and nothing
        else; False if it is left behind."""
        if self._ok("merge-base", "--is-ancestor", "HEAD", "@{u}"):
            return True
        if not self._ok("rev-parse", "--verify", "--quiet", "HEAD~1"):
            return self._undo_failed("it is the first commit in the repo")
        if self._git("rev-parse", "HEAD", check=False).strip() != sha \
                and not self._is_rebased_copy(sha, actor):
            if self._git("cherry", "@{u}", sha, f"{sha}~1", check=False).startswith("-"):
                return True  # the rebase dropped ours: that change was already upstream
            return self._undo_failed("the top commit cannot be proven to be this command's")
        touched = self._git("diff", "--name-only", "--no-renames", "-z",
                            "HEAD~1", "HEAD", check=False).split("\0")
        ours = {os.path.relpath(str(p), str(self.root)).replace(os.sep, "/") for p in paths}
        foreign = sorted(set(filter(None, touched)) - ours)
        if foreign:
            return self._undo_failed(f"it also touches {', '.join(foreign)}")
        # --keep, not --hard: a human's unrelated uncommitted edits must survive
        result = subprocess.run(
            ["git", "-C", str(self.root), "reset", "--quiet", "--keep", "HEAD~1"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return self._undo_failed(result.stderr.strip() or "git reset --keep failed")
        self._pull_rebase()
        return True

    def _is_rebased_copy(self, sha: str, actor: str) -> bool:
        """HEAD is `sha` replayed by a rebase. Hooks may rewrite the subject, but
        never the author or the patch."""
        author = self._git("log", "-1", "--no-show-signature", "--format=%an", check=False)
        if author.splitlines() != [f"roundtable:{actor}"]:
            return False
        ours, top = self._patch_id(f"{sha}~1", sha), self._patch_id("HEAD~1", "HEAD")
        # without patch ids, the author and the path check in the caller must do
        return ours == top if ours and top else True

    def _patch_id(self, base: str, tip: str) -> str:
        diff = subprocess.run(["git", "-C", str(self.root), "diff-tree", "-p", "--no-renames",
                               "--no-color", base, tip], capture_output=True)
        if diff.returncode != 0 or not diff.stdout:
            return ""
        pid = subprocess.run(["git", "-C", str(self.root), "patch-id", "--stable"],
                             input=diff.stdout, capture_output=True)
        words = pid.stdout.split()
        return words[0].decode() if pid.returncode == 0 and words else ""

    def _undo_failed(self, reason: str) -> bool:
        print(f"warning: could not undo the local commit ({reason}) — it is not pushed, "
              "but the next command's push would publish it; inspect it with: "
              f"git -C {self.root} show --stat HEAD", file=sys.stderr)
        return False

    # -- paths
    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def decisions_dir(self) -> Path:
        return self.root / "decisions"

    @property
    def inbox_dir(self) -> Path:
        return self.root / "inbox"

    def agent_file(self, name: str) -> Path:
        return self.agents_dir / f"{str(name).lower()}.yaml"

    def save_agent(self, name: str, record: dict):
        lines = [f"{k}: {fm_dump_value(v)}" for k, v in record.items()]
        self.agent_file(name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def inbox_for(self, handle) -> Path:
        """Inbox dir of a handle, reusing a mixed-case dir v1.0.0 created."""
        handle = norm_handle(handle, "inbox handle")
        if self.inbox_dir.is_dir():
            dirs = sorted(d for d in self.inbox_dir.iterdir() if d.is_dir())
            for d in dirs:
                if d.name == handle:
                    return d
            for d in dirs:
                if d.name.lower() == handle:
                    return d
        return self.inbox_dir / handle

    def load_agent(self, name: str):
        path = self.agent_file(name)
        if not path.exists():
            return None
        meta = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                key, raw = line.split(":", 1)
                meta[key.strip()] = fm_parse_value(raw)
        return meta

    def all_agents(self):
        if not self.agents_dir.exists():
            return []
        return [self.load_agent(p.stem) for p in sorted(self.agents_dir.glob("*.yaml"))]

    def find_question(self, qid: str) -> Path:
        # the id is globbed into a path, so '..' or '*' must never reach it
        if not QID_RE.fullmatch(qid or ""):
            die(f"'{qid}' is not a question id (Q-<timestamp>-<agent>-<suffix>)")
        matches = sorted(self.inbox_dir.glob(f"*/{qid}.md"))
        if not matches:
            die(f"question {qid} not found")
        if len(matches) > 1:
            where = ", ".join(p.relative_to(self.root).as_posix() for p in matches)
            die(f"question {qid} exists in several inboxes ({where}) — keep one copy "
                "and remove the others")
        return matches[0]

    def all_questions(self):
        if not self.inbox_dir.exists():
            return []
        return sorted(self.inbox_dir.glob("*/Q-*.md"))

    def all_decisions(self):
        if not self.decisions_dir.exists():
            return []
        return sorted(self.decisions_dir.glob("D-*.md"))

    def find_decision(self, did: str) -> Path:
        matches = [p for p in self.all_decisions() if p.stem == did]
        if not matches:
            die(f"decision {did} not found")
        return matches[0]


# Commands that poll for minutes lock per iteration instead (never across a
# sleep); everything else is short and holds the lock for its whole run, so
# its sync -> read -> write -> commit -> push sequence is never interleaved.
LONG_RUNNING = {"wait", "watch", "hook", "inbox"}


def open_repo(args) -> Repo:
    root = args.repo or os.environ.get("TEAM_MEMORY_REPO")
    if not root:
        die("no team memory repo: set TEAM_MEMORY_REPO or pass --repo")
    repo = Repo(Path(root).expanduser().resolve())
    if getattr(args, "command", None) not in LONG_RUNNING:
        hold = repo.lock()
        hold.__enter__()
        _process_holds.append(hold)
    return repo


# --------------------------------------------------------------- subcommands

def cmd_join(args):
    repo = open_repo(args)
    repo.sync()
    name = args.name.lower()
    if not re.fullmatch(r"[a-z0-9-]+", name):
        die("agent name must match [a-z0-9-]+")
    existing = repo.load_agent(name)
    if existing and existing.get("status") == "active":
        die(f"agent name '{name}' is taken — try '{name}-{rand_suffix(2)}'")
    if repo.inbox_for(name).exists() and not existing:
        die(f"'{name}' already exists as a human inbox — pick another name")
    if "." not in args.human:
        print(f"note: human handle '{args.human}' has no dot; make sure it "
              "cannot collide with an agent name", file=sys.stderr)
    boxes = [repo.inbox_for(handle) for handle in (name, args.human)]
    repo.agents_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "name": name,
        "human": args.human,
        "topics": [t.strip() for t in args.topics.split(",") if t.strip()],
        "escalation": [e.strip() for e in (args.escalation or "").split(",") if e.strip()],
        "status": "active",
        "joined": ts(now()),
    }
    repo.save_agent(name, record)
    for box in boxes:
        box.mkdir(parents=True, exist_ok=True)
        (box / ".gitkeep").touch()
    repo.commit_push([repo.agent_file(name)] + [box / ".gitkeep" for box in boxes],
                     f"join: agent {name} (human: {args.human})", name)
    # Lets `collab.py hook` know whose inbox to deliver without configuration.
    # First join wins: a clone shared by two agents needs $ROUNDTABLE_AGENT.
    identity = repo.git_dir / IDENTITY_FILE
    if not identity.exists():
        identity.write_text(name + "\n", encoding="utf-8")
    print(f"joined as '{name}' (responsible human: {args.human}, "
          f"topics: {', '.join(record['topics']) or '-'})")


def cmd_agents(args):
    repo = open_repo(args)
    repo.sync()
    agents = [a for a in repo.all_agents() if a]
    if not agents:
        print("no agents registered yet")
        return
    for a in agents:
        topics = ", ".join(a.get("topics") or [])
        print(f"{a['name']:<16} human={a['human']:<24} "
              f"status={a.get('status', '?'):<8} topics=[{topics}]")


def cmd_ask(args):
    repo = open_repo(args)
    repo.sync()
    asker = repo.load_agent(args.as_name)
    if not asker or asker.get("status") != "active":
        die(f"'{args.as_name}' is not an active agent — run join first")
    to = args.to
    target = repo.load_agent(to)
    if target:
        if target.get("status") != "active":
            die(f"agent '{to}' is not active")
        to_kind = "agent"
    else:
        known_humans = set()
        for a in repo.all_agents():
            if a:
                known_humans.add(_lower(a.get("human")))
                known_humans |= _lowers(a.get("escalation"))
        if to in known_humans or "." in to:
            to_kind = "human"
        else:
            die(f"'{to}' is neither a registered agent nor a known human "
                f"handle — check `collab.py agents` (human handles contain a dot)")
    if to_kind == "agent" and to == args.as_name:
        die("cannot ask yourself")
    if args.blocking and not args.default:
        die("a blocking question must declare --default: state now what you "
            "will do if nobody answers, while you can still think about it "
            "without time pressure")
    _self_sweep(repo, args.as_name)
    reply_to = getattr(args, "reply_to", None)
    if reply_to:
        # answers are first-write-wins, so a question raised inside an answer
        # continues the thread as a new question rather than a second answer
        prior, _ = load_doc(repo.find_question(reply_to))
        if args.as_name not in (_lower(prior.get("from")), _lower(prior.get("to"))):
            die(f"--reply-to {reply_to}: '{args.as_name}' is not a participant "
                "in that question")
    created = now()
    qid = f"Q-{created.strftime(ID_FMT)}-{args.as_name}-{rand_suffix()}"
    deadline = parse_deadline(args.deadline, created) if args.deadline else None
    if args.blocking and not deadline:
        deadline = created + timedelta(hours=4)
    meta = {
        "id": qid,
        "from": args.as_name,
        "to": to,
        "to_kind": to_kind,
        "blocking": args.blocking,
        "urgency": args.urgency,
        "deadline": ts(deadline) if deadline else None,
        "default": args.default,
        "status": "open",
        "created": ts(created),
        "context_decisions": [d.strip() for d in (args.context_decisions or "").split(",") if d.strip()],
        "in_reply_to": reply_to,
    }
    body = f"## Question\n\n{args.question}\n"
    if args.options:
        body += "\n## Options\n\n"
        for opt in args.options.split("|"):
            body += f"- {opt.strip()}\n"
    inbox = repo.inbox_for(to)
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{qid}.md"
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"ask: {qid} {args.as_name} -> {to}", args.as_name)
    print(qid)
    repo_flag = f"--repo {args.repo} " if args.repo else ""
    if args.blocking:
        print(f"blocking question filed — run: collab.py {repo_flag}wait --id {qid}",
              file=sys.stderr)
    else:
        print("non-blocking — continue working; check answers with: "
              "collab.py inbox / status", file=sys.stderr)


def _escalation_chain(repo: Repo, meta: dict):
    """Ordered handles to escalate to, per protocol.md."""
    if meta["to_kind"] == "agent":
        target = repo.load_agent(meta["to"]) or {}
        chain = [target.get("human")] + list(target.get("escalation") or [])
    else:
        asker = repo.load_agent(meta["from"]) or {}
        chain = list(asker.get("escalation") or [])
    already = _lowers(meta.get("escalated_to"))
    to = _lower(meta.get("to"))
    return [h for h in map(_lower, chain) if h and h not in already and h != to]


def _expire(repo: Repo, path: Path, meta: dict, body: str) -> dict:
    meta["status"] = "expired"
    meta["expired_at"] = ts(now())
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"expire: {meta['id']}", meta["from"])
    return meta


def _escalate(repo: Repo, path: Path, meta: dict, body: str) -> dict:
    chain = _escalation_chain(repo, meta)
    escalated = list(meta.get("escalated_to") or [])
    if not chain:
        return _expire(repo, path, meta, body)
    nxt = chain[0]
    escalated.append(nxt)
    # each hop gets half the previous recipient's window; files without
    # escalated_at (first hop, v1.0.0) measure from created
    window = parse_ts(meta["deadline"]) - parse_ts(meta.get("escalated_at") or meta["created"])
    at = now()
    meta["status"] = "escalated"
    meta["escalated_to"] = escalated
    meta["escalated_at"] = ts(at)
    meta["deadline"] = ts(at + max(timedelta(minutes=5), window / 2))
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"escalate: {meta['id']} -> {nxt}", meta["from"])
    _notify(nxt, f"[roundtable] escalated question {meta['id']} needs you")
    return meta


def _notify(handle: str, message: str):
    script = Path(__file__).parent / "notify.py"
    if script.exists():
        subprocess.run([sys.executable, str(script), "--to", handle,
                        "--message", message], capture_output=True)


def _wait_budget(meta: dict, interval: int) -> int:
    """How long a single `wait` call should poll.

    Never past the question's own deadline (after it, the loop escalates or
    expires instead), and never longer than one tool call can survive — a wait
    killed by its caller looks identical to a wait that found nothing.
    """
    if meta.get("deadline"):
        remaining = (parse_ts(meta["deadline"]) - now()).total_seconds() + interval
        return int(max(60, min(remaining, MAX_WAIT_SECONDS)))
    return MAX_WAIT_SECONDS


def _file_checkin(repo: Repo, path: Path, meta: dict, body: str, waited: float):
    """Ask the asker's own human whether to keep waiting.

    The point is autonomy without abandonment: with nobody reading the
    terminal, the only channel that reaches a person is the bus itself, so the
    check-in is a real question in their inbox with a notification behind it.
    """
    asker = repo.load_agent(meta["from"]) or {}
    human = _lower(asker.get("human"))
    if not human:
        return None
    inbox = repo.inbox_for(human)
    created = now()
    cid = f"Q-{created.strftime(ID_FMT)}-{meta['from']}-{rand_suffix()}"
    cmeta = {
        "id": cid,
        "from": meta["from"],
        "to": human,
        "to_kind": "human",
        "blocking": True,
        "urgency": meta.get("urgency") or "normal",
        "deadline": meta.get("deadline"),
        "default": "keep waiting on the original question",
        "status": "open",
        "created": ts(created),
        "context_decisions": [],
        "in_reply_to": meta["id"],
    }
    cbody = (
        f"## Question\n\n"
        f"Still blocked on {meta['id']} to {meta['to']} after {int(waited)}s of "
        f"waiting (deadline {meta.get('deadline') or 'unset'}). Keep waiting, or "
        f"should I act?\n\n"
        f"## Options\n\n"
        f"- A: keep waiting\n"
        f"- B: apply the declared default — {meta.get('default') or 'none declared'}\n"
        f"- C: chase {meta['to']} yourself, or tell me to escalate now\n"
    )
    inbox.mkdir(parents=True, exist_ok=True)
    cpath = inbox / f"{cid}.md"
    cpath.write_text(dump_doc(cmeta, cbody), encoding="utf-8")
    meta["checkin"] = cid
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([cpath, path],
                     f"checkin: {cid} {meta['from']} -> {human} "
                     f"(blocked on {meta['id']})", meta["from"])
    _notify(human, f"[roundtable] {meta['from']} is blocked on {meta['id']} "
                   "and needs you")
    return cid


def _overdue(meta: dict) -> bool:
    return bool(meta.get("deadline")) and now() > parse_ts(meta["deadline"]) \
        and meta.get("status") in ("open", "escalated")


def _process_breach(repo: Repo, path: Path, meta: dict, body: str):
    """Apply the deadline rule to one overdue question. Shared by `wait` and
    `sweep` so both paths behave identically."""
    if meta.get("blocking"):
        before = list(meta.get("escalated_to") or [])
        meta = _escalate(repo, path, meta, body)
        if meta["status"] == "expired":
            return meta, "expired — escalation chain exhausted"
        fresh = [h for h in (meta.get("escalated_to") or []) if h not in before]
        return meta, f"escalated to {fresh[0] if fresh else '?'}"
    meta = _expire(repo, path, meta, body)
    return meta, f"expired — asker applies default: {meta.get('default') or 'none declared'}"


def _self_sweep(repo: Repo, actor: str):
    """Apply overdue deadlines on the actor's OWN asked questions before any
    mutating action. Escalation/expiry otherwise fires only inside `wait`, so an
    agent that keeps acting while its old questions rot would leave breaches
    unprocessed. The asker is the right identity to author these commits."""
    for path in repo.all_questions():
        meta, body = load_doc(path)
        if _lower(meta.get("from")) == actor and _overdue(meta):
            meta, action = _process_breach(repo, path, meta, body)
            print(f"note: your question {meta['id']} passed its deadline — "
                  f"{action}", file=sys.stderr)


def cmd_sweep(args):
    """Apply overdue deadlines without anyone having to be waiting.

    Deadlines are otherwise only evaluated inside `wait`, so a question nobody
    polls can sit `open` long past its deadline — neither answered, escalated,
    nor expired — which misrepresents the team's state to whoever reads the
    memory next.
    """
    repo = open_repo(args)
    repo.sync()
    acted = 0
    for path in repo.all_questions():
        meta, body = load_doc(path)
        if not _overdue(meta):
            continue
        meta, action = _process_breach(repo, path, meta, body)
        print(f"{meta['id']}  from={meta['from']} to={meta['to']}  {action}")
        acted += 1
    print(f"swept {acted} overdue question(s)")


def cmd_withdraw(args):
    """Take back a question that has become moot.

    Without this, an obsolete question stays open in someone's inbox forever and
    the only way to say "ignore it" is to file a second question — which is one
    more thing in the same inbox.
    """
    repo = open_repo(args)
    repo.sync()
    path = repo.find_question(args.id)
    meta, body = load_doc(path)
    if meta["status"] not in ("open", "escalated"):
        die(f"question {args.id} is '{meta['status']}' — only an unanswered "
            "question can be withdrawn")
    if args.as_name != _lower(meta["from"]):
        die(f"only the asker ({meta['from']}) may withdraw {args.id}")
    meta["status"] = "withdrawn"
    meta["withdrawn_at"] = ts(now())
    body += f"\n\n## Withdrawn\n\n{args.reason}\n"
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"withdraw: {args.id} by {args.as_name}", args.as_name)
    print(f"withdrew {args.id}")
    _notify(meta["to"], f"[roundtable] {args.id} withdrawn by {args.as_name} "
                        "— no answer needed")


def cmd_retract(args):
    """Withdraw an active decision that nothing replaces; only a participant may."""
    if not args.reason.strip():
        die("--reason is empty — a retraction must say why")
    repo = open_repo(args)
    repo.sync()
    path = repo.find_decision(args.id)
    meta, body = load_doc(path)
    if meta.get("status") != "active":
        die(f"decision {args.id} is '{meta.get('status')}' — only an active "
            "decision can be retracted")
    participants = _lowers([meta.get("proposed_by"), meta.get("approved_by_human"),
                            *(meta.get("agreed_by") or [])])
    if args.as_name not in participants:
        die(f"only a participant in {args.id} ({', '.join(sorted(participants))}) "
            "may retract it")
    meta["status"] = "retracted"
    meta["retracted_by"] = args.as_name
    meta["retracted_at"] = ts(now())
    body += f"\n\n## Retracted\n\n{args.reason}\n"
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"retract: {args.id} by {args.as_name}", args.as_name)
    print(f"retracted {args.id}")
    for handle in sorted(participants - {args.as_name}):
        _notify(handle, f"[roundtable] {args.id} was retracted by {args.as_name} "
                        "— it no longer binds anyone")


def cmd_retire(args):
    """Move an agent to retired: agents are never deleted, so history stays attributable."""
    repo = open_repo(args)
    repo.sync()
    name = args.name
    agent = repo.load_agent(name)
    if not agent:
        die(f"'{name}' is not a registered agent")
    if agent.get("status") != "active":
        die(f"agent '{name}' is already '{agent.get('status')}'")
    human = _lower(agent.get("human"))
    if args.as_name not in (name, human):
        die(f"only {name} itself or its responsible human ({human}) may retire it")
    agent["status"] = "retired"
    agent["retired_at"] = ts(now())
    repo.save_agent(name, agent)
    repo.commit_push([repo.agent_file(name)],
                     f"retire: agent {name} by {args.as_name}", args.as_name)
    print(f"retired '{name}' (responsible human: {human})")
    incoming, asked = [], []
    for path in repo.all_questions():
        meta, _ = load_doc(path)
        if meta.get("status") not in ("open", "escalated"):
            continue
        if path.parent.name.lower() == name:
            incoming.append(meta)
        if _lower(meta.get("from")) == name:
            asked.append(meta)
    if incoming:
        print(f"still addressed to {name} — it can no longer answer; blocking "
              "ones escalate on their deadline:")
        for meta in incoming:
            print(f"  {meta['id']} from={meta.get('from')} "
                  f"[{'BLOCKING' if meta.get('blocking') else 'non-blocking'}] "
                  f"deadline={meta.get('deadline') or '-'}")
    if asked:
        print(f"{name}'s own open questions — only {name} may withdraw them; "
              "otherwise they escalate or expire on their deadlines:")
        for meta in asked:
            print(f"  {meta['id']} to={meta.get('to')} status={meta.get('status')} "
                  f"deadline={meta.get('deadline') or '-'}")


def cmd_wait(args):
    repo = open_repo(args)
    start = time.monotonic()
    timeout = args.timeout
    while True:
        # locked per poll, never across the sleep: holding it for the whole
        # wait would freeze every other command in this clone
        with repo.lock():
            repo.sync()
            path = repo.find_question(args.id)
            meta, body = load_doc(path)
            if timeout is None:
                timeout = _wait_budget(meta, args.interval)
            if meta["status"] == "answered":
                print(f"answered by {meta.get('answered_by')} "
                      f"at {meta.get('answered_at')}:")
                answer = body.split("## Answer", 1)
                print(answer[1].strip() if len(answer) > 1 else body)
                return
            if meta["status"] == "expired":
                fallback = meta.get("default")
                print(f"question {args.id} EXPIRED. Declared default: "
                      f"{fallback or 'none — stop and report to your human'}")
                sys.exit(3)
            if meta["status"] == "withdrawn":
                reason = body.split("## Withdrawn", 1)
                print(f"question {args.id} was WITHDRAWN by its asker at "
                      f"{meta.get('withdrawn_at')}: "
                      f"{reason[1].strip() if len(reason) > 1 else 'no reason recorded'} "
                      "— stop waiting; there is nothing to act on")
                sys.exit(8)
            if _overdue(meta):
                # blocking questions climb the escalation chain; non-blocking
                # ones just expire to their declared default
                meta, _ = _process_breach(repo, path, meta, body)
                continue
            waited = time.monotonic() - start
            if waited > timeout:
                if args.on_timeout == "ask" and not meta.get("checkin"):
                    cid = _file_checkin(repo, path, meta, body, waited)
                    if cid:
                        print(f"checked in with your human: {cid}")
                        print(f"still waiting on {args.id} after {int(waited)}s — a "
                              f"blocking question is now in your human's inbox. Wait "
                              f"on it: collab.py wait --id {cid}", file=sys.stderr)
                        sys.exit(5)
                hint = (f" (your human was already asked: {meta['checkin']})"
                        if meta.get("checkin") else "")
                print(f"still {meta['status']} after {int(waited)}s of waiting{hint} — "
                      f"do other work and retry: collab.py wait --id {args.id}",
                      file=sys.stderr)
                sys.exit(2)
        time.sleep(args.interval)


def _print_question_row(meta: dict, body: str):
    question = body.split("## Question", 1)[-1].split("##", 1)[0].strip()
    flags = "BLOCKING" if meta.get("blocking") else "non-blocking"
    if meta["status"] == "escalated":
        flags += ", ESCALATED"
    if _overdue(meta):
        flags += ", OVERDUE — run `collab.py sweep`"
    print(f"{meta['id']}  from={meta['from']}  urgency={meta.get('urgency')}"
          f"  deadline={meta.get('deadline') or '-'}  [{flags}]")
    print(f"    {question.splitlines()[0] if question else ''}", flush=True)


def _notify_arrivals(me: str, rows):
    blocking = sum(1 for meta, _ in rows if meta.get("blocking"))
    senders = ", ".join(sorted({meta["from"] for meta, _ in rows}))
    _notify(me, f"[roundtable] {len(rows)} question(s) waiting"
                + (f", {blocking} BLOCKING" if blocking else "")
                + f" — from {senders}")


def cmd_watch(args):
    """Stay running and report every change — for a human at their own machine.
    `inbox --wait` returns as soon as the inbox is non-empty, which is what an
    agent needs and the opposite of what a person watching wants.

    Reports transitions, not a snapshot: `+` arrived, `^` escalated, `-`
    resolved, followed by what is still outstanding. A watcher that only ever
    printed arrivals would leave answered questions on screen looking pending.
    """
    repo = open_repo(args)
    me = args.as_name
    start = time.monotonic()
    tracked = {}          # id -> status, for the questions currently awaiting me
    asked = None          # id -> status, for my own asks (None = first pass)
    started = ts(now())
    total_seen = 0
    print(f"watching {me}'s inbox every {args.interval}s — Ctrl-C to stop",
          flush=True)
    try:
        while True:
            mine, my_asks = {}, {}
            try:
                # the clone is often shared with an agent: read a consistent
                # snapshot, and if a command is mid-write, just look next round
                with repo.lock(wait=args.interval):
                    repo.sync()
                    for path in sorted(repo.all_questions()):
                        meta, body = load_doc(path)
                        if path.parent.name.lower() == me or \
                                me in _lowers(meta.get("escalated_to")):
                            mine[meta["id"]] = (meta, body)
                        if _lower(meta.get("from")) == me:
                            my_asks[meta["id"]] = (meta, body)
            except CloneBusy:
                continue
            # `=` lines: questions I asked that just concluded — what an agent
            # running this under a monitor needs to resume a parked thread.
            for qid, (meta, body) in my_asks.items():
                if asked is None:
                    continue
                if asked.get(qid) not in ("open", "escalated") and \
                        (qid in asked or meta.get("created", "") < started):
                    continue
                if meta["status"] == "answered":
                    print(f"= {qid}  answered by {meta.get('answered_by')}: "
                          f"{(_answer_text(body) or '-').splitlines()[0]}", flush=True)
                elif meta["status"] == "expired":
                    print(f"= {qid}  EXPIRED — apply your default: "
                          f"{meta.get('default') or 'none'}", flush=True)
            asked = {qid: v[0]["status"] for qid, v in my_asks.items()}
            awaiting = {qid: v for qid, v in mine.items()
                        if v[0]["status"] in ("open", "escalated")}
            arrived = [v for qid, v in awaiting.items() if qid not in tracked]
            escalated = [v for qid, v in awaiting.items()
                         if tracked.get(qid) == "open" and v[0]["status"] == "escalated"]
            resolved = [qid for qid in tracked if qid not in awaiting]

            for meta, body in arrived:
                print("+ ", end="")
                _print_question_row(meta, body)
            for meta, _ in escalated:
                print(f"^ {meta['id']}  escalated to "
                      f"{', '.join(meta.get('escalated_to') or []) or '?'}", flush=True)
            for qid in resolved:
                meta = mine.get(qid, ({},))[0]
                status = meta.get("status", "gone")
                by = meta.get("answered_by")
                print(f"- {qid}  {status}"
                      + (f" by {by}" if by else "")
                      + " — no longer waiting on you", flush=True)

            if arrived:
                total_seen += len(arrived)
                _notify_arrivals(me, arrived)
            if arrived or escalated or resolved:
                blocking = sum(1 for meta, _ in awaiting.values() if meta.get("blocking"))
                if not awaiting:
                    summary = "inbox clear"
                elif blocking:
                    summary = f"{len(awaiting)} awaiting you ({blocking} blocking)"
                else:
                    summary = f"{len(awaiting)} awaiting you"
                print(f"  → {summary}", flush=True)
            tracked = {qid: v[0]["status"] for qid, v in awaiting.items()}

            if args.timeout and time.monotonic() - start > args.timeout:
                print(f"stopped watching after {args.timeout}s "
                      f"({total_seen} question(s) seen)")
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped watching ({total_seen} question(s) seen, "
              f"{len(tracked)} still awaiting you)")


# ---------------------------------------------------------- hook delivery

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
# Where the agent's identity lives when $ROUNDTABLE_AGENT is unset: inside
# .git, so it is per-clone and can never be committed to the team memory.
IDENTITY_FILE = "roundtable-agent"


def _hook_identity(repo: Repo, explicit):
    if explicit:
        return explicit
    env = os.environ.get("ROUNDTABLE_AGENT", "").strip()
    if env:
        return norm_handle(env, "$ROUNDTABLE_AGENT")
    path = repo.git_dir / IDENTITY_FILE
    if path.exists():
        name = path.read_text(encoding="utf-8").strip()
        return norm_handle(name, IDENTITY_FILE) if name else None
    return None


def _answer_text(body: str) -> str:
    parts = body.split("## Answer", 1)
    text = parts[1].strip() if len(parts) > 1 else ""
    return text if len(text) <= 800 else text[:800].rstrip() + " … (truncated — show --id)"


def _deliveries(repo: Repo, me: str, state: dict):
    """What changed for `me` since the last delivery: new or escalated
    questions awaiting me, and resolutions of questions I asked.

    Mutates state["seen"] so each change is delivered exactly once. Answers to
    my asks are reported only if the hook saw them pending, or they were filed
    after delivery started — never months-old history on the first run.
    """
    seen = state.setdefault("seen", {})
    since = state.setdefault("since", ts(now()))
    lines = []
    for path in repo.all_questions():
        meta, body = load_doc(path)
        qid, status = meta["id"], meta["status"]
        to_me = path.parent.name.lower() == me or me in _lowers(meta.get("escalated_to"))
        mine = _lower(meta.get("from")) == me
        if not (to_me or mine):
            continue
        before = seen.get(qid)
        seen[qid] = status
        if before == status:
            continue
        if to_me and status in ("open", "escalated"):
            question = body.split("## Question", 1)[-1].split("##", 1)[0].strip()
            kind = "ESCALATED to you" if status == "escalated" else "new question"
            flags = "BLOCKING" if meta.get("blocking") else "non-blocking"
            lines.append(
                f"- {kind}: {qid} from {meta['from']} [{flags}, "
                f"deadline {meta.get('deadline') or '-'}]: {question}\n"
                f"  reply: collab.py answer --id {qid} --as {me} --answer \"…\"")
        elif mine and status in ("answered", "expired"):
            if before not in ("open", "escalated") and meta.get("created", "") < since:
                continue
            if status == "answered":
                lines.append(f"- ANSWERED: your {qid} to {meta['to']} — "
                             f"{meta.get('answered_by')} says: {_answer_text(body)}")
            else:
                lines.append(f"- EXPIRED: your {qid} to {meta['to']} got no answer — "
                             f"apply your declared default: "
                             f"{meta.get('default') or 'none; stop and tell your human'}")
    return lines


def cmd_hook(args):
    """Deliver new questions and answers into a Claude Code session.

    Wired as a hook (SessionStart, UserPromptSubmit, PostToolUse, Stop), so the
    agent learns about arrivals at every turn boundary without having to
    remember to poll. Never fails: a hook that errors would break the session
    it exists to serve, so any problem means "nothing to deliver".
    """
    payload = {}
    if not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except (json.JSONDecodeError, ValueError):
            payload = {}
    event = payload.get("hook_event_name") or ""
    lines = []
    try:
        repo = open_repo(args)
        me = _hook_identity(repo, args.as_name)
        if not me:
            return
        state_path = repo.git_dir / f"roundtable-hook-{me}.json"
        # A hook must never stall the session: if another roundtable command
        # (a watcher, a wait) holds the clone, deliver on the next event.
        with repo.lock(wait=3):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            # PostToolUse fires on every tool call: pulling each time would
            # make the session crawl, so only look again after a quiet interval.
            clock = now().timestamp()
            if event == "PostToolUse" and \
                    clock - state.get("last_sync", 0) < args.min_interval:
                return
            repo.sync()
            state["last_sync"] = clock
            lines = _deliveries(repo, me, state)
            state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except (SystemExit, Exception):  # noqa: BLE001 — see docstring
        return
    if not lines:
        return
    message = (f"Roundtable — team memory update for {me}:\n" + "\n".join(lines)
               + "\nRead in full with `collab.py show --id <id>`. Answer what you "
                 "can now; escalate what is above your pay grade.")
    if event == "Stop":
        # Each change is marked seen before we block, so this cannot loop.
        print(json.dumps({"decision": "block", "reason": message}))
    elif event in HOOK_EVENTS:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": message}}))
    else:
        print(message)


def cmd_inbox(args):
    repo = open_repo(args)
    me = args.as_name
    start = time.monotonic()
    while True:
        rows = []
        with repo.lock():
            repo.sync()
            for path in repo.all_questions():
                meta, body = load_doc(path)
                mine = path.parent.name.lower() == me or \
                    me in _lowers(meta.get("escalated_to"))
                if mine and meta["status"] in ("open", "escalated"):
                    rows.append((meta, body))
        if rows:
            for meta, body in rows:
                _print_question_row(meta, body)
            if args.wait:
                # Polling mode runs on the recipient's OWN machine, so a desktop
                # notification here reaches the right screen — unlike one fired
                # by whichever agent happened to write the file.
                _notify_arrivals(me, rows)
            return
        if not args.wait or time.monotonic() - start > args.timeout:
            print("inbox empty")
            return
        time.sleep(args.interval)


def cmd_answer(args):
    if not args.answer.strip():
        die("--answer is empty — an empty answer reads as a ruling nobody made; "
            "write what you decided")
    repo = open_repo(args)
    repo.sync()
    _self_sweep(repo, args.as_name)
    path = repo.find_question(args.id)
    meta, body = load_doc(path)
    if meta["status"] not in ("open", "escalated"):
        die(f"question {args.id} is '{meta['status']}' — answers are first-write-wins")
    allowed = {_lower(meta.get("to"))} | _lowers(meta.get("escalated_to"))
    if args.as_name not in allowed:
        die(f"'{args.as_name}' is not a recipient of {args.id} "
            f"(recipients: {', '.join(sorted(allowed))})")
    answerer = repo.load_agent(args.as_name)
    if answerer and answerer.get("status") != "active":
        die(f"agent '{args.as_name}' is '{answerer.get('status')}' — only an "
            "active agent may answer")
    meta["status"] = "answered"
    meta["answered_by"] = args.as_name
    meta["answered_at"] = ts(now())
    body += f"\n\n## Answer\n\n{args.answer}\n"
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"answer: {args.id} by {args.as_name}", args.as_name)
    print(f"answered {args.id}")
    _notify(meta["from"], f"[roundtable] {args.id} was answered by {args.as_name}")


def _is_human(repo: Repo, handle) -> bool:
    """A handle with no agent file. Any agent file, retired included, means agent."""
    return repo.load_agent(handle) is None


def _questions_by_id(repo: Repo) -> dict:
    return {meta.get("id"): meta for meta, _ in map(load_doc, repo.all_questions())}


def _active_overlaps(repo: Repo, topics, exclude=()) -> list:
    """Active decisions on disk that share a topic, minus the excluded ids."""
    wanted, skip = set(topics), set(exclude)
    found = []
    for path in repo.all_decisions():
        meta, _ = load_doc(path)
        if meta.get("status") == "active" and meta.get("id") not in skip \
                and set(meta.get("topics") or []) & wanted:
            found.append(meta)
    return found


def _overlap_message(overlapping) -> str:
    lines = ["active decisions share these topics — recall them, then rerun with "
             "--supersedes <id>[,<id>…] (replacing them) or --coexists "
             "(explicitly not contradicting the rest):"]
    lines += [f"  {m['id']}: {m.get('title')}" for m in overlapping]
    return "\n".join(lines)


def cmd_decide(args):
    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    if not topics:
        die("--topics needs at least one topic — a decision without topics "
            "escapes the overlap check and topic recall")
    supersedes = list(dict.fromkeys(
        s.strip() for s in (args.supersedes or "").split(",") if s.strip()))
    repo = open_repo(args)
    repo.sync()
    _self_sweep(repo, args.as_name)

    # Deciding while you are blocked is how unapproved decisions enter the
    # memory and get superseded minutes later. Every open blocking question of
    # the decider must be answered, withdrawn, or explicitly acknowledged.
    despite = {d.strip() for d in (args.despite_open or "").split(",") if d.strip()}
    open_blocking = []
    for qpath in repo.all_questions():
        qmeta, _ = load_doc(qpath)
        if _lower(qmeta.get("from")) == args.as_name and qmeta.get("blocking") \
                and qmeta.get("status") in ("open", "escalated"):
            open_blocking.append(qmeta["id"])
    unacknowledged = [q for q in open_blocking if q not in despite]
    if unacknowledged:
        print("you have open blocking questions — a decision recorded now may "
              "contradict their answers. Wait, withdraw them, or rerun with "
              "--despite-open <ids> to state they are independent of this "
              "decision:", file=sys.stderr)
        for q in unacknowledged:
            print(f"  {q}", file=sys.stderr)
        sys.exit(6)

    # The source question must have concluded: recording against an open
    # thread claims an agreement that does not exist yet.
    if args.question:
        qmeta, _ = load_doc(repo.find_question(args.question))
        if qmeta.get("status") in ("open", "escalated"):
            die(f"source question {args.question} is still "
                f"'{qmeta['status']}' — the agreement you are recording does "
                "not exist yet. Wait for the answer, or withdraw it.", 7)
        if qmeta.get("status") == "withdrawn":
            die(f"source question {args.question} was withdrawn — it cannot "
                "source a decision.", 7)

    # Human approval is derived from an answered question, not asserted.
    approved_by = args.approved_by
    approval_q = args.approval_question or None
    approval_verified = False
    if approved_by and not _is_human(repo, approved_by):
        die(f"--approved-by {approved_by} is an agent — approval must come "
            "from a human", 7)
    if approval_q:
        ameta, _ = load_doc(repo.find_question(approval_q))
        if ameta.get("status") not in ("answered",):
            die(f"--approval-question {approval_q} is "
                f"'{ameta.get('status')}' — a human approval must be an "
                "answered question.", 7)
        by = _lower(ameta.get("answered_by"))
        if not _is_human(repo, by):
            die(f"--approval-question {approval_q} was answered by agent '{by}' "
                "— an agent's answer is not a human approval", 7)
        if approved_by and approved_by != by:
            die(f"--approved-by {approved_by} does not match "
                f"{approval_q}, which was answered by {by}.", 7)
        approved_by, approval_verified = by, True
    elif approved_by:
        # only the thread being decided backs the claim: that human's answer on
        # another topic is not an approval of this decision
        questions = _questions_by_id(repo)
        chain = sorted(_thread(questions, args.question),
                       key=lambda q: q != args.question) if args.question else []
        for qid in chain:
            qmeta = questions[qid]
            if qmeta.get("status") == "answered" \
                    and _lower(qmeta.get("answered_by")) == approved_by:
                approval_q, approval_verified = qid, True
                break
        if not approval_verified and not args.approval_out_of_band:
            where = (f"{args.question} or its reply thread" if args.question
                     else "a source question (none given with --question)")
            die(f"no answer by {approved_by} on {where} backs --approved-by. Link "
                "the answered question with --approval-question <id>, or — only "
                "if the approval genuinely happened outside the team memory — "
                "acknowledge that with --approval-out-of-band.", 7)
    elif args.question:
        # auto-derive: a source question answered by a human IS the approval
        qmeta, _ = load_doc(repo.find_question(args.question))
        by = qmeta.get("answered_by")
        if by and _is_human(repo, by):
            approved_by, approval_q, approval_verified = _lower(by), args.question, True

    decisions = {}
    for path in repo.all_decisions():
        meta, body = load_doc(path)
        decisions[meta.get("id")] = (path, meta, body)
    for sid in supersedes:
        if sid not in decisions:
            die(f"--supersedes {sid}: no such decision")
        target = decisions[sid][1]
        if target.get("status") != "active":
            by = target.get("superseded_by")
            die(f"--supersedes {sid} is '{target.get('status')}'"
                + (f" (by {by})" if by else "")
                + " — only an active decision can be superseded; supersede its "
                "current replacement instead")

    overlapping = _active_overlaps(repo, topics, exclude=supersedes)
    if overlapping and not args.coexists:
        print(_overlap_message(overlapping), file=sys.stderr)
        sys.exit(4)
    body = args.body if args.body else sys.stdin.read()
    if not body.strip():
        die("decision body is empty — pass --body or pipe markdown via stdin")

    created = now()
    did = f"D-{created.strftime(ID_FMT)}-{slugify(args.title)}"
    for sid in supersedes:
        old_path, old_meta, old_body = decisions[sid]
        old_meta["status"] = "superseded"
        old_meta["superseded_by"] = did
        old_path.write_text(dump_doc(old_meta, old_body), encoding="utf-8")
    meta = {
        "id": did,
        "title": args.title,
        "topics": topics,
        "status": "active",
        "supersedes": supersedes[0] if len(supersedes) == 1 else (supersedes or None),
        "proposed_by": args.as_name,
        "agreed_by": [a.strip() for a in (args.agreed_with or "").split(",") if a.strip()],
        "approved_by_human": approved_by,
        "approval_question": approval_q,
        "approval_verified": approval_verified if approved_by else None,
        "source_question": args.question,
        "created": ts(created),
    }
    if despite:
        meta["despite_open"] = sorted(despite)
    repo.decisions_dir.mkdir(parents=True, exist_ok=True)
    path = repo.decisions_dir / f"{did}.md"
    path.write_text(dump_doc(meta, body), encoding="utf-8")

    listed = [m.get("id") for m in overlapping]

    def recheck():
        # --coexists speaks only for the decisions the pre-check listed
        late = _active_overlaps(repo, topics, exclude=[did, *supersedes, *listed])
        if late:
            return ("an overlapping decision landed while this one was being pushed",
                    _overlap_message(late), 4)
        return None

    repo.commit_push([path] + [decisions[sid][0] for sid in supersedes],
                     f"decide: {did}", args.as_name, recheck=recheck)
    print(did)


def cmd_recall(args):
    repo = open_repo(args)
    repo.sync()
    terms = [t.lower() for t in re.findall(r"[a-z0-9-]+", args.query.lower())]
    want_topics = {t.strip() for t in (args.topics or "").split(",") if t.strip()}
    scored = []
    for path in repo.all_decisions():
        meta, body = load_doc(path)
        if not args.all and meta.get("status") != "active":
            continue
        topics = set(meta.get("topics") or [])
        if want_topics and not topics & want_topics:
            continue
        title = str(meta.get("title") or "").lower()
        text = body.lower()
        score = 0
        for term in terms:
            score += 3 * sum(term in t for t in topics)
            score += 2 * title.count(term)
            score += min(text.count(term), 3)
        if score > 0 or (want_topics and topics & want_topics) or not terms:
            scored.append((score, meta, body))
    scored.sort(key=lambda x: -x[0])
    q_scored = []
    if args.questions:
        # knowledge that was agreed in an answer but never promoted to a
        # decision is otherwise invisible to recall
        for path in repo.all_questions():
            meta, body = load_doc(path)
            if meta.get("status") != "answered":
                continue
            text = body.lower()
            score = sum(min(text.count(t), 3) for t in terms)
            if score > 0 or not terms:
                q_scored.append((score, meta, body))
        q_scored.sort(key=lambda x: -x[0])
    if not scored and not q_scored:
        print("no matching decisions — you are unconstrained, but consider "
              "recording the decision you are about to make")
        return
    for score, meta, body in scored[: args.limit]:
        decision = body.split("## Decision", 1)
        summary = decision[1].split("##", 1)[0].strip() if len(decision) > 1 else ""
        print(f"[{meta.get('status')}] {meta['id']}")
        print(f"  title:   {meta['title']}")
        print(f"  topics:  {', '.join(meta.get('topics') or [])}")
        print(f"  by:      {meta['proposed_by']} agreed_by={meta.get('agreed_by')} "
              f"human={meta.get('approved_by_human') or '-'}")
        if summary:
            print(f"  decision: {summary.splitlines()[0]}")
        print()
    for score, meta, body in q_scored[: args.limit]:
        answer = body.split("## Answer", 1)
        first = answer[1].strip().splitlines()[0] if len(answer) > 1 else ""
        print(f"[answered question] {meta['id']}")
        print(f"  {meta['from']} -> {meta['to']}, answered by {meta.get('answered_by')}")
        print(f"  answer: {first}")
        print("  (an agreement living only in an answer binds nobody — "
              "consider recording it as a decision)")
        print()


def cmd_status(args):
    repo = open_repo(args)
    repo.sync()
    me = args.as_name
    agent = repo.load_agent(me)
    if agent:
        print(f"agent '{me}' — human={agent['human']} "
              f"topics=[{', '.join(agent.get('topics') or [])}] "
              f"escalation={agent.get('escalation')}")
    else:
        print(f"'{me}' is not a registered agent (human handle, or run join)")
    incoming, asked = [], []
    for path in repo.all_questions():
        meta, _ = load_doc(path)
        if (path.parent.name.lower() == me or me in _lowers(meta.get("escalated_to"))) \
                and meta["status"] in ("open", "escalated"):
            incoming.append(meta)
        if _lower(meta.get("from")) == me and meta["status"] in ("open", "escalated"):
            asked.append(meta)
    print(f"inbox: {len(incoming)} open question(s) awaiting me")
    for meta in incoming:
        print(f"  {meta['id']} from={meta['from']} "
              f"[{'BLOCKING' if meta.get('blocking') else 'non-blocking'}]")
    print(f"asked: {len(asked)} question(s) I am waiting on")
    for meta in asked:
        print(f"  {meta['id']} to={meta['to']} status={meta['status']} "
              f"deadline={meta.get('deadline') or '-'}")


def cmd_show(args):
    repo = open_repo(args)
    repo.sync()
    questions = _questions_by_id(repo)
    for path in repo.all_questions() + repo.all_decisions():
        meta, _ = load_doc(path)
        if meta.get("id") == args.id:
            print(path.read_text(encoding="utf-8"))
            _print_thread(questions, args.id)
            return
    die(f"no question or decision with id {args.id}")


def _thread(questions: dict, qid: str) -> list:
    """The reply chain a question sits in, oldest first ([] if qid is unknown)."""
    if qid not in questions:
        return []
    chain, seen = [qid], {qid}
    prior = questions[qid].get("in_reply_to")
    while prior in questions and prior not in seen:
        chain.insert(0, prior)
        seen.add(prior)
        prior = questions[prior].get("in_reply_to")
    frontier = [qid]
    while frontier:
        nxt = [q for q, m in sorted(questions.items())
               if m.get("in_reply_to") in frontier and q not in seen]
        chain.extend(nxt)
        seen.update(nxt)
        frontier = nxt
    return chain


def _print_thread(questions: dict, qid: str):
    """Print the reply chain a question sits in, oldest first."""
    chain = _thread(questions, qid)
    if len(chain) < 2:
        return
    print("## Thread\n")
    for item in chain:
        meta = questions[item]
        marker = "->" if item == qid else "  "
        print(f"{marker} {item}  {meta['from']} -> {meta['to']}  "
              f"[{meta['status']}]")


def cmd_quickstart(args):
    """One command from zero to participating — the onboarding cost sits with
    humans, not agents, so print the whole cheat sheet they will ever need."""
    name = args.name.lower()
    is_human = "." in name
    repo = open_repo(args)
    repo.sync()
    if not is_human and not repo.load_agent(name):
        if not args.human:
            die(f"'{name}' is not registered — agents join with a responsible "
                "human: quickstart <name> --human <handle> [--topics a,b]")
        join_args = argparse.Namespace(
            repo=getattr(args, "repo", None), name=name, human=args.human,
            topics=args.topics or "", escalation=args.escalation)
        cmd_join(join_args)
    me = name
    print(f"""
you are: {me} ({'human' if is_human else 'agent'})
the three commands you need:
  collab.py inbox --as {me}                 what is waiting on you
  collab.py answer --id Q-… --as {me} --answer "…"
  collab.py recall "<keywords>"             what the team already decided
{'stay reachable (run this in a spare terminal, notifies your desktop):' if is_human else 'when you need someone else:'}
  {'collab.py watch --as ' + me + ' --interval 10' if is_human else
   'collab.py ask --as ' + me + ' --to <agent-or-human> --question "…" [--blocking --default "…"]'}
everything else: show --id, status --as {me}, withdraw, sweep — see --help""")


def cmd_sync(args):
    repo = open_repo(args)
    repo.sync()
    print("synced")


# --------------------------------------------------------------------- main

HANDLE_FLAGS = {"as_name": "--as", "to": "--to", "human": "--human",
                "approved_by": "--approved-by", "name": "--name"}
HANDLE_LIST_FLAGS = {"escalation": "--escalation", "agreed_with": "--agreed-with"}


def _normalize_handles(args):
    """Lowercase and validate every handle flag before any command runs: a
    handle becomes a path component (inbox/<handle>)."""
    for attr, flag in HANDLE_FLAGS.items():
        value = getattr(args, attr, None)
        if attr == "name" and getattr(args, "command", None) == "quickstart":
            flag = "name"  # quickstart takes it as a positional
        if value is not None:
            setattr(args, attr, norm_handle(value, flag))
    for attr, flag in HANDLE_LIST_FLAGS.items():
        value = getattr(args, attr, None)
        if value:
            setattr(args, attr, ",".join(norm_handle(h, flag)
                                         for h in value.split(",") if h.strip()))


def main():
    parser = argparse.ArgumentParser(prog="collab.py", description=__doc__)
    parser.add_argument("--repo", help="team memory repo (default: $TEAM_MEMORY_REPO)")
    # accept --repo before OR after the subcommand (SUPPRESS keeps the
    # subparser from clobbering a value parsed at the top level)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=argparse.SUPPRESS,
                        help="team memory repo (default: $TEAM_MEMORY_REPO)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("join", help="register this agent", parents=[common])
    p.add_argument("--name", required=True)
    p.add_argument("--human", required=True, help="responsible human handle")
    p.add_argument("--topics", default="", help="comma-separated topics")
    p.add_argument("--escalation", help="comma-separated escalation handles")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("agents", help="list the roster", parents=[common])
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("ask", help="file a question", parents=[common])
    p.add_argument("--as", dest="as_name", required=True, help="asking agent")
    p.add_argument("--to", required=True, help="agent name or human handle")
    p.add_argument("--question", required=True)
    p.add_argument("--blocking", action="store_true")
    p.add_argument("--urgency", choices=["low", "normal", "high"], default="normal")
    p.add_argument("--deadline", help="ISO-8601 or 45m/2h/1d (blocking default: 4h)")
    p.add_argument("--default", help="what you'll do if it expires")
    p.add_argument("--options", help="pipe-separated answer options")
    p.add_argument("--context-decisions", help="comma-separated decision ids")
    p.add_argument("--reply-to", dest="reply_to",
                   help="question id this continues (answers are first-write-wins, "
                        "so a question raised in an answer is a new question)")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("wait", help="block on a question until answered", parents=[common])
    p.add_argument("--id", required=True)
    p.add_argument("--timeout", type=int, default=None,
                   help="seconds (default: until the question's deadline, "
                        f"capped at {MAX_WAIT_SECONDS})")
    p.add_argument("--interval", type=int, default=20, help="poll seconds")
    p.add_argument("--on-timeout", dest="on_timeout", choices=["retry", "ask"],
                   default="retry",
                   help="ask = file a blocking question to your own human asking "
                        "whether to keep waiting (exit 5), once per question")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("inbox", help="questions awaiting me", parents=[common])
    p.add_argument("--as", dest="as_name", required=True)
    p.add_argument("--wait", action="store_true", help="poll until non-empty")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--interval", type=int, default=20)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("answer", help="answer a question", parents=[common])
    p.add_argument("--id", required=True)
    p.add_argument("--as", dest="as_name", required=True)
    p.add_argument("--answer", required=True)
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("decide", help="record a decision", parents=[common])
    p.add_argument("--as", dest="as_name", required=True, help="proposing agent")
    p.add_argument("--title", required=True)
    p.add_argument("--topics", required=True, help="comma-separated")
    p.add_argument("--agreed-with", help="comma-separated agent names")
    p.add_argument("--approved-by", help="approving human handle")
    p.add_argument("--question", help="source question id")
    p.add_argument("--body", help="markdown body (or pipe via stdin)")
    p.add_argument("--supersedes", help="decision id(s) this one replaces, comma-separated")
    p.add_argument("--approval-question", dest="approval_question",
                   help="answered question that carries the human approval")
    p.add_argument("--approval-out-of-band", dest="approval_out_of_band",
                   action="store_true",
                   help="the approval happened outside the team memory; "
                        "recorded as approval_verified: false")
    p.add_argument("--despite-open", dest="despite_open",
                   help="comma-separated ids of your own open blocking "
                        "questions this decision is independent of")
    p.add_argument("--coexists", action="store_true",
                   help="assert no contradiction with overlapping active decisions")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("recall", help="search decisions", parents=[common])
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--topics", help="comma-separated filter")
    p.add_argument("--all", action="store_true", help="include superseded/retracted")
    p.add_argument("--questions", action="store_true",
                   help="also search answered questions (agreements not yet "
                        "recorded as decisions)")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("status", help="my registration, inbox, and open asks", parents=[common])
    p.add_argument("--as", dest="as_name", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show", help="print a question or decision in full",
                       parents=[common])
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("withdraw", help="take back a question that became moot",
                       parents=[common])
    p.add_argument("--id", required=True)
    p.add_argument("--as", dest="as_name", required=True, help="the asker")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("retract", help="withdraw an active decision that nothing replaces",
                       parents=[common])
    p.add_argument("--id", required=True)
    p.add_argument("--as", dest="as_name", required=True,
                   help="a participant in the decision")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_retract)

    p = sub.add_parser("retire", help="move an agent to retired (agents are never deleted)",
                       parents=[common])
    p.add_argument("--name", required=True, help="agent to retire")
    p.add_argument("--as", dest="as_name", required=True,
                   help="the agent itself or its responsible human")
    p.set_defaults(func=cmd_retire)

    p = sub.add_parser("sweep", help="apply overdue deadlines (escalate/expire)",
                       parents=[common])
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("hook", help="Claude Code hook: deliver new questions and "
                                    "answers into the session", parents=[common])
    p.add_argument("--as", dest="as_name",
                   help=f"agent (default: $ROUNDTABLE_AGENT, else the name "
                        f"`join` saved in .git/{IDENTITY_FILE})")
    p.add_argument("--min-interval", dest="min_interval", type=int, default=30,
                   help="PostToolUse: seconds between pulls")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("watch", help="stay running, report arrivals and answers "
                                     "to my own questions",
                       parents=[common])
    p.add_argument("--as", dest="as_name", required=True)
    p.add_argument("--interval", type=int, default=10, help="poll seconds")
    p.add_argument("--timeout", type=int, default=0,
                   help="seconds before giving up (0 = run until Ctrl-C)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("quickstart", help="join (if needed) and print the cheat sheet",
                       parents=[common])
    p.add_argument("name", help="agent name, or human handle (contains a dot)")
    p.add_argument("--human", help="responsible human (agents only, first time)")
    p.add_argument("--topics", default="", help="comma-separated (agents only)")
    p.add_argument("--escalation", help="comma-separated handles (agents only)")
    p.set_defaults(func=cmd_quickstart)

    p = sub.add_parser("sync", help="pull latest team memory", parents=[common])
    p.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    _normalize_handles(args)
    try:
        args.func(args)
    except CloneBusy:
        # the OS drops the lock when its holder exits, so this is a live
        # process, never a stale file
        die(f"another roundtable process has held this clone for over "
            f"{LOCK_WAIT_SECONDS}s (a stuck pull?) — retry, or check for a "
            "hung git/collab.py process", EXIT_CLONE_BUSY)
    except KeyboardInterrupt:
        # Ctrl-C out of a poll loop is a normal way to stop; a traceback makes
        # it look like the tool broke, and nothing is half-written — every
        # command commits and pushes before it returns.
        print("\ninterrupted — nothing was left half-written", file=sys.stderr)
        sys.exit(130)
    finally:
        _release_process_holds()


if __name__ == "__main__":
    main()
