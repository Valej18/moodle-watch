"""Les proprietes sur lesquelles repose tout le projet.

Chaque test ici correspond a une promesse faite a l'utilisateur. S'il tombe, la
promesse est fausse, pas seulement le code.
"""

from __future__ import annotations

import pytest

from moodle_watch import queries
from moodle_watch.ledger import Ledger, natural_key
from tests.conftest import FauxCours


def test_idempotence(registre: Ledger, miroir):
    """Deux enregistrements du meme lot n'inscrivent rien la seconde fois.

    C'est LE test : il attrape toute instabilite de `item_key`. Un run rejoue ne
    doit ni dupliquer une observation ni deplacer une `first_seen`.
    """
    deltas = [miroir.ajouter(i, filename=f"f{i}.pdf") for i in range(1, 6)]
    run = registre.start_run("watch")

    assert registre.record(run, deltas, now=1000) == 5
    assert registre.record(run, deltas, now=2000) == 0

    vues = {r["file_id"]: r["first_seen"] for r in registre.con.execute("SELECT * FROM observation")}
    assert set(vues.values()) == {1000}, "une first_seen a bouge au second passage"


def test_fichier_modifie_redevient_a_traiter(registre: Ledger, miroir):
    """Un fichier qui change redevient du travail, meme deja traite.

    C'est la corvee que le projet supprime : plus besoin de comparer a la main ce
    qui a bouge depuis la derniere fois.
    """
    v1 = miroir.ajouter(1, filename="support.pdf", timemodified=1000)
    run = registre.start_run("watch")
    registre.record(run, [v1], now=1000)
    registre.mark_handled([1], action="ingested", by="nightly", note="[[Seance 1]]", now=1100)

    assert queries.backlog(registre) == [], "traite, donc hors du retard"

    # moodle-dl insere une ligne NEUVE et pointe old_file_id vers l'ancienne.
    v2 = miroir.ajouter(2, filename="support.pdf", timemodified=2000, old_file_id=1, modified=1)
    registre.record(registre.start_run("watch"), [v2], now=2000)

    retard = queries.backlog(registre)
    assert [x["file_id"] for x in retard] == [2]
    assert retard[0]["event"] == "changed"

    # et la revision suit la chaine : c'est le meme element, pas un nouveau.
    cles = {r["item_key"] for r in registre.con.execute("SELECT item_key FROM observation")}
    assert len(cles) == 1, "la revision 2 a recu une cle differente de la revision 1"

    h = queries.history(registre, cles.pop())
    assert len(h["revisions"]) == 2
    assert len(h["handlings"]) == 1


def test_disparition_n_efface_rien(registre: Ledger, miroir):
    """Rien n'est jamais supprime. Une disparition s'observe.

    Chez moodle-dl, `deleted` veut dire « Moodle ne l'offre plus », pas « le
    fichier local a disparu » : sur le miroir precedent, 4 des 5 lignes marquees
    supprimees avaient toujours leur fichier sur le disque.
    """
    d = miroir.ajouter(1)
    run = registre.start_run("watch")
    registre.record(run, [d], now=1000)

    parti = miroir.ajouter(2, old_file_id=1, deleted=1)
    registre.record(registre.start_run("watch"), [parti], now=2000)

    assert registre.con.execute("SELECT count(*) FROM observation").fetchone()[0] == 2
    assert queries.backlog(registre) == [], "un element disparu n'est pas du travail"


def test_cle_suit_un_deplacement(registre: Ledger, miroir):
    """Un fichier deplace reste le meme element, grace a `old_file_id`.

    Sans cette chaine, un deplacement se lirait comme une disparition suivie
    d'une apparition, et l'historique se couperait en deux.
    """
    v1 = miroir.ajouter(1, filepath="/", filename="x.pdf")
    registre.record(registre.start_run("watch"), [v1], now=1000)
    v2 = miroir.ajouter(2, filepath="/annexes/", filename="x.pdf", old_file_id=1, modified=1)
    registre.record(registre.start_run("watch"), [v2], now=2000)

    cles = {r["item_key"] for r in registre.con.execute("SELECT item_key FROM observation")}
    assert len(cles) == 1
    assert cles.pop() == natural_key(1631, 900, "/", "x.pdf"), "la cle d'origine doit primer"


