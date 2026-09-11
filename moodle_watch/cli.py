"""La ligne de commande. Sortie en anglais : elle sert aussi aux inconnus."""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config
from .engine import Engine, TokenExpired
from .ledger import Ledger
from . import queries, sync


def _print(donnees) -> None:
    print(json.dumps(donnees, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="moodle-watch",
        description="Watch a Moodle course space: what is new, what changed, what you already handled.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run one watch cycle")
    r.add_argument("--fetch", action="store_true", help="also download files (default: detect only)")
    r.add_argument("--by", default="policy")

    sub.add_parser("status", help="last runs and ledger size")
    c = sub.add_parser("courses", help="courses and why each one is watched or not")
    c.add_argument("--all", action="store_true", help="include ignored courses")
    n = sub.add_parser("new", help="what changed")
    n.add_argument("--since", default="last_run")
    n.add_argument("--course", type=int)
    b = sub.add_parser("backlog", help="everything not handled yet")
    b.add_argument("--course", type=int)
    b.add_argument("--limit", type=int, default=50)
    d = sub.add_parser("deadlines", help="upcoming deadlines")
    d.add_argument("--within", default="30d")
    doc = sub.add_parser("doctor", help="four-layer diagnostic")
    doc.add_argument("--no-server", action="store_true", help="skip layer 3 (the MCP session)")
    doc.add_argument("--remote", action="store_true", help="also exercise layer 4 (the public door)")

    args = p.parse_args(argv)
    cfg = Config.from_env()

    if args.command == "run":
        try:
            _print(sync.run_once(cfg, download=args.fetch, by=args.by))
        except TokenExpired as err:
            print(f"token_expired: {err}", file=sys.stderr)
            return 2
        return 0

    if args.command == "doctor":
        from .verify import doctor

        return doctor(cfg)

    with Ledger(cfg.db, Engine(cfg.mirror).state_db) as l:
        if args.command == "status":
            dernier = l.last_run(only_ok=False)
            _print(
                {
                    "ledger": str(cfg.db),
                    "mirror": str(cfg.mirror),
                    "observations": l.con.execute("SELECT count(*) FROM observation").fetchone()[0],
                    "handled": l.con.execute("SELECT count(*) FROM handled").fetchone()[0],
                    "last_run": dict(dernier) if dernier else None,
                }
            )
        elif args.command == "courses":
            _print(queries.courses(l, include_ignored=args.all))
        elif args.command == "new":
            _print(queries.whats_new(l, since=args.since, course=args.course))
        elif args.command == "backlog":
            _print(queries.backlog(l, course=args.course, limit=args.limit))
        elif args.command == "deadlines":
            _print(queries.deadlines(l, within=args.within))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
