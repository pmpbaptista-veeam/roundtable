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
/plugin marketplace add <git-url-of-this-repo>
/plugin install roundtable@roundtable
```

Or copy the skill by hand (the folder name must be `roundtable`):

```bash
cp -R plugins/roundtable/skills/roundtable ~/.claude/skills/roundtable
```

On claude.ai, upload `roundtable-skill.zip` from the release page under
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

Humans use the same CLI from a plain shell:

```bash
C=~/.claude/skills/roundtable/scripts/collab.py   # or the plugin's path
python3 $C watch  --as alice        # stays running, notifies on each arrival
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

## Compliance

The skill tells agents never to write PII, customer data, or production code
into the team memory. That covers questions and answers too, not only
decisions. Everything in `demo/` is fictional.
