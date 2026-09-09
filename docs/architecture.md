# Architecture

![Architecture](architecture.png)

Diagram: [architecture.png](architecture.png). Editable source: [architecture.excalidraw](architecture.excalidraw) (open at excalidraw.com), also [architecture.svg](architecture.svg).

## Components

| Layer | Where | Notes |
| --- | --- | --- |
| Demo data | `data/*.json`, `data/org.yaml` | Loaded once by `pantrypilot.seed` into SQLite. Replace with real connectors (Twilio, Google Sheets, Airtable) behind the same `Store` reads. |
| Store | `src/pantrypilot/store.py` | `sqlite3`, WAL mode, one `RLock`. Tables: `docs` (org, volunteers, shifts, inventory, contacts as JSON), `messages`, `decisions`, `audit`, `cycles`, `reports`, `hours`, `donations`, `meta`. Small enough to swap for DynamoDB. |
| Tools | `src/pantrypilot/tools/{common,roster,inventory}.py` | Plain functions decorated with `strands.tool`. They receive the calling `agent` and read `agent.state` for `cycle_id` and `approved`. |
| Ranking | `src/pantrypilot/ranking.py` | Pure functions: skills filter, availability windows, fairness (fewest shifts in 30 days), reliability tiebreak, minor supervision rule, quiet hours. |
| Agents | `src/pantrypilot/agents.py`, `prompts.py` | `dispatcher`, `roster`, `steward` in a `Swarm`; `ask` read-only agent; `briefer` for `WeeklyBrief` structured output. |
| Hooks | `src/pantrypilot/hooks.py` | `AuditHook` subscribes to `AfterToolCallEvent` and writes one audit row per tool call with `event.agent.name`. |
| Service | `src/pantrypilot/service.py` | `run_sweep`, `process_inbound`, `decide`, `ask`, `status`, `state_snapshot`. One `CYCLE_LOCK`; each cycle gets its own Swarm session id. |
| Entrypoints | `main.py`, `app/server.py`, `src/pantrypilot/cli.py` | Thin wrappers over `service`. `main.py` is the AgentCore HTTP entrypoint (`POST /invocations`). |
| Backends | `src/pantrypilot/backend.py` | `LocalBackend` (in-process) and `AgentCoreBackend` (`bedrock-agentcore` `invoke_agent_runtime`). Selected with `AGENT_BACKEND`. |

## A cycle, step by step

1. `service.run_sweep()` opens a cycle row, builds a fresh `Swarm` with a `FileSessionManager` (or `S3SessionManager` when `SESSION_BUCKET` is set) and an `AuditHook`, and stores `cycle_id` in every agent's state.
2. The task prompt gives the date, the unhandled message count and "Process every unhandled message, then run the daily checklist."
3. `dispatcher` reads the queue and hands off with the Swarm's built-in `handoff_to_agent(agent_name, message, context)` tool. `roster` and `steward` do their part and hand back.
4. Every tool call lands in `audit` via the hook. Routine tools (assign, send 1:1 message, log donation) act immediately. `escalate_to_coordinator` and the gated tools (`send_broadcast`, `place_supply_order`) only write a `decisions` row.
5. `SwarmResult.node_history` is saved on the cycle as the handoff trail. A `briefer` agent then produces the `WeeklyBrief` (structured output) from deterministic facts computed in `brief.compute_facts`.
6. The console polls `GET /api/state`. The coordinator taps an option. `service.decide` resolves the decision: approvals execute the stored tool call directly on a `dispatcher` instance constructed with `approved=True`; escalations are queued as an inbound message from `coordinator`, and the next cycle handles it like any other text.
