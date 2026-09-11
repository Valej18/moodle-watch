"""La politique : classer sans jamais decider en silence."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from moodle_watch.ledger import Ledger
from moodle_watch.policy import (Politique, Regle, appliquer, ecrire_config_moodle_dl,
                                 parse_taille)
from tests.conftest import FauxCours

def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


ANNEE_2 = _epoch("2026-09-01")   # la rentree en cours
ANNEE_1 = _epoch("2025-09-01")   # celle d'avant, dont on ne veut plus

TOML = """
default = "ask"
[[rule]]
match  = { startdate_after = "2026-08-01" }
action = "watch"
[[rule]]
match  = { shortname = "*-2025" }
action = "ignore"
[exclude]
extensions  = ["mp4"]
larger_than = "500MB"
filenames   = ["department-logo.png"]
"""


@pytest.fixture
def politique(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text(TOML, encoding="utf-8")
    return Politique.charger(p)


def test_le_fichier_se_cree_seul(tmp_path):
    pol = Politique.charger(tmp_path / "sous" / "policy.toml")
    assert (tmp_path / "sous" / "policy.toml").exists()
    assert pol.defaut == "ask" and pol.regles == ()


def test_classement_par_date_et_par_nom(politique):
    assert politique.classer(FauxCours(id=1, startdate=ANNEE_2))[0] == "watch"
    assert politique.classer(FauxCours(id=2, shortname="LING204-2025", startdate=ANNEE_1))[0] == "ignore"
    # Ni l'un ni l'autre : reste a arbitrer, jamais decide en silence.
    etat, regle = politique.classer(FauxCours(id=3, shortname="PSYC220", startdate=ANNEE_1))
    assert (etat, regle) == ("pending", "default:ask")


def test_la_premiere_regle_gagne(politique):
    """Un cours courant nomme *-2025 doit etre surveille : sa regle vient avant."""
    assert politique.classer(FauxCours(id=4, shortname="X-2025", startdate=ANNEE_2))[0] == "watch"


def test_une_decision_humaine_n_est_jamais_ecrasee(tmp_path, politique):
    """Sinon, repondre a une question de `pending` ne servirait a rien."""
    with Ledger(tmp_path / "w.db") as l:
        vieux = FauxCours(id=1631, shortname="LING204-2025", startdate=ANNEE_1)
        assert appliquer(politique, l, [vieux])["ignore"] == [1631]

        l.set_course_policy(vieux, "watch", by="alice", rule="je le veux quand meme")
        assert appliquer(politique, l, [vieux])["watch"] == [1631]
        assert l.policies()[1631]["decided_by"] == "alice"


def test_ecriture_du_config_moodle_dl(tmp_path, politique):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"token": "secret", "download_course_ids": [48],
                             "download_forums": True}), encoding="utf-8")

    conf = ecrire_config_moodle_dl(politique, p, [1631, 1362])

    assert conf["download_course_ids"] == [1362, 1631]
    assert conf["exclude_file_extensions"] == ["mp4"]
    assert conf["max_file_size"] == 500 * 1024**2
    assert conf["token"] == "secret", "le jeton ne doit jamais etre touche"
    assert conf["download_forums"] is True, "les options existantes sont preservees"
    assert oct(p.stat().st_mode)[-3:] == "600", "le fichier porte le jeton en clair"


def test_exclusion_par_nom_de_fichier(politique):
    """Un vrai miroir avait tire le meme logo sept fois, pour 9,2 Mo de bruit."""
    assert politique.exclut_fichier("department-logo.png")
    assert not politique.exclut_fichier("Programme.pdf")


@pytest.mark.parametrize("texte, octets", [("500MB", 524288000), ("2GB", 2147483648),
                                           ("1024", 1024), ("", 0), (None, 0)])
def test_tailles(texte, octets):
    assert parse_taille(texte) == octets


def test_critere_inconnu_est_une_erreur(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('default = "watch"\n[[rule]]\nmatch = { couleur = "bleu" }\naction = "watch"\n')
    with pytest.raises(ValueError, match="critere inconnu"):
        Politique.charger(p).classer(FauxCours(id=1))


def test_le_glob_doit_coller_aux_vrais_noms(politique):
    """Piege reel : `*-2025` ne prend pas `LING301-SA2025`, seulement `LING204-2025`.

    Les codes de cours melangent souvent les deux formes (`LING204-2025`, `LING301-SA2025`,
    `EDUC110-2025`, `METH101- 2025-2026`). Un glob trop etroit laisse passer des
    cours qu'on croyait ecartes, en silence. `*2025*` les prend tous.
    """
    assert politique.classer(FauxCours(id=1, shortname="LING204-2025", startdate=ANNEE_1))[0] == "ignore"
    assert politique.classer(FauxCours(id=2, shortname="LING301-SA2025", startdate=ANNEE_1))[0] == "pending"

    large = Politique(defaut="ask", regles=(Regle({"shortname": "*2025*"}, "ignore"),))
    for nom in ("LING204-2025", "LING301-SA2025", "EDUC110-2025", "METH101- 2025-2026"):
        assert large.classer(FauxCours(id=3, shortname=nom))[0] == "ignore", nom
