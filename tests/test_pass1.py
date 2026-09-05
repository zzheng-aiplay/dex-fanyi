from __future__ import annotations

import os

import json
from pathlib import Path

import pytest

from fanyi_dex import detscan, fake_agent
from fanyi_dex.pass1_flow import _reattach
from fanyi_dex.pass1_prompts import (
    LENSES,
    VoiceBlocks,
    access_prompt,
    audit_prompt,
    det_fix_prompt,
    dialogue_repair_prompt,
    pass1_prompt,
    pass2_prompt,
    remediate_prompt,
)
from fanyi_dex.project import Project, load_items_file
from fanyi_dex.prompts import TIER_BRIEF, TIER_FULL

# A real project's config, for the handful of tests that assert against one. Unset by
# default so a clone skips them instead of failing on a path it does not have.
TK_CONFIG = os.path.expanduser(os.environ.get("FANYI_TK_CONFIG", ""))
needs_tk = pytest.mark.skipif(
    not Path(TK_CONFIG).is_file(), reason="set FANYI_TK_CONFIG to run these"
)

CHAPTER = {
    "hui": 12,
    "local": 1,
    "named": ["Alpha", "Beta"],
    "verbatim_lines": ["「吾意已決。」"],
    "beats": [
        {"id": "12.1", "tier": "FULL", "beat": "opening", "zh_span": "話說天下大勢。"},
        {"id": "12.2", "tier": "FULL", "beat": "oath", "zh_span": "某曰：「吾意已決。」"},
    ],
    "zh": "話說天下大勢。某曰：「吾意已決。」",
}


# -- dialogue-flatten detection ------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "prose", "expected"),
    [
        (TIER_FULL, "He agreed.", True),
        # Narrating speech is correct at the compressed tier, so flagging it there is a
        # false positive.
        (TIER_BRIEF, "He agreed.", False),
        (TIER_BRIEF, "", False),
        (TIER_FULL, '"Good," he said.', False),
        (TIER_FULL, "「善。」他說。", False),
    ],
)
def test_flatten_detection_is_tier_aware(tier: str, prose: str, expected: bool) -> None:
    segment = {"tier": tier, "zh_span": "某曰：「善。」", "prose": prose}
    assert detscan.is_flattened(segment) is expected


def test_flatten_needs_speech_in_source() -> None:
    assert not detscan.is_flattened(
        {"tier": TIER_FULL, "zh_span": "話說天下大勢。", "prose": "The realm split."}
    )


def test_assemble_skips_empty_prose() -> None:
    segments = [{"prose": "one"}, {"prose": "   "}, {"prose": ""}, {"prose": "two"}]
    assert detscan.assemble(segments) == "one\n\ntwo"


# -- deterministic gate (reused from build_pass2.py) ---------------------------


@needs_tk
def test_gate_catches_each_defect_class() -> None:
    result = detscan.scan(
        TK_CONFIG,
        "He bade them go thirty li, duels with his tongue. A later poet wrote of it.",
    )
    assert "bade" in result["archaisms"]
    assert "with his tongue" in result["calques"]
    assert "thirty li" in result["unit_leaks"]
    assert result["poem_refs"]
    assert detscan.gate_count(result) >= 4


@needs_tk
def test_gate_is_clean_on_plain_modern_prose() -> None:
    assert detscan.gate_count(detscan.scan(TK_CONFIG, "He told them to ride three miles.")) == 0


@needs_tk
def test_gate_excludes_the_surname_zhang() -> None:
    """The unit zhang (丈) must not swallow the surname Zhang (張)."""
    assert detscan.scan(TK_CONFIG, "two Zhang Fei banners")["unit_leaks"] == []


@needs_tk
def test_gate_count_ignores_wrong_roman() -> None:
    """wrong_roman is reported but excluded from the count, matching the shipped gate."""
    result = {"archaisms": [], "calques": [], "unit_leaks": [], "poem_refs": [], "wrong_roman": ["X"]}
    assert detscan.gate_count(result) == 0


# -- segment re-attachment -----------------------------------------------------


def test_reattach_keeps_base_order_and_metadata() -> None:
    base = [
        {"beat_id": "a", "tier": "FULL", "zh_span": "甲", "prose": "old a"},
        {"beat_id": "b", "tier": "FULL", "zh_span": "乙", "prose": "old b"},
    ]
    returned = [{"beat_id": "b", "prose": "new b"}, {"beat_id": "a", "prose": "new a"}]
    out = _reattach(base, returned)
    assert [s["beat_id"] for s in out] == ["a", "b"]
    assert [s["prose"] for s in out] == ["new a", "new b"]
    assert out[0]["zh_span"] == "甲" and out[1]["tier"] == "FULL"


def test_reattach_keeps_previous_prose_for_a_dropped_segment() -> None:
    base = [
        {"beat_id": "a", "tier": "FULL", "zh_span": "甲", "prose": "keep me"},
        {"beat_id": "b", "tier": "FULL", "zh_span": "乙", "prose": "old b"},
    ]
    out = _reattach(base, [{"beat_id": "b", "prose": "new b"}])
    assert out[0]["prose"] == "keep me"


def test_reattach_ignores_unknown_beat_ids() -> None:
    base = [{"beat_id": "a", "tier": "FULL", "zh_span": "甲", "prose": "old"}]
    out = _reattach(base, [{"beat_id": "zzz", "prose": "invented"}])
    assert len(out) == 1 and out[0]["prose"] == "old"


# -- prompts -------------------------------------------------------------------


