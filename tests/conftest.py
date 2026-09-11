"""Un miroir moodle-dl fabrique, pour tester le registre sans reseau.

Le schema est celui de moodle-dl 2.3.13, recopie verbatim depuis
`moodle_dl/database.py`. Le recopier plutot que l'importer est delibere : si
l'amont change de forme, ces tests doivent continuer de decrire le contrat sur
lequel le registre est bati, et c'est `doctor` qui signale la divergence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

SCHEMA_MIROIR = """
CREATE TABLE files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id integer NOT NULL,
    course_fullname integer NOT NULL,
    module_id integer NOT NULL,
    section_name text NOT NULL,
    module_name text NOT NULL,
    content_filepath text NOT NULL,
    content_filename text NOT NULL,
    content_fileurl text NOT NULL,
    content_filesize integer NOT NULL,
    content_timemodified integer NOT NULL,
    module_modname text NOT NULL,
    content_type text NOT NULL,
    content_isexternalfile text NOT NULL,
    saved_to text NOT NULL,
    hash text NULL,
    time_stamp integer NOT NULL,
    old_file_id integer NULL,
    modified integer DEFAULT 0 NOT NULL,
    moved integer DEFAULT 0 NOT NULL,
    deleted integer DEFAULT 0 NOT NULL,
    notified integer DEFAULT 0 NOT NULL,
    section_id integer DEFAULT 0 NOT NULL
);
"""


@dataclass
class FauxDelta:
    """La forme que `engine.Delta` presente au registre, sans importer moodle-dl."""

    file_id: int
    course_id: int
    module_id: int
    content_filepath: str
    content_filename: str
    event: str = "appeared"


class Miroir:
    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA_MIROIR)
        self.con.commit()

    def ajouter(
        self,
        file_id: int,
        *,
        course_id: int = 1631,
        module_id: int = 900,
        filename: str = "cours.pdf",
        filepath: str = "/",
        timemodified: int = 1_700_000_000,
        old_file_id: int | None = None,
        modified: int = 0,
        deleted: int = 0,
        section: str = "Seance 1",
        module: str = "Support de cours",
        modname: str = "resource",
    ) -> FauxDelta:
        self.con.execute(
            "INSERT INTO files (file_id, course_id, course_fullname, module_id, section_name, "
            "module_name, content_filepath, content_filename, content_fileurl, content_filesize, "
            "content_timemodified, module_modname, content_type, content_isexternalfile, saved_to, "
            "time_stamp, old_file_id, modified, deleted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (file_id, course_id, "LING204 - Language across the curriculum", module_id, section, module,
             filepath, filename, "https://exemple/pluginfile.php/1/x", 1024, timemodified,
             modname, "file", "0", f"/tmp/{filename}", old_file_id, modified, deleted),
        )
        self.con.commit()
        evenement = "vanished" if deleted else ("changed" if modified or old_file_id else "appeared")
        return FauxDelta(file_id, course_id, module_id, filepath, filename, evenement)


@dataclass
class FauxCours:
    id: int
    shortname: str = "LING204-2025"
    fullname: str = "LING204 - Language across the curriculum"
    startdate: int = 1_757_282_400
    enddate: int = 0


@pytest.fixture
def miroir(tmp_path: Path) -> Miroir:
    return Miroir(tmp_path / "moodle_state.db")


@pytest.fixture
def registre(tmp_path: Path, miroir: Miroir):
    from moodle_watch.ledger import Ledger

    with Ledger(tmp_path / "moodle_watch.db", miroir.path) as l:
        yield l
