"""
ww.addresses
------------
Region-specific address tables live here.

By default, callers import region modules directly (e.g., ww.addresses.ww_jp),
or rely on ww.memory's resolve_address() which prefers ww.versioning if present.

Nothing needs to be exported here, but we keep this file so the package is explicit.
"""

# Having an __all__ is optional; keep it empty to avoid implying a default region.
__all__ = []