# Architecture

## The division of labour

```
moodle-dl      what exists, what changed     mods, LTI extractors, yt-dlp
moodle-watch   since when, what you did      the clock, the ledger, the MCP surface
```

moodle-dl is **imported, not reimplemented**. Its `moodle/mods/` covers assign,
book, calendar, data, folder, forum, lesson, page, quiz and workshop; its
`downloader/extractors/` covers echo360, opencast, kalvidres, helixmedia,
sharepoint, owncloud and googledrive; yt-dlp is already one of its dependencies.
Rewriting any of that would be years of work reproducing cases it already
handles.

What it does not do is remember time. `time_stamp` is written only on its
modification paths, so a fresh insert leaves the default: on a 2210-row mirror
and on a fresh 95-row one alike, every value is `0`. And its `notified` flag,
once raised, erases the seen / not-seen distinction for good.

That gap is the whole project.

## Layers

| Module | Rule |
|---|---|
| `engine.py` | **the only file that imports `moodle_dl`** |
| `ledger.py` | no MCP, no moodle-dl. Testable alone. |
| `queries.py` | no MCP, no moodle-dl. Read-only. Testable alone. |
| `policy.py` | decides, and writes moodle-dl's own `config.json` |
| `sync.py` | one cycle, in the order that makes it safe |
| `server.py` | tool surface only. No logic. |

`engine.py` is a firewall, not a convenience. moodle-dl's classes are internal
to a CLI application and carry no stability guarantee, so the version is pinned
and `doctor` asserts every call site still exists. If upstream moves, one file
changes.

## Why the cycle is ordered the way it is

```
classify courses → write moodle-dl config → collect → record → confirm
```

`changes_to_notify()` stays re-readable until `notified()` is called. Verified:
two consecutive cycles without consuming return the same 95 files; the cycle
after consuming returns zero.

So we **record into the ledger first and consume second**. A run killed between
the two is picked up whole by the next one. Nothing is lost, and nothing is
double-counted, because `record()` ignores a `file_id` it already holds and
never moves a `first_seen`.

## Why the ledger keys on `file_id`

moodle-dl inserts a **new row** when a file changes, pointing `old_file_id` at
the previous one. Its `file_id` is therefore already a revision number.

`handled` is keyed on `(item_key, file_id, action)`. Consequence: a file updated
after you read it returns to the backlog by itself, because the handling belongs
to the previous revision. That is the manual comparison this project removes.

`item_key` is `sha1(course|module|filepath|filename)`, except when moodle-dl
reports a move, where the `old_file_id` chain is followed so the history stays
in one piece. A plain rename does read as a disappearance plus an appearance;
following that would need content hashes, and moodle-dl computes those for only
382 rows in 2210.

Only the **latest** revision of an item counts as work. Without that predicate a
replaced file left its old revision in the backlog and a vanished file stayed
there forever.

## Nothing is ever deleted

A disappearance is an observation with `event = 'vanished'`, not a `DELETE`.
Note that moodle-dl's `deleted` means "Moodle no longer offers it", not "the
local file is gone": on a real mirror, four of five rows marked deleted still
had their file on disk.

## Deciding out loud

A newly discovered course enters `pending`. It is neither watched nor ignored,
and it surfaces in `whats_new` until somebody arbitrates. Every course carries
the `rule` that decided it, so "why does this course not show up" always has an
answer.

A decision recorded with a `decided_by` other than `policy` is never overwritten
by a rule. Without that, answering a pending course would be pointless.
