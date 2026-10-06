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

# An uppercase prefix of 2-4 letters, a hyphen, then digits. Deliberately broad: an id
# the customer invents (CMP-77) must be caught so the lookup can report it as unknown,
# rather than silently treated as "no asset mentioned".
_EQUIPMENT_ID = re.compile(r"\b([A-Z]{2,4})-(\d{1,6})\b")

# Prefixes that identify something other than a piece of equipment.
_NON_ASSET_PREFIXES = frozenset({"REQ", "WO", "CON", "CUS", "SITE", "TECH", "ATT", "POL", "OPS", "SYS"})

_REQUEST_REFERENCE = re.compile(r"\b((?:REQ|WO)-[A-Z0-9-]+)\b")


def find_equipment_ids(text: str) -> list[str]:
    """Equipment identifiers in order of first appearance, de-duplicated.

    Excludes contract, customer, site, technician and request references, which are
    identifiers but not equipment.
    """
    seen: list[str] = []
    for match in _EQUIPMENT_ID.finditer(text or ""):
        prefix = match.group(1)
        if prefix in _NON_ASSET_PREFIXES:
            continue
        identifier = match.group(0)
        if identifier not in seen:
            seen.append(identifier)
    return seen


def find_request_references(text: str) -> list[str]:
    """Earlier request or work-order references (REQ-…, WO-…), de-duplicated."""
    seen: list[str] = []
    for match in _REQUEST_REFERENCE.finditer((text or "").upper()):
        identifier = match.group(1)
        if identifier not in seen:
            seen.append(identifier)
    return seen
