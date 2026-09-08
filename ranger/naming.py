"""What the assistant is called, and what the folders are called.

**These are two different names on purpose, and the split was a decision.**

The operator settled on *Jarvis* for the assistant: it is what they say to it,
what the wake phrase is, what the window says, and how it refers to itself. It
is not what the plumbing is called. The `ranger` command, this repository, the
vault directory and the `Ranger/` folder inside it all keep the old name.

That is not a half-finished rename. Those names appear in Amendment D, in the
snapshot allow list, in the marker documentation and in every path-escape test,
so renaming them is a migration through the safety code rather than a cosmetic
change — and the safety code is the last place to accept churn for a cosmetic
win. The two names are stable, they are both written down here, and a future
reader deciding to finish the job should know what it costs first.

One coupling worth keeping in view: `ASSISTANT` is also what
`desktop.focus_window` and `desktop.minimise_window` match window titles
against. Change the title without changing them and surfacing silently stops
finding the window.
"""

from __future__ import annotations

#: What it is called. Window title, browser tab, prompts, speech, notices.
ASSISTANT = "Jarvis"

#: What the folders are called. The vault directory, the folder inside it, the
#: CLI command, this repository. Deliberately unchanged.
PROJECT = "Ranger"
