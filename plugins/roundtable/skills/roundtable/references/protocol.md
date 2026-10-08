# Roundtable Protocol Reference

This document is the authoritative spec for the files, states, and rules that
`scripts/collab.py` implements. SKILL.md tells Claude *when* to act; this file
defines *what* the actions mean.

## The team memory repo

All coordination state lives in one git repository (the "team memory"), located
by the `TEAM_MEMORY_REPO` environment variable or the `--repo` flag.

```
team-memory/
├── agents/                      # registry: one file per agent
│   └── ada.yaml
├── decisions/                   # one file per decision, never deleted
│   └── D-20260820T104500Z-report-export-schema.md
└── inbox/                       # one folder per recipient (agent name or human handle)
    ├── ada/
    │   └── Q-20260820T093000Z-grace-k3f9.md
    └── pedro.baptista/
        └── Q-20260820T101200Z-grace-p2x1.md
```

git provides identity (commit author), audit (history), synchronization
(push/pull), and durability. `collab.py sync` pulls on demand; every other
command pulls before acting anyway. Every mutating command syncs first
(`git pull --rebase`), then commits and pushes with bounded retry on rejection.
If the repo has no remote, push is skipped (local/demo mode).

## File format

Every protocol file is markdown with a frontmatter block between `---` lines.
Frontmatter is a restricted YAML subset so the stdlib-only CLI can parse it:

- one `key: value` per line, no nesting;
- lists are JSON arrays: `topics: ["api", "reports"]`;
- a string is bare only if it reads back as the same string, else JSON-quoted;
- everything else is a bare scalar (`blocking: true`, `status: open`).

The block opens with a first line of exactly `---`. It closes at the next line
that is exactly `---`, so a `---` inside a value or in the body is safe.

The CLI writes a string bare only if it reads back as the identical string. It
JSON-quotes anything else: special characters, or text that would read as a
number, boolean or null (`"2026"`, `"true"`). No field is numeric, so readers
take a bare number as text. (v1.0.0 wrote numeric-looking titles unquoted.)

## Registry: `agents/<name>.yaml`

```yaml
name: ada
human: pedro.baptista
topics: ["api", "reports"]
escalation: ["dana.lead"]
status: active
joined: 2026-08-20T09:00:00Z
```

Rules:
- `name` is the agent's mesh-wide identity; lowercase `[a-z0-9-]+`, unique
  (case-insensitive) among `status: active` agents. `join` fails on collision
  and suggests a suffixed alternative.
- `human` is the responsible human's handle (e.g. the email local part). Human
  handles must contain a dot or be otherwise distinct from agent names; `join`
  refuses an agent name that matches an existing inbox of a human.
- `escalation` is an ordered chain of human handles used on deadline breach.
- **Handles** (agent names and human handles) are lowercase
  `[a-z0-9][a-z0-9._-]*` with no `..`. The CLI lowercases every `--as`, `--to`,
  `--human`, `--escalation`, `--approved-by`, `--agreed-with` and `--name`
  value and refuses anything else. So no handle can name a path outside the
  repo. The CLI matches inbox directories case-insensitively, so v1.0.0
  mixed-case directories keep working.
- Agents are never deleted. `collab.py retire --name <agent> --as <actor>` sets
  `status: retired` and stamps `retired_at`. Only the agent itself or its
  responsible human may run it. It lists the agent's open questions (addressed
  to it, and asked by it) and changes none of them. A retired agent cannot ask
  or answer, and its answers never count as a human's.

## Questions: `inbox/<recipient>/Q-<utc>-<from>-<rand>.md`

The unit of both agent↔agent alignment and human-in-the-loop escalation.
A question addressed to an agent name is agent↔agent; addressed to a human
handle it is a human decision request. `ask` rejects recipients that are
neither an active registered agent nor a plausible human handle (a handle
already bound to an agent as `human`/escalation, or one containing a dot) —
this catches typo'd agent names before they become dead-letter inboxes.

```markdown
---
id: Q-20260820T093000Z-ada-k3f9
from: ada
to: grace
to_kind: agent            # agent | human
blocking: true            # asker will not proceed on this thread until answered
urgency: normal           # low | normal | high
deadline: 2026-08-20T13:30:00Z
default: "Option A"       # what the asker will do on expiry (REQUIRED when blocking)
status: open              # open | answered | escalated | expired | withdrawn
created: 2026-08-20T09:30:00Z
context_decisions: ["D-20260812T140000Z-api-pagination"]
in_reply_to: null         # question id this continues, if any
---

## Question

Which fields will `backups` expose for the report export? I need a stable
contract before I generate the API serializer.

## Options

- A: reuse existing columns only
- B: add a materialized view with aggregates
```

