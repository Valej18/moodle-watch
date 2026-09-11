"""Le pont vers moodle-dl.

**C'est le seul module du projet qui importe `moodle_dl`.** Toute la fragilite de
l'amont est confinee ici : moodle-dl est une application en ligne de commande, ses
classes sont internes et n'offrent aucune garantie de stabilite. La version est
epinglee dans `pyproject.toml`, et `check_call_sites()` verifie que les points
d'appel existent toujours. Si l'amont casse un jour, ce fichier est le seul a
changer.

Le partage du travail, qui est la raison d'etre du projet :

    moodle-dl repond a « qu'est-ce qui existe, qu'est-ce qui a change »
    moodle-watch repond a « depuis quand, et qu'est-ce qu'on en a fait »

moodle-dl n'ecrit `time_stamp` que sur ses chemins de modification ; une insertion
neuve laisse le defaut a zero. Verifie sur une base fraiche : la somme des 95
`time_stamp` vaut 0. Il n'existe donc chez lui aucune trace de quand un fichier a
ete vu pour la premiere fois. C'est l'horloge que nous fournissons.

Le protocole de collecte, et pourquoi il est sur
------------------------------------------------
`changes_to_notify()` reste relisible tant que `notified()` n'a pas ete appele
(verifie : deux cycles successifs sans consommer rendent les memes 95 fichiers).
On enregistre donc d'abord dans notre registre, on consomme ensuite. Si
l'enregistrement echoue, les deltas restent en attente et le run suivant les
reprend. Rien ne se perd.

Contrainte d'execution
----------------------
`MoodleService.fetch_state()` appelle `asyncio.run()` en interne. `collect()` ne
peut donc pas etre appele depuis une boucle evenementielle deja active, ce qui est
le cas du serveur MCP. C'est la raison pour laquelle un run vit dans un
sous-processus (voir `cli.py` et l'outil `sync`).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable

from moodle_dl.config import ConfigHelper
from moodle_dl.database import StateRecorder
from moodle_dl.downloader.download_service import DownloadService
from moodle_dl.downloader.fake_download_service import FakeDownloadService
from moodle_dl.moodle.moodle_service import MoodleService
from moodle_dl.moodle.request_helper import RequestHelper
from moodle_dl.types import MoodleDlOpts
from moodle_dl.utils import PathTools, ProcessLock

PINNED_VERSION = "2.3.13"

#: Les points d'appel dont depend tout le projet. `doctor` les verifie un a un,
#: parce qu'une montee de version silencieuse de moodle-dl doit echouer avec un
#: message clair plutot qu'avec un AttributeError au milieu d'un run nocturne.
CALL_SITES: tuple[tuple[str, type | None, str], ...] = (
    ("ConfigHelper.load", ConfigHelper, "load"),
    ("ConfigHelper.get_token", ConfigHelper, "get_token"),
    ("MoodleService.fetch_state", MoodleService, "fetch_state"),
    ("StateRecorder.changes_to_notify", StateRecorder, "changes_to_notify"),
    ("StateRecorder.notified", StateRecorder, "notified"),
    ("DownloadService.run", DownloadService, "run"),
    ("FakeDownloadService.run", FakeDownloadService, "run"),
    ("RequestHelper.post", RequestHelper, "post"),
)


class EngineError(Exception):
    """Toute panne du pont vers moodle-dl."""


class TokenExpired(EngineError):
    """Le jeton Moodle n'est plus valide.

    Traite a part parce que c'est le mode de panne attendu : un jeton delivre par SSO est
    delivre par SSO et se renouvelle a la main. Un serveur qui meurt en silence
    la-dessus est un piege ; celui-ci doit le dire.
    """


class UpstreamChanged(EngineError):
    """Un point d'appel de moodle-dl a disparu. Voir CALL_SITES."""


class AlreadyRunning(EngineError):
    """Un autre run tient deja le verrou de ce miroir."""


