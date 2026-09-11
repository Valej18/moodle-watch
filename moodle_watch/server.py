"""La surface MCP, et rien d'autre.

Ce module ne contient aucune logique : il traduit `queries.py` et `ledger.py` en
outils. Toute question qu'on peut poser ici doit pouvoir se poser sans MCP, par
la ligne de commande ou par un test.

Les noms d'outils et leurs docstrings sont en anglais : ils sont lus par le
modele et par des inconnus qui installent le projet. Les commentaires internes
restent en francais, comme le reste du depot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import queries
from .config import Config
from .documents import extract_text
from .engine import Engine, TokenExpired
from .ledger import Ledger

INSTRUCTIONS = """\
A Moodle course space, watched over time.

This server does not just query Moodle. It keeps a ledger of its own, and that
ledger answers the two questions the Moodle API cannot:

  * when did this first appear — Moodle reports only its own modification time,
    never when you first saw something;
  * what have you already done with it — and a file that changes afterwards
    becomes untreated again, automatically.

Three rules.

1. Start with `whats_new`. It is the point of this server. `courses` and
   `backlog` frame it; `course_tree` and `search` drill into it.
2. This server never writes to Moodle. It cannot submit, grade, or post: only
   read functions are reachable. `mark_handled` writes the ledger, not Moodle.
3. A course listed under `pending_courses` has never been arbitrated. Do not
   assume it is being watched. Ask the user, then call `set_course_policy`.