When answered, the CLI updates frontmatter (`status: answered`,
`answered_by`, `answered_at`) and appends:

```markdown
## Answer

Option B — we already need the aggregates for the dashboard. View name:
`backups_report_v1`.
```

### Question lifecycle

```
open ──answer──▶ answered                 (terminal)
open ──deadline breach on a blocking q──▶ escalated ──answer──▶ answered
open ──deadline breach, non-blocking──▶ expired       (no escalation; `default` applies)
escalated ──chain exhausted + deadline──▶ expired     (asker applies `default` or stops)
open/escalated ──asker withdraws──▶ withdrawn         (terminal; became moot)
```

Deadline transitions are applied by whoever next polls the question (`wait`, or
`sweep`); nothing fires on a timer.

- **Blocking** questions: the asker polls (`collab.py wait --id Q-…`) and does
  not act on the affected thread until `answered`. It may do unrelated work.
  One `wait` call is bounded: it polls until the question's own deadline, capped
  at 570s, because a command killed by its harness cannot be told apart from one
  that found nothing. Exceeding the budget is not a verdict — re-enter the wait.
- **Check-in** (`wait --on-timeout ask`): when the budget runs out, the asker
  files a blocking question to **its own human** — distinct from escalation,
  which travels the *recipient's* chain on a deadline breach — offering: keep
  waiting, apply the declared default, or chase the recipient. It carries
  `in_reply_to: <original>`, the original records `checkin: <new id>` so it can
  happen at most once, and `wait` exits 5. This is what keeps a blocking thread
  alive when no human is watching the terminal: the bus is the only channel that
  reaches a person.
- **Non-blocking** questions: the asker proceeds and picks up the answer at the
  next inbox check.
- **Escalation**: on deadline breach the CLI marks the question `escalated`,
  appends the next handle in the chain to `escalated_to`, and records the
  moment in `escalated_at`. The new deadline is that moment plus
  `max(5 minutes, previous window / 2)`. The previous window is
  `deadline − created` on the first hop and `deadline − escalated_at` after
  that (files without `escalated_at` measure from `created`). Each hop gets
  half the time of the one before, never under 5 minutes. The file **stays in the original
  recipient's directory** — ownership is `inbox/<dir>` OR membership of
  `escalated_to`, so no copy is made and none should be expected. For a question
  to an agent the chain is that agent's human then their escalation list; for a
  question to a human it is the asker's escalation list. When the chain is
  exhausted, the question becomes `expired`; the asker must apply the declared
  `default` — or stop and report — never invent a different action silently.
- Answers are first-write-wins: the CLI refuses to answer a non-`open`/
  non-`escalated` question. It also refuses an empty answer, and an answer from
  an agent that is not `active`.
- **Withdrawn**: the asker — and only the asker — may `withdraw` an unanswered
  question that has become moot, which appends a `## Withdrawn` section with the
  reason and notifies the recipient. Needed because automatic mechanisms
  (escalation, check-in) cannot know a question was settled elsewhere, and
  filing a second question to say "ignore the first" leaves two items where none
  are actionable. `wait` on a withdrawn question exits 8 and prints the reason,
  so a waiting agent stops instead of polling until it times out.
- **Deadlines are lazily applied.** Nothing in the design runs on a timer: a
  breach is processed by whoever next polls the question, which means a question
  nobody waits on can sit `open` long past its deadline. Listings flag those as
  `OVERDUE`, and `collab.py sweep` applies them — escalating blocking questions
  and expiring the rest — so the memory does not misreport live threads.
- **Threads**: because an answer is written once, a question raised *inside* an
  answer continues the thread as a new question carrying
  `in_reply_to: <original id>`. Only a participant in the original (its `from`
  or `to`) may reply to it. `show --id` walks `in_reply_to` in both directions
  and prints the chain, so a multi-turn exchange reads as one conversation
  without any thread state to maintain.

## Decisions: `decisions/D-<utc>-<slug>.md`

The persistent team memory. One file per decision; provenance is first-class —
and since 2026-08-26, partly **enforced** rather than merely recorded:

- A blocking `ask` requires a declared `default` (refused otherwise).
- `decide` refuses (exit 6) while the decider has open blocking questions,
  unless each is named in `--despite-open` — the acknowledgment is stored in the
  decision as `despite_open`. Rationale: deciding while blocked is how
  unapproved decisions enter the memory and get superseded minutes later.
- `decide --question` refuses (exit 7) a source question that is still
  `open`/`escalated` (the agreement does not exist yet) or `withdrawn`.
