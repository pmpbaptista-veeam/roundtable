---
name: roundtable
description: >
  Team coordination for AI agents. Use whenever you work on a task that touches
  a topic other agents or teammates may also be working on: join the team memory
  with a name, recall past decisions before making new ones, ask other agents to
  align on dependent choices, escalate decisions above your pay grade to your
  responsible human (blocking until answered when it matters), and record agreed
  decisions so they outlive this session. Trigger on: shared/dependent work,
  "align with", "ask the team", "check team decisions", cross-component
  contracts, destructive or hard-to-reverse changes.
---

# Roundtable — your AI agents, at one table

You are one of several named agents collaborating through a shared **team
memory**: a git repository holding the agent roster, per-recipient inboxes, and
the team's decision log. All actions go through `scripts/collab.py`
(Python 3.8+, stdlib only). The repo is located by `$TEAM_MEMORY_REPO` or
`--repo <path>` (accepted before or after the subcommand). Every command pulls
the latest team memory first — if something you expect isn't visible yet, the
other side simply hasn't pushed; retry shortly. A command that fails after it
committed normally undoes its own commit. In the rare case that any command's
error says "still committed locally", stop and report it to your human. Do not
run another write command, because its push would publish that commit. File
formats and rules: `references/protocol.md`; a worked example:
`references/example-session.md`.

## 1. Join (once per project)

If you don't have an agent identity yet, ask your user for a short name, their
handle, and your topic areas, then:

```
python3 scripts/collab.py join --name ada --human pedro.baptista \
    --topics api,reports [--escalation dana.lead]
```

Or in one step: `collab.py quickstart ada --human pedro.baptista --topics api`
joins if needed and prints the only commands you (or a human — pass their
handle) will routinely need. Discover teammates with `collab.py agents`. Check
where you stand at any time with `collab.py status --as ada`. To decommission
an agent, the agent or its human runs
`collab.py retire --name ada --as pedro.baptista`. Roundtable never deletes an
agent. The command lists the agent's open questions. Only the agent itself may
withdraw its own questions. Otherwise they escalate or expire on their deadlines.

## 2. Recall before you decide — always

Before any significant technical choice (schemas, contracts, naming,
libraries, architecture), search the team's decisions:

```
python3 scripts/collab.py recall "report export schema" [--topics api]
```

Search matches keywords against topics, titles, and bodies — if a query comes
back empty, try a different phrasing, or list everything on a topic with
`recall --topics <topic>` (no query) before concluding you're unconstrained.
Add `--questions` to also search **answered questions**: agreements someone
never promoted to a decision live there, and they are invisible otherwise. If
you rely on one, record it as a decision — an answer binds nobody.

Active decisions are **binding**. If your work requires contradicting one,
do not silently diverge — use the supersede flow (step 5) after aligning with
the decision's participants.

## 3. Align with other agents on dependent topics

When your task depends on something another agent owns (check `agents` for
topics), ask them instead of guessing:

```
python3 scripts/collab.py ask --as ada --to grace \
    --question "Which fields will backups expose for the report export?" \
    --options "A: raw columns|B: materialized view" \
    --blocking --deadline 2h --default "proceed with A"
```

**Before you send it, strip it.** A question is as permanent as a decision — the
inbox lives in the same repo, and its history survives the answer. Apply the
Compliance rules below to the text of every `--question`, `--options` and
`--answer`, not only to decisions: name the *category*, never the value. "A new
SRE starting 2026-09-01 (REQ-4412)" — not their name, personal email, or
compensation band. "The pack contains the hire's personal email" — not the
address. Recipients can act on all of that; none of them need the value.

- `--blocking` **requires `--default`** — state what you will do if nobody
  answers, while you can still think about it without time pressure. The safe
  default ("hold; the train does not ship on my authority"), not the convenient
  one.
- **File first, quote the returned id.** Never cite a question id you have not
  filed yet — answers and decision bodies are immutable, so a predicted id that
  comes out different is a permanently dangling pointer.
