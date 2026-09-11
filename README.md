# moodle-watch

**Give your AI agents a memory of your Moodle.**

Moodle can tell an agent what exists. It cannot tell it what is *new to you*,
what changed *since you last looked*, or what you have *already dealt with*.
Every Moodle MCP server out there is a stateless wrapper around the same API:
ask it twice, get the same answer twice, learn nothing.

moodle-watch keeps a ledger. That one difference changes what an agent can do.

---

## Why this matters if you work with agents

A stateless tool makes your agent a search box. You ask, it fetches, it forgets.
You are still the one who remembers what you already read, notices that a
lecturer silently re-uploaded a PDF, and keeps the mental list of what is left.
That bookkeeping is the actual work, and no amount of prompting removes it.

With a ledger, the agent holds all three:

**It can open with news instead of waiting for a question.** `whats_new` is a
delta, not a dump. "Since Tuesday: four new files in two courses, one deadline
moved a week earlier, and a course you have never triaged just appeared."

**It stops re-reading what it already read.** Every item carries its handling
state. A nightly agent can ask for the backlog, work through it, call
`mark_handled` with a link to whatever it produced, and pick up exactly where it
left off tomorrow. No "have I seen this already" heuristics, no re-summarising
the same lecture four times, no burned context.

**It notices the thing you would have missed.** A lecturer replaces a slide deck
two days before the exam. moodle-dl overwrites the file. Moodle sends nothing.
Here, the item gets a new revision, and because handling is recorded *per
revision*, it **returns to your backlog by itself**. You never diff anything by
hand again.

**A swarm can divide the work.** `mark_handled` takes a `by` field. Several
agents, or an agent and a human, can share one queue without stepping on each
other, and `history` shows who did what to which revision.

**It never decides behind your back.** A newly discovered course lands in
`pending`: not watched, not ignored, surfaced on every call until somebody
arbitrates. Every course carries the rule that decided it, so "why is this not
showing up" always has an answer. Silent whitelists are how you find out in
March that a module was never being tracked.

And it is read-only against Moodle by construction, not by instruction. The
web-service allowlist lives in code; submitting, grading and posting are
unreachable. You can hand this to an autonomous agent without wondering what it
might click.

---

## How it works

It does not reimplement [moodle-dl][dl]. It imports it.

```
moodle-dl      what exists, what changed     mods, LTI extractors, yt-dlp
moodle-watch   since when, what you did      the clock, the ledger, MCP
```

Every course module type, every LTI video extractor, yt-dlp, years of awkward
real-world cases: all of it comes along. moodle-watch adds the two things
moodle-dl does not have.

Because moodle-dl records **what** it has, not **when** it got it. Its
`time_stamp` column is written only on modification paths; a fresh insert leaves
the default. On a real mirror of 2210 rows, and again on a fresh one of 95,
every single value is `0`. And once its `notified` flag is raised, the seen /
not-seen distinction is gone for good.

So the ledger records three things:

| | |
|---|---|
| `first_seen` | our own clock, on every revision |
| `handled` | who did what with an item, **per revision** |
| `course_policy` | why a course is watched or not, and which rule decided |

Handling is keyed on moodle-dl's `file_id`, and moodle-dl inserts a *new row*
whenever a file changes. The "it comes back to your backlog" behaviour is not a
feature anyone maintains; it is a property of the schema.

Nothing is ever deleted. A disappearance is recorded as an observation.

[dl]: https://github.com/C0D3D3V/Moodle-DL

---

## Install

```bash
pipx install "moodle-watch[documents]"
```

You need a moodle-dl mirror directory. If you do not have one:

```bash
moodle-dl --init --sso --path ~/Moodle     # or --init -u USER -pw PASS
```

Then point moodle-watch at it and run a first cycle:

```bash
export MOODLE_WATCH_MIRROR=~/Moodle
moodle-watch run
moodle-watch courses
```

The first run discovers every course you are enrolled in and puts them all in
`pending`. Decide with a policy file, or one tool call per course.

## Tools

| Tool | Answers |
|---|---|
| `whats_new` | what appeared, changed or vanished since `last_run`, `7d`, a date |
| `courses` | what is watched, what is pending, and the rule that decided |
| `deadlines` | what is due, and whether a due date **moved** |
| `backlog` | everything not handled yet, oldest first |
| `course_tree` | sections and modules, each annotated with its state |
| `search` | accent-insensitive full text over course, section, module, file names |
| `read_document` | the text of one file, from the mirror or through the token |
| `history` | every revision of an item, and everything done with it |
| `mark_handled` | record that items were dealt with |
| `set_course_policy` | answer a pending course |
| `sync` / `sync_status` | start a cycle in the background, poll it |

