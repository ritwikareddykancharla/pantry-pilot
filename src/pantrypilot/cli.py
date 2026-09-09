"""Command line interface over the same service functions used by main.py and the web app.

python -m pantrypilot.cli seed
python -m pantrypilot.cli sweep
python -m pantrypilot.cli inbound --from V-04 "I can drive Thursday"
python -m pantrypilot.cli decide D-001 yes
python -m pantrypilot.cli ask "who is on Saturday morning?"
python -m pantrypilot.cli status
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config, service
from .seed import seed


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_seed(_: argparse.Namespace) -> int:
    counts = seed()
    print(f"Seeded {config.DB_PATH}")
    _print(counts)
    return 0


def cmd_sweep(_: argparse.Namespace) -> int:
    out = service.run_sweep()
    print(f"Cycle {out['cycle_id']} {out['status']}; handoff trail: {' -> '.join(out['handoff_trail']) or '-'}")
    print("\nActions taken:")
    for a in out["actions_taken"]:
        print(f"  [{a['agent']}] {a['tool']} {json.dumps(a['input'], default=str)[:100]}")
    print("\nOutbound messages:")
    for m in out["outbound"]:
        print(f"  -> {m['to_name']} ({m['kind']}): {m['body'][:90]}")
    print("\nPending decisions:")
    for d in out["pending_decisions"]:
        print(f"  {d['id']} [{d['kind']}] {d['summary']}")
        for i, opt in enumerate(d["options"]):
            print(f"      {i}. {opt}")
        if d.get("recommendation"):
            print(f"      recommendation: {d['recommendation']}")
    print("\nWeekly brief:")
    _print(out["report"])
    return 0


def cmd_inbound(args: argparse.Namespace) -> int:
    out = service.process_inbound(" ".join(args.text), from_id=args.from_id, from_name=args.from_name)
    print(f"Cycle {out['cycle_id']} {out['status']}; trail: {' -> '.join(out['handoff_trail']) or '-'}")
    for m in out.get("replies", []):
        print(f"  -> {m['to_name']} ({m['kind']}): {m['body'][:120]}")
    for d in out.get("pending_decisions", []):
        print(f"  pending {d['id']}: {d['summary']}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    edits = json.loads(args.edits) if args.edits else None
    out = service.decide(args.decision_id, " ".join(args.response), edits)
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_ask(args: argparse.Namespace) -> int:
    print(service.ask(" ".join(args.prompt)))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    _print(service.status())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pantrypilot", description="Pantry Pilot CLI")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="(re)create .data from data/*.json").set_defaults(func=cmd_seed)
    sub.add_parser("sweep", help="run one daily cycle").set_defaults(func=cmd_sweep)
    ib = sub.add_parser("inbound", help="simulate an incoming text and handle it now")
    ib.add_argument("--from", dest="from_id", default=None, help="volunteer/contact id or name")
    ib.add_argument("--from-name", dest="from_name", default=None)
    ib.add_argument("text", nargs="+")
    ib.set_defaults(func=cmd_inbound)
    d = sub.add_parser("decide", help="answer a decision: option index, option text, yes/no, or free text")
    d.add_argument("decision_id")
    d.add_argument("response", nargs="+")
    d.add_argument("--edits", default=None, help='JSON edits for approvals, e.g. \'{"body": "..."}\'')
    d.set_defaults(func=cmd_decide)
    a = sub.add_parser("ask", help="ask the read-only agent a question")
    a.add_argument("prompt", nargs="+")
    a.set_defaults(func=cmd_ask)
    sub.add_parser("status", help="counts, last report, last sweep time").set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    config.configure_logging()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
