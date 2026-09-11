"""Les questions, en lecture seule.

Aucune dependance MCP, aucune dependance moodle-dl : testable seul. `server.py`
ne fait que traduire ces fonctions en outils.

Toutes les jointures passent par `mirror.files`, la table de moodle-dl, attachee
en lecture seule. Le registre ne duplique jamais un nom de fichier ni une taille.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from .ledger import Ledger

#: `todo` est une intention, pas un traitement : un element marque `todo` reste
#: dans le retard. Les trois autres actions le retirent.
ACTIONS_QUI_TRAITENT = ("read", "ingested", "ignored")

_DUREE = re.compile(r"^(\d+)\s*([hjdwm])$", re.IGNORECASE)

#: Seule la **derniere** revision d'un element compte comme du travail. Sans ce
#: predicat, un fichier remplace laissait son ancienne revision dans le retard, et
#: un fichier disparu y restait pour toujours. Detecte par
#: `test_disparition_n_efface_rien`.
_DERNIERE_REVISION = (
    "o.file_id = (SELECT max(o2.file_id) FROM observation o2 WHERE o2.item_key = o.item_key)"
)

#: La jointure commune. Tout ce qui suit s'appuie dessus, pour que « traite » ait
#: exactement le meme sens partout.
_BASE = """
    SELECT o.file_id, o.item_key, o.course_id, o.first_seen, o.event, o.run_id,
           f.course_fullname, f.section_name, f.module_id, f.module_name,
           f.module_modname, f.content_filepath, f.content_filename,
           f.content_filesize, f.content_timemodified, f.content_type, f.saved_to,
           h.action AS handled_action, h.by AS handled_by, h.at AS handled_at, h.note AS handled_note
      FROM observation o
      JOIN mirror.files f ON f.file_id = o.file_id
      LEFT JOIN handled h ON h.file_id = o.file_id AND h.action IN ('read','ingested','ignored')