@needs_tk
def test_pass1_prompt_carries_the_dialogue_rule_and_beats() -> None:
    project = Project(TK_CONFIG)
    prompt = pass1_prompt(VoiceBlocks(project), CHAPTER)
    for marker in (
        "DIALOGUE IS A PROPERTY OF THE SOURCE",
        "*** HOW TO RENDER EACH TIER (the project's policy): ***",
        "WHEN A BEAT MUST BE FULL:",
        "NAMING POLICY:",
        "*** UNITS",
        "Chapter 12.",
        "12.2",
        "*** OUTPUT FORMAT",
    ):
        assert marker in prompt, marker


@needs_tk
def test_pass2_prompt_names_the_three_missed_things() -> None:
    project = Project(TK_CONFIG)
    prompt = pass2_prompt(
        VoiceBlocks(project),
        project,
        CHAPTER,
        [{"beat_id": "12.1", "tier": "FULL", "prose": "p"}],
    )
    assert "FLUENCY EDITOR" in prompt
    # The three habitual failure modes are named, without the worked examples that
    # would amount to publishing a project's house style.
    assert "Three things are missed most often" in prompt
    assert "carried into English word for word" in prompt
    assert "archaic or stilted diction" in prompt
    assert "chapter title" in prompt
    assert "Fidelity is absolute" in prompt


@needs_tk
def test_fidelity_lens_gets_untruncated_source_others_do_not() -> None:
    """Truncating the source for the fidelity lens caused false invention flags."""
    project = Project(TK_CONFIG)
    long_source = "話" * 500
    segments = [{"tier": "FULL", "zh_span": long_source, "prose": "x"}]
    fidelity = audit_prompt(project, LENSES[2], 86, "en", segments, long_source)
    calque = audit_prompt(project, LENSES[0], 86, "en", segments, long_source)
    assert "FULL SOURCE (authoritative)" in fidelity
    assert long_source in fidelity
    assert long_source not in calque
    assert "per segment, reference" in calque


@needs_tk
def test_dialogue_repair_prompt_differs_by_tier() -> None:
    project = Project(TK_CONFIG)
    blocks = VoiceBlocks(project)
    full = dialogue_repair_prompt(
        blocks, ["Alpha"], {"tier": TIER_FULL, "zh_span": "曰", "prose": "p"}
    )
    brief = dialogue_repair_prompt(
        blocks, ["Alpha"], {"tier": TIER_BRIEF, "zh_span": "曰", "prose": "p"}
    )
    assert "keep the whole exchange" in full
    assert "prune purely repetitive" in brief


@needs_tk
def test_every_prompt_requests_one_json_object() -> None:
    project = Project(TK_CONFIG)
    blocks = VoiceBlocks(project)
    segments = [{"beat_id": "12.1", "tier": "FULL", "prose": "p", "zh_span": "甲"}]
    det = {"calques": [], "archaisms": ["bade"], "unit_leaks": [], "poem_refs": []}
    prompts = [
        pass1_prompt(blocks, CHAPTER),
        dialogue_repair_prompt(blocks, [], segments[0]),
        pass2_prompt(blocks, project, CHAPTER, segments),
        audit_prompt(project, LENSES[0], 86, "en", segments, "src"),
        remediate_prompt(blocks, CHAPTER, segments, det),
        access_prompt(project, "A", "B"),
        det_fix_prompt(CHAPTER, segments, det),
    ]
    for prompt in prompts:
        assert "Output ONE JSON object" in prompt
        assert "escape quotes and newlines" in prompt


# -- items resolution ----------------------------------------------------------


def test_load_items_accepts_a_bare_list(tmp_path) -> None:
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"book": 1, "hui": 12, "beats": [{"id": "12.1"}]}]))
    assert len(load_items_file(path, 1)) == 1


def test_load_items_accepts_a_chapters_wrapper(tmp_path) -> None:
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps({"chapters": [{"book": 1, "hui": 12, "beats": [{"id": "12.1"}]}]})
    )
    assert len(load_items_file(path, 1)) == 1


def test_load_items_filters_by_book(tmp_path) -> None:
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps(
            [
                {"book": 1, "hui": 1, "beats": [{"id": "1.1"}]},
                {"book": 2, "hui": 12, "beats": [{"id": "12.1"}]},
            ]
        )
    )
    assert [c["hui"] for c in load_items_file(path, 2)] == [12]


def test_load_items_drops_beatless_chapters(tmp_path) -> None:
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"book": 1, "hui": 12, "beats": []}]))
    assert load_items_file(path, 5) == []


# -- fake agent routing --------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "key"),
    [
        ("pass2", "segments"),
        ("audit", "verdict"),
        ("access", "version_B_score"),
        ("dlg", "prose"),
        ("pass1", "segments"),
        ("remediate", "segments"),
        ("detfix", "segments"),
        ("beatplan", "beats"),
    ],
)
def test_fake_agent_routes_each_phase(phase: str, key: str) -> None:
    """Driven off _PHASE_MARKERS rather than hand-copied prompt text, which went stale
    the first time the prompts were reworded."""
    marker = fake_agent._PHASE_MARKERS[phase]
    assert key in json.loads(fake_agent.reply_for(marker))


def test_fake_agent_ignores_output_format_placeholder_ids() -> None:
    prompt = (
        f'{fake_agent._PHASE_MARKERS["pass1"]} Chapter 12 [{{"id": "12.1", "tier": "FULL"}}]'
        "\n*** OUTPUT FORMAT ***\n"
        '{"segments": [{"beat_id": "12.3", "tier": "FULL", "prose": "..."}]}'
    )
    segments = json.loads(fake_agent.reply_for(prompt))["segments"]
    assert [s["beat_id"] for s in segments] == ["12.1"]
