"""Putting a Ranger together.

Its own module because three front ends now need it, and assembling the core
is not the terminal's business. It was in cli.py, where it defaulted the gate
to TerminalGate. That default is fine for the terminal and wrong for everyone
else: a browser or a service inheriting it would block forever on an `input()`
call nobody can see.

So the gate is passed in or it is not passed at all, and a caller that does not
pass one gets `DenyingGate` from the core. Tier 6's rule, kept where it cannot
be defaulted away.
"""

from __future__ import annotations

from .audit import AuditLog
from .config import Config
from .core import Ranger
from .gate import Gate
from .knowledge import KnowledgeLoader
from .provider import build_provider
from .toolset import build_registry
from .vault import Vault


def build_agent(
    config: Config,
    *,
    gate: Gate | None = None,
    origin: str = "conversation",
    api_key: str | None = None,
) -> Ranger:
    if api_key is None:
        from .cli import require_api_key

        api_key = require_api_key()

    vault = Vault(config.vault)
    audit = AuditLog(vault, config.vault.log)
    return Ranger(
        config=config,
        provider=build_provider(config.model, api_key),
        # The registry gets the log too, so a clear through the panel button --
        # which never goes through a turn -- is recorded the same as one the
        # model made. The core still logs every tool call, so a clear through
        # the model appears from both vantage points.
        registry=build_registry(config, vault, audit=audit),
        vault=vault,
        knowledge_loader=KnowledgeLoader(vault, config.vault, config.knowledge),
        gate=gate,  # None means DenyingGate, and that is the point
        audit=audit,
        origin=origin,
    )
