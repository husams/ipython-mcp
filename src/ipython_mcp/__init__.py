"""Persistent IPython execution over FastMCP."""

__version__ = "0.1.0"

from .config import ServerConfig
from .controller import ManagedRuntime
from .server import create_server, mcp

__all__ = ["ManagedRuntime", "ServerConfig", "__version__", "create_server", "mcp"]
