"""Le diagnostic, du plus profond au plus expose.

Quatre etages, et chacun se teste depuis l'endroit qui peut legitimement
l'atteindre. Un delai d'attente depuis la mauvaise machine n'est pas une panne,
c'est le pare-feu qui fonctionne : c'est la lecon du pont SOO, et elle est
reprise telle quelle.

Aucune sortie ne contient le segment secret de l'URL. `_masquer` le retire avant
toute impression, parce que ce segment **est** le mot de passe du montage et
qu'un diagnostic finit toujours par etre copie-colle quelque part.
"""

from __future__ import annotations

import os
import re
import sys
import time
import urllib.request
from typing import Any

VERT, ROUGE, GRIS, FIN = "\033[32m", "\033[31m", "\033[90m", "\033[0m"
_URL = re.compile(r"https?://[^\s'\"]+")
_SEGMENT = re.compile(r"/[0-9a-zA-Z_-]{16,}")

ECHECS: list[str] = []


def _masquer(texte: str) -> str:
    """Retire le segment secret des URL, et seulement des URL.

    Masquer partout cachait aussi des chemins de fichiers, ce qui rendait le
    diagnostic illisible. Le secret ne vit que dans une URL : c'est la qu'on
    coupe, en gardant l'hote visible pour que le diagnostic reste utile.
    """
    def _une_url(m: re.Match[str]) -> str:
        scheme, _, reste = m.group(0).partition("://")
        hote, barre, chemin = reste.partition("/")
        return f"{scheme}://{hote}{barre}{_SEGMENT.sub('/[SEGMENT]', '/' + chemin)[1:]}"

    return _URL.sub(_une_url, texte)


def dire(ok: bool, etage: str, detail: str) -> None:
    marque = f"{VERT}ok{FIN}" if ok else f"{ROUGE}ECHEC{FIN}"
    print(f"  {marque}  {etage} {GRIS}{_masquer(detail)}{FIN}")
    if not ok:
        ECHECS.append(etage)


def etage_1_registre(cfg: Any) -> None:
    """Le registre s'ouvre, le miroir s'attache, un run recent a abouti."""
    from .engine import Engine
    from .ledger import Ledger

    print(f"\n{GRIS}Etage 1 · le registre et le miroir, sur le disque{FIN}")
    try:
        with Ledger(cfg.db, Engine(cfg.mirror).state_db) as l:
            dire(True, "registre", f"{cfg.db}")
            attache = l.ensure_attached()
            n = l.con.execute("SELECT count(*) FROM observation").fetchone()[0] if attache else 0
            dire(attache, "miroir attache", f"{cfg.mirror} · {n} observations")

            run = l.last_run()
            if run is None:
                dire(False, "dernier run", "aucun run reussi ; lancer `moodle-watch run`")
            else:
                age = (time.time() - run["finished"]) / 3600
                dire(age < 24, "dernier run", f"il y a {age:.1f} h · {run['events']} evenements")

            attente = l.con.execute(
                "SELECT count(*) FROM course_policy WHERE state = 'pending'"
            ).fetchone()[0]
            if attente:
                print(f"  {GRIS}     {attente} cours attendent un arbitrage (ce n'est pas une panne){FIN}")
    except Exception as err:  # noqa: BLE001
        dire(False, "registre", f"{type(err).__name__} {err}")


def etage_2_amont(cfg: Any) -> None:
    """Les points d'appel de moodle-dl existent, et le jeton repond."""
    from .engine import PINNED_VERSION, Engine, TokenExpired, check_call_sites

    print(f"\n{GRIS}Etage 2 · moodle-dl et le jeton{FIN}")
    manquants = check_call_sites()
    dire(
        not manquants,
        "points d'appel",
        "intacts" if not manquants else f"disparus : {manquants} (version epinglee {PINNED_VERSION})",
    )
    try:
        info = Engine(cfg.mirror).site_info()
        dire(True, "jeton Moodle", f"{info.get('sitename', '?')} · {info.get('release')} · userid {info.get('userid')}")
    except TokenExpired as err:
        dire(False, "jeton Moodle", f"expire · {err}")
    except Exception as err:  # noqa: BLE001
        dire(False, "jeton Moodle", f"{type(err).__name__} {err}")


