"""Prompts for the two-pass transcreation chain and its QA loop.

Pass 1 renders each beat faithfully from the source at its tier. Pass 2 rewrites that
for fluency without touching meaning. Then a small panel of independent auditors reads
the result, a remediation pass fixes what they flag, and a deterministic scan supplied
by the project has the last word.

As with the beat-plan prompt, the editorial substance is the project's: the target
voice, the fluency rules, the naming policy, the unit policy, and the blocklists all
arrive from `config.json` or from the project's own `build_pass2.py`. What lives here is
the mechanics — the passes, the two tiers, the lens structure, and the output contracts.

**Why no structured-output schemas.** A schema on every call at medium reasoning effort
is the request shape that stalls generation past a 180-second first-token watchdog. Each
prompt therefore asks for one JSON object as text and the caller parse-retries. That is
safe here because these run as separate processes with no such watchdog.
"""

from __future__ import annotations

import json
from typing import Any

from fanyi_dex.project import Project
from fanyi_dex.prompts import TIER_BRIEF, TIER_FULL, TIERS

JSON_TAIL = (
    "\n*** OUTPUT FORMAT — CRITICAL ***\nOutput ONE JSON object and nothing after it. "
    "Do NOT echo the source back. A ```json fenced block is fine. "
    "Ensure valid JSON: escape quotes and newlines inside strings.\nExactly this shape:\n"
)

_TIER_UNION = "|".join(TIERS)


def _j(value: Any) -> str:
    """JSON.stringify equivalent — non-ASCII stays raw."""
    return json.dumps(value, ensure_ascii=False)


class VoiceBlocks:
    """The project's own policy, read once and fed to every prompt.

    None of this is written in this repository. A project that leaves a block empty gets
    a prompt without that instruction, which is usually a mistake — `structure.units` in
    particular is load-bearing rather than stylistic, because without it a model relabels
    measurements 1:1 by name and inflates every one of them.
    """

    def __init__(self, project: Project) -> None:
        voice = project.cfg["voice"]
        structure = project.cfg["structure"]
        self.voice = voice["target"]
        self.fluency = voice["_pass2_fluency"]
        self.verbs = voice.get("verbs", "")
        self.emdash = voice.get("em_dashes", "")
        self.cuttr = voice.get("always_cut_translationese", "")
        self.core = voice.get("core_principle", "")
        self.naming = project.cfg["naming"].get("courtesy_names", "")
        self.tierh = structure["tier_handling"]
        self.policy = structure["verbatim_policy"]
        self.units = structure.get("units", "")


# -- Pass 1: faithful transcreation -------------------------------------------


def pass1_prompt(blocks: VoiceBlocks, chapter: dict[str, Any]) -> str:
    return (
        "Render this whole chapter into English, working beat by beat from the plan and applying "
        "each beat's tier. This is a retelling in strong prose, not a line-by-line rewrite. Keep "
        "every named participant named. Render idioms as an English author would, not word for "
        "word. Invent nothing: no added events, dates, numbers, or interiority.\n"
        "*** THE FIRST RULE — DIALOGUE IS A PROPERTY OF THE SOURCE, NOT OF THE TIER. ***\n"
        "If a beat's source is direct speech, the English must be direct speech too: quoted lines "
        f"with speaker tags and a real back-and-forth. At {TIER_FULL} keep the whole exchange "
        f"quoted. Only {TIER_BRIEF} may replace speech with narration. A {TIER_FULL} beat whose "
        "source is speech but whose prose has no quotation marks is wrong. Interior thought "
        "correctly stays narrated.\n"
        f"*** HOW TO RENDER EACH TIER (the project's policy): ***\n{blocks.tierh}\n"
        f"WHEN A BEAT MUST BE {TIER_FULL}:\n{blocks.policy}\nNAMING POLICY:\n{blocks.naming}\n"
        f"*** UNITS — convert by the true ratio, never relabel 1:1: ***\n{blocks.units}\n"
        f"TARGET VOICE:\n{blocks.voice}\n"
        f"Chapter {chapter['hui']}. Named (keep all): {_j(chapter.get('named', []))}\n"
        f"Lines to preserve word for word (quote them): {_j(chapter.get('verbatim_lines', []))}\n"
        "PLAN BEATS in order (each has id, tier, a synopsis, and its source span):\n"
        f"{_j(chapter.get('beats', []))}\n"
        "For EACH beat emit one segment: beat_id (copy the beat's id — a short label like \"12.3\", "
        "not the source text), tier (copy the beat's tier), prose (the English; an empty string is "
        f"allowed only for a {TIER_BRIEF} beat the chapter does not need). Do the beats IN ORDER. "
        "Keep the \"## \" chapter heading as the first segment's prose."
        + JSON_TAIL
        + f'{{"hui": {chapter["hui"]}, "title_en": "English chapter title", '
        '"segments": [{"beat_id": "12.3", "tier": "' + _TIER_UNION + '", "prose": "..."}], '
        '"words_after": 0}'
    )