- `decide` checks `approved_by_human` against an answered question. It accepts
  three sources:
  1. The question that `--approval-question` names.
  2. For `--approved-by`, that human's answer on the source question or on its
     `in_reply_to` chain in both directions, as `show` prints it.
  3. With no approval flags, the source question itself, if a human answered it.

  "A human" means a handle with no agent file, so a retired agent still counts
  as an agent. An answer that the human gave anywhere else does not count. An
  agent's answer is never a human approval: `--approval-question` answered by
  an agent, or `--approved-by` naming an agent, exits 7. A claim with no
  backing answer exits 7, unless you pass `--approval-out-of-band`. That flag
  records `approval_verified: false` for good.
- **`approval_verified: true` means a human answered — not that they said
  yes.** The CLI does not interpret answers (it refuses only an empty one). So
  "Rejected." verifies exactly like "Approved.". The recorder must record what
  the human actually ruled.
- `decide` requires at least one topic. It validates topics, body and
  supersede targets before it writes the decision or touches a superseded one.
  (Its self-sweep of the decider's overdue questions may still write first.)
- Every mutating command (`ask`/`answer`/`decide`) first **self-sweeps** the
  actor's own overdue questions, so breaches are processed by normal activity
  rather than only inside `wait`.

```markdown
---
id: D-20260820T104500Z-report-export-schema
title: Report export reads from backups_report_v1 view
topics: ["api", "reports", "db-schema"]
status: active            # active | superseded | retracted
approval_question: null   # the answered question that backs the approval
approval_verified: null   # true = derived from a real answer; false = out-of-band
supersedes: null          # the replaced id, or a JSON array of ids; required when replacing an overlapping active decision
proposed_by: ada
agreed_by: ["grace"]
approved_by_human: pedro.baptista   # null when no human approval was required
source_question: Q-20260820T093000Z-ada-k3f9
created: 2026-08-20T10:45:00Z
---

## Context
The export feature needs aggregate fields the raw table doesn't have.

## Options considered
A: raw columns only (rejected: three N+1 aggregations in the API layer).
B: materialized view `backups_report_v1` (chosen).

## Decision
The report export API reads exclusively from `backups_report_v1`. Schema
changes to the view require a superseding decision.

## Consequences
grace owns the view migration; ada pins the serializer to the view columns.
```

Rules:
- **Recall before decide**: agents search decisions before making significant
  choices, and treat `active` decisions as binding.
- **No silent contradiction**: `decide` scans `active` decisions that share any
  topic. If any exist, the command lists them and exits 4. The caller must
  cover every overlapping decision. `--supersedes <id>[,<id>…]` replaces the
  ones it names: each becomes `superseded` and gets `superseded_by`.
  `--coexists` states that the new decision does not contradict the rest, and
  it covers only the decisions this pre-check listed. `--supersedes` accepts
  only `active` decisions. After every rebase during the push, `decide`
  repeats the check. It exits 4 if an overlapping decision that the pre-check
  did not list landed first, with or without `--coexists`.
- `retracted` is for a decision withdrawn without replacement:
  `collab.py retract --id D-… --as <participant> --reason "…"`. `retract`
  accepts only an `active` decision, and only from a participant
  (`proposed_by`, `agreed_by` or `approved_by_human`). It stamps
  `retracted_by` and `retracted_at`, appends a `## Retracted` section with the
  reason, and notifies the other participants. `recall` then hides the
  decision, and `recall --all` still shows it.
- Decisions are append-only history: never edit a superseded decision's body.

## Identifiers and time

- Question id: `Q-<UTC compact timestamp>-<from>-<4 random chars>`. The CLI
  refuses a question id that is not `Q-` followed by letters, digits and `-`,
  so an id can never name a path outside the inbox or act as a wildcard. If
  the same id exists in two inbox directories (possible with v1.0.0 mixed-case
  directories on Linux), the CLI refuses it and lists both files.
- Decision id: `D-<UTC compact timestamp>-<kebab slug of title>`.
- All timestamps are UTC ISO-8601 (`2026-08-20T09:30:00Z`).
- Deadlines accept ISO-8601 UTC (`2026-08-20T13:30:00Z`) or shorthand (`45m`,
  `2h`, `1d`). The CLI refuses anything else.

## Concurrency and conflicts

- Every recipient has their own inbox directory and every question/decision is
  its own file, so concurrent agents rarely touch the same file.
- The only shared write per file is answering; first-write-wins is enforced by
  the status check after a fresh `pull --rebase`.
- Push rejection → `pull --rebase` → retry, three attempts, then the command
  fails loudly (the agent should report it to its human, not retry forever).
- Every command commits only the files it wrote (`git commit -- <paths>`).
  Other staged, modified or untracked files in the clone stay out of its
  commit, so a human's uncommitted work there is never published with it
  (unless a commit hook in that clone stages more files).
- A command that fails after it committed locally undoes its own commit, and
  only its own. This covers a rebase conflict (a concurrent write to the same
  file won, e.g. two answers) and a push that never lands. The clone returns
  to the team's state, so a retry after `sync` is safe.
- The undo keeps the working-tree content of a human's uncommitted edits. It
  resets the index, so a staged edit comes back unstaged. The staged version
  is lost when it differs from the working tree. The working-tree content
  always survives.
- The undo runs only when the top commit is provably the command's own. It is
  either the exact commit the command made, or its rebased copy: the same
  author (`roundtable:<actor>`) and the same patch (`git patch-id`). The
  subject is never evidence, because commit hooks may rewrite it. The commit
  must also touch no file the command did not write. If the rebase dropped
  the command's commit because the same patch is already upstream
  (`git cherry`), nothing local remains, so the command reports the outcome
  with no warning. Otherwise the command leaves the commit in place, prints a
  warning, and says the change is still committed locally, because the next
  command's push would publish it.
- `decide` repeats the overlap check after each rebase. It exits 4 if a
  concurrent overlapping decision that its pre-check did not list landed
  first.
- **Processes sharing one clone take turns.** Git is not safe for two
  processes pulling and committing in one working tree (concurrent fetches
  corrupt `FETCH_HEAD`, and a pull can land on another process's uncommitted
  question file). Every `collab.py` process takes an OS lock on
  `.git/roundtable.lock`: short commands hold it for their whole
  sync → write → commit → push; `wait`, `watch` and `inbox --wait` hold it per
  poll, never across a sleep; the hook gives up after 3 s and delivers on the
  next event. The OS drops the lock when its holder exits, so it cannot go
  stale. A command that cannot get it within 60 s exits 9.

## Notification

The repo is the channel of record; notification is best-effort and never
load-bearing. `notify.py` swallows every error, so a failed notification can
never block a coordination action.

- **Desktop** notifications fire on the machine that ran the command. A popup
  addressed to a human by an *agent's* command therefore appears on the agent's
  machine. Treat single-machine demos accordingly.
- **Hook**: `collab.py hook` is the agent-side delivery. Run by Claude Code at
  session start, on each prompt, after tool calls (pulling at most every 30 s)
  and on stop, it injects new questions to the agent and answers to its asks,
  each change once (state in `.git/roundtable-hook-<agent>.json`). On stop it
  blocks the turn's end so the agent acts first. It never fails the session.
- **Watcher**: `watch --as <handle>` runs persistently on the recipient's own
  machine, reporting transitions (`+` arrived, `^` escalated, `-` resolved,
  `=` one of my own questions answered or expired) and
  notifying there — the right screen by construction, and the recommended way
  for a human to be interrupted. (`inbox --wait` is the one-shot variant an
  agent uses to proceed as soon as anything arrives.)
- **Webhook**: if `$ROUNDTABLE_NOTIFY_WEBHOOK` is set, every notification is
  also POSTed as `{"text": "..."}` — the shape Teams and Slack incoming webhooks
  accept. Unset by default; this is the only channel that crosses machines.

## Identity

`--as` is **self-asserted**: the CLI does not authenticate actors, because the
team memory is a git repo and any participant can write any file. What it does
provide is attribution — every commit is authored `roundtable:<actor>`, so an
agent that answered as its human leaves a permanent, checkable record of having
done so.

The boundary is therefore behavioural and enforced by the skill: an agent acts
only as itself. Reading another inbox (a management agent triaging its human's
queue) is legitimate and useful; answering, withdrawing or deciding as another
handle is not, and is detectable with
`git log --format="%an %s"`.

Teams needing enforced identity rather than attributed identity should run the
mesh transport, where actors authenticate via SSO — the file formats and state
machine are unchanged.

## Compliance

The team memory must never contain PII, customer data, or production code.
Questions and decisions reference artifacts by name/path; they do not embed
protected content.

Pseudonymisation is not sufficient. A ticket id in place of a name protects the
identifier while leaving the attributes attached to it, and id + date is usually
re-identifying inside a company. Excluded alongside names, contact details and
government ids: **compensation band, salary, level-for-pay, performance ratings,
health or leave details, immigration or right-to-work status, disciplinary
matters**.

Because the memory is a git repo, a leak is permanent in history even if a later
commit removes the file — there is no redaction, only rewriting shared history.
That asymmetry is why the rule is "don't write it", not "clean it up after".

Verify rather than assume, e.g.:

```bash
git log -p | grep -i -c -e "<name>" -e "@" -e "band "
```
