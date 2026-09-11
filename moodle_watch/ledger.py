"""Le registre : l'horloge et la memoire de traitement.

Aucune dependance MCP, aucune dependance moodle-dl. Testable seul, et c'est
delibere : c'est la partie qui porte la valeur du projet, elle doit pouvoir etre
exercee sans reseau ni serveur.

Le registre ne redit pas ce que `moodle_state.db` sait deja. Il s'y attache en
lecture seule et n'ajoute que trois choses : quand un element a ete vu pour la
premiere fois, ce qu'on en a fait, et pourquoi son cours est suivi.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = Path(__file__).with_name("schema.sql")

#: Les actions admises par `handled`. Elles doivent rester peu nombreuses : une
#: taxonomie ouverte se remplirait de synonymes et la question « qu'est-ce qui
#: reste a faire » n'aurait plus de reponse.
ACTIONS = ("read", "ingested", "ignored", "todo")


def natural_key(course_id: int, module_id: int, filepath: str, filename: str) -> str:
    """La cle stable d'un element, independante de sa revision.

    Un renommage change la cle, donc se lit comme une disparition suivie d'une
    apparition. C'est assume : suivre un renommage demanderait de comparer des
    contenus, et moodle-dl ne calcule d'empreinte que pour 382 lignes sur 2210.
    Quand il signale lui-meme un deplacement, `Ledger.item_key` suit la chaine
    `old_file_id` et retrouve la bonne cle.
    """
    brut = f"{course_id}|{module_id}|{filepath}|{filename}"
    return hashlib.sha1(brut.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DeadlineChange:
    event_id: int
    name: str
    kind: str  # 'deadline_set' | 'deadline_moved' | 'deadline_gone'
    timesort: int
    previous: int | None


class Ledger:
    """Notre base, attachee a celle de moodle-dl.

    Le miroir est ouvert en `mode=ro` : moodle-watch ne modifie jamais l'etat de
    moodle-dl autrement que par moodle-dl lui-meme.
    """

    def __init__(self, db: str | Path, mirror_state_db: str | Path | None = None):
        self.path = Path(db)
        self.mirror_state_db = Path(mirror_state_db) if mirror_state_db else None
        self.con = sqlite3.connect(self.path, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.ensure_attached()

    def ensure_attached(self) -> bool:
        """Attache le miroir s'il existe et ne l'est pas deja.

        A appeler **apres** une collecte, pas seulement a la construction : sur un
        miroir neuf, `moodle_state.db` n'existe pas encore quand le registre
        s'ouvre, c'est moodle-dl qui le cree pendant la collecte. Sans ce rappel,
        tout ce qui depend de `mirror` echouait en silence et `rebuild_fts` rendait
        0. Detecte au premier vrai run, corrige par
        `test_attachement_tardif_du_miroir`.
        """
        if self.attached:
            return True
        if self.mirror_state_db and self.mirror_state_db.exists():
            self.attach(self.mirror_state_db)
            return True
        return False

    def attach(self, state_db: str | Path) -> None:
        uri = f"file:{Path(state_db).as_posix()}?mode=ro"
        self.con.execute("ATTACH DATABASE ? AS mirror", (uri,))

    @property
    def attached(self) -> bool:
        return any(r["name"] == "mirror" for r in self.con.execute("PRAGMA database_list"))

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------- runs

    def start_run(self, mode: str) -> int:
        cur = self.con.execute(
            "INSERT INTO run (started, mode) VALUES (?, ?)", (int(time.time()), mode)
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, courses: int = 0, events: int = 0, error: str | None = None) -> None:
        self.con.execute(
            "UPDATE run SET finished = ?, courses = ?, events = ?, error = ? WHERE id = ?",
            (int(time.time()), courses, events, error, run_id),
        )

    def last_run(self, *, only_ok: bool = True) -> sqlite3.Row | None:
        sql = "SELECT * FROM run WHERE finished IS NOT NULL"
        if only_ok:
            sql += " AND error IS NULL"
        return self.con.execute(sql + " ORDER BY finished DESC LIMIT 1").fetchone()

    # -------------------------------------------------------------- item_key

    def item_key(self, delta: Any) -> str:
        """La cle d'un delta, en suivant la chaine de revisions quand elle existe.

        moodle-dl pointe `old_file_id` vers la ligne precedente quand un fichier
        est modifie ou deplace. Si on connait deja cette ligne, on reprend sa cle :
        c'est ce qui fait qu'un deplacement reste le meme element, et non une
        disparition suivie d'une apparition.
        """
        if self.attached:
            row = self.con.execute(
                "SELECT old_file_id FROM mirror.files WHERE file_id = ?", (delta.file_id,)
            ).fetchone()
            if row and row["old_file_id"]:
                prev = self.con.execute(
                    "SELECT item_key FROM observation WHERE file_id = ?", (row["old_file_id"],)
                ).fetchone()
                if prev:
                    return prev["item_key"]
        return natural_key(
            delta.course_id, delta.module_id, delta.content_filepath, delta.content_filename
        )

    # -------------------------------------------------------------- ecritures

    def record(self, run_id: int, deltas: Sequence[Any], *, now: int | None = None) -> int:
        """Enregistre des deltas. Rend le nombre de lignes reellement inscrites.

        Idempotent : un `file_id` deja observe n'est pas reinscrit, et sa
        `first_seen` ne bouge jamais. C'est ce qui permet de rejouer un run
        interrompu sans fausser l'horloge.
        """
        maintenant = int(time.time()) if now is None else now
        lignes = [
            (d.file_id, self.item_key(d), d.course_id, maintenant, d.event, run_id)
            for d in deltas
        ]
        avant = self.con.execute("SELECT count(*) FROM observation").fetchone()[0]
        self.con.executemany(
            "INSERT OR IGNORE INTO observation "
            "(file_id, item_key, course_id, first_seen, event, run_id) VALUES (?,?,?,?,?,?)",
            lignes,
        )
        return self.con.execute("SELECT count(*) FROM observation").fetchone()[0] - avant

    def mark_handled(
        self,
        file_ids: Iterable[int],
        *,
        action: str,
        by: str,
        note: str | None = None,
        now: int | None = None,
    ) -> int:
        if action not in ACTIONS:
            raise ValueError(f"action inconnue : {action!r}. Admises : {', '.join(ACTIONS)}")
        maintenant = int(time.time()) if now is None else now
        lignes = []
        for fid in file_ids:
            row = self.con.execute(
                "SELECT item_key FROM observation WHERE file_id = ?", (fid,)
            ).fetchone()
            if row is None:
                raise KeyError(f"file_id {fid} inconnu du registre. Lancer une synchro d'abord.")
            lignes.append((row["item_key"], fid, maintenant, by, action, note))
        self.con.executemany(
            "INSERT OR REPLACE INTO handled (item_key, file_id, at, by, action, note) "
            "VALUES (?,?,?,?,?,?)",
            lignes,
        )
        return len(lignes)

    def set_course_policy(
        self,
        course: Any,
        state: str,
        *,
        by: str = "policy",
        rule: str | None = None,
        now: int | None = None,
    ) -> None:
        if state not in ("watch", "ignore", "pending"):
            raise ValueError(f"etat inconnu : {state!r}")
        self.con.execute(
            "INSERT INTO course_policy "
            "(course_id, shortname, fullname, startdate, enddate, state, decided_at, decided_by, rule) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(course_id) DO UPDATE SET "
            "  shortname=excluded.shortname, fullname=excluded.fullname, "
            "  startdate=excluded.startdate, enddate=excluded.enddate, state=excluded.state, "
            "  decided_at=excluded.decided_at, decided_by=excluded.decided_by, rule=excluded.rule",
            (
                course.id,
                getattr(course, "shortname", ""),
                getattr(course, "fullname", ""),
                getattr(course, "startdate", 0),
                getattr(course, "enddate", 0),
                state,
                int(time.time()) if now is None else now,
                by,
                rule,
            ),
        )

    def policies(self) -> dict[int, sqlite3.Row]:
        return {r["course_id"]: r for r in self.con.execute("SELECT * FROM course_policy")}

    def watched_ids(self) -> list[int]:
        return [
            r["course_id"]
            for r in self.con.execute(
                "SELECT course_id FROM course_policy WHERE state = 'watch' ORDER BY course_id"
            )
        ]

    # ------------------------------------------------------------- echeances

    def record_deadlines(self, events: Sequence[dict[str, Any]], *, now: int | None = None) -> list[DeadlineChange]:
        """Compare les echeances vues aux echeances connues.

        Rend les changements. `deadline_moved` est celui qui compte : aucune API
        Moodle ne signale qu'un rendu a ete repousse ou avance, il faut l'avoir
        vu avant pour le savoir.
        """
        maintenant = int(time.time()) if now is None else now
        connus = {r["event_id"]: r for r in self.con.execute("SELECT * FROM deadline WHERE gone_at IS NULL")}
        changements: list[DeadlineChange] = []
        vus: set[int] = set()

        for ev in events:
            eid = int(ev["id"])
            vus.add(eid)
            timesort = int(ev.get("timesort") or 0)
            nom = ev.get("name", "")
            cours = (ev.get("course") or {}).get("id")
            ancien = connus.get(eid)

            if ancien is None:
                self.con.execute(
                    "INSERT INTO deadline (event_id, course_id, cmid, name, modulename, timesort, "
                    "timesort_previous, first_seen, last_seen) VALUES (?,?,?,?,?,?,NULL,?,?)",
                    (eid, cours, ev.get("instance"), nom, ev.get("modulename"), timesort,
                     maintenant, maintenant),
                )
                changements.append(DeadlineChange(eid, nom, "deadline_set", timesort, None))
            elif ancien["timesort"] != timesort:
                self.con.execute(
                    "UPDATE deadline SET timesort = ?, timesort_previous = ?, last_seen = ?, name = ? "
                    "WHERE event_id = ?",
                    (timesort, ancien["timesort"], maintenant, nom, eid),
                )
                changements.append(
                    DeadlineChange(eid, nom, "deadline_moved", timesort, ancien["timesort"])
                )
            else:
                self.con.execute("UPDATE deadline SET last_seen = ? WHERE event_id = ?", (maintenant, eid))

        for eid, row in connus.items():
            if eid not in vus:
                self.con.execute("UPDATE deadline SET gone_at = ? WHERE event_id = ?", (maintenant, eid))
                changements.append(
                    DeadlineChange(eid, row["name"], "deadline_gone", row["timesort"], None)
                )
        return changements

    # ------------------------------------------------------------------- fts

    def rebuild_fts(self) -> int:
        """Reconstruit l'index plein texte depuis le miroir.

        Reconstruction complete plutot qu'incrementale : sur 2210 lignes c'est
        instantane, et un index reconstruit ne peut pas deriver de sa source.
        """
        if not self.attached:
            return 0
        self.con.execute("DELETE FROM item_fts")
        lignes = [
            (
                natural_key(r["course_id"], r["module_id"], r["content_filepath"], r["content_filename"]),
                r["course_id"],
                r["file_id"],
                " ".join(
                    filter(
                        None,
                        (
                            r["course_fullname"],
                            r["section_name"],
                            r["module_name"],
                            r["content_filename"],
                            r["module_modname"],
                        ),
                    )
                ),
            )
            for r in self.con.execute(
                "SELECT file_id, course_id, course_fullname, section_name, module_id, "
                "module_name, module_modname, content_filepath, content_filename "
                "FROM mirror.files WHERE deleted = 0"
            )
        ]
        self.con.executemany(
            "INSERT INTO item_fts (item_key, course_id, file_id, haystack) VALUES (?,?,?,?)", lignes
        )
        return len(lignes)