Watching and mirroring are the same code with a different argument:
`moodle-watch run` detects only, `moodle-watch run --fetch` also downloads,
forums and videos included.

## The policy

`~/.config/moodle-watch/policy.toml`, written for you on first run. Rules are
evaluated in order, first match wins, like a firewall.

```toml
default = "ask"          # ask | watch | ignore

[[rule]]
match  = { startdate_after = "2026-08-01" }   # this year only
action = "watch"

[[rule]]
match  = { shortname = "*2025*" }
action = "ignore"

[exclude]
extensions  = ["mp4"]
larger_than = "500MB"
filenames   = ["department-logo.png"]    # moodle-dl cannot filter by name
```

Criteria: `course`, `shortname`, `fullname`, `startdate_after`,
`startdate_before`. The `[exclude]` block is written into moodle-dl's own
`config.json`, so there is one source of truth. A decision you make by hand is
never overwritten by a rule afterwards.

## Running it

Over stdio, for a local client:

```json
{ "mcpServers": { "moodle-watch": {
    "command": "moodle-watch-server",
    "env": { "MOODLE_WATCH_MIRROR": "/home/you/Moodle" } } } }
```

Over HTTP, behind your own reverse proxy:

```bash
MOODLE_WATCH_HOSTS=moodle.example.org moodle-watch-server --http
```

It binds loopback only. `MOODLE_WATCH_HOSTS` feeds the SDK's DNS-rebinding
guard, which validates the `Host` header; the public name must be listed there
or the proxy gets a `421`. See [`docs/deployment.md`](docs/deployment.md) and the
templates in [`deploy/`](deploy/).

## Environment

| Variable | Meaning |
|---|---|
| `MOODLE_WATCH_MIRROR` | the moodle-dl directory. **Required.** |
| `MOODLE_WATCH_DB` | ledger path. Default: `<mirror>/moodle_watch.db` |
| `MOODLE_WATCH_POLICY` | policy file. Default: `~/.config/moodle-watch/policy.toml` |
| `MOODLE_WATCH_PORT` | HTTP port. Default: `3041` |
| `MOODLE_WATCH_HOSTS` | comma-separated public host names |
| `MOODLE_WATCH_BASE_URL`, `MOODLE_WATCH_SEGMENT` | only for `doctor --remote` |

No secret lives in this repository. The Moodle token stays in the mirror's
`config.json`, where moodle-dl put it.

## Diagnosing

```bash
moodle-watch doctor              # layers 1 to 3
moodle-watch doctor --remote     # and the public door
```

Four layers, deepest to most exposed. Layer 3 is not a health check: it opens a
real MCP session and asserts the answers are coherent with each other. A server
returning `200` while reporting empty courses is not a server that works. The
output never prints the secret URL segment.

## When the token expires

Moodle web-service tokens expire, and an SSO-issued one is renewed by hand:

```bash
moodle-dl -nt -sso --path ~/Moodle
```

moodle-watch names this failure mode rather than dying quietly: the tools return
a `token_expired` payload carrying that exact command, `doctor` gives it its own
line, and a failed run records it in the ledger.

---

## Honest status

**This works. It is also vibe-coded.** It was built in one sitting, with an AI,
by someone who is a teacher and not a software maintainer. It is verified rather
than merely hoped for: every claim above was checked against a live Moodle 4.5
instance, there are 40 tests, and the load-bearing behaviours (idempotence,
revisions returning to the backlog, nothing ever deleted, the `Host` guard) each
have one. Two real design bugs were caught by those tests and fixed. But there
is no roadmap, no support, and no promise of a second sitting.

One thing in particular deserves a warning: moodle-watch drives moodle-dl
through **internal classes of a command-line application**, which carry no
stability guarantee. That is why the version is pinned, why every import is
confined to `engine.py`, and why `doctor` asserts each call site still exists.
It is a deliberate and contained bet, not an oversight, but it is a bet.

So: shared gladly, take it, fork it, do whatever you like with it. **If you
think the idea is good, the right move is to maintain your own copy**, not to
wait on this one. Issues may go unanswered. A fork that outgrows this repo would
be a happy outcome, not a slight.

## Licence

GPL-3.0-or-later, inherited from moodle-dl, which is imported rather than
reimplemented.
