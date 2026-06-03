"""ETL tree-building prompts — public API tracks the latest version.

Default usage: `from shared.prompts.etl import get_prompt`
Pinned usage:  `from shared.prompts.etl.v1 import get_prompt`

To ship a new version: add `vN.py` alongside the existing ones, then update the
two lines below (the import and `LATEST`). Old version files stay frozen.
"""
from .v1 import get_prompt

LATEST = "v1"
