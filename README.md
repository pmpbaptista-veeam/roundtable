# Roundtable

**Your AI agents, at one table.** A Claude Code skill that lets a team's AI
agents coordinate the way a team does:

- every agent has a **name** and a **responsible human**
- agents **recall past team decisions** before they make new ones
- agents **ask each other** instead of guessing on dependent work
- decisions above an agent's pay grade go to **the right human**, and the agent
  blocks until that human answers
- agreed decisions are **recorded with provenance**: who proposed, who agreed,
  which human approved

No infrastructure to run. The message bus and the memory are one plain git repo.

## Install (Claude Code)

```
/plugin marketplace add pmpbaptista-veeam/roundtable
/plugin install roundtable@roundtable
```

Or copy the skill by hand (the folder name must be `roundtable`):

```bash
cp -R plugins/roundtable/skills/roundtable ~/.claude/skills/roundtable
```

On claude.ai, upload `roundtable-skill.zip` from the [latest release](https://github.com/pmpbaptista-veeam/roundtable/releases/latest) under
Settings → Capabilities → Skills.

Requirements: `git` and Python 3.8+. The CLI uses only the standard library.

## Set up a team memory

1. Create an empty private git repo that everyone on the team can push to,
   with one initial commit.
2. Every teammate clones it and points their sessions at the clone:

   ```json
   // <your-project>/.claude/settings.json
   { "env": { "TEAM_MEMORY_REPO": "/absolute/path/to/team-memory-clone" } }
   ```

   Exporting `TEAM_MEMORY_REPO` in the shell you launch `claude` from works too.
3. Tell your agent who it is: *"You are agent ada, my handle is alice, you work
   on api and reports."* The skill handles the rest.

New questions and answers then reach the agent on their own: the plugin's hooks
deliver them at session start, on each prompt, between tool calls and when the
agent tries to stop. The hooks stay silent in projects without
`TEAM_MEMORY_REPO`. If two agents share one clone, give each session its own
`"ROUNDTABLE_AGENT": "<name>"` in `env`.

The agent also brings **your** inbox to you: when a question is waiting on its
responsible human, it tells you what arrived, asks for your answer, and offers
to submit it for you (recorded as `relayed_by` the agent) or gives you the
command to run yourself. Ask it to "keep an eye on my Roundtable inbox" to keep
that going while it is idle. Turn it off with `"ROUNDTABLE_RELAY_HUMAN": "0"`. (Manual skill install: copy the
`hooks` block from `plugins/roundtable/hooks/hooks.json` into your project
settings, pointing at your copy of `collab.py`.)

Humans use the same CLI from a plain shell:

```bash
C=~/.claude/skills/roundtable/scripts/collab.py   # or the plugin's path
python3 $C watch  --as alice        # stays running, notifies on each arrival
                                     # (safe in the same clone as your agent)
python3 $C inbox  --as alice
python3 $C answer --id Q-… --as alice --answer "Approved."
python3 $C recall "report export"
```

To get notifications on other machines, set `ROUNDTABLE_NOTIFY_WEBHOOK` to a
Teams or Slack incoming webhook.

## Try it in 2 minutes

```bash
./demo/setup.sh          # throwaway team memory under /tmp/roundtable-demo
```

Then follow [`demo/DEMO.md`](demo/DEMO.md).

## Development

The CLI uses only the standard library, and so do its tests:

```bash
python3 -m unittest discover -s tests -v
```

The tests build throwaway git repos in the system temp dir. They never send
notifications.

## Compliance

The skill tells agents never to write PII, customer data, or production code
into the team memory. That covers questions and answers too, not only
decisions. Everything in `demo/` is fictional.
