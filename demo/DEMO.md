# Roundtable: a 2-minute demo

One agent, one human, one decision that the agent isn't allowed to make alone.
Two terminals side by side, no slides.

## Before you record (off camera)

```bash
./demo/setup.sh                       # resets /tmp/roundtable-demo every time
```

- **Left terminal (agent ada):** `cd /tmp/roundtable-demo/workspace && claude`
- **Right terminal (you, as the human `pedro.baptista`):** paste the three
  lines `setup.sh` printed. The last one starts `watch`, which sits and waits.
- Make the font big (18pt or more), use a dark theme, and silence other notifications.
- Rehearse once, then run `./demo/setup.sh` again to get a clean take.

You type two prompts and two shell commands. Everything else is the agent.

---

## 0:00 – 0:15 · The problem

*Both terminals idle.*

> We all have AI agents now. What we don't have is a way to know **who decided
> what**, or a way to pull **a person** in when a decision is too big for an
> agent.

## 0:15 – 0:40 · Agents remember what the team decided

**Left, type:**

```
You are agent ada. Who's on the team, and has anyone already decided anything about the report export?
```

*ada runs `agents` and `recall`, finds the cursor-pagination decision, and says it will follow it.*

> Every agent has a name and a human who answers for it. Before it decides
> anything, it checks what the team already decided. That memory is just a git
> repo, so it outlives this chat.

## 0:40 – 1:10 · The agent knows what isn't its call

**Left, type:**

```
Report exports are ours alone. We want to permanently delete exports older than 30 days. Get it decided and recorded for the team.
```

*ada sees that the delete can't be undone, files a **blocking** question to `pedro.baptista` with a safe default ("don't purge"), and starts waiting. The watcher on the right picks it up.*

> Deleting data can't be undone, so the agent doesn't get to decide. It asks
> its human, says what it'll do if nobody answers, which is nothing, and waits.
> It doesn't guess.

## 1:10 – 1:35 · The human decides, from anywhere

**Right, Ctrl-C the watcher, then:**

```bash
python3 $C inbox --as pedro.baptista
python3 $C answer --id <paste Q-id> --as pedro.baptista \
  --answer "Approved, but keep 90 days, not 30."
```

> I don't need to be in the agent's session. I answer from my own machine, and
> I can change the plan. It's 90 days, not 30.

## 1:35 – 2:00 · The record

*ada's wait returns. It records the decision with **90 days**. If it stops to
ask, type `Record it.`*

**Right:**

```bash
python3 $C show --id <paste D-id>
```

*Point at `approved_by_human: pedro.baptista` and `approval_verified: true`.*

> The decision is on record: who proposed it, which human approved it, and
> proof of that approval. The next agent to touch report exports will find it
> first. That's Roundtable: your agents at one table, with a person in the
> loop when it matters.

---

## If a take goes sideways

| What happens | What to do |
|---|---|
| ada wants to write code instead of asking | Add: *"This is destructive. Follow the roundtable rules."* |
| The watcher shows nothing | Run `python3 $C inbox --as pedro.baptista`. It pulls first. |
| ada's wait returns before your answer | Type `Check again.` It re-enters the wait. |
| Anything else | Quit claude, run `./demo/setup.sh`, start over. It takes 2 seconds. |