@dataclass(frozen=True, slots=True)
class Delta:
    """Un changement observe, aplati depuis les objets de moodle-dl.

    `file_id` est celui de moodle-dl. Point de conception heureux : quand un
    fichier change, moodle-dl **insere une ligne neuve** et pointe `old_file_id`
    vers l'ancienne. Son `file_id` est donc deja un numero de revision, et le
    registre s'y accroche au lieu d'en inventer un.
    """

    file_id: int
    course_id: int
    course_fullname: str
    section_name: str
    module_id: int
    module_name: str
    module_modname: str
    content_filepath: str
    content_filename: str
    content_fileurl: str
    content_filesize: int
    content_timemodified: int
    content_type: str
    saved_to: str
    modified: bool
    moved: bool
    deleted: bool

    @property
    def event(self) -> str:
        """`appeared`, `changed` ou `vanished`.

        Attention : chez moodle-dl `deleted` veut dire « Moodle ne l'offre plus »,
        pas « le fichier local a disparu ». Sur le miroir precedent, 4 des 5 lignes
        marquees supprimees avaient toujours leur fichier sur le disque.
        """
        if self.deleted:
            return "vanished"
        if self.modified or self.moved:
            return "changed"
        return "appeared"


@dataclass(frozen=True, slots=True)
class CourseInfo:
    id: int
    shortname: str
    fullname: str
    startdate: int
    enddate: int


_OPT_DEFAULTS: dict[str, Any] = {
    "init": False,
    "config": False,
    "new_token": False,
    "change_notification_mail": False,
    "change_notification_telegram": False,
    "change_notification_discord": False,
    "change_notification_ntfy": False,
    "change_notification_xmpp": False,
    "manage_database": False,
    "delete_old_files": False,
    "log_responses": False,
    "add_all_visible_courses": False,
    "sso": False,
    "username": None,
    "password": None,
    "token": None,
    "max_parallel_api_calls": 10,
    "max_parallel_downloads": 5,
    "max_parallel_yt_dlp": 5,
    "download_chunk_size": 102400,
    "ignore_ytdl_errors": True,
    "without_downloading_files": True,
    "max_path_length_workaround": False,
    "allow_insecure_ssl": False,
    "use_all_ciphers": False,
    "skip_cert_verify": False,
    "verbose": False,
    "quiet": True,
    "log_to_file": False,
    "log_file_path": None,
}


def build_opts(mirror: Path, *, download: bool = False, **overrides: Any) -> MoodleDlOpts:
    """Construit un `MoodleDlOpts` sans passer par l'analyseur d'arguments.

    `MoodleDlOpts` est une dataclass a 33 champs tous obligatoires, normalement
    remplie par argparse. On la remplit nous-memes, et on verifie que la liste des
    champs n'a pas bouge : c'est le neuvieme point d'appel, celui qui casserait le
    plus silencieusement.
    """
    voulus = dict(_OPT_DEFAULTS, path=str(mirror), without_downloading_files=not download)
    voulus.update(overrides)
    connus = {f.name for f in fields(MoodleDlOpts)}
    manquants = connus - set(voulus)
    surplus = set(voulus) - connus
    if manquants or surplus:
        raise UpstreamChanged(
            f"MoodleDlOpts a change de forme : champs non fournis={sorted(manquants)}, "
            f"champs inconnus={sorted(surplus)}. Verifier la version de moodle-dl "
            f"(epinglee a {PINNED_VERSION})."
        )
    return MoodleDlOpts(**voulus)


def check_call_sites() -> list[str]:
    """Rend la liste des points d'appel manquants. Vide = l'amont est intact."""
    manquants = [nom for nom, cls, attr in CALL_SITES if not hasattr(cls, attr)]
    try:
        build_opts(Path("/nonexistent"))
    except UpstreamChanged as err:
        manquants.append(str(err))
    return manquants


def _is_token_error(err: BaseException) -> bool:
    texte = str(err).lower()
    return "invalidtoken" in texte or "invalid token" in texte or "accessexception" in texte