A `token_expired` payload is not a server fault: the Moodle token is issued by
SSO and is renewed by hand. Report the command it gives you.
"""

server = MCPServer(
    name="moodle-watch",
    title="Moodle watch",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

_cfg: Config | None = None


def cfg() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config.from_env()
    return _cfg


def _json(donnees: Any) -> str:
    return json.dumps(donnees, ensure_ascii=False, indent=2, default=str)


def _ledger() -> Ledger:
    c = cfg()
    return Ledger(c.db, Engine(c.mirror).state_db)


def _token_expired(err: TokenExpired) -> str:
    # Rendu comme une donnee et non comme une erreur, pour que le modele le dise
    # a l'utilisateur avec la commande exacte au lieu de tenter autre chose.
    return _json({"status": "token_expired", "detail": str(err)})


# --- Reading the watch ------------------------------------------------------


@server.tool()
def courses(include_ignored: bool = False) -> str:
    """List watched courses, with unhandled counts and the next deadline.

    Each course carries the `rule` that decided its state, so "why does this
    course not show up" always has an answer. `state` is one of watch, ignore,
    or pending; a pending course has never been arbitrated by anyone.
    """
    with _ledger() as l:
        return _json(queries.courses(l, include_ignored=include_ignored))


@server.tool()
def whats_new(since: str = "last_run", course: int | None = None,
              include_handled: bool = False, limit: int = 200) -> str:
    """What appeared, changed or vanished since `since`. The main tool.

    `since` accepts `last_run` (the default), `all`, a duration like `7d` or
    `48h`, an ISO date like `2026-09-01`, or an epoch integer.

    Already handled items are hidden unless `include_handled` is true: the
    question asked is what remains, not what happened. The reply also carries
    deadline changes (a `moved_from` means the due date was shifted) and
    `pending_courses`, which need the user's arbitration.
    """
    with _ledger() as l:
        return _json(queries.whats_new(l, since=since, course=course,
                                       include_handled=include_handled, limit=limit))


@server.tool()
def deadlines(within: str = "30d", include_past: bool = False) -> str:
    """Upcoming deadlines, soonest first.

    `moved` is true when the due date changed since it was first seen, and
    `moved_from` gives the previous one. No Moodle API reports this; it is known
    only because the ledger saw the earlier value.
    """
    with _ledger() as l:
        return _json(queries.deadlines(l, within=within, include_past=include_past))


@server.tool()
def course_tree(course: int, only_unhandled: bool = False) -> str:
    """Sections and modules of one course, each item annotated with its state.

    Use this to navigate a course rather than to search it. For a whole-space
    lookup by name, use `search`.
    """
    with _ledger() as l:
        return _json(queries.course_tree(l, course, only_unhandled=only_unhandled))


@server.tool()
def search(query: str, course: int | None = None, limit: int = 40) -> str:
    """Full-text search over course, section, module and file names.

    Accent-insensitive: `evaluation` finds `Évaluation`. Supports FTS5 syntax,
    so `dimension NEAR langagiere` and `program*` work.
    """
    with _ledger() as l:
        return _json(queries.search(l, query, course=course, limit=limit))


@server.tool()
def backlog(course: int | None = None, limit: int = 100) -> str:
    """Everything not handled yet, oldest first. The work queue.

    An item leaves this list when `mark_handled` records a `read`, `ingested` or
    `ignored` action for it. Marking it `todo` keeps it here on purpose.
    """
    with _ledger() as l:
        return _json(queries.backlog(l, course=course, limit=limit))


@server.tool()
def history(item_key: str) -> str:
    """Every revision of one item, and everything that was done with it.

    `item_key` comes from any other tool's output. Revisions follow the file
    across changes and moves, so this is the honest answer to "has this been
    updated since I read it".
    """
    with _ledger() as l:
        return _json(queries.history(l, item_key))


@server.tool()
def read_document(file_id: int, pages: str | None = None, max_chars: int = 40000) -> str:
    """Extract the text of one file, by its `file_id`.

    Reads the local copy when the mirror has it, and downloads through the
    Moodle token otherwise. `pages` selects PDF pages, for example `1-5` or `3`.
    Media files return their metadata and no text: a video is not summarised
    here.
    """
    c = cfg()
    with _ledger() as l:
        if not l.ensure_attached():
            return _json({"status": "no_mirror", "detail": "Run a sync first."})
        row = l.con.execute(
            "SELECT f.* FROM mirror.files f WHERE f.file_id = ?", (file_id,)
        ).fetchone()
        if row is None:
            return _json({"status": "unknown_file_id", "file_id": file_id})
        try:
            return _json(extract_text(dict(row), Engine(c.mirror), pages=pages, max_chars=max_chars))
        except TokenExpired as err:
            return _token_expired(err)


# --- Writing the ledger, never Moodle ---------------------------------------


@server.tool()
def mark_handled(file_ids: list[int], action: str, by: str, note: str | None = None) -> str:
    """Record that these items were dealt with. Writes the ledger, not Moodle.

    `action` is one of read, ingested, ignored, todo. `by` is free text naming
    who did it, for example `nightly` or `alice`. `note` is the right place
    for a link to whatever was produced.

    Handling is per revision: if the file changes later, it returns to the
    backlog on its own.
    """
    with _ledger() as l:
        try:
            n = l.mark_handled(file_ids, action=action, by=by, note=note)
        except (KeyError, ValueError) as err:
            return _json({"status": "rejected", "detail": str(err)})
        return _json({"status": "ok", "marked": n, "action": action, "by": by})


@server.tool()
def set_course_policy(course_id: int, state: str, by: str, reason: str | None = None) -> str:
    """Decide whether a course is watched. Use this to answer a pending course.

    `state` is watch, ignore or pending. A decision made with a `by` other than
    `policy` is never overwritten by a policy rule afterwards, so the user's
    answer sticks.
    """
    with _ledger() as l:
        connu = l.policies().get(course_id)
        if connu is None:
            return _json({"status": "unknown_course", "course_id": course_id,
                          "detail": "Run a sync first so the course is discovered."})

        class _C:
            id = course_id
            shortname = connu["shortname"]
            fullname = connu["fullname"]
            startdate = connu["startdate"]
            enddate = connu["enddate"]

        try:
            l.set_course_policy(_C, state, by=by, rule=reason or "manual")
        except ValueError as err:
            return _json({"status": "rejected", "detail": str(err)})
        return _json({"status": "ok", "course_id": course_id, "state": state, "by": by})


# --- Running a sync ---------------------------------------------------------


@server.tool()
def sync(download: bool = False) -> str:
    """Start a watch cycle in the background and return at once.

    A full cycle takes minutes, so it never runs inside a tool call. Poll
    `sync_status` for the result. With `download` true it also fetches files,
    which takes considerably longer.
    """
    c = cfg()
    with _ledger() as l:
        en_cours = l.con.execute(
            "SELECT id, started, mode FROM run WHERE finished IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if en_cours and time.time() - en_cours["started"] < 3600:
            return _json({"status": "already_running", "run_id": en_cours["id"],
                          "started": en_cours["started"], "mode": en_cours["mode"]})

    journal = Path(c.db).with_suffix(".sync.log")
    argv = [sys.executable, "-m", "moodle_watch.cli", "run"] + (["--fetch"] if download else [])
    with journal.open("ab") as sortie:
        proc = subprocess.Popen(
            argv, stdout=sortie, stderr=sortie, start_new_session=True, env=os.environ.copy()
        )
    return _json({"status": "started", "pid": proc.pid, "mode": "fetch" if download else "watch",
                  "log": str(journal), "next": "poll sync_status"})


@server.tool()
def sync_status() -> str:
    """The state of the most recent sync, running or finished.

    `finished` null means it is still going. A non-null `error` starting with
    `token_expired` means the Moodle token must be renewed by hand; report the
    command rather than retrying.
    """
    with _ledger() as l:
        row = l.con.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        return _json(
            {
                "run": dict(row) if row else None,
                "observations": l.con.execute("SELECT count(*) FROM observation").fetchone()[0],
                "unhandled": len(queries.backlog(l, limit=100000)) if l.attached else 0,
            }
        )


def main() -> None:
    c = cfg()
    if "--http" in sys.argv:
        server.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=c.port,
            transport_security=TransportSecuritySettings(
                allowed_hosts=list(c.hosts),
                allowed_origins=list(c.origins) or None,
            ),
        )
    else:
        server.run()


if __name__ == "__main__":
    main()
