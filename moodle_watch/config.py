"""Le seul endroit qui lit l'environnement.

Rien d'autre dans le projet n'appelle `os.environ`. Un reglage a donc toujours un
seul endroit ou vivre, et `doctor` peut les afficher tous.

Aucun secret ne vit ici ni dans le depot : le jeton Moodle reste dans le
`config.json` du miroir, ou moodle-dl le met, et les secrets du montage HTTP
vivent dans un fichier d'environnement hors du depot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MOODLE_WATCH_CONFIG_DIR", "~/.config/moodle-watch")).expanduser()


def _liste(nom: str, defaut: str = "") -> tuple[str, ...]:
    brut = os.environ.get(nom, defaut)
    return tuple(x.strip() for x in brut.split(",") if x.strip())


@dataclass(frozen=True, slots=True)
class Config:
    mirror: Path
    db: Path
    policy: Path
    port: int
    hosts: tuple[str, ...]
    origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        miroir = os.environ.get("MOODLE_WATCH_MIRROR")
        if not miroir:
            raise SystemExit(
                "MOODLE_WATCH_MIRROR n'est pas defini. Il doit pointer le dossier "
                "moodle-dl (celui qui contient config.json et moodle_state.db).\n"
                "  export MOODLE_WATCH_MIRROR=~/Moodle"
            )
        m = Path(miroir).expanduser()
        port = int(os.environ.get("MOODLE_WATCH_PORT", "3041"))

        # Les variantes avec ET sans port sont obligatoires : le SDK MCP valide
        # l'en-tete Host contre cette liste (protection anti-DNS-rebinding), et
        # derriere un proxy cet en-tete porte le nom public. Sans les deux formes,
        # le proxy recoit un 421 et le diagnostic coute une soiree.
        nommes = _liste("MOODLE_WATCH_HOSTS")
        hotes = ["127.0.0.1", "localhost", *nommes]
        hotes += [f"{h}:{port}" for h in hotes]

        return cls(
            mirror=m,
            db=Path(os.environ.get("MOODLE_WATCH_DB", m / "moodle_watch.db")).expanduser(),
            policy=Path(os.environ.get("MOODLE_WATCH_POLICY", CONFIG_DIR / "policy.toml")).expanduser(),
            port=port,
            hosts=tuple(dict.fromkeys(hotes)),
            origins=tuple(f"https://{h}" for h in nommes),
        )