class Engine:
    """Pilote moodle-dl sur un dossier miroir.

    Un miroir est un dossier au sens de `moodle-dl --path` : il contient
    `config.json`, `moodle_state.db` et l'arborescence telechargee. moodle-watch
    n'en cree pas de nouveau format, il se pose sur celui-la.
    """

    def __init__(self, mirror: str | Path):
        self.mirror = Path(mirror)
        self._opts: MoodleDlOpts | None = None
        self._config: ConfigHelper | None = None
        self._pending: list[Any] | None = None
        self._recorder: StateRecorder | None = None

    # ------------------------------------------------------------------ config

    @property
    def config_path(self) -> Path:
        return self.mirror / "config.json"

    @property
    def state_db(self) -> Path:
        return self.mirror / "moodle_state.db"

    def _load(self, *, download: bool = False) -> tuple[ConfigHelper, MoodleDlOpts]:
        opts = build_opts(self.mirror, download=download)
        config = ConfigHelper(opts)
        if not config.is_present():
            raise EngineError(
                f"Aucun config.json dans {self.mirror}. Creer le miroir avec "
                f"`moodle-dl --init --sso --path '{self.mirror}'`."
            )
        config.load()
        # L'etat global que run_main pose lui-meme, avec un « TODO: Change this »
        # dans le source de moodle-dl. Sans lui, les noms de fichiers ne sont pas
        # assainis de la meme facon, et les chemins calcules divergent du miroir.
        PathTools.restricted_filenames = config.get_restricted_filenames()
        self._config, self._opts = config, opts
        return config, opts

    def raw_config(self) -> dict[str, Any]:
        """Le `config.json` de moodle-dl, tel quel. `policy.py` y ecrit."""
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    # --------------------------------------------------------------------- api

    def _request(self) -> RequestHelper:
        config, opts = self._load()
        return RequestHelper(config, opts, config.get_moodle_URL(), config.get_token())

    def api(self, wsfunction: str, **params: Any) -> Any:
        """Un appel brut a l'API Moodle, en lecture seule.

        On reutilise le client de moodle-dl plutot que d'en embarquer un second :
        il porte deja les reessais, l'entete mobile et la detection des erreurs
        Moodle, qui ne sont pas des codes HTTP.
        """
        if wsfunction not in READ_ONLY_FUNCTIONS:
            raise EngineError(
                f"{wsfunction} n'est pas dans la liste blanche. moodle-watch ne pose "
                f"a Moodle que des questions ; aucune ecriture n'est joignable."
            )
        try:
            return self._request().post(wsfunction, {k: v for k, v in params.items() if v is not None})
        except Exception as err:  # noqa: BLE001 - on veut le diagnostic, pas la trace
            if _is_token_error(err):
                raise TokenExpired(
                    f"Le jeton de {self.mirror} n'est plus valide. Le renouveler par "
                    f"`moodle-dl -nt -sso --path '{self.mirror}'`."
                ) from err
            raise EngineError(f"{wsfunction} : {type(err).__name__} {err}") from err

    def site_info(self) -> dict[str, Any]:
        return self.api("core_webservice_get_site_info")

    def enrolled_courses(self) -> list[CourseInfo]:
        """Tous les cours ou l'utilisateur est inscrit, surveilles ou non.

        C'est ce qui fait qu'un cours nouvellement ouvert apparait tout seul, au
        lieu d'attendre qu'on pense a l'ajouter a une liste blanche.
        """
        userid = self.site_info()["userid"]
        brut = self.api("core_enrol_get_users_courses", userid=userid)
        return [
            CourseInfo(
                id=c["id"],
                shortname=c.get("shortname", ""),
                fullname=c.get("fullname", ""),
                startdate=c.get("startdate", 0) or 0,
                enddate=c.get("enddate", 0) or 0,
            )
            for c in brut
        ]

    def deadlines(self, since: int, limit: int = 50) -> list[dict[str, Any]]:
        rep = self.api(
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=since,
            limitnum=limit,
        )
        return rep.get("events", [])

    # ---------------------------------------------------------------- collecte

    def collect(self, *, download: bool = False) -> list[Delta]:
        """Un cycle de veille. Rend les deltas **sans les consommer**.

        Appeler `confirm()` ensuite, et seulement une fois qu'ils sont enregistres.
        En mode `download=False`, `FakeDownloadService` marque les fichiers comme
        acquis sans rien telecharger : veille seule et aspiration complete sont le
        meme code avec un objet different.

        Ne pas appeler depuis une boucle asyncio active : `fetch_state` fait son
        propre `asyncio.run()`.
        """
        config, opts = self._load(download=download)
        verrou = config.get_misc_files_path()
        try:
            ProcessLock.lock(verrou)
        except ProcessLock.LockError as err:
            raise AlreadyRunning(str(err)) from err

        try:
            moodle = MoodleService(config, opts)
            recorder = StateRecorder(config, opts)
            try:
                changed = asyncio.run(moodle.fetch_state(recorder))
            except Exception as err:  # noqa: BLE001
                if _is_token_error(err):
                    raise TokenExpired(
                        f"Le jeton de {self.mirror} n'est plus valide. Le renouveler par "
                        f"`moodle-dl -nt -sso --path '{self.mirror}'`."
                    ) from err
                raise EngineError(f"fetch_state : {type(err).__name__} {err}") from err

            service = DownloadService if download else FakeDownloadService
            downloader = service(changed, config, opts, recorder)
            downloader.run()
            self.failed = list(downloader.get_failed_tasks())

            self._pending = recorder.changes_to_notify()
            self._recorder = recorder
            return list(_flatten(self._pending))
        finally:
            ProcessLock.unlock(verrou)

    def confirm(self) -> int:
        """Consomme les deltas rendus par le dernier `collect()`.

        A n'appeler qu'une fois le registre ecrit : c'est ce qui rend un run
        interrompu rattrapable. Leve le drapeau `notified` de moodle-dl, apres quoi
        la distinction vu / pas vu est perdue de son cote, mais conservee du notre.
        """
        if self._recorder is None or self._pending is None:
            raise EngineError("confirm() sans collect() prealable.")
        n = sum(len(c.files) for c in self._pending)
        if self._pending:
            self._recorder.notified(self._pending)
        self._pending = None
        return n