def etage_3_serveur(cfg: Any) -> None:
    """Le serveur repond sur la boucle locale, et ses reponses ont du sens.

    Ce n'est pas un controle de vie : on ouvre une vraie session MCP et on
    verifie que les outils rendent des donnees coherentes entre elles. Un serveur
    qui repond 200 en rendant des cours vides n'est pas un serveur qui marche.
    """
    import asyncio
    import json

    print(f"\n{GRIS}Etage 3 · le serveur MCP, sur la boucle locale{FIN}")
    url = f"http://127.0.0.1:{cfg.port}/mcp"

    async def exercer() -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        async with create_mcp_http_client() as http:
            async with streamable_http_client(url, http_client=http) as (lire, ecrire):
                async with ClientSession(lire, ecrire) as s:
                    init = await s.initialize()
                    outils = (await s.list_tools()).tools
                    dire(len(outils) >= 10, "session", f"{init.server_info.name} · {len(outils)} outils")

                    cours = json.loads((await s.call_tool("courses", {})).content[0].text)
                    surveilles = [c for c in cours if c["state"] == "watch"]
                    dire(isinstance(cours, list) and len(surveilles) >= 1, "courses",
                         f"{len(surveilles)} surveilles, {len(cours) - len(surveilles)} a arbitrer")

                    neuf = json.loads(
                        (await s.call_tool("whats_new", {"since": "all", "limit": 5})).content[0].text
                    )
                    items = [i for c in neuf["courses"] for i in c["items"]]
                    dire(
                        all(i.get("item_key") and i.get("filename") for i in items),
                        "whats_new",
                        f"{len(items)} elements, tous avec cle et nom",
                    )

    try:
        asyncio.run(exercer())
    except Exception as err:  # noqa: BLE001
        dire(False, "session", f"{type(err).__name__} {_masquer(str(err))} · {url}")


def etage_4_porte(cfg: Any) -> None:
    """La porte publique, depuis une machine exterieure.

    Aucun en-tete n'est pose ici : c'est le proxy qui les injecte. Si ca passe
    avec des en-tetes poses a la main, le montage n'est pas celui qu'on croit.
    """
    print(f"\n{GRIS}Etage 4 · la porte publique{FIN}")
    base = os.environ.get("MOODLE_WATCH_BASE_URL")
    segment = os.environ.get("MOODLE_WATCH_SEGMENT")
    if not (base and segment):
        print(f"  {GRIS}     MOODLE_WATCH_BASE_URL ou MOODLE_WATCH_SEGMENT absent, etage ignore{FIN}")
        return
    url = f"{base.rstrip('/')}/{segment}/mcp"
    try:
        requete = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(requete, timeout=15) as rep:
            code = rep.status
    except urllib.error.HTTPError as err:
        code = err.code
    except Exception as err:  # noqa: BLE001
        dire(False, "porte publique", f"{type(err).__name__} {_masquer(str(err))}")
        return
    # 405 ou 406 sur un GET est la bonne reponse d'un point de terminaison MCP :
    # il attend un POST. Un 404 signale un segment faux ou un hote proxy absent,
    # un 403 une cle desaccordee, un 421 un hote non declare cote serveur.
    dire(code in (200, 405, 406), "porte publique", f"HTTP {code}")


def doctor(cfg: Any) -> int:
    ECHECS.clear()
    print(f"{GRIS}moodle-watch · diagnostic{FIN}")
    etage_1_registre(cfg)
    etage_2_amont(cfg)
    if "--no-server" not in sys.argv:
        etage_3_serveur(cfg)
    if "--remote" in sys.argv:
        etage_4_porte(cfg)
    print()
    if ECHECS:
        print(f"{ROUGE}{len(ECHECS)} etage(s) en echec : {', '.join(ECHECS)}{FIN}")
        return 1
    print(f"{VERT}tous les etages repondent{FIN}")
    return 0
