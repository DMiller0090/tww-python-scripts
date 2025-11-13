"""
ww.config
---------
User-tunable settings

Notes
- Place your CSVs inside the ww/data folder (so ww.data_path(...) resolves them).
- With the current analog loader, only INPUT_TABLE_PATH is used.
  INPUT_TABLE_PATHS is included for future multi-CSV merging (optional).
"""

from __future__ import annotations
from . import data_path  # provided by ww/__init__.py

# ── Region / version ──────────────────────────────────────────────────────────
# Default region. When you add ww.versioning, this can be overridden there.
REGION: str = "JP"

# ── Analog CSVs ───────────────────────────────────────────────────────────────
# Put your files in ww/data/ and point here. The current analog module uses only INPUT_TABLE_PATH.
# Keep INPUT_TABLE_PATHS so we can support merging later without touching scripts.
INPUT_TABLE_PATH: str = data_path("INPUT_DUMP_MAIN.csv")

# Optional: ALT table is for stages that use alternate stick directions. Such as Fire Mountain
INPUT_TABLE_PATHS: list[str] = [
    data_path("INPUT_DUMP_MAIN.csv"),
    data_path("INPUT_DUMP_ALT.csv"),
]