def test_echeance_deplacee(registre: Ledger):
    """Un rendu repousse est detecte. Aucune API Moodle ne le signale."""
    ev = {"id": 77, "name": "Dossier de certification", "timesort": 1_767_826_740,
          "course": {"id": 1631}, "modulename": "assign", "instance": 5}

    assert [c.kind for c in registre.record_deadlines([ev], now=1000)] == ["deadline_set"]
    assert registre.record_deadlines([ev], now=2000) == [], "rien n'a bouge, rien ne doit sortir"

    repousse = dict(ev, timesort=1_768_431_540)
    (chg,) = registre.record_deadlines([repousse], now=3000)
    assert chg.kind == "deadline_moved"
    assert chg.previous == 1_767_826_740

    (disparu,) = registre.record_deadlines([], now=4000)
    assert disparu.kind == "deadline_gone"


def test_cours_inconnu_reste_en_attente(registre: Ledger):
    """Un cours decouvert n'est ni surveille ni ignore en silence."""
    registre.set_course_policy(FauxCours(id=48), "pending", by="policy", rule="default")
    assert registre.watched_ids() == []

    vu = queries.whats_new(registre, since="all")["pending_courses"]
    assert [c["course_id"] for c in vu] == [48]

    registre.set_course_policy(FauxCours(id=48), "watch", by="alice", rule="manuel")
    assert registre.watched_ids() == [48]
    assert queries.whats_new(registre, since="all")["pending_courses"] == []


def test_courses_dit_la_regle_qui_a_decide(registre: Ledger):
    """« Pourquoi ce cours ne remonte pas » doit toujours avoir une reponse."""
    registre.set_course_policy(FauxCours(id=1579, shortname="LING301-SA2025"), "ignore",
                               rule="shortname:*-2025")
    registre.set_course_policy(FauxCours(id=1631), "watch", rule="startdate_after:2026-08-01")

    visibles = queries.courses(registre)
    assert [c["course_id"] for c in visibles] == [1631]
    assert visibles[0]["rule"] == "startdate_after:2026-08-01"

    tous = queries.courses(registre, include_ignored=True)
    ignore = next(c for c in tous if c["course_id"] == 1579)
    assert ignore["state"] == "ignore" and ignore["rule"] == "shortname:*-2025"


@pytest.mark.parametrize(
    "texte, attendu",
    [("all", 0), ("0", 0), (1_700_000_000, 1_700_000_000), ("2026-09-01", 1_788_220_800)],
)
def test_parse_since(registre: Ledger, texte, attendu):
    assert queries.parse_since(registre, texte) == attendu


def test_parse_since_relatif_et_dernier_run(registre: Ledger):
    import time

    assert abs(queries.parse_since(registre, "7d") - (time.time() - 7 * 86400)) < 2
    assert queries.parse_since(registre, "last_run") == 0, "jamais tourne : on montre tout"

    run = registre.start_run("watch")
    registre.finish_run(run)
    assert queries.parse_since(registre, "last_run") > 0

    with pytest.raises(ValueError):
        queries.parse_since(registre, "la semaine derniere")


def test_recherche_ignore_les_accents(registre: Ledger, miroir):
    """On doit retrouver « Évaluation » en tapant « evaluation ».

    Les titres de cours sont souvent accentues et personne ne tape le bon accent du premier
    coup. C'est `remove_diacritics 2` du schema qui le permet.
    """
    d1 = miroir.ajouter(1, filename="Évaluation formative.pdf")
    d2 = miroir.ajouter(2, filename="Programme.pdf")
    registre.record(registre.start_run("watch"), [d1, d2], now=1)
    registre.rebuild_fts()

    assert registre.con.execute("SELECT count(*) FROM item_fts").fetchone()[0] == 2
    trouves = queries.search(registre, "evaluation")
    assert [x["file_id"] for x in trouves] == [1]
    assert [x["file_id"] for x in queries.search(registre, "Évaluation")] == [1]


def test_attachement_tardif_du_miroir(tmp_path):
    """Le miroir n'existe pas encore quand le registre s'ouvre.

    C'est le cas normal d'un premier run : moodle-dl cree `moodle_state.db`
    pendant la collecte. Sans `ensure_attached()`, tout ce qui depend du miroir
    echouait sans bruit, et `rebuild_fts()` rendait 0 sur 174 elements reels.
    """
    from tests.conftest import Miroir

    etat = tmp_path / "moodle_state.db"
    with Ledger(tmp_path / "w.db", etat) as l:
        assert not l.attached, "rien a attacher, le fichier n'existe pas"
        assert l.rebuild_fts() == 0

        miroir = Miroir(etat)
        d = miroir.ajouter(1, filename="Programme.pdf")

        assert l.ensure_attached() is True
        l.record(l.start_run("watch"), [d], now=1000)
        assert l.rebuild_fts() == 1
        assert l.ensure_attached() is True, "un second appel ne doit pas rattacher"
