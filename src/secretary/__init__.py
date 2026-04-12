"""Claude Secretary — AI agent framework with model routing, Gmail/Calendar tools, and 24/7 watcher."""

__version__ = "0.2.0"

# Windows UTF-8 encoding fix (must run before any file I/O)
from .encoding_fix import fix_windows_encoding as _fix_encoding
_fix_encoding()

import logging as _logging
_logging.getLogger("googleapiclient.discovery_cache").setLevel(_logging.ERROR)

# Export main public APIs
from .agent import run, RunResult, ClaudeAgentOptions
from .config import SecretaryConfig
from .memory import MemoryStore

__all__ = [
    "run",
    "RunResult",
    "ClaudeAgentOptions",
    "SecretaryConfig",
    "MemoryStore",
]
