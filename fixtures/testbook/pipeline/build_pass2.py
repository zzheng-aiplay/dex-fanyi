#!/usr/bin/env python3
"""Deterministic translationese scan — the fixture's ship gate.

`fanyi_dex/detscan.py` imports this file from beside the project's `config.json` and
calls `scan_translationese`. That indirection is the point: the blocklists that decide
whether a chapter ships live with the *project*, not with the runner, so they cannot
drift from whatever a real project has settled on.

This is a fixture implementation. A real project's version is longer and tuned; the
contract is what matters:

    scan_translationese(text) -> {
        "archaisms": [...], "calques": [...], "unit_leaks": [...],
        "poem_refs": [...], "wrong_roman": [...],
    }

`detscan.gate_count()` sums the first four. `wrong_roman` is reported but not counted,
matching the shipped gate.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Words that read as costume-drama English rather than as prose. The fake agent in
# `fanyi_dex/fake_agent.py` deliberately emits "bade" so a test run exercises the
# targeted-fix branch instead of sailing through a clean gate.
ARCHAISMS = (
    "bade", "betook", "thereupon", "whereupon", "forthwith", "henceforth",
    "it came to pass", "made haste to", "spake", "thence", "hither", "whilst",
)

# Chinese idiom carried into English word-for-word.
CALQUES = (
    "bent to the wind", "eat the wind", "add oil", "open the mountain",
    "iron rice bowl", "look at flowers from horseback", "drew a snake and added feet",
)

# A bare Chinese unit that survived into the English.
UNIT_LEAK = re.compile(r"\b\d+\s*(?:li|chi|zhang|mu|dan|jin)\b", re.I)

# A translator's note about a poem instead of the poem, or a bare marker.
POEM_REF = re.compile(r"\b(?:a poem (?:says|runs)|later (?:a )?poet|there is a poem)\b", re.I)


def _load_pinned_names() -> tuple[str, ...]:
    """Names the project has pinned, read from config so the two cannot disagree."""
    path = None
    if "--config" in sys.argv:
        path = sys.argv[sys.argv.index("--config") + 1]
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ()
    tiers = cfg.get("cast_tiers", {})
    return tuple(tiers.get("tier1", []) + tiers.get("tier2", []))


PINNED = _load_pinned_names()

# Misspellings of a pinned name: same first syllable, wrong tail. Cheap and effective
# for the ones that actually happen: a name whose first syllable is right and whose
# tail is a different romanization system.
# Placeholder pairs, matching the placeholder cast in config.json. A real project
# supplies the misspellings its own pinned names actually attract.
_WRONG_ROMAN = {
    "Alfa": "Alpha", "Beeta": "Beta", "Gama": "Gamma",
    "Delda": "Delta", "Epsilo": "Epsilon", "Zeeta": "Zeta",
}


def scan_translationese(text: str) -> dict[str, list[str]]:
    """Return every deterministic offence found in `text`, grouped by kind."""
    body = text or ""
    lowered = body.lower()
    return {
        "archaisms": [word for word in ARCHAISMS if _has_word(lowered, word)],
        "calques": [phrase for phrase in CALQUES if phrase in lowered],
        "unit_leaks": sorted({m.group(0) for m in UNIT_LEAK.finditer(body)}),
        "poem_refs": sorted({m.group(0) for m in POEM_REF.finditer(body)}),
        "wrong_roman": [
            f"{wrong} (should be {right})"
            for wrong, right in _WRONG_ROMAN.items()
            if wrong.lower() in lowered and right in PINNED
        ],
    }


def _has_word(lowered: str, needle: str) -> bool:
    """Whole-word match, so 'thence' does not fire inside 'thenceforward'."""
    if " " in needle:
        return needle in lowered
    return re.search(rf"\b{re.escape(needle)}\b", lowered) is not None


if __name__ == "__main__":
    payload = sys.stdin.read()
    print(json.dumps(scan_translationese(payload), ensure_ascii=False, indent=1))