def _flatten(courses: Iterable[Any]) -> Iterable[Delta]:
    for course in courses:
        for f in course.files:
            yield Delta(
                file_id=f.file_id,
                course_id=course.id,
                course_fullname=course.fullname,
                section_name=f.section_name,
                module_id=f.module_id,
                module_name=f.module_name,
                module_modname=f.module_modname,
                content_filepath=f.content_filepath,
                content_filename=f.content_filename,
                content_fileurl=f.content_fileurl,
                content_filesize=f.content_filesize or 0,
                content_timemodified=f.content_timemodified or 0,
                content_type=f.content_type,
                saved_to=f.saved_to or "",
                modified=bool(f.modified),
                moved=bool(f.moved),
                deleted=bool(f.deleted),
            )


#: La garantie est dans le code, pas dans la prose : aucune fonction d'ecriture
#: n'est joignable depuis `api()`. `mod_assign_save_grade`, `submit_assignment` et
#: `core_calendar_create_calendar_events` sont hors d'atteinte par construction.
READ_ONLY_FUNCTIONS = frozenset(
    {
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_course_get_contents",
        "core_course_get_courses_by_field",
        "core_course_get_recent_courses",
        "core_course_get_updates_since",
        "core_calendar_get_action_events_by_timesort",
        "core_calendar_get_action_events_by_courses",
        "message_popup_get_popup_notifications",
        "core_message_get_unread_notification_count",
        "gradereport_user_get_grade_items",
        "mod_assign_get_assignments",
        "mod_assign_get_submission_status",
    }
)
