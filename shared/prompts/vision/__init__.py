"""Vision OCR prompts.

Default usage: `from shared.prompts.vision import CHANDRA_OCR_LAYOUT_PROMPT`
Pinned usage:  `from shared.prompts.vision.v1 import CHANDRA_OCR_LAYOUT_PROMPT`
"""

from .v1 import (
    CHANDRA_ALLOWED_ATTRS,
    CHANDRA_ALLOWED_TAGS,
    CHANDRA_OCR_LAYOUT_PROMPT,
    CHANDRA_OCR_PROMPT,
    LAB_ELEMENT_OCR_PROMPT,
)

__all__ = [
    "CHANDRA_ALLOWED_ATTRS",
    "CHANDRA_ALLOWED_TAGS",
    "CHANDRA_OCR_LAYOUT_PROMPT",
    "CHANDRA_OCR_PROMPT",
    "LAB_ELEMENT_OCR_PROMPT",
]
