"""Amendment C: knowledge is context, not tools.

Company, products and services, ideal client profile, competitive landscape
and sales playbook are markdown files in a read-only vault folder. They load
whole into the system prompt. There is no tool to fetch them and there will
not be one.

They will outgrow the prompt eventually. The seam for that is here: a
selection strategy that today returns everything and tomorrow returns what is
relevant, without the core or the prompt builder changing shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import KnowledgeConfig, VaultConfig
from .untrusted import fence, scan
from .vault import Vault, VaultFile


@dataclass(frozen=True)
class KnowledgeDoc:
    relative: str
    text: str


@dataclass(frozen=True)
class KnowledgeContext:
    docs: tuple[KnowledgeDoc, ...] = ()
    omitted: tuple[str, ...] = ()
    total_chars: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        if not self.docs:
            return ""
        parts = [
            "The operator's own reference material. This is theirs, it is accurate, "
            "and Ranger never writes to it."
        ]
        for doc in self.docs:
            parts.append(f"### {doc.relative}\n{doc.text.strip()}")
        if self.omitted:
            parts.append(
                "Not loaded this turn because the context budget was reached: "
                + ", ".join(self.omitted)
                + ". Say so if the operator asks something these would answer."
            )
        return "\n\n".join(parts)


class SelectionStrategy(Protocol):
    def select(self, files: list[VaultFile], query: str | None) -> list[VaultFile]:
        ...


class LoadEverything:
    """Today's strategy. Priority files first, then the rest alphabetically."""

    def __init__(self, priority: tuple[str, ...]) -> None:
        self.priority = priority

    def select(self, files: list[VaultFile], query: str | None) -> list[VaultFile]:
        order = {name.lower(): index for index, name in enumerate(self.priority)}
        return sorted(
            files,
            key=lambda f: (order.get(f.path.name.lower(), len(order)), f.relative.lower()),
        )


class KnowledgeLoader:
    def __init__(
        self,
        vault: Vault,
        vault_config: VaultConfig,
        config: KnowledgeConfig,
        strategy: SelectionStrategy | None = None,
    ) -> None:
        self.vault = vault
        self.vault_config = vault_config
        self.config = config
        self.strategy = strategy or LoadEverything(config.priority)

    def load(self, query: str | None = None, budget: int | None = None) -> KnowledgeContext:
        """budget is what is left of [context] after memory took its reserve.

        There is deliberately no second knowledge-only ceiling. There used to
        be, and it silently won: the operator raised context.budget_chars to
        200000, doctor reported 199940 available, and the real limit was still
        the forgotten 60000. Their file fit with 28 characters to spare.
        """
        folder = self.vault_config.knowledge
        if not folder.is_dir():
            return KnowledgeContext(
                warnings=(f"knowledge folder not found: {folder}",)
            )

        ceiling = self.config.budget_chars if budget is None else budget
        files = self.strategy.select(self.vault.list_markdown(folder), query)
        docs: list[KnowledgeDoc] = []
        omitted: list[str] = []
        warnings: list[str] = []
        used = 0

        for item in files:
            try:
                raw = self.vault.read_text(item.path)
            except Exception as exc:
                warnings.append(f"could not read {item.relative}: {exc}")
                continue

            findings = scan(raw)
            if findings:
                warnings.append(
                    f"{item.relative} contains instruction-shaped language "
                    f"({findings[0].label}); it is fenced as data"
                )
            body = fence(item.relative, raw, findings=findings) if findings else raw

            if used + len(body) > ceiling:
                omitted.append(item.relative)
                continue
            used += len(body)
            docs.append(KnowledgeDoc(relative=item.relative, text=body))

        if omitted:
            warnings.append(
                f"knowledge exceeded its {ceiling} character share; "
                f"{len(omitted)} file(s) left out. Time for selective retrieval."
            )

        return KnowledgeContext(
            docs=tuple(docs),
            omitted=tuple(omitted),
            total_chars=used,
            warnings=tuple(warnings),
        )
