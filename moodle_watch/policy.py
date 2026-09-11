"""Qui est surveille, et pourquoi.

moodle-dl sait deja filtrer par cours, par section, par extension et par taille.
Ce module ne refait pas ce travail : il **ecrit son `config.json`**, pour qu'il n'y
ait qu'une seule source de verite. Il ajoute les deux choses qui lui manquent.

1. La classification automatique. Une liste blanche d'identifiants numeriques ne
   sait pas dire « cette annee seulement ». Une regle sur `startdate` le sait, et
   elle continue de valoir quand les cours changent.
2. Le refus de decider en silence. Un cours decouvert entre en `pending`, il
   n'est ni surveille ni ignore. Il remonte dans `whats_new` jusqu'a ce qu'on
   tranche. C'est la mecanique des cases a cocher de la note du jour, appliquee
   aux inscriptions.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAUT = """\
# Politique de surveillance de moodle-watch.
# Les regles sont evaluees dans l'ordre, la premiere qui correspond gagne, comme
# un pare-feu. Ce qui ne correspond a aucune regle prend `default`.

default = "ask"          # ask | watch | ignore

# [[rule]]
# match  = { startdate_after = "2026-08-01" }
# action = "watch"
#
# [[rule]]
# match  = { shortname = "*2025*" }    # et non "*-2025" : les codes melangent
# action = "ignore"                    # LING204-2025 et LING301-SA2025
#
# [[rule]]                 # un identifiant nomme gagne toujours, s'il vient avant
# match  = { course = 48 }
# action = "watch"

[exclude]
# Traduits dans le config.json de moodle-dl.
extensions  = []         # ex. ["mp4", "mkv"]
larger_than = ""         # ex. "500MB"
# Applique par moodle-watch, moodle-dl ne sait pas filtrer par nom.
filenames   = []         # ex. ["department-logo.png"]
"""

_TAILLE = re.compile(r"^\s*([\d.]+)\s*(o|b|k|ko|kb|m|mo|mb|g|go|gb)?\s*$", re.IGNORECASE)
_FACTEURS = {None: 1, "o": 1, "b": 1, "k": 1024, "ko": 1024, "kb": 1024,
             "m": 1024**2, "mo": 1024**2, "mb": 1024**2,
             "g": 1024**3, "go": 1024**3, "gb": 1024**3}


def parse_taille(texte: str | int | None) -> int:
    """« 500MB » vers un nombre d'octets. 0 veut dire « aucune limite »."""
    if not texte:
        return 0
    if isinstance(texte, int):
        return texte
    m = _TAILLE.match(texte)
    if not m:
        raise ValueError(f"taille incomprehensible : {texte!r}. Exemples : 500MB, 2GB, 1048576")
    return int(float(m.group(1)) * _FACTEURS[(m.group(2) or "").lower() or None])


def _epoch(valeur: Any) -> int:
    if isinstance(valeur, (int, float)):
        return int(valeur)
    if isinstance(valeur, datetime):
        return int(valeur.replace(tzinfo=valeur.tzinfo or timezone.utc).timestamp())
    return int(datetime.fromisoformat(str(valeur)).replace(tzinfo=timezone.utc).timestamp())


@dataclass(frozen=True, slots=True)
class Regle:
    critere: dict[str, Any]
    action: str

    def correspond(self, cours: Any) -> bool:
        for cle, attendu in self.critere.items():
            if cle == "course":
                ids = attendu if isinstance(attendu, list) else [attendu]
                if cours.id not in ids:
                    return False
            elif cle == "shortname":
                if not fnmatch.fnmatch(cours.shortname or "", str(attendu)):
                    return False
            elif cle == "fullname":
                if not fnmatch.fnmatch(cours.fullname or "", str(attendu)):
                    return False
            elif cle == "startdate_after":
                if not cours.startdate or cours.startdate < _epoch(attendu):
                    return False
            elif cle == "startdate_before":
                if not cours.startdate or cours.startdate >= _epoch(attendu):
                    return False
            else:
                raise ValueError(
                    f"critere inconnu : {cle!r}. Admis : course, shortname, fullname, "
                    f"startdate_after, startdate_before."
                )
        return bool(self.critere)

    @property
    def etiquette(self) -> str:
        return ",".join(f"{k}:{v}" for k, v in self.critere.items())