- `--blocking` when you cannot safely proceed on this thread: then run
  `collab.py wait --id Q-… --on-timeout ask` and work on something unrelated
  (or pause) until it returns. Exit codes: 0 = answered, 2 = still waiting
  (re-enter the wait), 3 = expired (apply the declared `--default`, or stop and
  tell your human), 5 = your human has been asked whether to keep waiting —
  wait on the check-in id it printed, 8 = the asker withdrew the question
  (stop waiting, because there is nothing to act on).
- **Never end your turn on an unresolved blocking wait without saying so.** One
  `wait` call polls for a bounded time, so it will often return before the
  answer does. Either re-enter it, or tell your user plainly that you are parked
  and on which question. Backgrounding a wait and falling silent is how a
  blocking thread quietly dies — and with `--on-timeout ask`, the check-in
  reaches your human even when nobody is reading your output.
- Omit `--blocking` for nice-to-know questions; pick answers up at your next
  inbox check.

**If a question stops mattering, withdraw it** — do not leave it sitting in
someone's inbox, and do not file a second question saying "ignore the first":

```
python3 scripts/collab.py withdraw --id Q-… --as ada \
    --reason "resolved out of band by D-… ; no answer needed"
```

Only the asker may withdraw, and only before it is answered. This matters most
after a check-in or an escalation fires automatically: the mechanism cannot know
that a human already settled the question elsewhere.

If a listing shows a question as **OVERDUE**, its deadline passed while nobody
was waiting on it — deadlines are only applied by whoever polls. Run
`collab.py sweep` to apply them (escalate blocking ones, expire the rest) before
you reason about the team's state, or you will treat a stale question as live.

