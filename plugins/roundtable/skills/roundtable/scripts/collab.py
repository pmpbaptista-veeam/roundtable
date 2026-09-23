#!/usr/bin/env python3
"""Roundtable — team coordination CLI over a shared git repo.

Stdlib only. See references/protocol.md for the file formats and rules
this tool implements. Subcommands:

  join     register this agent in the team memory
  agents   list the agent roster
  ask      file a question to another agent or a human
  wait     block on a question until answered (handles escalation)
  inbox    list open questions addressed to me (or escalated to me)
  answer   answer a question
  decide   record a decision with provenance
  recall   search past decisions
  status   my registration, my inbox, and my open asks
  show     print a question or decision in full
  withdraw take back a question that became moot
  sweep    apply overdue deadlines without anyone waiting
  watch    stay running and notify on each new arrival (for humans)
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
    return parse_ts(value)


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


# ------------------------------------------------------- frontmatter format

def fm_dump_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:@/-]+", value):
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
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def dump_doc(meta: dict, body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {fm_dump_value(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def load_doc(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        die(f"{path} has no frontmatter block")
    _, fm, body = text.split("---", 2)
    meta = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        meta[key.strip()] = fm_parse_value(raw)
    return meta, body.strip()


# ----------------------------------------------------------------- git layer

class Repo:
    def __init__(self, root: Path):
        self.root = root
        if not (root / ".git").exists():
            die(f"{root} is not a git repository (set TEAM_MEMORY_REPO or --repo)")
        self._has_remote = bool(self._git("remote").strip())

    def _git(self, *args, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True,
        )
        if check and result.returncode != 0:
            die(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def sync(self):
        if not self._has_remote:
            return
        result = subprocess.run(
            ["git", "-C", str(self.root), "pull", "--rebase", "--quiet"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            subprocess.run(["git", "-C", str(self.root), "rebase", "--abort"],
                           capture_output=True)
            print(f"warning: sync failed ({result.stderr.strip()}) — "
                  "working from local state", file=sys.stderr)

    def commit_push(self, paths, message: str, actor: str):
        for p in paths:
            self._git("add", str(p))
        # Guard on the INDEX, not on `status --porcelain`: the latter also
        # reports untracked files a teammate's editor left in the clone, which
        # would send an idempotent write into `git commit` with nothing staged.
        nothing_staged = subprocess.run(
            ["git", "-C", str(self.root), "diff", "--cached", "--quiet"],
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
            ["git", "-C", str(self.root), "commit", "--quiet", "-m", message],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            die(f"git commit failed: {result.stderr.strip()}")
        if not self._has_remote:
            return
        for attempt in range(3):
            result = subprocess.run(
                ["git", "-C", str(self.root), "push", "--quiet"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return
            pull = subprocess.run(
                ["git", "-C", str(self.root), "pull", "--rebase", "--quiet"],
                capture_output=True, text=True,
            )
            if pull.returncode != 0:
                subprocess.run(["git", "-C", str(self.root), "rebase", "--abort"],
                               capture_output=True)
                die("a concurrent write to the same file won the race "
                    f"({pull.stderr.strip()}) — sync and re-check before retrying")
        die("push failed after 3 rebase retries — report this to your human")

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
        return self.agents_dir / f"{name}.yaml"

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
        matches = list(self.inbox_dir.glob(f"*/{qid}.md"))
        if not matches:
            die(f"question {qid} not found")
        return matches[0]

    def all_questions(self):
        if not self.inbox_dir.exists():
            return []
        return sorted(self.inbox_dir.glob("*/Q-*.md"))

    def all_decisions(self):
        if not self.decisions_dir.exists():
            return []
        return sorted(self.decisions_dir.glob("D-*.md"))


def open_repo(args) -> Repo:
    root = args.repo or os.environ.get("TEAM_MEMORY_REPO")
    if not root:
        die("no team memory repo: set TEAM_MEMORY_REPO or pass --repo")
    return Repo(Path(root).expanduser().resolve())


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
    if (repo.inbox_dir / name).exists() and not existing:
        die(f"'{name}' already exists as a human inbox — pick another name")
    if "." not in args.human:
        print(f"note: human handle '{args.human}' has no dot; make sure it "
              "cannot collide with an agent name", file=sys.stderr)
    repo.agents_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "name": name,
        "human": args.human,
        "topics": [t.strip() for t in args.topics.split(",") if t.strip()],
        "escalation": [e.strip() for e in (args.escalation or "").split(",") if e.strip()],
        "status": "active",
        "joined": ts(now()),
    }
    lines = [f"{k}: {fm_dump_value(v)}" for k, v in record.items()]
    repo.agent_file(name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (repo.inbox_dir / name).mkdir(parents=True, exist_ok=True)
    (repo.inbox_dir / name / ".gitkeep").touch()
    (repo.inbox_dir / args.human).mkdir(parents=True, exist_ok=True)
    (repo.inbox_dir / args.human / ".gitkeep").touch()
    repo.commit_push([repo.agents_dir, repo.inbox_dir],
                     f"join: agent {name} (human: {args.human})", name)
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
    to = args.to.lower()
    target = repo.load_agent(to)
    if target:
        if target.get("status") != "active":
            die(f"agent '{to}' is not active")
        to_kind = "agent"
    else:
        known_humans = set()
        for a in repo.all_agents():
            if a:
                known_humans.add(a.get("human"))
                known_humans.update(a.get("escalation") or [])
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
        if args.as_name not in (prior.get("from"), prior.get("to")):
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
    inbox = repo.inbox_dir / to
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
    already = list(meta.get("escalated_to") or [])
    return [h for h in chain if h and h not in already and h != meta["to"]]


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
    duration = parse_ts(meta["deadline"]) - parse_ts(meta["created"])
    new_deadline = now() + max(timedelta(minutes=5), duration / 2)
    meta["status"] = "escalated"
    meta["escalated_to"] = escalated
    meta["deadline"] = ts(new_deadline)
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    (repo.inbox_dir / nxt).mkdir(parents=True, exist_ok=True)
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
    human = asker.get("human")
    if not human:
        return None
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
    inbox = repo.inbox_dir / human
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
        if meta.get("from") == actor and _overdue(meta):
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
    if args.as_name != meta["from"]:
        die(f"only the asker ({meta['from']}) may withdraw {args.id}")
    meta["status"] = "withdrawn"
    meta["withdrawn_at"] = ts(now())
    body += f"\n\n## Withdrawn\n\n{args.reason}\n"
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"withdraw: {args.id} by {args.as_name}", args.as_name)
    print(f"withdrew {args.id}")
    _notify(meta["to"], f"[roundtable] {args.id} withdrawn by {args.as_name} "
                        "— no answer needed")


def cmd_wait(args):
    repo = open_repo(args)
    start = time.monotonic()
    timeout = args.timeout
    while True:
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
    total_seen = 0
    print(f"watching {me}'s inbox every {args.interval}s — Ctrl-C to stop",
          flush=True)
    try:
        while True:
            repo.sync()
            mine = {}
            for path in sorted(repo.all_questions()):
                meta, body = load_doc(path)
                if path.parent.name == me or me in (meta.get("escalated_to") or []):
                    mine[meta["id"]] = (meta, body)
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


def cmd_inbox(args):
    repo = open_repo(args)
    me = args.as_name
    start = time.monotonic()
    while True:
        repo.sync()
        rows = []
        for path in repo.all_questions():
            meta, body = load_doc(path)
            mine = path.parent.name == me or me in (meta.get("escalated_to") or [])
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
    repo = open_repo(args)
    repo.sync()
    _self_sweep(repo, args.as_name)
    path = repo.find_question(args.id)
    meta, body = load_doc(path)
    if meta["status"] not in ("open", "escalated"):
        die(f"question {args.id} is '{meta['status']}' — answers are first-write-wins")
    allowed = {meta["to"], *(meta.get("escalated_to") or [])}
    if args.as_name not in allowed:
        die(f"'{args.as_name}' is not a recipient of {args.id} "
            f"(recipients: {', '.join(sorted(allowed))})")
    meta["status"] = "answered"
    meta["answered_by"] = args.as_name
    meta["answered_at"] = ts(now())
    body += f"\n\n## Answer\n\n{args.answer}\n"
    path.write_text(dump_doc(meta, body), encoding="utf-8")
    repo.commit_push([path], f"answer: {args.id} by {args.as_name}", args.as_name)
    print(f"answered {args.id}")
    _notify(meta["from"], f"[roundtable] {args.id} was answered by {args.as_name}")


def _is_registered_agent(repo: Repo, name: str) -> bool:
    a = repo.load_agent(name)
    return bool(a and a.get("status") == "active")


def cmd_decide(args):
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
        if qmeta.get("from") == args.as_name and qmeta.get("blocking") \
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
    if approval_q:
        ameta, _ = load_doc(repo.find_question(approval_q))
        if ameta.get("status") not in ("answered",):
            die(f"--approval-question {approval_q} is "
                f"'{ameta.get('status')}' — a human approval must be an "
                "answered question.", 7)
        by = ameta.get("answered_by")
        if approved_by and approved_by != by:
            die(f"--approved-by {approved_by} does not match "
                f"{approval_q}, which was answered by {by}.", 7)
        approved_by, approval_verified = by, True
    elif approved_by:
        # look for the answer that backs the claim: the primary question first,
        # then any question that human answered (the recorder is not always
        # the asker — a management agent records what its human ruled on
        # someone else's thread)
        backing = []
        for qpath in repo.all_questions():
            qmeta, _ = load_doc(qpath)
            if qmeta.get("status") == "answered" \
                    and qmeta.get("answered_by") == approved_by:
                backing.append(qmeta["id"])
        if args.question and args.question in backing:
            approval_q, approval_verified = args.question, True
        elif backing:
            approval_q, approval_verified = backing[0], True
        if not approval_verified and not args.approval_out_of_band:
            die(f"no answered question by {approved_by} found to back "
                "--approved-by. Link it with --approval-question <id>, or — "
                "only if the approval genuinely happened outside the team "
                "memory — acknowledge that with --approval-out-of-band.", 7)
    elif args.question:
        # auto-derive: a source question answered by a human IS the approval
        qmeta, _ = load_doc(repo.find_question(args.question))
        by = qmeta.get("answered_by")
        if by and not _is_registered_agent(repo, by):
            approved_by, approval_q, approval_verified = by, args.question, True

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    overlapping = []
    for path in repo.all_decisions():
        meta, _ = load_doc(path)
        if meta.get("status") == "active" and set(meta.get("topics") or []) & set(topics):
            overlapping.append((path, meta))
    supersedes = args.supersedes
    if overlapping and not supersedes and not args.coexists:
        print("active decisions share these topics — recall them, then rerun "
              "with --supersedes <id> (replacing one) or --coexists "
              "(explicitly not contradicting):", file=sys.stderr)
        for _, meta in overlapping:
            print(f"  {meta['id']}: {meta['title']}", file=sys.stderr)
        sys.exit(4)
    created = now()
    did = f"D-{created.strftime(ID_FMT)}-{slugify(args.title)}"
    if supersedes:
        old = {m["id"]: (p, m) for p, m in overlapping}
        for path in repo.all_decisions():
            meta, body = load_doc(path)
            if meta["id"] == supersedes:
                old[supersedes] = (path, meta)
        if supersedes not in old:
            die(f"--supersedes {supersedes}: no such decision")
        old_path, old_meta = old[supersedes]
        _, old_body = load_doc(old_path)
        old_meta["status"] = "superseded"
        old_meta["superseded_by"] = did
        old_path.write_text(dump_doc(old_meta, old_body), encoding="utf-8")
    body = args.body if args.body else sys.stdin.read()
    if not body.strip():
        die("decision body is empty — pass --body or pipe markdown via stdin")
    meta = {
        "id": did,
        "title": args.title,
        "topics": topics,
        "status": "active",
        "supersedes": supersedes,
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
    changed = [path] + ([old_path] if supersedes else [])
    repo.commit_push(changed, f"decide: {did}", args.as_name)
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
        title = meta.get("title", "").lower()
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
        if (path.parent.name == me or me in (meta.get("escalated_to") or [])) \
                and meta["status"] in ("open", "escalated"):
            incoming.append(meta)
        if meta.get("from") == me and meta["status"] in ("open", "escalated"):
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
    questions = {}
    for path in repo.all_questions():
        meta, _ = load_doc(path)
        questions[meta.get("id")] = meta
    for path in repo.all_questions() + repo.all_decisions():
        meta, _ = load_doc(path)
        if meta.get("id") == args.id:
            print(path.read_text(encoding="utf-8"))
            _print_thread(questions, args.id)
            return
    die(f"no question or decision with id {args.id}")


def _print_thread(questions: dict, qid: str):
    """Print the reply chain a question sits in, oldest first."""
    if qid not in questions:
        return
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
    p.add_argument("--supersedes", help="decision id this one replaces")
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

    p = sub.add_parser("sweep", help="apply overdue deadlines (escalate/expire)",
                       parents=[common])
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("watch", help="stay running, notify on each new arrival",
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
    try:
        args.func(args)
    except KeyboardInterrupt:
        # Ctrl-C out of a poll loop is a normal way to stop; a traceback makes
        # it look like the tool broke, and nothing is half-written — every
        # command commits and pushes before it returns.
        print("\ninterrupted — nothing was left half-written", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
