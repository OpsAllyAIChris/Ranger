"""Jarvis. A voice-first assistant for one person.

The core is a library (see core.py). The terminal, the push-to-talk loop, the
heartbeat and the browser are all callers of it.
"""

from .config import Config, ConfigError, load_config
from .core import Ranger
from .events import (
    ConfirmationRequested,
    Event,
    Notice,
    State,
    StateChanged,
    TextDelta,
    ToolCalled,
    ToolFinished,
    TurnComplete,
)
from .knowledge import KnowledgeLoader
from .provider import Provider, build_provider
from .tools import Tool, ToolRegistry, ToolResult
from .toolset import build_registry
from .vault import Vault, VaultError, VaultPathDenied, VaultWriteDenied

__version__ = "0.1.0"

__all__ = [
    "Config",
    "ConfigError",
    "ConfirmationRequested",
    "Event",
    "KnowledgeLoader",
    "Notice",
    "Provider",
    "Jarvis",
    "State",
    "StateChanged",
    "TextDelta",
    "Tool",
    "ToolCalled",
    "ToolFinished",
    "ToolRegistry",
    "ToolResult",
    "TurnComplete",
    "Vault",
    "VaultError",
    "VaultPathDenied",
    "VaultWriteDenied",
    "__version__",
    "build_provider",
    "build_registry",
    "load_config",
]
