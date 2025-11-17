"""
ww.addresses
------------
Region-specific address tables live here.

By default, callers import region modules directly (e.g., ww.addresses.ww_jp),
or rely on ww.memory's resolve_address() which prefers ww.versioning if present.

Nothing needs to be exported here, but we keep this file so the package is explicit.
"""
from .address import Address
# Maybe adjust this import to be an instance instead of the class? Would let us use
# __getattr__ and audit anything missing.

__all__ = ['Address']