"""


def parse_since(ledger: Ledger, since: str | int | None) -> int:
    """Traduit `last_run`, `7d`, `48h`, une date ISO ou un entier en epoch.

    `last_run` rend la fin du dernier run reussi, ou 0 s'il n'y en a jamais eu :
    un premier appel montre alors tout, ce qui est le comportement attendu.
    """
    if since is None or since == "":
        since = "last_run"
    if isinstance(since, int):
        return since
    texte = str(since).strip()
    if texte.isdigit():
        return int(texte)
    if texte == "last_run":
        row = ledger.last_run()
        return int(row["finished"]) if row and row["finished"] else 0
    if texte in ("all", "always", "toujours"):
        return 0
    m = _DUREE.match(texte)
    if m:
        n, unite = int(m.group(1)), m.group(2).lower()
        secondes = {"h": 3600, "j": 86400, "d": 86400, "w": 604800, "m": 2592000}[unite]
        return int(time.time()) - n * secondes
    try:
        dt = datetime.fromisoformat(texte)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError as err:
        raise ValueError(
            f"`since` incomprehensible : {since!r}. Accepte last_run, all, 7d, 48h, "
            f"une date ISO (2026-09-01) ou un entier epoch."
        ) from err


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _item(row: Any) -> dict[str, Any]:
    return {
        "file_id": row["file_id"],
        "item_key": row["item_key"],
        "course_id": row["course_id"],
        "course": row["course_fullname"],
        "section": row["section_name"],
        "module": row["module_name"],
        "modname": row["module_modname"],
        "filename": row["content_filename"],
        "path": row["content_filepath"],
        "size": row["content_filesize"],
        "type": row["content_type"],
        "event": row["event"],
        "first_seen": _iso(row["first_seen"]),
        "server_modified": _iso(row["content_timemodified"]),
        "saved_to": row["saved_to"] or None,
        "handled": (
            None
            if row["handled_action"] is None
            else {
                "action": row["handled_action"],
                "by": row["handled_by"],
                "at": _iso(row["handled_at"]),
                "note": row["handled_note"],
            }
        ),
    }


def courses(ledger: Ledger, *, include_ignored: bool = False) -> list[dict[str, Any]]:
    """Les cours, avec leur retard, leur prochaine echeance et la regle qui a decide.

    La regle est rendue exprès : « pourquoi ce cours ne remonte pas » doit avoir
    une reponse, sinon une liste blanche devient un piege silencieux.
    """
    sortie = []
    for p in ledger.con.execute("SELECT * FROM course_policy ORDER BY startdate DESC, course_id"):
        if p["state"] == "ignore" and not include_ignored:
            continue
        retard = ledger.con.execute(
            f"SELECT count(*) FROM ({_BASE} WHERE o.course_id = ? AND h.action IS NULL "
            f"AND o.event != 'vanished' AND {_DERNIERE_REVISION})",
            (p["course_id"],),
        ).fetchone()[0] if ledger.attached else 0
        total = ledger.con.execute(
            "SELECT count(*) FROM observation WHERE course_id = ?", (p["course_id"],)
        ).fetchone()[0]
        prochaine = ledger.con.execute(
            "SELECT name, timesort FROM deadline WHERE course_id = ? AND gone_at IS NULL "
            "AND timesort > ? ORDER BY timesort LIMIT 1",
            (p["course_id"], int(time.time())),
        ).fetchone()
        sortie.append(
            {
                "course_id": p["course_id"],
                "shortname": p["shortname"],
                "fullname": p["fullname"],
                "state": p["state"],
                "rule": p["rule"],
                "decided_by": p["decided_by"],
                "starts": _iso(p["startdate"]),
                "items": total,
                "unhandled": retard,
                "next_deadline": (
                    None if prochaine is None
                    else {"name": prochaine["name"], "due": _iso(prochaine["timesort"])}
                ),
            }
        )
    return sortie


def whats_new(
    ledger: Ledger,
    *,
    since: str | int | None = "last_run",
    course: int | None = None,
    include_handled: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """Ce qui a bouge depuis `since`, groupe par cours.

    L'outil principal. Un element deja traite est masque par defaut : la question
    posee est « qu'est-ce qui me reste », pas « qu'est-ce qui s'est passe ».
    """
    borne = parse_since(ledger, since)
    if not ledger.attached:
        return {"since": _iso(borne), "courses": [], "pending_courses": [], "deadlines": []}

    sql = _BASE + f" WHERE o.first_seen >= ? AND {_DERNIERE_REVISION}"
    params: list[Any] = [borne]
    if course is not None:
        sql += " AND o.course_id = ?"
        params.append(course)
    if not include_handled:
        sql += " AND h.action IS NULL"
    sql += " ORDER BY o.course_id, o.first_seen DESC, o.file_id LIMIT ?"
    params.append(limit)

    par_cours: dict[int, dict[str, Any]] = {}
    for row in ledger.con.execute(sql, params):
        bloc = par_cours.setdefault(
            row["course_id"], {"course_id": row["course_id"], "course": row["course_fullname"], "items": []}
        )
        bloc["items"].append(_item(row))

    echeances = [
        {
            "name": r["name"],
            "course_id": r["course_id"],
            "due": _iso(r["timesort"]),
            "moved_from": _iso(r["timesort_previous"]),
            "gone": bool(r["gone_at"]),
        }
        for r in ledger.con.execute(
            "SELECT * FROM deadline WHERE (first_seen >= ? OR (timesort_previous IS NOT NULL "
            "AND last_seen >= ?)) ORDER BY timesort",
            (borne, borne),
        )
    ]

    # Un cours decouvert et jamais arbitre. Il ne doit ni se surveiller ni
    # s'ignorer en silence : il remonte ici jusqu'a ce qu'on tranche.
    en_attente = [
        {"course_id": r["course_id"], "shortname": r["shortname"], "fullname": r["fullname"],
         "starts": _iso(r["startdate"])}
        for r in ledger.con.execute(
            "SELECT * FROM course_policy WHERE state = 'pending' ORDER BY startdate DESC"
        )
    ]

    return {
        "since": _iso(borne),
        "courses": list(par_cours.values()),
        "deadlines": echeances,
        "pending_courses": en_attente,
    }


def deadlines(ledger: Ledger, *, within: str = "30d", include_past: bool = False) -> list[dict[str, Any]]:
    m = _DUREE.match(within)
    horizon = int(time.time()) + (
        int(m.group(1)) * {"h": 3600, "j": 86400, "d": 86400, "w": 604800, "m": 2592000}[m.group(2).lower()]
        if m else 30 * 86400
    )
    sql = "SELECT * FROM deadline WHERE gone_at IS NULL AND timesort <= ?"
    params: list[Any] = [horizon]
    if not include_past:
        sql += " AND timesort >= ?"
        params.append(int(time.time()))
    return [
        {
            "event_id": r["event_id"],
            "name": r["name"],
            "course_id": r["course_id"],
            "modname": r["modulename"],
            "due": _iso(r["timesort"]),
            "in_days": round((r["timesort"] - time.time()) / 86400, 1),
            "moved": r["timesort_previous"] is not None,
            "moved_from": _iso(r["timesort_previous"]),
        }
        for r in ledger.con.execute(sql + " ORDER BY timesort", params)
    ]


def course_tree(ledger: Ledger, course: int, *, only_unhandled: bool = False) -> dict[str, Any]:
    """Sections et modules d'un cours, chacun annote de son etat. C'est « naviguer »."""
    if not ledger.attached:
        return {"course_id": course, "sections": []}
    sql = _BASE + f" WHERE o.course_id = ? AND o.event != 'vanished' AND {_DERNIERE_REVISION}"
    if only_unhandled:
        sql += " AND h.action IS NULL"
    sections: dict[str, dict[str, Any]] = {}
    nom = None
    for row in ledger.con.execute(sql + " ORDER BY f.section_name, f.module_name, f.content_filename", (course,)):
        nom = nom or row["course_fullname"]
        sec = sections.setdefault(row["section_name"] or "", {"section": row["section_name"], "modules": {}})
        mod = sec["modules"].setdefault(
            row["module_name"], {"module": row["module_name"], "modname": row["module_modname"], "items": []}
        )
        mod["items"].append(_item(row))
    return {
        "course_id": course,
        "course": nom,
        "sections": [
            {"section": s["section"], "modules": list(s["modules"].values())} for s in sections.values()
        ],
    }


def search(ledger: Ledger, query: str, *, course: int | None = None, limit: int = 40) -> list[dict[str, Any]]:
    if not ledger.attached:
        return []
    sql = (
        "SELECT o.file_id FROM item_fts t JOIN observation o ON o.file_id = t.file_id "
        "WHERE item_fts MATCH ?"
    )
    params: list[Any] = [query]
    if course is not None:
        sql += " AND t.course_id = ?"
        params.append(course)
    ids = [r["file_id"] for r in ledger.con.execute(sql + " LIMIT ?", (*params, limit))]
    if not ids:
        return []
    marques = ",".join("?" * len(ids))
    return [_item(r) for r in ledger.con.execute(_BASE + f" WHERE o.file_id IN ({marques})", ids)]


def backlog(ledger: Ledger, *, course: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Tout ce qui n'a pas ete traite, du plus ancien. La liste de travail."""
    if not ledger.attached:
        return []
    sql = _BASE + f" WHERE h.action IS NULL AND o.event != 'vanished' AND {_DERNIERE_REVISION}"
    params: list[Any] = []
    if course is not None:
        sql += " AND o.course_id = ?"
        params.append(course)
    sql += " ORDER BY o.first_seen, o.file_id LIMIT ?"
    params.append(limit)
    return [_item(r) for r in ledger.con.execute(sql, params)]


def history(ledger: Ledger, item_key: str) -> dict[str, Any]:
    """Toutes les revisions d'un element, et tout ce qu'on en a fait."""
    revisions = []
    for r in ledger.con.execute(
        (_BASE if ledger.attached else "SELECT o.* FROM observation o LEFT JOIN handled h ON 0")
        + " WHERE o.item_key = ? ORDER BY o.file_id",
        (item_key,),
    ):
        revisions.append(_item(r) if ledger.attached else dict(r))
    traitements = [
        {"file_id": r["file_id"], "action": r["action"], "by": r["by"], "at": _iso(r["at"]), "note": r["note"]}
        for r in ledger.con.execute(
            "SELECT * FROM handled WHERE item_key = ? ORDER BY at", (item_key,)
        )
    ]
    return {"item_key": item_key, "revisions": revisions, "handlings": traitements}