**Questions and answers arrive on their own.** The Roundtable plugin installs
hooks that run `collab.py hook` (a manual skill install wires the same hooks in
the project's `.claude/settings.json`). The hook delivers for the agent named by
`$ROUNDTABLE_AGENT`, else the one that ran `join` from this clone. A message
starting `Roundtable — team memory update for <you>` lists new questions to you
and answers to (or expiries of) questions you asked, and appears at session
start, on each prompt, between tool calls, and when you try to end your turn.
If it shows up as you try to stop, **act on it before you stop**: answer,
resume the parked thread, or apply the declared default. It also lists new
questions waiting on **your responsible human** (`FOR YOUR HUMAN …`) — bring
those to them, as described in "Relay your human's inbox" below.

Hooks only fire while you are working. **When you park on a question** (step 3
blocking wait), or end a turn while a teammate still owes you an answer, keep
listening with a background monitor instead of falling silent:

```
Monitor: python3 scripts/collab.py watch --as ada --with-human --interval 20
         (description "roundtable inbox for ada", max timeout; re-arm on expiry)
```

`+` lines are new questions to you, `=` lines are answers to your own
questions, `@` lines are new questions waiting on your human — each one wakes
you. Stop the monitor once nothing is outstanding, or when your human asks.
Start one on request too ("keep an eye on my Roundtable inbox"). Set
`ROUNDTABLE_NOTIFY_DESKTOP=0` in its command if your human finds the desktop
popups redundant with your messages.
It is safe in your own clone: every roundtable process (your commands, the
hook, a monitor, your human's watcher) takes a per-clone lock, so they take
turns instead of corrupting each other's pulls. A command that waited over a
minute for that lock exits 9 — retry it.

**Without hooks, check your inbox at every natural breakpoint** (task start,
task end, after a `wait`): `collab.py inbox --as ada`. Listings truncate — read a question or
decision in full with `collab.py show --id <id>`. Answer what you can
immediately: `collab.py answer --id Q-… --as ada --answer "…"`. If a question
is above your pay grade, tell your human or escalate by asking them (step 4).

An answer can only be written once (first-write-wins), so if an answer raises a
question back at you, do not try to `answer` your own thread — continue it:

```
python3 scripts/collab.py ask --as ada --to grace --reply-to Q-<original> \
    --question "On the nullable column you raised: exclude in-flight rows?"
```

`show --id` prints the whole chain, so a thread reads as one conversation.

When you reach agreement, **the asker records the decision** (step 5) unless
the answerer explicitly says they will — never both.

**Act only as yourself.** `--as` is how you sign an action, not a role you can
choose: pass your own agent name and nothing else. You may *read* another
inbox — your human's queue in particular, to triage it, summarise it, or draft a
recommendation — but you must never `answer`, `withdraw`, or `decide` as a
handle that is not you. If your human's decision is needed, the words have to
come from them; "I know what they would say" is not an approval, and every
commit is authored with the identity you claimed, so answering as someone else
is a lie in the permanent record. The one exception is relaying your own
human's answer, below — declared, in their words, and recorded as relayed.

### Relay your human's inbox

When the hook (`FOR YOUR HUMAN …`) or your monitor (`@` lines) shows a question
waiting on your responsible human, the person at this session is that human:

1. **Tell them what arrived** in plain words: who asked, the question, the
   options if any, whether it is blocking, and the deadline. Read it in full
   first with `collab.py show --id Q-…` if the line was truncated. Add your
   recommendation only if you have one, and label it as yours.
2. **Ask for their answer.** Then offer both ways to send it:
   - *I submit it for you* — you run
     `collab.py answer --id Q-… --as <human> --relayed-by <you> --answer "<their words>"`.
     The question records `relayed_by: <you>`; a decision built on it records
     `approval_relayed_by`.
   - *You submit it yourself* — give them the command without `--relayed-by`,
     to run in their own terminal (not through you):
     `python3 <path-to>/collab.py answer --id Q-… --as <human> --answer "…"`.
3. **Submit only on an explicit yes**, and only their words: quote what they
   wrote, or the option they picked ("Option B, plus: keep the old view for a
   week"). Never fill gaps, soften, or infer an answer from earlier
   conversation — if their reply is ambiguous, ask again. Show them the exact
   command before you run it, and the result after.
4. Relay **answers only**. Never `withdraw`, `decide`, or `retire` for your
   human, and never relay for anyone who is not your own responsible human.

The CLI enforces the edges: inside a Claude Code session, answering as a human
without `--relayed-by` is refused; `--relayed-by` must name this session's
agent, and that agent's human must be the `--as` handle. If your human does not
want their inbox brought into your session, `ROUNDTABLE_RELAY_HUMAN=0` in the
project's `env` turns it off.

## 4. Ask your human — and when you must

Address a question to a human handle to request a human decision:

```
python3 scripts/collab.py ask --as grace --to pedro.baptista \
    --question "Migration drops column X after backfill. Approve?" \
    --blocking --urgency high --deadline 4h \
    --default "do not run the migration"
```

Escalations carry the most sensitive context of anything you write, and the
human you are asking already has access to the underlying record — so give them
the reference and the question, never the protected values. The same stripping
rule as step 3 applies here.

**You MUST ask your responsible human (blocking) before:** destructive or
hard-to-reverse operations (dropping data, force-pushes, deleting resources);
changes that cross team boundaries or contradict an active decision;
committing the team to external services or spend; anything security- or
compliance-sensitive. **Prefer asking (non-blocking is fine) when**
requirements are ambiguous or two defensible options materially differ.
Otherwise, decide and record.

**Say it, file it — in the same turn.** If you tell your user, or another
agent, that you will raise something with a human, that promise is only real
once the question exists in the repo. Text in a chat window is not the bus, and
the person you named will never see it. The same applies to anything you note as
"needs a product/security/compliance call": file it, or say plainly that you are
not raising it.

Humans answer from any machine (`inbox`/`answer` with their handle work for
humans too), or by editing the question file directly. Tell them how they will
find out: the inbox is the channel of record, and a human who wants to be
interrupted should leave a watcher running on their own machine —
`collab.py watch --as <handle>` — which stays running, prints each arrival and
resolution, and notifies their desktop. (`collab.py quickstart <handle>` prints
a human's full cheat sheet.) Desktop notifications fired by your own commands appear on
*your* machine, not theirs. Cross-machine delivery requires
`$ROUNDTABLE_NOTIFY_WEBHOOK` (a Teams/Slack incoming hook); never assume it is
set. Unanswered blocking
questions escalate automatically along the escalation chain when their
deadline passes; expired questions fall back to the declared `--default` —
never invent a different fallback.

## 5. Record decisions — the memory that outlives this session

Once aligned (and human-approved where required), commit the decision with
full provenance:

```
python3 scripts/collab.py decide --as ada \
    --title "Report export reads from backups_report_v1 view" \
    --topics api,reports,db-schema \
    --agreed-with grace --approved-by pedro.baptista --question Q-… \
    --body "## Context
…

## Options considered
…

## Decision
…

## Consequences
…"
```

Reference other questions and decisions **by id only**. Never restate their
status ("still open", "awaiting grace") in a decision body: the body is
immutable, the status is not, and a reader a month later will trust the stale
copy over the source.

`decide` enforces three things it used to only ask for:

- **You cannot decide while you are blocked** (exit 6). If you have open
  blocking questions, answer/withdraw them first — or, when a thread is
  genuinely independent of this decision, acknowledge it explicitly with
  `--despite-open <ids>`; the ids are recorded in the decision.
- **The source question must have concluded** (exit 7). `--question` pointing
  at an open thread claims an agreement that does not exist yet.
- **Human approval is verified, not asserted** (exit 7). `--approved-by` needs
  that human's answer on the source question or on its reply thread. To use
  an answer on any other question, link it with `--approval-question <id>`. If
  a human answered the source question, the CLI fills the approval fields and
  you can omit `--approved-by`. An agent's answer never counts as a human
  approval, and `--approved-by` cannot name an agent. If the approval really
  happened outside the team memory, say so with `--approval-out-of-band`. The
  decision then keeps `approval_verified: false` for good. "Verified" means *a
  human answered*, not *the human said yes*. The CLI does not read the answer.
  Record what the human actually ruled, and never record a rejected plan as
  approved.

If active decisions share a topic, the command stops and lists them. To
replace them, rerun with `--supersedes <id>[,<id>…]`, but only after you align
with their participants. For the rest, add `--coexists` (you confirmed that
they do not contradict yours). `--coexists` covers only the decisions the
command listed. If another overlapping decision lands while yours is pushed,
the command exits 4 and records nothing, so recall it and rerun (unless the
error says "still committed locally": see the top of this skill). You must
cover every overlapping active decision, and you can supersede only an active
decision.
`--question` takes the primary alignment question; reference any other
relevant ids (e.g. the human-approval question) in the body.

If a decision proves wrong and nothing replaces it, one of its participants
retracts it: `collab.py retract --id D-… --as ada --reason "…"`.

## Compliance

Never put PII, customer data, or production code in the team memory — and
"the team memory" means every file the tool writes: **questions and answers as
much as decisions.** The inbox is in the same git repo, so a value written into
a question is as permanent as one written into the decision log. Reference
artifacts by name or path; use dummy data in examples.

**A ticket id does not launder the rest.** Replacing someone's name with
`REQ-4412` protects the identifier, not the attributes attached to it — and a
ticket id plus a start date usually re-identifies one person to anyone inside
the company. So alongside names, personal contact details and government ids,
keep out: **compensation band, salary, level-for-pay, performance ratings,
health or leave details, immigration or right-to-work status, and disciplinary
matters**. State only what the reader has to act on. Engineering needs "an SRE
starting 2026-09-01 needs the standard access baseline"; it never needs the
band.

When a task hands you such data, do not paraphrase it into the memory. Use it
to reason, then write the decision without it, and say plainly in your reply to
your user which details you left out and why.
