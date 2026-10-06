"""Pull exact equipment identifiers out of request text.

Code does this, not the model: an exact id is a cheap, deterministic regex match, and
spending a model call on it would be both slower and less reliable. The model is for
language, not for pattern matching.

Matches the shapes seen in the data and in customer messages:
  AST-101, AST-1002   assets on record
  CMP-77, LM-4        equipment the customer names that may not be on record
  REQ-8259, WO-9294   earlier requests and jobs
"""

from __future__ import annotations

import re

# A prefix of 2-4 letters, an optional separator, then digits. Deliberately broad: an id
# the customer invents (CMP-77) must be caught so the lookup can report it as unknown,
# rather than silently treated as "no asset mentioned".
#
# Case-insensitive, and the separator may be a hyphen, en dash, underscore, space or
# nothing at all: customers copy ids off a sticker ("AST 1202"), out of a portal
# ("ast-1202") or from memory. Requiring the canonical spelling meant asking a customer
# with a failed unit to identify equipment they had already named twice.
_EQUIPMENT_ID = re.compile(r"\b([A-Za-z]{2,4})[-–_ ]?(\d{1,6})\b")

# The prefix the catalogue uses for equipment on record (SYS-CATALOG-001). Only these
# are recognised when the customer omits the hyphen, because without a separator the
# evidence that an id was meant at all is weak.
_EQUIPMENT_PREFIXES = frozenset({"AST"})

# Prefixes that identify something other than a piece of equipment.
_NON_ASSET_PREFIXES = frozenset({"REQ", "WO", "CON", "CUS", "SITE", "TECH", "ATT", "POL", "OPS", "SYS"})

_REQUEST_REFERENCE = re.compile(r"\b((?:REQ|WO)-[A-Z0-9-]+)\b")


def find_equipment_ids(text: str) -> list[str]:
    """Equipment identifiers in order of first appearance, normalised and de-duplicated.

    Whatever spelling the customer used, the canonical "AST-1202" comes back, so the
    lookup and every downstream comparison see one form. "AST 1202" and "ast-1202" in the
    same message are one identifier, not two.

    Excludes contract, customer, site, technician and request references, which are
    identifiers but not equipment.
    """
    seen: list[str] = []
    for match in _EQUIPMENT_ID.finditer(text or ""):
        prefix = match.group(1).upper()
        if prefix in _NON_ASSET_PREFIXES:
            continue
        # A hyphen or underscore is an explicit identifier, whatever the case: anyone
        # writing "cmp-77" means an id. A space or no separator is much weaker evidence —
        # "Unit 4 stopped", "AHU 2", "line4" are all ordinary prose — so those are only
        # read as an id for a prefix the catalogue actually uses for equipment. That
        # keeps "AST 1202" off the sticker while leaving prose alone.
        separator = match.group(0)[len(match.group(1)) : -len(match.group(2))]
        explicit = separator in {"-", "–", "_"}
        if not explicit and prefix not in _EQUIPMENT_PREFIXES:
            continue
        identifier = f"{prefix}-{match.group(2)}"
        if identifier not in seen:
            seen.append(identifier)
    return seen


def id_appears_in_text(identifier: str, text: str) -> bool:
    """Is this identifier present in the text, in any spelling the matcher accepts?

    Used to check the model's asset id before relying on it: the model may name an asset
    it inferred from the records rather than one the customer wrote. "AST-1202" counts
    whether they typed it as AST-1202, ast-1202, AST 1202 or AST1202.
    """
    canonical = str(identifier or "").strip().upper().replace(" ", "-").replace("_", "-")
    if not canonical:
        return False
    return canonical in find_equipment_ids(text or "")


def find_request_references(text: str) -> list[str]:
    """Earlier request or work-order references (REQ-…, WO-…), de-duplicated."""
    seen: list[str] = []
    for match in _REQUEST_REFERENCE.finditer((text or "").upper()):
        identifier = match.group(1)
        if identifier not in seen:
            seen.append(identifier)
    return seen
