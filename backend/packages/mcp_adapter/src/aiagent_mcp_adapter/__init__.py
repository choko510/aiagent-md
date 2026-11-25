"""aiagent-mcp-adapter: generic MCP adapter foundation (see README)."""

from .config import AdapterLimits, ProviderConfig
from .errors import ErrorCode, McpError

__all__ = ["AdapterLimits", "ErrorCode", "McpError", "ProviderConfig"]