# -- Pass 1b: dialogue repair --------------------------------------------------


def dialogue_repair_prompt(
    blocks: VoiceBlocks, named: list[str], segment: dict[str, Any]
) -> str:
    tier = segment.get("tier")
    tier_clause = (
        f"{TIER_FULL} — keep the whole exchange, every speech quoted, nothing collapsed."
        if tier == TIER_FULL
        else f"{TIER_BRIEF} — give each named speaker's turn as an actual quoted line, which may "
        "be a single crisp sentence; prune purely repetitive volleys, but it must read as people "
        "speaking."
    )
    return (
        "Repair ONE segment that flattened dialogue into narration. The source is direct speech; "
        "the current English narrates it. Re-render as real dialogue: quoted lines with speaker "
        f"tags, in the original order and give-and-take. Tier {tier}: {tier_clause}"
        " Keep the target voice, follow the naming policy, render idioms naturally, invent "
        "nothing.\n"
        f"Named (keep all): {_j(named)}\n"
        f"=== SOURCE ===\n{segment.get('zh_span', '')}\n"
        f"=== CURRENT (flattened) ===\n{segment.get('prose', '')}\n=== END ===\n"
        "Return ONLY the corrected dialogue-preserving prose for this one segment."
        + JSON_TAIL
        + '{"prose": "the corrected prose"}'
    )


# -- Pass 2: fluency -----------------------------------------------------------


def pass2_prompt(
    blocks: VoiceBlocks, project: Project, chapter: dict[str, Any], p1: list[dict[str, Any]]
) -> str:
    payload = [
        {"beat_id": s["beat_id"], "tier": s.get("tier"), "pass1": s.get("prose", "")}
        for s in p1
    ]
    return (
        f"You are the FLUENCY EDITOR for a published English {project.source_work}. The input is a "
        "faithful, dialogue-preserving first pass: segments, each tagged with its tier. Rewrite "
        "each segment's prose into the shipping voice under the project's fluency rules. Rewrite "
        "assertively — a segment that comes back nearly unchanged is a failure.\n"
        "Three things are missed most often, so check for them specifically: a source idiom "
        "carried into English word for word instead of by its meaning; archaic or stilted diction, "
        "especially inside dialogue; and the chapter title, which is prose too and gets the same "
        "treatment as the body.\n"
        f"FLUENCY RULES:\n{blocks.fluency}\nALWAYS CUT: {blocks.cuttr}\n"
        f"CORE PRINCIPLE: {blocks.core}\nVERBS: {blocks.verbs}\nEM DASHES: {blocks.emdash}\n"
        f"UNITS — keep the true-ratio conversions the first pass made:\n{blocks.units}\n"
        f"TARGET VOICE:\n{blocks.voice}\n"
        "*** PRESERVE — fluency must not break these: ***\n"
        f"- Dialogue stays dialogue at its tier. {blocks.tierh}\n"
        "  Naturalize the wording of quoted lines but keep them quoted with speaker tags. Do not "
        "collapse a quoted exchange back into narration.\n"
        "- Fidelity is absolute: every event, name, number, and line-meaning preserved. Invent "
        "nothing. Follow the naming policy the first pass followed.\n"
        "- Return one segment per input segment, same beat_id and same tier; change only prose. A "
        "segment that came in with empty prose goes out empty.\n"
        f"Chapter {chapter['hui']}. Named: {_j(chapter.get('named', []))}\n"
        f"=== SEGMENTS (beat_id | tier | first-pass prose) ===\n{_j(payload)}\n=== END ==="
        + JSON_TAIL
        + f'{{"hui": {chapter["hui"]}, "segments": [{{"beat_id": "12.3", "tier": "{TIER_FULL}", '
        '"prose": "the rewritten prose"}]}'
    )


