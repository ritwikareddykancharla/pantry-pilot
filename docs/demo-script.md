# Demo video script (4:00)

Setup before recording: `make seed`, `SWEEP_INTERVAL_SECONDS=0 make serve`, open http://localhost:8000, browser at 1440px wide, console panel empty (fresh seed). Credentials for Bedrock configured.

## 0:00-0:40 The problem

Screen: a phone-shaped mock of a coordinator's text thread (or the Messages panel of the console).

"Meet Aisha. She coordinates the Maple Street Community Pantry, which means that at 9pm on a Friday she is texting to fill tomorrow's 9am intake desk after a cancellation, telling a church donor when to bring 40 pounds of rice, deciding whether 30 frozen turkeys will fit in one chest freezer, and checking that a 16-year-old volunteer will have a supervisor on Tuesday. None of this is hard. It is relentless, and coordinator burnout is one of the main reasons small pantries shrink or close."

## 0:40-1:00 Who it is for, and why an agent

"Pantry Pilot is for the one or two volunteer coordinators who run small pantries, community fridges and mutual-aid groups. It handles the routine on its own, and it asks Aisha only when a person has to decide. She lives in a messaging app, so the agent asks her the way a volunteer would: a short question with two or three options."

## 1:00-3:20 Live demo

**1:00** Show the console header: "Pantry Pilot", the board with Saturday intake in amber (2 of 3), Thursday delivery in red (0 of 1). Point at the Messages panel: seven unhandled texts from this morning.

**1:15** Click **Run daily cycle**. While it runs (20-40s), narrate: "The dispatcher reads the queue, hands shift questions to the roster agent and donation questions to the steward. Every tool call is recorded with the agent's name."

**1:45** Handoff trail appears: `dispatcher -> roster -> dispatcher -> steward -> dispatcher`. Scroll the Activity feed: "assign_volunteer Jorge confirmed for Thursday delivery", "send_message to Jorge", "log_donation Linda, 40 lbs rice", "send_message Linda: Saturday 9-10am works", "send_shift_reminders 5 sent", "mark_message_handled".

**2:05** Panel **Needs you**: two or three cards.
- "Priya and Marcus both want the last Saturday intake spot. Recommendation: Priya (fewest recent shifts)." Options as buttons.
- "Riverside Church offers 30 frozen turkeys Friday; freezer has room for about 10." Options: accept 10 / decline / borrow freezer.
- "Sam (16) asked for Tuesday 4pm; no supervisor on that shift yet." Options.

**2:25** Click **Give Saturday 9am intake to Priya** on the first card. Card moves to resolved. Narrate: "That tap becomes an inbound message from the coordinator. The swarm reads it on the next cycle like any other text."

**2:35** Follow-up cycle runs automatically. Board updates: Saturday intake now 3 of 3, Priya confirmed. Messages panel: outbound to Priya ("you're confirmed") and Marcus ("first alternate, thank you").

**2:50** Live text. In **Simulate incoming text** choose "Helen" and type "Yes I can supervise Tuesday". Submit. Within seconds: activity shows roster assigning Helen and confirming Sam, outbound texts to both.

**3:05** Scroll to **Weekly brief**: coverage percent, open slots, donations, below par (diapers size 4, canned beans), expiring (milk), thank-you list (Jorge, Priya). Inventory bars below.

## 3:20-3:50 Architecture

Screen: `docs/architecture.png`.

"Three Strands agents in a Swarm with handoffs and a shared session. Two dozen tools over a small SQLite store. An AuditHook on AfterToolCallEvent writes the trail you saw. Escalation is a tool that only creates a decision; the coordinator's answer comes back as a message. Gated actions like broadcasts store their exact payload and only execute after a yes. The whole thing runs as one entrypoint on Amazon Bedrock AgentCore Runtime with Claude Sonnet on Bedrock, and the console can point at the deployed runtime with one environment variable."

## 3:50-4:00 Close

"Pantry Pilot: fills shifts, tracks donations, answers volunteers, and escalates only real conflicts. Open source, Apache 2.0. Thanks."
