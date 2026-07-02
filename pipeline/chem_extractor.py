import re
from pathlib import Path
import yaml

'''
First-pass regex extraction over OCR text + captions. Pure substring + pattern matching;
no LLM verification stage. Image-derived hits are inherently noisy — downstream consumers
should treat low-frequency entities accordingly.
'''


# NASICON-type: Na3Zr2Si2PO12, Na1+xZr2SixP3-xO12, etc.
NASICON_RE = re.compile(
    r"Na[1-9]?\+?[x-z]?Zr\d?Si[0-9x-z]?P[0-9x-z]?O\d{1,2}"
)

# Common sodium salts
SODIUM_SALT_RE = re.compile(
    r"Na(?:PF6|BF4|ClO4|FSI|TFSI|DFOB|BOB|FP|OTf|Tf|N\(SO2F\)2|N\(CF3SO2\)2)"
)

# Layered oxide cathodes: P2/O3-type NaxMO2
LAYERED_OXIDE_RE = re.compile(
    r"(?:P2|P3|O3|O2)-type\s*Na[x0-9\.]*[A-Z][a-z]?\d?O\d"
)

# General chemical formula: element sequences with subscripts
GENERAL_FORMULA_RE = re.compile(
    r"\b(?:Na|Li|K|Mg|Ca|Al|Zr|Ti|Mn|Co|Ni|Fe|Cr|V|P|S|O|Cl|F|N|C)"
    r"(?:[a-z])?(?:\d+\.?\d*)?(?:[A-Z][a-z]?(?:\d+\.?\d*)?){1,8}\b"
)

# Electrochemical values with units
ECHEM_VALUE_RE = re.compile(
    r"\d+\.?\d*\s*(?:mS/cm|S/cm|mAh/g|Wh/kg|V\s*vs\.?\s*Na\+?/?Na|μS/cm|"
    r"eV|GPa|MPa|kPa|mA/cm[²2]|μA/cm[²2]|mol/L|M\b|wt\.?%|vol\.?%)"
)

ALL_PATTERNS = [NASICON_RE, SODIUM_SALT_RE, LAYERED_OXIDE_RE, GENERAL_FORMULA_RE, ECHEM_VALUE_RE]


def extract_chem_entities(text: str, seed_entities: list[str] | None = None) -> list[str]:
    if not text:
        return []

    found: set[str] = set()

    if seed_entities:
        for entity in seed_entities:
            if entity in text:
                found.add(entity)
            elif not entity[0].isupper() and entity.lower() in text.lower():
                found.add(entity)

    for pattern in ALL_PATTERNS:
        for match in pattern.finditer(text):
            found.add(match.group(0).strip())

    # Filter noise: single chars, pure numbers
    found = {e for e in found if len(e) > 1 and not re.fullmatch(r"\d+\.?\d*", e)}
    return sorted(found)


def load_seed_entities(config_path: str = "config/chem_entities.yaml") -> list[str]:
    path = Path(config_path)
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("entities", [])
