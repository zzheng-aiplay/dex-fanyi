"""Canned replies for FANYI_FAKE_AGENT, one per prompt shape.

Lets the whole graph be walked without spending on Claude. The replies are
deliberately *defective* in specific ways so the conditional branches actually
execute: a flattened dialogue beat, an audit finding, and a blocklisted archaism
that trips the deterministic ship gate.
"""

from __future__ import annotations

import json
import os
import re

from fanyi_dex.prompts import TIER_BRIEF, TIER_FULL

# `"id": "86.3"` in a beat plan, `"beat_id": "86.3"` in a segment list.
_BEAT_ID_RE = re.compile(r'"(?:beat_)?id"\s*:\s*"([^"]+)"')
_TIER_RE = re.compile(r'"tier"\s*:\s*"(FULL|BRIEF)"')
_HUI_RE = re.compile(r'"hui"\s*:\s*(\d+)|Chapter (\d+)|hui (\d+)')

# Which phase a prompt belongs to, sniffed from a distinctive phrase. These have to
# track the prompt text, and once did not: rewording three prompt openings left three
# markers stale, every pass-1 call fell through to the catch-all reply, and the produce
# chain failed validation three times per chapter with nothing in the log naming the
# cause. `tests/test_book.py::test_the_fake_agent_answers_every_phase` now asserts every
# marker still matches the prompt it belongs to.
_PHASE_MARKERS = {
    "beatplan": "BEAT-PLANNER",
    "pass1": "Render this whole chapter into English",
    "dlg": "Repair ONE segment",
    "pass2": "FLUENCY EDITOR",
    "audit": "ADVERSARIAL AUDITOR",
    "remediate": "REMEDIATION.",
    "access": "NEWCOMER READER",
    "detfix": "Final targeted line-fix",
}


def _hui(prompt: str) -> int:
    match = _HUI_RE.search(prompt)
    if not match:
        return 0
    return int(next(g for g in match.groups() if g))


def _ids_and_tiers(prompt: str) -> list[tuple[str, str]]:
    # Only the payload, never the OUTPUT FORMAT example's placeholder ids.
    body = prompt.split("*** OUTPUT FORMAT")[0]
    ids = _BEAT_ID_RE.findall(body)
    tiers = _TIER_RE.findall(body)
    out = []
    for index, beat_id in enumerate(ids):
        out.append((beat_id, tiers[index] if index < len(tiers) else TIER_FULL))
    return out


def _segments(prompt: str, *, flatten_speech: bool, seed_archaism: bool) -> list[dict]:
    segments = []
    for index, (beat_id, tier) in enumerate(_ids_and_tiers(prompt)):
        if tier == TIER_BRIEF and index % 3 == 2:
            # A compressed beat is allowed to come back empty; emit one so the
            # missing-prose check is exercised on the tier where it is legitimate.
            prose = ""
        elif flatten_speech and index == 1:
            # No quotation marks: trips the dialogue-flatten detector when the
            # matching zh_span carries 曰 / 「.
            prose = "He told them his mind was made up and they agreed."
        elif seed_archaism and index == 0:
            # "bade" is on the project's archaism blocklist, so the deterministic
            # ship gate must catch it and run the targeted fix.
            prose = "## A Test Chapter\n\nHe bade them ride out at dawn."
        else:
            prose = f'"So it stands," he said. Beat {beat_id} rendered at tier {tier}.'
        segments.append({"beat_id": beat_id, "tier": tier, "prose": prose})
    return segments


def reply_for(prompt: str) -> str:
    """Pick a canned reply by sniffing which phase's prompt this is."""
    hui = _hui(prompt)

    # Test hook: FANYI_FAKE_FAIL_PHASE=pass2 makes that phase return unparseable
    # text, so the parse-retry exhausts and the Step's failure route is exercised.
    fail = os.environ.get("FANYI_FAKE_FAIL_PHASE", "")
    if fail and _PHASE_MARKERS.get(fail, "\0") in prompt:
        return "I am afraid I cannot comply with that request."

    if _PHASE_MARKERS["beatplan"] in prompt:
        return json.dumps(
            {
                "hui": hui,
                "named": ["DryRun"],
                "verbatim_lines": [],
                "beats": [{"beat": "dry-run beat", "tier": TIER_FULL, "zh_span": "占位"}],
                "uncertain": [],
            },
            ensure_ascii=False,
        )

    if _PHASE_MARKERS["dlg"] in prompt:
        return json.dumps(
            {"prose": '"My mind is made up," he said. "Ride at dawn." They agreed.'},
            ensure_ascii=False,
        )

    if _PHASE_MARKERS["audit"] in prompt:
        # One calque on the CALQUE lens only, so Remediate runs exactly once.
        calques = (
            [{"phrase": "a calque left in", "chinese": "占位", "natural": "the natural rewrite"}]
            if "CALQUE LENS" in prompt
            else []
        )
        return json.dumps(
            {
                "hui": hui,
                "calques": calques,
                "archaisms": [],
                "flattened": [],
                "fidelity_issues": [],
                "verdict": "ATTENTION" if calques else "CLEAN",
            },
            ensure_ascii=False,
        )

    if _PHASE_MARKERS["access"] in prompt:
        return json.dumps(
            {
                "version_A_score": 3,
                "version_B_score": 4,
                "version_A_reason": "faithful but stiff",
                "version_B_reason": "reads naturally",
                "top_friction_B": "none material",
            },
            ensure_ascii=False,
        )

    if _PHASE_MARKERS["detfix"] in prompt:
        # Must clear the archaism, or the gate would never go clean.
        return json.dumps(
            {"hui": hui, "segments": _segments(prompt, flatten_speech=False, seed_archaism=False)},
            ensure_ascii=False,
        )

    if _PHASE_MARKERS["remediate"] in prompt:
        return json.dumps(
            {"hui": hui, "segments": _segments(prompt, flatten_speech=False, seed_archaism=True)},
            ensure_ascii=False,
        )

    if _PHASE_MARKERS["pass2"] in prompt:
        return json.dumps(
            {"hui": hui, "segments": _segments(prompt, flatten_speech=False, seed_archaism=True)},
            ensure_ascii=False,
        )

    if _PHASE_MARKERS["pass1"] in prompt:
        return json.dumps(
            {
                "hui": hui,
                "title_en": "A Test Chapter",
                "segments": _segments(prompt, flatten_speech=True, seed_archaism=False),
                "words_after": 120,
            },
            ensure_ascii=False,
        )

    return json.dumps({"ok": True, "unmatched_prompt": True})
