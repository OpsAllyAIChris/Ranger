"""Dashlets: what the panel shows without asking the model anything.

This module is small and the rule it sets is not, so the rule goes first.

**A dashlet is a Python-computed read of the vault.** Not an agent turn, not a
cached model answer, not a background prompt on a timer. The command centre
will grow several of these -- gross profit, commitments, what went quiet, the
calendar -- and every one of them takes this shape or it does not ship.

Two reasons, and the second is the one that matters.

*Cost and noise.* Eight dashlets refreshing on a heartbeat would be eight model
calls a minute, each one spending tokens to restate a number that was already
sitting on disk. The panel redraws on every turn and every panel push; nothing
that redraws that often may cost money to redraw.

*Correctness.* A figure a language model arrived at is a figure that can be
wrong in a way that looks exactly right. The operator acts on gross profit.
A hallucinated GP number is worse than an empty panel, because an empty panel
is obviously empty. So the arithmetic is Python's, the rounding is `Decimal`'s,
and the model's only relationship with these numbers is that it may be told
what they are.

Three properties every reading here holds to:

- **Absence is never zero.** A dashlet with nothing to show says so. Rendering
  a missing figure as 0 is how a gap becomes a fact.
- **Every reading carries when it is from.** `as_of` is the moment the
  underlying entry was recorded, never the moment the panel drew it, and a
  reading past its freshness window says it is stale.
- **A dashlet that cannot read says that too.** An exception becomes a reading
  with `error` set and no value. It never becomes a blank, which would read as
  a legitimate zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only, and deliberately so
    from .config import Config
    from .vault import Vault


@dataclass(frozen=True)
class Reading:
    """One number on the panel, and everything needed to render it honestly.

    `value` is a string because formatting money is a decision -- separators,
    symbol, two decimals -- and making it once here means the browser cannot
    make it differently. `value` is empty when there is nothing to show, and
    the front end must render `empty` rather than inventing a nought.
    """

    key: str
    title: str
    value: str = ""
    detail: str = ""
    #: Human text: "8 September, 14:02". Empty when there is nothing to date.
    as_of: str = ""
    #: How old the underlying entry is, in days. None when there is none.
    age_days: int | None = None
    stale: bool = False
    #: What to show instead of a value. Never "0", never "-".
    empty: str = ""
    #: Set when the read itself failed. A reading with an error has no value.
    error: str = ""
    #: Anything the front end needs beyond text, such as the entry form's
    #: current period. Never a computed figure the panel would re-render.
    extra: dict[str, Any] | None = None

    @property
    def has_value(self) -> bool:
        return bool(self.value) and not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "value": self.value,
            "detail": self.detail,
            "as_of": self.as_of,
            "age_days": self.age_days,
            "stale": self.stale,
            "empty": self.empty,
            "error": self.error,
            "extra": dict(self.extra or {}),
        }


#: Every dashlet, in the order the panel draws them. A function of
#: (config, vault, today) returning one Reading. Adding an entry here is the
#: whole registration mechanism; there is no plugin system and no discovery,
#: because a dashlet that appears by being on disk is a dashlet nobody chose.
def sources() -> tuple[tuple[str, Callable[..., Reading]], ...]:
    from . import gp

    return (("gp", gp.reading),)


def readings(
    config: "Config", vault: "Vault", today: date | None = None, now: datetime | None = None
) -> list[Reading]:
    """Every dashlet, read fresh off the disk. No model, no cache, no clock skew.

    A source that raises becomes a reading that says so. One dashlet failing
    must not empty the panel, and it must not quietly render as nothing either:
    "could not read" and "nothing entered" are different states and the
    operator needs to be able to tell them apart.
    """
    today = today or date.today()
    now = now or datetime.now()
    out: list[Reading] = []
    for key, source in sources():
        try:
            out.append(source(config, vault, today=today, now=now))
        except Exception as exc:  # a broken dashlet must not take the panel down
            out.append(
                Reading(
                    key=key,
                    title=key,
                    error=f"{type(exc).__name__}: {exc}",
                    empty="could not read",
                )
            )
    return out