# -- QA: the audit panel -------------------------------------------------------

# Three lenses, run independently so each is blind to what the others find. The
# substance of what counts as an offence is the project's: the CALQUE and ARCHAISM
# lenses are checked again afterwards by the project's own deterministic blocklists,
# which have the last word.
LENSES: tuple[dict[str, str], ...] = (
    {
        "name": "CALQUE",
        "job": "Find every place a source idiom or image has been reproduced word for word "
        "instead of rendered by its meaning in natural English. For each: the phrase, the source "
        "expression if you can identify it, and a natural rewrite. Report in 'calques'.",
    },
    {
        "name": "ARCHAISM",
        "job": "Find every archaic, stilted, or translationese phrase a modern reader trips on — "
        "inverted word order, archaic vocabulary, courtly circumlocution, stiff narration — "
        "especially inside dialogue. For each: the phrase, why it is stiff, and a natural "
        "rewrite. Report in 'archaisms'.",
    },
    {
        "name": "FIDELITY",
        "job": "Compare against the full source above. Report in 'fidelity_issues' any changed, "
        "dropped, added, or invented event, name, number, or title — but ONLY where it genuinely "
        "conflicts with the source; do not flag content that IS in the source. Report in "
        f"'flattened' any {TIER_FULL} segment whose source is direct speech but whose English "
        "narrates it. Interior thought staying narrated is correct, not a defect.",
    },
)


def audit_prompt(
    project: Project,
    lens: dict[str, str],
    hui: int,
    english: str,
    segments: list[dict[str, Any]],
    full_source: str,
) -> str:
    # The fidelity lens gets the whole source untruncated. Giving it only the head of each
    # span made it report invention that was in fact in the source.
    if lens["name"] == "FIDELITY":
        source_block = f"=== FULL SOURCE (authoritative) ===\n{full_source}\n"
    else:
        heads = [
            {"tier": s.get("tier"), "zh": (s.get("zh_span") or "")[:120]} for s in segments
        ]
        source_block = f"=== SOURCE HEADS (per segment, reference) ===\n{_j(heads)}\n"
    return (
        f"You are an INDEPENDENT ADVERSARIAL AUDITOR for a published English "
        f"{project.source_work} — the {lens['name']} lens. {lens['job']}\n"
        "Be exhaustive and specific: quote the exact offending phrase. Return empty arrays if the "
        "chapter is genuinely clean; do not invent problems. verdict=CLEAN only if your lens found "
        "nothing, otherwise ATTENTION.\n"
        f"Chapter {hui}.\n"
        + source_block
        + f"=== ENGLISH ===\n{english}\n=== END ==="
        + JSON_TAIL
        + f'{{"hui": {hui}, "calques": [{{"phrase": "...", "chinese": "...", "natural": "..."}}], '
        '"archaisms": [{"phrase": "...", "why": "...", "natural": "..."}], '
        '"flattened": ["..."], "fidelity_issues": ["..."], "verdict": "CLEAN|ATTENTION"}'
    )


# -- QA: remediation -----------------------------------------------------------


