# The name

**The assistant is Jarvis. The folders are Ranger.** Both are deliberate and
neither is half-finished.

## What is called Jarvis

Everything the operator sees or says:

- the window title, the browser tab, and the pinned app's label
- how it refers to itself in speech and in text
- the assistant name in the system prompt, the morning brief and inbox notices
- user-facing documentation

The wake phrase was already `hey jarvis`, and the spoken dismissal derives its
name from that phrase's last word, so both were consistent with this before it
was decided.

## What is still called Ranger

Everything structural:

| what | why it stays |
| --- | --- |
| the `ranger` CLI command | the entry point in `pyproject.toml`, in every doc and in muscle memory |
| this repository and its branches | renaming a remote is not free and buys nothing |
| the vault directory `Ranger-Vault` | a path in `ranger.toml` and in Obsidian |
| the `Ranger/` folder inside the vault | **this is the one that matters** |
| the `Ranger` class in `core.py` | plumbing. "Nothing enters `Ranger.turn()`" is a rule written in a dozen places |
| the scheduled task names | installed state on the machine, see below |

`Ranger/` appears in **Amendment D**, in the snapshot allow list, in the
`ranger:below` marker documentation, and in every path-escape test. Renaming it
is a migration through the safety code, and the safety code is the last place
to accept churn for a cosmetic win. That was the operator's call and it was the
right one.

The **scheduled task names** stay for a different reason: `Ranger heartbeat`
and `Ranger interface` are already registered on the machine. Renaming them
would leave the old tasks in place and running, so a logon would start the
interface twice. That is a migration too, and a worse one, because it is
invisible until something behaves oddly.

## One coupling to keep in view

`naming.ASSISTANT` is both the window title and what `desktop.focus_window`,
`minimise_window` and `window_state` match window titles against. Change the
title without changing them and surfacing silently stops finding the window —
`focus_window` would return `not_found` and the wake word would stop bringing
the window forward, with nothing failing loudly. They read the same constant
for exactly that reason.

## If the rest is ever renamed

Read this file first, then Amendment D, then `docs/vault-conventions.md`. The
order of operations is: move the vault folder, update `ranger.toml`, re-run
`ranger snapshot init` because the allow list names `Ranger/` by path, and only
then touch the marker docs. It is an afternoon and it touches the code that
stops Ranger writing where it should not.
