#!/usr/bin/env python3
"""Anchor a planner-flagged uncertain tier call back to the beat it came from.

`fanyi_dex/book/finishing.py` imports `anchor_beat` from beside the project's
`config.json`. Only that one function is used, and only its `main()` is avoided — a
real project's version of this file also writes review artifacts as a side effect,
which the Flow deliberately does not trigger.

Why it exists at all: a planner reports which beat it hesitated over by index, and that
index is wrong often enough that trusting it mislabels roughly a quarter of the flags.
So the flag carries a copy of the beat's Chinese instead, and the beat is found by
content.

    anchor_beat(text, beats) -> (index | None, method)

`method` is a label for how it matched, carried through to the review artifact so a
human can see which anchors were exact and which were fuzzy.
"""

from __future__ import annotations

import sys
from typing import Any


def cjk(text: str) -> int:
    """Count CJK characters — the unit both coverage and matching are measured in."""
    return sum(1 for ch in text or "" if "一" <= ch <= "鿿")


def _normalise(text: str) -> str:
    """Drop everything that a paraphrase or a re-wrap could have changed."""
    return "".join(ch for ch in text or "" if "一" <= ch <= "鿿")


def anchor_beat(text: str, beats: list[dict[str, Any]]) -> tuple[int | None, str]:
    """Find which beat `text` belongs to, by content.

    Tried in descending order of confidence, so the method label means something:

    1. `exact`      — the flag's Chinese is byte-identical to a beat's span.
    2. `normalised` — identical once punctuation and spacing are stripped.
    3. `contains`   — the flag's Chinese sits inside a beat's span, or vice versa.
    4. `overlap`    — the longest shared run of characters, if it is long enough.

    Returns `(None, "unresolved")` rather than guessing, because a wrong anchor puts a
    human's tier decision on the wrong beat.
    """
    needle = text or ""
    if not needle.strip() or not beats:
        return None, "unresolved"

    spans = [b.get("zh_span", "") or "" for b in beats]

    for index, span in enumerate(spans):
        if span and span == needle:
            return index, "exact"

    flat = _normalise(needle)
    if not flat:
        return None, "unresolved"

    normalised = [_normalise(span) for span in spans]
    for index, span in enumerate(normalised):
        if span and span == flat:
            return index, "normalised"

    for index, span in enumerate(normalised):
        if span and (flat in span or span in flat):
            return index, "contains"

    best_index, best_run = None, 0
    for index, span in enumerate(normalised):
        run = _longest_common_run(flat, span)
        if run > best_run:
            best_index, best_run = index, run
    # Require a run long enough to be a real match rather than a coincidence of
    # common characters.
    if best_index is not None and best_run >= max(8, len(flat) // 3):
        return best_index, "overlap"
    return None, "unresolved"


def _longest_common_run(left: str, right: str) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


if __name__ == "__main__":
    print(
        "This fixture exposes anchor_beat() and cjk() for import. It has no main(): the "
        "Flow imports the function and writes its own review artifact.",
        file=sys.stderr,
    )
