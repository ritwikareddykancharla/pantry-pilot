#!/usr/bin/env bash
# Three-minute terminal demo: seed, run the daily cycle, answer the escalations the way a coordinator would,
# then run the follow-up cycle. Uses the same service functions as the web console.
#
#   ./scripts/demo.sh            # real model (needs Bedrock or Anthropic credentials)
#   ./scripts/demo.sh --dry-run  # print the commands without calling a model
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

step() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
run() {
  printf '\033[2m$ %s\033[0m\n' "$*"
  if [[ $DRY -eq 0 ]]; then "$@"; fi
}

step "1. Seed the demo pantry (Maple Street Community Pantry, DEMO_TODAY=${DEMO_TODAY:-2026-09-11})"
run "$PY" -m pantrypilot.cli seed

step "2. Daily cycle: 7 unhandled texts, then the checklist. Watch the dispatcher -> roster -> steward trail."
run "$PY" -m pantrypilot.cli sweep

step "3. Coordinator answers the escalations (simulated from data/coordinator_replies.json)"
if [[ $DRY -eq 1 ]]; then
  echo '$ python -m pantrypilot.cli decide <decision-id> "<coordinator reply>"   # one per pending decision'
else
  "$PY" - <<'EOF'
import json, pathlib
from pantrypilot import service
from pantrypilot.store import get_store

replies = json.loads(pathlib.Path("data/coordinator_replies.json").read_text())
pending = get_store().list_decisions(status="pending")
if not pending:
    print("no pending decisions; nothing to answer")
for d in pending:
    hit = next((r for r in replies if r["matches"].lower() in (d["summary"] or "").lower()), None)
    answer = hit["text"] if hit else (d.get("recommendation") or "yes")
    print(f"{d['id']} [{d['kind']}] {d['summary'][:80]}\n   -> {answer}")
    service.decide(d["id"], answer, {})
EOF
fi

step "4. Follow-up cycle: the swarm reads the coordinator's replies as inbound messages and acts on them"
run "$PY" -m pantrypilot.cli sweep

step "5. Ask a read-only question"
run "$PY" -m pantrypilot.cli ask "who is on Saturday morning intake, and is anyone still tentative?"

step "6. Status"
run "$PY" -m pantrypilot.cli status
