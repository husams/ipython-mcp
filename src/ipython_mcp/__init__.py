"""Persistent IPython execution over FastMCP."""

__version__ = "0.1.0"

from .config import ServerConfig
from .runtime import ShellRuntime
from .server import create_server, mcp

__all__ = ["ServerConfig", "ShellRuntime", "__version__", "create_server", "mcp"]
