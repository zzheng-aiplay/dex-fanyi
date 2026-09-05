"""Beat-plan prompt: segment a source chapter into beats and assign each a tier.

This is the reference two-tier scheme. A project's own editorial policy — what earns
which tier, how names are handled, how units convert, what the target voice is — comes
from its `config.json` and is interpolated below. This module supplies only the
mechanics: the beat concept, the two tiers, and the output contract.

Two tiers, and no more, deliberately: it is the smallest scheme that still lets a
translation be an abridgement rather than a transcription.

    FULL   render the beat completely; speech stays quoted speech
    BRIEF  compress the beat as far as the material allows, down to omitting it

A beat is the unit of curation: one to three sentences of source, a single dramatic
turn. Every beat's `zh_span` must tile the chapter with no gaps, so a later pass can
check the English against the exact source it came from.
"""

from __future__ import annotations

import json

from fanyi_dex.project import Project

# Tier vocabulary. Referenced by the engine (see `detscan.is_flattened` and the
# missing-prose check in the produce chain), so it is a durable contract: a plan and
# the passes that consume it must agree on these names.
TIER_FULL = "FULL"
TIER_BRIEF = "BRIEF"
TIERS = (TIER_FULL, TIER_BRIEF)

# Applied to a chapter a volume needs compressed harder than the default.
AGGRESSIVE_DIRECTIVE = (
    "Compress this chapter harder than usual: the volume is carrying more chapters than "
    "it has room for. Default a beat to BRIEF unless it turns the plot or contains a line "
    "worth quoting. Reduce ceremony, logistics, minor encounters, and digressions to a "
    "bridging sentence each. Keep speech quoted only where the words themselves matter. "
    "Never drop a plot-bearing event — tell it faster."
)

RETRY_NUDGE = (
    "\n\n(Your previous reply did not parse as one valid JSON object. "
    "Output ONLY the JSON object, all interior quotes/newlines escaped.)"
)


def _js_json(value: object) -> str:
    """JSON.stringify equivalent — non-ASCII stays raw."""
    return json.dumps(value, ensure_ascii=False)


def beatplan_prompt(project: Project, hui: int, zh: str, aggressive: bool = False) -> str:
    structure = project.cfg["structure"]
    cast = project.cfg["cast_tiers"]
    naming = project.cfg.get("naming", {}).get("courtesy_names", "")
    directive = AGGRESSIVE_DIRECTIVE if aggressive else ""

    return (
        f"You are the BEAT-PLANNER for an abridged English edition of {project.source_work}. "
        f"Do NOT translate. Segment chapter {hui} into a FLAT ORDERED LIST OF BEATS and assign "
        "EACH beat exactly one tier, judged fresh from the source.\n\n"
        + (
            f"*** COMPRESSION DIRECTIVE FOR THIS CHAPTER: ***\n{directive}\n\n"
            if directive
            else "Choose the beat count the chapter needs; a beat is one dramatic turn.\n\n"
        )
        + f"*** THE TWO TIERS: ***\n"
        f"{TIER_FULL} — render the beat completely; speech stays quoted speech.\n"
        f"{TIER_BRIEF} — compress the beat as far as the material allows, down to omitting it "
        "entirely (a beat that carries nothing a reader needs may return no prose at all).\n\n"
        f"*** WHICH TIER A BEAT EARNS (the project's policy): ***\n{structure['tier_handling']}\n\n"
        f"*** WHEN A BEAT MUST BE {TIER_FULL} REGARDLESS: ***\n{structure['verbatim_policy']}\n\n"
        f"*** CAST FLOOR (raises the minimum tier for beats centred on major figures): ***\n"
        f"{cast['floor_rule']}\n"
        f"MAJOR: {_js_json(cast['tier1'])}\nRECURRING: {_js_json(cast['tier2'])}\n"
        "Everyone else is unlisted: keep them NAMED — never fold a named participant into "
        "'a general' — but their beats are free to be compressed.\n\n"
        "FOR EACH BEAT return: beat (a one-line English synopsis of what happens), tier, and "
        "zh_span (the EXACT contiguous source it covers, copied verbatim — the spans in order "
        "must tile the whole chapter with no gaps).\n"
        "ALSO return: named (every named character, given names only, no courtesy names); "
        "verbatim_lines (the specific spoken lines, poems, or letters worth preserving word for "
        "word, quoted from your reading of the source).\n"
        "ALSO return uncertain: ONLY the genuinely borderline tier calls, typically none to a "
        "handful per chapter, where you hesitated between the two tiers. For each give: beat_zh "
        "(COPY the flagged beat's ENTIRE zh_span exactly as you wrote it in beats[], so it can be "
        "matched back — do not paraphrase or abbreviate), tier (your pick), alt_tier (the runner "
        "up), zh (the single most decision-relevant line, for display), and rationale_zh (why it "
        "is borderline, in Chinese). Do NOT report a numeric index. Do NOT flag clear-cut beats.\n"
        f"NAMING: {naming}\n"
        f"=== SOURCE (chapter {hui}) ===\n{zh}\n=== END ===\n"
        "*** OUTPUT FORMAT — CRITICAL ***\nReason through the chapter first, then output ONE JSON "
        "object and nothing after it. Do NOT echo the source back. Exactly these keys:\n"
        f'{{"hui": {hui}, "named": ["..."], "verbatim_lines": ["..."], '
        '"beats": [{"beat":"one-line English synopsis","tier":"'
        + "|".join(TIERS)
        + '","zh_span":"exact contiguous source"}], '
        '"uncertain": [{"beat_zh":"the flagged beat\'s ENTIRE zh_span","tier":"your pick",'
        '"alt_tier":"runner-up","zh":"one display line","rationale_zh":"why borderline, in Chinese"}]}\n'
        "Output ONLY that JSON object (a ```json fenced block is fine). "
        "Ensure valid JSON: escape quotes and newlines inside strings."
    )