@dataclass(frozen=True, slots=True)
class Politique:
    defaut: str = "ask"
    regles: tuple[Regle, ...] = ()
    extensions: tuple[str, ...] = ()
    larger_than: int = 0
    filenames: tuple[str, ...] = ()

    @classmethod
    def charger(cls, chemin: str | Path) -> "Politique":
        p = Path(chemin)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(DEFAUT, encoding="utf-8")
        brut = tomllib.loads(p.read_text(encoding="utf-8"))
        defaut = brut.get("default", "ask")
        if defaut not in ("ask", "watch", "ignore"):
            raise ValueError(f"default doit valoir ask, watch ou ignore, pas {defaut!r}")
        regles = []
        for r in brut.get("rule", []):
            action = r.get("action")
            if action not in ("watch", "ignore"):
                raise ValueError(f"action de regle invalide : {action!r}. Admises : watch, ignore.")
            regles.append(Regle(dict(r.get("match", {})), action))
        ex = brut.get("exclude", {})
        return cls(
            defaut=defaut,
            regles=tuple(regles),
            extensions=tuple(ex.get("extensions", []) or ()),
            larger_than=parse_taille(ex.get("larger_than")),
            filenames=tuple(ex.get("filenames", []) or ()),
        )

    def classer(self, cours: Any) -> tuple[str, str]:
        """Rend (etat, regle) pour un cours. L'etat `pending` vient de `default = ask`."""
        for regle in self.regles:
            if regle.correspond(cours):
                return regle.action, regle.etiquette
        return ("pending" if self.defaut == "ask" else self.defaut), f"default:{self.defaut}"

    def exclut_fichier(self, nom: str) -> bool:
        return any(fnmatch.fnmatch(nom, motif) for motif in self.filenames)


def appliquer(politique: Politique, ledger: Any, cours: list[Any], *, by: str = "policy") -> dict[str, list[int]]:
    """Classe chaque cours et inscrit la decision au registre.

    Une decision prise par un humain n'est jamais ecrasee par une regle : c'est
    `decided_by` qui l'atteste. Sans cette garde, repondre a une question de
    `pending` ne servirait a rien, la regle reprendrait la main au run suivant.
    """
    connus = ledger.policies()
    resultat: dict[str, list[int]] = {"watch": [], "ignore": [], "pending": []}
    for c in cours:
        ancien = connus.get(c.id)
        if ancien is not None and ancien["decided_by"] != "policy":
            resultat[ancien["state"]].append(c.id)
            continue
        etat, regle = politique.classer(c)
        ledger.set_course_policy(c, etat, by=by, rule=regle)
        resultat[etat].append(c.id)
    return resultat


def ecrire_config_moodle_dl(politique: Politique, config_path: str | Path, watched: list[int]) -> dict[str, Any]:
    """Reporte la politique dans le `config.json` de moodle-dl.

    Ecriture chirurgicale : on ne touche que les trois cles concernees, jamais le
    jeton ni les options de telechargement. Le fichier est reecrit en 600, comme
    moodle-dl le fait lui-meme, parce qu'il contient le jeton en clair.
    """
    p = Path(config_path)
    conf = json.loads(p.read_text(encoding="utf-8"))
    conf["download_course_ids"] = sorted(watched)
    if politique.extensions:
        conf["exclude_file_extensions"] = list(politique.extensions)
    if politique.larger_than:
        conf["max_file_size"] = politique.larger_than
    p.write_text(json.dumps(conf, indent=4, ensure_ascii=False), encoding="utf-8")
    p.chmod(0o600)
    return conf
