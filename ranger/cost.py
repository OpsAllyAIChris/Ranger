"""What a turn cost, and whether the cache is working.

Prices are per million tokens and live in config, because they change and
because guessing at them in code would be worse than saying nothing. Cached
input is charged at roughly a tenth of normal input, which is the whole reason
the knowledge budget stopped mattering.

If cache_read stays at zero across consecutive turns, something is changing the
stable half of the prompt and caching is not happening. That is the number to
watch, and it is why it is printed rather than buried.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ModelConfig


@dataclass(frozen=True)
class TurnCost:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: dict) -> "TurnCost":
        return cls(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
        )

    def __add__(self, other: "TurnCost") -> "TurnCost":
        return TurnCost(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    @property
    def cached_fraction(self) -> float:
        return self.cache_read_tokens / self.total_input if self.total_input else 0.0

    def dollars(self, model: ModelConfig) -> float:
        million = 1_000_000
        return (
            self.input_tokens * model.price_input
            + self.output_tokens * model.price_output
            + self.cache_write_tokens * model.price_cache_write
            + self.cache_read_tokens * model.price_cache_read
        ) / million

    def render(self, model: ModelConfig) -> str:
        if not self.total_input and not self.output_tokens:
            return ""
        parts = [f"in {self.total_input}", f"out {self.output_tokens}"]
        if self.cache_read_tokens:
            parts.append(f"{self.cached_fraction:.0%} cached")
        elif self.cache_write_tokens:
            parts.append("cache written")
        else:
            parts.append("uncached")
        if model.price_input:
            parts.append(f"${self.dollars(model):.4f}")
        return ", ".join(parts)
