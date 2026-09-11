"""L'extraction de texte, hors ligne."""

from __future__ import annotations

import urllib.parse

import pytest

from moodle_watch.documents import _pages, extract_text, url_avec_jeton


def _param(url: str, cle: str) -> list[str]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get(cle, [])


def test_le_jeton_est_ajoute_sans_ecraser_les_autres_parametres():
    url = url_avec_jeton(
        "https://m.example/webservice/pluginfile.php/1/mod_resource/content/1/x.pdf?forcedownload=1",
        "SECRET",
    )
    assert _param(url, "token") == ["SECRET"]
    assert _param(url, "forcedownload") == ["1"]


def test_un_jeton_deja_present_est_remplace_et_non_duplique():
    """Sinon Moodle recoit deux valeurs et en choisit une au hasard."""
    url = url_avec_jeton("https://m.example/pluginfile.php/x.pdf?token=VIEUX", "NEUF")
    assert _param(url, "token") == ["NEUF"]


@pytest.mark.parametrize("spec, total, attendu", [
    (None, 5, [0, 1, 2, 3, 4]),
    ("3", 5, [2]),
    ("1-3", 5, [0, 1, 2]),
    ("4-99", 5, [3, 4]),     # borne au nombre de pages reel
    ("0", 5, [0]),           # une page 0 n'existe pas, on prend la premiere
])
def test_selection_de_pages(spec, total, attendu):
    assert list(_pages(spec, total)) == attendu


def test_selection_de_pages_invalide():
    with pytest.raises(ValueError, match="incomprehensible"):
        _pages("les trois premieres", 10)


def _ligne(tmp_path, nom: str, contenu: bytes | None = None) -> dict:
    chemin = tmp_path / nom
    if contenu is not None:
        chemin.write_bytes(contenu)
    return {
        "file_id": 1, "content_filename": nom, "module_modname": "resource",
        "content_filesize": len(contenu or b""), "course_id": 1631,
        "module_name": "Support", "saved_to": str(chemin), "content_fileurl": "",
    }


def test_lecture_locale_d_un_pdf(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for texte in ("Dimension langagiere", "Evaluation formative"):
        page = doc.new_page()
        page.insert_text((72, 72), texte)
    donnees = doc.tobytes()
    doc.close()

    r = extract_text(_ligne(tmp_path, "cours.pdf", donnees), engine=None)
    assert r["status"] == "ok" and r["source"] == "local"
    assert r["page_count"] == 2
    assert "Dimension langagiere" in r["text"]

    une = extract_text(_ligne(tmp_path, "cours.pdf", donnees), engine=None, pages="2")
    assert "Evaluation" in une["text"] and "Dimension" not in une["text"]


def test_html_est_debarrasse_de_son_balisage(tmp_path):
    html = b"<html><head><style>p{color:red}</style></head><body><p>Seance <b>3</b></p>" \
           b"<script>alert(1)</script></body></html>"
    r = extract_text(_ligne(tmp_path, "page.html", html), engine=None)
    assert r["status"] == "ok"
    assert "Seance" in r["text"] and "3" in r["text"]
    assert "color:red" not in r["text"] and "alert" not in r["text"]


def test_une_video_ne_se_resume_pas_ici(tmp_path):
    r = extract_text(_ligne(tmp_path, "seance.mp4", b"\x00\x00"), engine=None)
    assert r["status"] == "not_text"
    assert "text" not in r


def test_texte_tronque_le_dit(tmp_path):
    r = extract_text(_ligne(tmp_path, "notes.md", b"a" * 5000), engine=None, max_chars=100)
    assert r["truncated"] is True and len(r["text"]) == 100 and r["chars"] == 5000


def test_sans_copie_locale_ni_url(tmp_path):
    ligne = _ligne(tmp_path, "absent.pdf")
    ligne["saved_to"] = str(tmp_path / "nexiste-pas.pdf")
    assert extract_text(ligne, engine=None)["status"] == "no_source"
