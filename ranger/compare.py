"""Showing what Ranger thought you said, next to what you said.

start-here.md asks for the transcript beside the reply so that when the answer
is wrong you can see whether the ears or the brain was at fault. This is the
measuring half of that: give it what you actually said and it marks every word
that came back different, and reports the error rate.

It exists so the hinting question can be settled with numbers rather than
impressions. Run the same recording with and without stt.keyterms and compare.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r"[\w']+")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True)
class Comparison:
    expected: tuple[str, ...]
    heard: tuple[str, ...]
    substitutions: int = 0
    insertions: int = 0
    deletions: int = 0
    pairs: tuple[tuple[str, str], tuple, ...] = ()

    @property
    def errors(self) -> int:
        return self.substitutions + self.insertions + self.deletions

    @property
    def word_error_rate(self) -> float:
        return self.errors / len(self.expected) if self.expected else 0.0

    @property
    def perfect(self) -> bool:
        return self.errors == 0

    def render(self) -> str:
        """Aligned, so a mishearing is obvious at a glance."""
        lines = []
        for kind, want, got in self.pairs:
            if kind == "equal":
                continue
            if kind == "substitution":
                lines.append(f"    said {want!r}  ->  heard {got!r}")
            elif kind == "deletion":
                lines.append(f"    said {want!r}  ->  heard nothing")
            else:
                lines.append(f"    said nothing  ->  heard {got!r}")
        return "\n".join(lines)


def compare(expected_text: str, heard_text: str) -> Comparison:
    """Word error rate by edit distance, with the alignment kept.

    difflib is not used here: it optimises for readable diffs rather than
    minimum edits, and would report a different error count than the standard
    word error rate does.
    """
    expected = tokens(expected_text)
    heard = tokens(heard_text)

    rows, cols = len(expected) + 1, len(heard) + 1
    cost = [[0] * cols for _ in range(rows)]
    back = [[""] * cols for _ in range(rows)]

    for i in range(1, rows):
        cost[i][0] = i
        back[i][0] = "deletion"
    for j in range(1, cols):
        cost[0][j] = j
        back[0][j] = "insertion"

    for i in range(1, rows):
        for j in range(1, cols):
            if expected[i - 1] == heard[j - 1]:
                cost[i][j] = cost[i - 1][j - 1]
                back[i][j] = "equal"
                continue
            substitute = cost[i - 1][j - 1] + 1
            delete = cost[i - 1][j] + 1
            insert = cost[i][j - 1] + 1
            best = min(substitute, delete, insert)
            cost[i][j] = best
            back[i][j] = (
                "substitution" if best == substitute else "deletion" if best == delete else "insertion"
            )

    pairs: list[tuple[str, str, str]] = []
    counts = {"substitution": 0, "insertion": 0, "deletion": 0}
    i, j = len(expected), len(heard)
    while i > 0 or j > 0:
        move = back[i][j]
        if move == "equal":
            pairs.append(("equal", expected[i - 1], heard[j - 1]))
            i, j = i - 1, j - 1
        elif move == "substitution":
            pairs.append(("substitution", expected[i - 1], heard[j - 1]))
            counts["substitution"] += 1
            i, j = i - 1, j - 1
        elif move == "deletion":
            pairs.append(("deletion", expected[i - 1], ""))
            counts["deletion"] += 1
            i -= 1
        else:
            pairs.append(("insertion", "", heard[j - 1]))
            counts["insertion"] += 1
            j -= 1

    pairs.reverse()
    return Comparison(
        expected=tuple(expected),
        heard=tuple(heard),
        substitutions=counts["substitution"],
        insertions=counts["insertion"],
        deletions=counts["deletion"],
        pairs=tuple(pairs),
    )
