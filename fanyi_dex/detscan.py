"""The deterministic ship gate, reused from the project rather than reimplemented.

`build_pass2.py` owns the authoritative blocklists (archaisms, calque fragments,
unit-leak regex, narrator-poem references, wrong romanizations). Copying them
here would let them drift from the gate that actually decides whether a chapter
ships, so this loads that module directly — the same importlib trick
`harvest_reprocess.py` already uses.

Also ports the JS-side dialogue-flatten helpers from the generated workflow, which
are defined in the script body rather than in build_pass2.py's Python half.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import threading
from pathlib import Path
from typing import Any

from fanyi_dex.prompts import TIER_FULL

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}


def load_pass2_module(config_path: str):
    """Import `<project>/pipeline/build_pass2.py` with its config bound.

    The module reads `--config` off sys.argv at import time, so argv is swapped
    for the duration of the import. Cached per config path; the lock keeps
    concurrent chapter Steps from racing on the argv swap.
    """
    key = str(config_path)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        script = Path(config_path).expanduser().parent / "build_pass2.py"
        if not script.is_file():
            raise FileNotFoundError(f"cannot find build_pass2.py next to {config_path}")
        spec = importlib.util.spec_from_file_location("_fanyi_bp2", str(script))
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load {script}")
        module = importlib.util.module_from_spec(spec)
        saved = sys.argv
        sys.argv = ["build_pass2.py", "--config", str(Path(config_path).expanduser())]
        try:
            spec.loader.exec_module(module)
        finally:
            sys.argv = saved
        _CACHE[key] = module
        return module


def scan(config_path: str, text: str) -> dict[str, list[str]]:
    """Full deterministic scan: archaisms, calques, unit_leaks, poem_refs, wrong_roman."""
    module = load_pass2_module(config_path)
    return module.scan_translationese(text or "")


def gate_count(scan_result: dict[str, list[str]]) -> int:
    """How many deterministic offences block a chapter from shipping.

    A misspelled pinned name is reported and surfaced separately rather than counted
    here, because a project may want to decide that one by eye.
    """
    return (
        len(scan_result.get("archaisms") or [])
        + len(scan_result.get("calques") or [])
        + len(scan_result.get("unit_leaks") or [])
        + len(scan_result.get("poem_refs") or [])
    )


# -- dialogue-flatten detection (ported from the generated workflow body) ------

HAS_QUOTE = re.compile(r'["“”「『]')
# Markers of direct speech in classical Chinese. A project translating from another
# source language supplies its own scan through `build_pass2.py`; this covers the one
# the reference fixture uses.
SRC_SPEECH = re.compile(r"曰|「|『|道[：:]")


def source_has_speech(zh: str) -> bool:
    return bool(SRC_SPEECH.search(zh or ""))


def is_flattened(segment: dict[str, Any]) -> bool:
    """A beat whose source is direct speech but whose prose carries no quotation marks.

    Only checked at the tier that promises to keep speech quoted. Narrating speech is
    correct at the compressed tier, so flagging it there produced false positives.
    """
    prose = segment.get("prose") or ""
    if not prose.strip():
        return False
    if segment.get("tier") != TIER_FULL:
        return False
    return source_has_speech(segment.get("zh_span") or "") and not HAS_QUOTE.search(prose)


def assemble(segments: list[dict[str, Any]]) -> str:
    """Join non-empty segment prose, as both the workflow and harvest do."""
    return "\n\n".join(
        (s.get("prose") or "").strip()
        for s in (segments or [])
        if (s.get("prose") or "").strip()
    )
