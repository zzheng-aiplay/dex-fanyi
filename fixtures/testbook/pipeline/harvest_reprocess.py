#!/usr/bin/env python3
"""Write finished chapters into the book folder, gated on the QA signals.

Consumed by `HarvestStep`, which shells out to whatever `harvest_reprocess.py` sits in
the project's `pipeline/`. This fixture implements the contract:

    harvest_reprocess.py <chapters.json> [...] [--config PATH] [--force]

    input   {"chapters": [{hui, local, book, final: [segments], det_clean, lift_ok,
                           flattened_p1_after, ...}]}
    output  <vault_book_dir>/Book N — 回LO-HI (translation)/回NN (edited).md
    stdout  "harvested N chapters", then a SKIPPED block for anything the gate held back

The gate is the reason this runs as a script rather than inline: the decision about
whether a chapter is fit to ship belongs with the project, and a project can make it
stricter without the runner changing.

A chapter must clear all three to be written:
  * the deterministic scan is clean (`det_clean`)
  * the accessibility lift held (`lift_ok`)
  * no beat still flattens direct speech into narration (`flattened_p1_after == 0`)

`--force` writes anyway and says so.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from build_pass2 import scan_translationese
except ImportError:  # pragma: no cover - keeps a partial fixture usable
    def scan_translationese(text: str) -> dict[str, list[str]]:
        return {"archaisms": [], "calques": [], "unit_leaks": [], "poem_refs": []}


def assemble_segments(segments: list[dict]) -> str:
    """Join the non-empty prose. A CUT beat contributes nothing, by design."""
    return "\n\n".join(
        (segment.get("prose") or "").strip()
        for segment in segments or []
        if (segment.get("prose") or "").strip()
    )


def load_config(argv: list[str]) -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if "--config" in argv:
        path = os.path.expanduser(argv[argv.index("--config") + 1])
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def chapter_path(cfg: dict, chapter: dict) -> str | None:
    """Resolve the vault file for a chapter that only knows its own hui.

    Matches the book folder by its 回LO-HI range rather than by book number, because a
    folder may have been renamed and the range is the durable part.
    """
    base = os.path.expanduser(cfg.get("project", {}).get("vault_book_dir", ""))
    if not base or not os.path.isdir(base):
        return None
    hui = chapter.get("hui")
    for number, book in (cfg.get("books") or {}).items():
        low, high = book.get("hui", [0, 0])
        if not (low <= hui <= high):
            continue
        for name in sorted(os.listdir(base)):
            directory = os.path.join(base, name)
            if not os.path.isdir(directory) or not name.startswith("Book "):
                continue
            if f"回{low}-{high}" in name or f"回{low:02d}-{high:02d}" in name:
                return os.path.join(directory, f"回{hui:02d} (edited).md")
        # No folder yet: create the canonical one rather than dropping the chapter.
        directory = os.path.join(base, f"Book {number} — 回{low}-{high} (translation)")
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"回{hui:02d} (edited).md")
    return None


def gate(chapter: dict, markdown: str) -> list[str]:
    """Every reason this chapter is not fit to ship. Empty means it is."""
    problems = []
    scan = scan_translationese(markdown)
    countable = sum(
        len(scan.get(key) or [])
        for key in ("archaisms", "calques", "unit_leaks", "poem_refs")
    )
    if countable:
        found = {k: v for k, v in scan.items() if v}
        problems.append(f"deterministic scan found {countable}: {found}")
    if not chapter.get("det_clean", True):
        problems.append("chapter reports det_clean=false")
    if not chapter.get("lift_ok", True):
        problems.append(
            f"accessibility lift did not hold "
            f"({chapter.get('access_before')} -> {chapter.get('access_after')})"
        )
    flattened = chapter.get("flattened_p1_after") or 0
    if flattened:
        problems.append(f"{flattened} beat(s) still flatten direct speech")
    return problems


def main() -> int:
    argv = sys.argv[1:]
    paths = [a for a in argv if not a.startswith("--") and a.endswith(".json")]
    # Drop the value that follows --config.
    if "--config" in argv:
        config_value = argv[argv.index("--config") + 1]
        paths = [p for p in paths if p != config_value]
    if not paths:
        print("usage: harvest_reprocess.py <chapters.json> [...] [--config PATH] [--force]")
        return 1

    force = "--force" in argv
    cfg = load_config(argv)

    chapters = []
    for path in paths:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            payload = json.load(handle)
        found = payload.get("result", payload).get("chapters", payload) if isinstance(
            payload, dict
        ) else payload
        chapters.extend(found if isinstance(found, list) else [])

    wrote, skipped = 0, []
    for chapter in chapters:
        segments = chapter.get("final")
        if not isinstance(segments, list):
            skipped.append((chapter.get("hui"), ["no `final` segments"]))
            continue
        markdown = assemble_segments(segments)
        if not markdown.strip():
            skipped.append((chapter.get("hui"), ["every beat produced empty prose"]))
            continue
        problems = gate(chapter, markdown)
        if problems and not force:
            skipped.append((chapter.get("hui"), problems))
            continue
        target = chapter_path(cfg, chapter)
        if not target:
            skipped.append((chapter.get("hui"), ["cannot resolve the vault path"]))
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        title = f"# 回{chapter.get('hui'):02d}\n\n" if not markdown.startswith("#") else ""
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(title + markdown.rstrip() + "\n")
        wrote += 1

    print(f"harvested {wrote} chapters")
    if force and wrote:
        print("  (--force: the ship gate was bypassed)")
    if skipped:
        print(f"\nSKIPPED {len(skipped)} (use --force to write anyway):")
        for hui, problems in skipped:
            print(f"  回{hui}: {'; '.join(str(p) for p in problems)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
