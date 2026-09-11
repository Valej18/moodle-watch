"""Un cycle de veille, de bout en bout.

L'ordre des etapes n'est pas indifferent. On enregistre **avant** de consommer :
`changes_to_notify()` de moodle-dl reste relisible tant que `notified()` n'a pas
ete appele, donc un run interrompu apres l'ecriture du registre mais avant la
confirmation se rattrape tout seul au run suivant. Rien ne se perd.

Ne pas appeler depuis une boucle asyncio active : `Engine.collect()` fait son
propre `asyncio.run()`. Le serveur MCP passe donc par un sous-processus.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .config import Config
from .engine import Engine, EngineError, TokenExpired
from .ledger import Ledger
from .policy import Politique, appliquer, ecrire_config_moodle_dl


def run_once(cfg: Config, *, download: bool = False, by: str = "policy") -> dict[str, Any]:
    """Classe, collecte, enregistre, confirme. Rend un compte rendu."""
    debut = time.time()
    moteur = Engine(cfg.mirror)
    politique = Politique.charger(cfg.policy)
    mode = "fetch" if download else "watch"

    with Ledger(cfg.db, moteur.state_db) as registre:
        run_id = registre.start_run(mode)
        try:
            cours = moteur.enrolled_courses()
            classement = appliquer(politique, registre, cours, by=by)
            ecrire_config_moodle_dl(politique, moteur.config_path, classement["watch"])

            deltas = moteur.collect(download=download)
            # Sur un miroir neuf, moodle_state.db vient seulement d'etre cree.
            registre.ensure_attached()
            gardes = [d for d in deltas if not politique.exclut_fichier(d.content_filename)]
            ecartes = len(deltas) - len(gardes)

            inscrits = registre.record(run_id, gardes)
            echeances = registre.record_deadlines(moteur.deadlines(int(time.time()) - 30 * 86400))
            indexes = registre.rebuild_fts()

            # Et seulement maintenant : le registre est ecrit, on peut consommer.
            confirmes = moteur.confirm()

            registre.finish_run(run_id, courses=len(classement["watch"]), events=inscrits)
            return {
                "run_id": run_id,
                "mode": mode,
                "ok": True,
                "courses_watched": len(classement["watch"]),
                "courses_pending": classement["pending"],
                "deltas_seen": len(deltas),
                "deltas_recorded": inscrits,
                "deltas_excluded_by_name": ecartes,
                "deadline_changes": [c.kind for c in echeances],
                "indexed": indexes,
                "confirmed": confirmes,
                "failed_downloads": len(getattr(moteur, "failed", [])),
                "seconds": round(time.time() - debut, 1),
            }
        except TokenExpired as err:
            # Le mode de panne attendu, nomme a part pour que l'agent le dise au
            # lieu de deviner, et que le timer envoie la bonne commande.
            registre.finish_run(run_id, error=f"token_expired: {err}")
            raise
        except (EngineError, Exception) as err:  # noqa: BLE001
            registre.finish_run(run_id, error=f"{type(err).__name__}: {err}")
            raise
