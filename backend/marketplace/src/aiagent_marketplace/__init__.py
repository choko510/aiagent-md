"""MKT-1: read-only catalog. No install, MCP execution or credential handling."""

from .catalog import CatalogError, CatalogService, FixedSourceHTTP

__all__ = ["CatalogError", "CatalogService", "FixedSourceHTTP"]
