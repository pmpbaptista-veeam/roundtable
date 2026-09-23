#!/usr/bin/env bash
# Throwaway Roundtable demo under /tmp/roundtable-demo:
#   remote.git   a local bare repo standing in for the shared team memory
#   ada/         agent ada's clone     (human: pedro.baptista)
#   human/       the human's clone     (plain shell)
#   workspace/   the folder ada's Claude session runs in
# Local git identities, signing off. Never touches your real config.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${1:-/tmp/roundtable-demo}"
SKILL="$(cd "$HERE/../plugins/roundtable/skills/roundtable" && pwd)"

rm -rf "$ROOT"
mkdir -p "$ROOT"
git init -q --bare -b main "$ROOT/remote.git"

seed="$ROOT/.seed"
git clone -q "$ROOT/remote.git" "$seed" 2>/dev/null
cp -R "$HERE/team-memory/." "$seed/"
git -C "$seed" -c user.name=seed -c user.email=seed@demo.local -c commit.gpgsign=false add -A
git -C "$seed" -c user.name=seed -c user.email=seed@demo.local -c commit.gpgsign=false \
  commit -qm "seed demo team memory"
git -C "$seed" push -q origin main
rm -rf "$seed"

for who in ada human; do
  git clone -q "$ROOT/remote.git" "$ROOT/$who"
  git -C "$ROOT/$who" config user.name "$who"
  git -C "$ROOT/$who" config user.email "$who@demo.local"
  git -C "$ROOT/$who" config commit.gpgsign false
done

mkdir -p "$ROOT/workspace/.claude"
cat > "$ROOT/workspace/.claude/settings.json" <<JSON
{
  "env": { "TEAM_MEMORY_REPO": "$ROOT/ada" },
  "permissions": { "allow": ["Bash(python3 *)", "Read(/$SKILL/**)", "Read(~/.claude/skills/roundtable/**)"] }
}
JSON

cat <<MSG
Demo ready under $ROOT

  Left terminal (agent ada):
    cd $ROOT/workspace && claude

  Right terminal (the human, pedro.baptista):
    export TEAM_MEMORY_REPO=$ROOT/human
    C=$SKILL/scripts/collab.py
    python3 \$C watch --as pedro.baptista

  Skill not installed yet?  cp -R $SKILL ~/.claude/skills/roundtable
MSG