def remediate_prompt(
    blocks: VoiceBlocks,
    chapter: dict[str, Any],
    segments: list[dict[str, Any]],
    merged: dict[str, list[Any]],
) -> str:
    payload = [
        {"beat_id": s["beat_id"], "tier": s.get("tier"), "prose": s.get("prose", "")}
        for s in segments
    ]
    return (
        "REMEDIATION. Fix ONLY the flagged problems below, preserving every fact, keeping dialogue "
        "as dialogue, and keeping the segment structure — same beat_id, same order, same tier; "
        "change only prose.\n"
        f"CALQUES to render idiomatically: {_j(merged.get('calques', []))}\n"
        f"ARCHAISMS to modernize: {_j(merged.get('archaisms', []))}\n"
        f"FLATTENED dialogue to restore as quoted speech: {_j(merged.get('flattened', []))}\n"
        "FIDELITY issues to correct against the source, only where genuinely wrong: "
        f"{_j(merged.get('fidelity_issues', []))}\n"
        f"FLUENCY RULES:\n{blocks.fluency}\nVERBS: {blocks.verbs}\n"
        f"TARGET VOICE:\n{blocks.voice}\n"
        f"Chapter {chapter['hui']}. Named: {_j(chapter.get('named', []))}\n"
        f"=== CURRENT SEGMENTS (beat_id | tier | prose) ===\n{_j(payload)}\n=== END ==="
        + JSON_TAIL
        + f'{{"hui": {chapter["hui"]}, "segments": [{{"beat_id": "12.3", "tier": "{TIER_FULL}", '
        '"prose": "the corrected prose"}]}'
    )


# -- QA: the accessibility gate ------------------------------------------------


def access_prompt(project: Project, version_a: str, version_b: str) -> str:
    return (
        "You are a NEWCOMER READER who reads literary fiction and knows nothing of "
        f"{project.source_work} or of Chinese classics. Score TWO versions of the SAME chapter for "
        "ACCESSIBILITY on a 1-5 scale, where 5 is an effortless page-turner, 3 is competent but a "
        "slog, and 1 bounces off. Judge only readability for a true newcomer — clarity, flow, "
        "idiom that reads as natural English, momentum — and not fidelity. Score each version "
        "independently; do not inflate the second because it came second.\n"
        f"=== VERSION A ===\n{version_a}\n"
        f"=== VERSION B ===\n{version_b}\n=== END ==="
        + JSON_TAIL
        + '{"version_A_score": 3, "version_B_score": 4, "version_A_reason": "...", '
        '"version_B_reason": "...", "top_friction_B": "..."}'
    )


# -- QA: the deterministic targeted fix ---------------------------------------


def det_fix_prompt(
    chapter: dict[str, Any], segments: list[dict[str, Any]], det: dict[str, list[str]]
) -> str:
    payload = [
        {"beat_id": s["beat_id"], "tier": s.get("tier"), "prose": s.get("prose", "")}
        for s in segments
    ]
    return (
        "Final targeted line-fix. The defects below were found by a deterministic scan and must be "
        "fixed, preserving meaning and dialogue form:\n"
        f"CALQUES (render idiomatically): {_j(det.get('calques', []))}\n"
        f"ARCHAISMS (modernize): {_j(det.get('archaisms', []))}\n"
        "UNCONVERTED UNITS (convert by the true ratio for the period, never relabel 1:1): "
        f"{_j(det.get('unit_leaks', []))}\n"
        "NARRATOR ASIDES ABOUT POEMS (remove entirely rather than summarizing): "
        f"{_j(det.get('poem_refs', []))}\n"
        "Fix ONLY these; keep everything else exactly as it is. Same beat_id and tier; change "
        "prose only where a listed phrase appears.\n"
        f"=== SEGMENTS (beat_id | tier | prose) ===\n{_j(payload)}\n=== END ==="
        + JSON_TAIL
        + f'{{"hui": {chapter["hui"]}, "segments": [{{"beat_id": "12.3", "tier": "{TIER_FULL}", '
        '"prose": "the corrected prose"}]}'
    )
