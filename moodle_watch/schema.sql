-- Le registre de moodle-watch.
--
-- Il ne redit PAS ce que moodle-dl sait deja. `moodle_state.db` porte le « quoi »
-- (cours, section, module, nom, taille, URL, chemin local) et se joint en lecture
-- seule par ATTACH. Ce fichier ne porte que ce qui lui manque :
--
--   1. l'horloge      `first_seen` — moodle-dl laisse `time_stamp` a 0 sur toutes
--                     ses lignes, verifie sur 2210 anciennes et 95 fraiches.
--   2. le traitement  `handled` — qui a fait quoi de cet element, et a quelle
--                     revision.
--   3. la decision    `course_policy` — pourquoi ce cours est suivi ou non.
--
-- Rien n'est jamais efface. Une disparition s'observe, elle ne se supprime pas.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Une ligne par `file_id` de moodle-dl.
--
-- Point de conception heureux : quand un fichier change, moodle-dl **insere une
-- ligne neuve** et pointe `old_file_id` vers l'ancienne. Son `file_id` est donc
-- deja un numero de revision. On s'y accroche au lieu d'en inventer un.
CREATE TABLE IF NOT EXISTS observation (
    file_id     INTEGER PRIMARY KEY,     -- celui de moodle-dl, donc une revision
    item_key    TEXT    NOT NULL,        -- stable entre revisions d'un meme element
    course_id   INTEGER NOT NULL,
    first_seen  INTEGER NOT NULL,        -- NOTRE horloge, en secondes epoch
    event       TEXT    NOT NULL CHECK (event IN ('appeared', 'changed', 'vanished')),
    run_id      INTEGER NOT NULL REFERENCES run(id)
);
CREATE INDEX IF NOT EXISTS idx_obs_item   ON observation (item_key, file_id);
CREATE INDEX IF NOT EXISTS idx_obs_course ON observation (course_id, first_seen);
CREATE INDEX IF NOT EXISTS idx_obs_run    ON observation (run_id);

-- Ce qu'on a fait d'un element, par revision.
--
-- La cle porte `file_id` et non `item_key` : un fichier qui change recoit un
-- nouveau `file_id`, donc il **redevient non traite** sans qu'on ait a y penser.
-- « Verifier s'il y a eu des modifications » cesse d'etre une corvee et devient
-- une propriete du schema.
CREATE TABLE IF NOT EXISTS handled (
    item_key TEXT    NOT NULL,
    file_id  INTEGER NOT NULL,
    at       INTEGER NOT NULL,
    by       TEXT    NOT NULL,           -- texte libre : nightly, claude-code, alice
    action   TEXT    NOT NULL CHECK (action IN ('read', 'ingested', 'ignored', 'todo')),
    note     TEXT,                       -- ex. un lien vers la note produite
    PRIMARY KEY (item_key, file_id, action)
);
CREATE INDEX IF NOT EXISTS idx_handled_file ON handled (file_id);

CREATE TABLE IF NOT EXISTS run (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started  INTEGER NOT NULL,
    finished INTEGER,
    mode     TEXT    NOT NULL CHECK (mode IN ('watch', 'fetch')),
    courses  INTEGER NOT NULL DEFAULT 0,
    events   INTEGER NOT NULL DEFAULT 0,
    error    TEXT                        -- NULL = succes. Un run tue laisse finished NULL.
);

-- Pourquoi un cours est suivi, ou ne l'est pas.
--
-- `pending` est l'etat qui compte : un cours nouvellement decouvert n'est ni
-- surveille ni ignore en silence. Il attend un arbitrage, et sort dans whats_new.
CREATE TABLE IF NOT EXISTS course_policy (
    course_id  INTEGER PRIMARY KEY,
    shortname  TEXT,
    fullname   TEXT,
    startdate  INTEGER,
    enddate    INTEGER,
    state      TEXT    NOT NULL CHECK (state IN ('watch', 'ignore', 'pending')),
    decided_at INTEGER NOT NULL,
    decided_by TEXT    NOT NULL,         -- 'policy' ou un nom d'humain ou d'agent
    rule       TEXT                      -- la regle qui a tranche, pour pouvoir l'expliquer
);

-- Les echeances. `timesort_previous` est la raison d'etre de cette table :
-- un rendu deplace est ce qui fait vraiment mal a un etudiant, et aucune API ne
-- le signale. On le deduit en comparant a ce qu'on avait vu.
CREATE TABLE IF NOT EXISTS deadline (
    event_id          INTEGER PRIMARY KEY,   -- l'id de l'evenement de calendrier Moodle
    course_id         INTEGER,
    cmid              INTEGER,
    name              TEXT,
    modulename        TEXT,
    timesort          INTEGER NOT NULL,
    timesort_previous INTEGER,
    first_seen        INTEGER NOT NULL,
    last_seen         INTEGER NOT NULL,
    gone_at           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_deadline_sort ON deadline (timesort);

-- Recherche plein texte, reconstruite a chaque run depuis mirror.files.
-- `remove_diacritics 2` parce que les titres de cours sont souvent accentues et que
-- personne ne tape « dimension langagiere » avec le bon accent du premier coup.
CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5 (
    item_key UNINDEXED,
    course_id UNINDEXED,
    file_id UNINDEXED,
    haystack,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
