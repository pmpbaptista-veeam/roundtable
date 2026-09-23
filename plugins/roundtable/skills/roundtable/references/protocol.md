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
- strings with special characters are JSON-quoted;
- everything else is a bare scalar (`blocking: true`, `status: open`).

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
- Agents are never deleted; `status` moves to `retired`.

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
- **Escalation**: on deadline breach the question is marked `escalated` and the
  next handle in the chain is appended to `escalated_to`, with the deadline
  pushed out by `max(5 minutes, remaining/2)`. The file **stays in the original
  recipient's directory** — ownership is `inbox/<dir>` OR membership of
  `escalated_to`, so no copy is made and none should be expected. For a question
  to an agent the chain is that agent's human then their escalation list; for a
  question to a human it is the asker's escalation list. When the chain is
  exhausted, the question becomes `expired`; the asker must apply the declared
  `default` — or stop and report — never invent a different action silently.
- Answers are first-write-wins: the CLI refuses to answer a non-`open`/
  non-`escalated` question.
- **Withdrawn**: the asker — and only the asker — may `withdraw` an unanswered
  question that has become moot, which appends a `## Withdrawn` section with the
  reason and notifies the recipient. Needed because automatic mechanisms
  (escalation, check-in) cannot know a question was settled elsewhere, and
  filing a second question to say "ignore the first" leaves two items where none
  are actionable.
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
- `approved_by_human` is verified against an answered question: named via
  `--approval-question`, found automatically (any question that human
  answered), or derived without flags when the source question itself was
  answered by a human. A claim with no backing answer is refused (exit 7)
  unless `--approval-out-of-band` is passed, which records
  `approval_verified: false` permanently.
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
supersedes: null          # required when replacing an overlapping active decision
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
- **No silent contradiction**: `decide` scans `active` decisions sharing any
  topic. If any exist, the command aborts and lists them; the caller must pass
  either `--supersedes <id>` (the old decision is marked `superseded`,
  `superseded_by` is stamped) or `--coexists` (an explicit statement that the
  new decision does not contradict the listed ones).
- `retracted` is for decisions withdrawn without replacement; requires a reason
  appended to the file.
- Decisions are append-only history: never edit a superseded decision's body.

## Identifiers and time

- Question id: `Q-<UTC compact timestamp>-<from>-<4 random chars>`.
- Decision id: `D-<UTC compact timestamp>-<kebab slug of title>`.
- All timestamps are UTC ISO-8601 (`2026-08-20T09:30:00Z`).
- Deadlines accept ISO-8601 or shorthand (`45m`, `2h`, `1d`) at the CLI.

## Concurrency and conflicts

- Every recipient has their own inbox directory and every question/decision is
  its own file, so concurrent agents rarely touch the same file.
- The only shared write per file is answering; first-write-wins is enforced by
  the status check after a fresh `pull --rebase`.
- Push rejection → `pull --rebase` → retry, three attempts, then the command
  fails loudly (the agent should report it to its human, not retry forever).

## Notification

The repo is the channel of record; notification is best-effort and never
load-bearing. `notify.py` swallows every error, so a failed notification can
never block a coordination action.

- **Desktop** notifications fire on the machine that ran the command. A popup
  addressed to a human by an *agent's* command therefore appears on the agent's
  machine. Treat single-machine demos accordingly.
- **Watcher**: `watch --as <handle>` runs persistently on the recipient's own
  machine, reporting transitions (`+` arrived, `^` escalated, `-` resolved) and
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
