# Worked example: two agents ship a feature together

Dummy project: **export-backups-report**. Agent **ada** (topics: api, reports;
human: pedro.baptista) builds the export API. Agent **grace** (topics:
db-schema, reports; human: dana.lead) owns the database. Annotations in
*(italics)* explain what each side's Claude session is doing and why.

---

**ada's session** — task: "implement the report export endpoint".

```
$ python3 scripts/collab.py recall "report export schema"
no matching decisions — you are unconstrained, but consider recording the
decision you are about to make
```

*(Recall first. Nothing binds ada, but the response format depends on the DB —
grace's topic. Ask instead of guessing:)*

```
$ python3 scripts/collab.py ask --as ada --to grace \
    --question "Which fields will backups expose for the report export? I need a stable contract for the serializer." \
    --options "A: raw columns only|B: materialized view with aggregates" \
    --blocking --deadline 2h --default "proceed with A"
Q-20260820T093000Z-ada-k3f9
blocking question filed — run: collab.py wait --id Q-20260820T093000Z-ada-k3f9
$ python3 scripts/collab.py wait --id Q-20260820T093000Z-ada-k3f9
```

*(ada blocks on this thread; the session can meanwhile scaffold tests that
don't depend on the schema.)*

---

**grace's session** — reaches a natural breakpoint and checks the inbox:

```
$ python3 scripts/collab.py inbox --as grace
Q-20260820T093000Z-ada-k3f9  from=ada  urgency=normal  deadline=2026-08-20T11:30:00Z  [BLOCKING]
    Which fields will backups expose for the report export? I need a stable contract...
```

*(grace evaluates: option B needs a materialized view — fine. But the
migration also drops a legacy column: destructive → the skill's rules say
BLOCKING human approval first:)*

```
$ python3 scripts/collab.py ask --as grace --to dana.lead \
    --question "View backups_report_v1 requires dropping legacy column raw_size (fully migrated). Approve the drop?" \
    --blocking --urgency high --deadline 4h
Q-20260820T101200Z-grace-p2x1
$ python3 scripts/collab.py wait --id Q-20260820T101200Z-grace-p2x1
```

*(The question is now in dana's inbox in the repo. How dana learns about it:
if dana is watching — `collab.py inbox --as dana.lead --wait` — the watcher
polls on dana's own machine and pops a desktop notification there. Off-machine
delivery needs `$ROUNDTABLE_NOTIFY_WEBHOOK` set to a Teams/Slack incoming hook.
The desktop notification grace's `ask` fires appears on grace's machine, which
is why the inbox, not the popup, is the channel of record. From any machine — or
their own Claude session — dana answers:)*

```
$ python3 scripts/collab.py inbox --as dana.lead
Q-20260820T101200Z-grace-p2x1  from=grace  urgency=high  [BLOCKING]
$ python3 scripts/collab.py answer --id Q-20260820T101200Z-grace-p2x1 \
    --as dana.lead --answer "Approved — verify the backfill job ran in staging first."
```

*(grace's `wait` returns with the approval; now answer ada:)*

```
$ python3 scripts/collab.py answer --id Q-20260820T093000Z-ada-k3f9 \
    --as grace --answer "Option B. View name backups_report_v1; columns: id, name, size_bytes, last_success_at, success_rate_30d. Human-approved drop of raw_size included."
```

---

**ada's session** — `wait` returns the answer. ada records the agreement:

```
$ python3 scripts/collab.py decide --as ada \
    --title "Report export reads from backups_report_v1 view" \
    --topics api,reports,db-schema \
    --agreed-with grace --approved-by dana.lead \
    --question Q-20260820T093000Z-ada-k3f9 \
    --body "## Context
Export needs aggregates the raw table lacks.

## Options considered
A: raw columns (rejected — N+1 aggregation in the API layer). B: view (chosen).

## Decision
The export API reads exclusively from backups_report_v1
(id, name, size_bytes, last_success_at, success_rate_30d).

## Consequences
grace owns the view migration; ada pins the serializer to these columns.
Schema changes require a superseding decision."
D-20260820T104500Z-report-export-reads-from-backups-report-v1-view
```

---

**A week later, a brand-new session** (different engineer, zero context) is
asked to "add a column to the report export":

```
$ python3 scripts/collab.py recall "report export"
[active] D-20260820T104500Z-report-export-reads-from-backups-report-v1-view
  title:   Report export reads from backups_report_v1 view
  topics:  api, reports, db-schema
  by:      ada agreed_by=['grace'] human=dana.lead
  decision: The export API reads exclusively from backups_report_v1
```

*(The new session immediately knows the contract, who agreed to it, and that
changing it means a superseding decision agreed with grace — the memory
outlived every session that created it.)*
