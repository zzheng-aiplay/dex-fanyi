"""Unit coverage for the end-to-end volume Flow's pure parts.

The durable behaviour (waits, SubFlow batching, gates, recovery) is verified by
`tests/integration_book.py` against a real `dexcli dev` + Worker, because none of
it is observable without them.
"""

from __future__ import annotations

import json

from fanyi_dex.book import finishing
from fanyi_dex.book.model import (
    ChapterOutcome,
    Manifest,
    RunPlan,
    StageRef,
    coverage,
    ints,
    join,
    key,
    loads,
)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


def test_stage_ref_defaults_are_a_valid_start_input():
    ref = StageRef(config_path="/tmp/config.json", book=1)
    assert ref.stage == "init"
    assert ref.pending == ""


def test_run_plan_hui_list_round_trips_the_cursor():
    plan = RunPlan(chapters=join([1, 2, 3]))
    assert plan.chapters == "1,2,3"
    assert plan.hui_list == [1, 2, 3]


def test_ints_tolerates_blanks_and_spaces():
    assert ints(" 1, 2 ,,3 ") == [1, 2, 3]
    assert ints("") == []


def test_key_sorts_lexically_in_chapter_order():
    keys = [key(h) for h in (1, 2, 10, 100)]
    assert keys == sorted(keys)


def test_coverage_is_the_share_of_source_the_beats_tile():
    zh = "一二三四五六七八九十"
    beats = [{"zh_span": "一二三四五"}, {"zh_span": "六七"}]
    assert coverage(beats, zh) == 70
    assert coverage(beats, "") == 0


def test_loads_returns_the_fallback_on_junk():
    assert loads("not json", {"ok": False}) == {"ok": False}
    assert loads("", None) is None


# --------------------------------------------------------------------------
# approved items — the handoff v1 left to a human
# --------------------------------------------------------------------------


PLAN = {
    "book": 1,
    "hui": 7,
    "local": 7,
    "zh": "甲乙丙",
    "named": ["Alpha"],
    "verbatim_lines": ["某之一言"],
    "beats": [
        {"beat": "one", "tier": "FULL", "zh_span": "甲"},
        {"beat": "two", "tier": "FULL", "zh_span": "乙"},
        {"beat": "three", "tier": "BRIEF", "zh_span": "丙"},
    ],
    "uncertain": [],
}


def test_stage_item_assigns_the_canonical_one_based_beat_ids():
    item = finishing.stage_item(PLAN, {})
    assert [b["id"] for b in item["beats"]] == ["7.1", "7.2", "7.3"]
    assert item["zh"] == "甲乙丙"
    assert item["verbatim_lines"] == ["某之一言"]


def test_stage_item_applies_a_director_tier_override():
    item = finishing.stage_item(PLAN, {"7.2": "FULL", "7.3": "BRIEF"})
    tiers = {b["id"]: b["tier"] for b in item["beats"]}
    assert tiers == {"7.1": "FULL", "7.2": "FULL", "7.3": "BRIEF"}


def test_stage_item_keeps_unflipped_tiers():
    item = finishing.stage_item(PLAN, {"7.99": "BRIEF"})
    assert [b["tier"] for b in item["beats"]] == ["FULL", "FULL", "BRIEF"]


def test_stage_item_omits_verse_keys_the_plan_did_not_carry():
    item = finishing.stage_item(PLAN, {})
    assert "is_verse" not in item["beats"][0]
    verse = {**PLAN, "beats": [{**PLAN["beats"][0], "is_verse": True, "verse_kind": "shi"}]}
    assert finishing.stage_item(verse, {})["beats"][0]["is_verse"] is True


# --------------------------------------------------------------------------
# transport between parent and chapter SubFlows
# --------------------------------------------------------------------------


def test_read_json_is_none_for_a_missing_or_broken_export(tmp_path):
    assert finishing.read_json("") is None
    assert finishing.read_json(str(tmp_path / "nope.json")) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert finishing.read_json(str(broken)) is None


def test_read_json_reads_an_export(tmp_path):
    path = tmp_path / "chapter.json"
    path.write_text(json.dumps({"hui": 3}), encoding="utf-8")
    assert finishing.read_json(str(path)) == {"hui": 3}


def test_chapter_outcome_carries_a_path_not_a_payload():
    """SDK 0.2.5 does not hydrate a blob-backed value on a SubFlow completion, so a
    large field here fails every parent Step with 'was not hydrated'."""
    fields = ChapterOutcome().__dict__
    assert "export_path" in fields
    assert not any(name.endswith("payload_json") for name in fields)


def test_chapters_payload_is_the_shape_harvest_reprocess_consumes():
    payload = json.loads(finishing.chapters_payload([{"hui": 1, "final": []}]))
    assert list(payload) == ["chapters"]
    assert payload["chapters"][0]["hui"] == 1


# --------------------------------------------------------------------------
# script-output parsing
# --------------------------------------------------------------------------


def test_harvest_tally_reads_the_script_report():
    """Matched against harvest_reprocess.py's real wording, not a guess at it."""
    assert finishing.harvest_tally(
        "harvested 3 chapters\n  Book 1: 3 chapters, 0 footnotes total\n"
        "\nSKIPPED 1 (verdict != PASS; use --force to write anyway):"
    ) == (3, 1)
    assert finishing.harvest_tally("harvested 3 chapters") == (3, 0)
    assert finishing.harvest_tally("nothing to say") == (0, 0)


def test_scrape_int_finds_the_page_count():
    assert finishing.scrape_int("built 20 pages, even", r"(\d+)\s+pages") == 20
    assert finishing.scrape_int("no count", r"(\d+)\s+pages") == 0


def test_epubcheck_verdict_maps_exit_code_and_keeps_the_errors():
    assert finishing.epubcheck_verdict("", "", 0) == ("pass", "no errors")
    verdict, detail = finishing.epubcheck_verdict("", "ERROR(RSC-005): bad", 1)
    assert verdict == "fail"
    assert "RSC-005" in detail


def test_one_line_drops_grpc_noise_and_keeps_failures():
    tail = (
        "I0904 22:01:12.108329 6515656 ev_poll_posix.cc:593] FD from fork parent\n"
        "    [ok  ] all fonts embedded\n"
        "    [FAIL] page count within cream 24-776 - 20 pages\n"
    )
    line = finishing.one_line(tail)
    assert "ev_poll_posix" not in line
    assert "[FAIL] page count" in line
    assert "\n" not in line


def test_one_line_falls_back_to_everything_when_nothing_failed():
    assert finishing.one_line("    [ok  ] all fonts embedded") == "[ok  ] all fonts embedded"


def test_existing_returns_the_first_present_path(tmp_path):
    present = tmp_path / "there.md"
    present.write_text("x", encoding="utf-8")
    assert finishing.existing(tmp_path / "missing.md", present) == str(present)
    assert finishing.existing(tmp_path / "missing.md") == ""


def test_manifest_starts_with_checks_unrun():
    manifest = Manifest()
    assert manifest.epubcheck == "not-run"
    assert manifest.preflight == "not-run"
    assert manifest.pages == 0


# --------------------------------------------------------------------------
# Regressions found by the graph audit
# --------------------------------------------------------------------------


def test_the_two_chapter_export_maps_are_distinct_and_registered():
    """One shared map let a produce wave overwrite the beat-plan path under the same
    key, so re-entering the director gate after produce read a finished chapter as a
    plan — breaking exactly the recovery paths."""
    from fanyi_dex.book import book_flow

    assert book_flow.chapter_plans.name != book_flow.chapter_records.name
    schema = _schema_names()
    assert book_flow.chapter_plans.name in schema
    assert book_flow.chapter_records.name in schema
    assert book_flow.tier_overrides.name in schema


def _schema_names() -> set[str]:
    from fanyi_dex.book import book_flow
    from fanyi_dex.book.curate_chapter import CurateChapterFlow
    from fanyi_dex.book.produce_chapter import ProduceChapterFlow
    from fanyi_dex.config import Config

    config = Config.from_env()
    flow = book_flow.BookFlow(
        config, CurateChapterFlow(config), ProduceChapterFlow(config)
    )
    schema = flow.get_persistence_schema()
    return {
        d.name
        for group in (schema.attributes, schema.channels, schema.streams)
        for d in group
    }


def test_every_definition_the_module_declares_is_in_the_schema():
    """An Attribute or Channel a Step touches but the schema omits is rejected at
    runtime, not at import."""
    from dex import Attribute, AttributeMap, Channel, ChannelMap, Stream

    from fanyi_dex.book import book_flow

    declared = {
        value.name
        for value in vars(book_flow).values()
        if isinstance(value, (Attribute, AttributeMap, Channel, ChannelMap, Stream))
    }
    assert declared and declared <= _schema_names()


def test_tier_overrides_are_read_from_the_attribute_not_the_channel():
    """`Channel.results()` only returns values to the Step execution whose wait
    consumed them, so reading it from a later Step silently discarded every
    --tier-override."""
    import inspect

    from fanyi_dex.book import book_flow

    source = inspect.getsource(book_flow._tier_overrides)
    body = source.split('"""')[-1]  # skip the docstring, which names the old bug
    assert "tier_overrides.get(context)" in body
    assert "approvals.results" not in body
    # And the Step that DID consume the approval must commit it.
    gate = inspect.getsource(book_flow.DirectorGate)
    assert "tier_overrides.set(context, decision.payload" in gate


def test_every_book_step_but_the_recovery_gate_routes_failures_to_it():
    """DESIGN.md claims 'any stage -> RecoveryGate'. Five Steps had no route at all, so
    a deterministic exception there burned the retry budget and failed the volume."""
    from fanyi_dex.book import book_flow
    from fanyi_dex.book.curate_chapter import CurateChapterFlow
    from fanyi_dex.book.produce_chapter import ProduceChapterFlow
    from fanyi_dex.config import Config

    config = Config.from_env()
    flow = book_flow.BookFlow(
        config, CurateChapterFlow(config), ProduceChapterFlow(config)
    )
    unrouted = []
    for name, step in vars(flow).items():
        if not hasattr(step, "get_step_options"):
            continue
        options = step.get_step_options()
        target = getattr(options, "_execute_failure_target", None)
        if target is None:
            unrouted.append(type(step).__name__)
    assert unrouted == ["RecoveryGate"], unrouted


def test_the_recovery_gate_can_reach_every_stage_including_init():
    import inspect

    from fanyi_dex.book import book_flow, model

    source = inspect.getsource(book_flow.RecoveryGate.execute)
    stages = [
        model.INIT,
        model.CURATING,
        model.DIRECTOR_GATE,
        model.APPROVING_ITEMS,
        model.PRODUCING,
        model.QA_GATE,
        model.HARVESTING,
        model.ASSEMBLING,
        model.PRINTING,
        model.CHECKING,
        model.PROOF_GATE,
    ]
    names = {
        "init": "INIT",
        "curating": "CURATING",
        "director-gate": "DIRECTOR_GATE",
        "approving-items": "APPROVING_ITEMS",
        "producing": "PRODUCING",
        "qa-gate": "QA_GATE",
        "harvesting": "HARVESTING",
        "assembling": "ASSEMBLING",
        "printing": "PRINTING",
        "checking": "CHECKING",
        "proof-gate": "PROOF_GATE",
    }
    for stage in stages:
        assert f"target == {names[stage]}" in source, stage


def test_the_proof_gate_refuses_to_sign_off_on_an_incomplete_volume():
    assert finishing.missing_final_artifacts(3, "/x/master.md", "/x/i.pdf") == []
    assert finishing.missing_final_artifacts(0, "/x/master.md", "/x/i.pdf") == [
        "harvested chapters"
    ]
    assert finishing.missing_final_artifacts(0, "", "") == [
        "harvested chapters",
        "master markdown",
        "interior PDF",
    ]


def test_resume_targets_cover_every_stage_the_recovery_gate_dispatches():
    import inspect

    from fanyi_dex.book import book_flow

    source = inspect.getsource(book_flow.RecoveryGate.execute)
    for target in book_flow.RESUME_TARGETS:
        assert f'target == {target.upper().replace("-", "_")}' in source, target


def test_gates_read_the_channel_before_consulting_the_timer():
    """Testing has_timer_fired() first discarded a decision that arrived in the same
    tick as the reminder — and the message was already consumed, so it was lost."""
    import inspect

    from fanyi_dex.book import book_flow

    for step in (book_flow.DirectorGate, book_flow.QaGate, book_flow.ProofGate,
                 book_flow.RecoveryGate):
        body = _code_only(inspect.getsource(step.execute))
        assert ".results(context" in body, step.__name__
        # The gates no longer consult the timer at all: an empty Channel result IS the
        # "reminder fired, nobody decided" case, and it cannot lose a decision.
        assert "has_timer_fired" not in body, (
            f"{step.__name__} still branches on the timer"
        )


def _code_only(source: str) -> str:
    """Strip docstrings and comments — they name the old bugs on purpose."""
    body = source.split('"""')[-1] if source.count('"""') >= 2 else source
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


def test_an_unknown_resume_target_keeps_the_failure_record():
    import inspect

    from fanyi_dex.book import book_flow

    body = inspect.getsource(book_flow.RecoveryGate.execute)
    unknown = body.index("unknown resume target")
    cleared = body.index("failure.set(context, StageFailure())")
    assert unknown < cleared, "the diagnosis is erased before the target is validated"


def test_every_long_subprocess_step_states_a_heartbeat_budget():
    """SDK 0.2.5 offers no way to emit a heartbeat, so a pandoc/typst/harvest subprocess
    that is silent for minutes must not inherit the server's default."""
    from fanyi_dex.book import book_flow
    from fanyi_dex.config import Config

    config = Config.from_env()
    for step in (
        book_flow.HarvestStep(config),
        book_flow.AssembleStep(config),
        book_flow.PrintStep(config),
        book_flow.QualityStep(config),
    ):
        options = step.get_step_options()
        assert options.heartbeat_timeout is not None, type(step).__name__
        assert options.heartbeat_timeout >= options.execute_method_timeout


def test_a_restart_does_not_reseed_the_frozen_plan():
    import inspect

    import book as cli

    source = inspect.getsource(cli.cmd_start)
    assert "resuming" in source
    assert "StartFlowOptions()\n        if resuming" in source


def test_gates_pending_names_the_gate_the_volume_is_parked_on():
    import inspect

    from fanyi_dex.book import book_flow

    source = inspect.getsource(book_flow.BookFlow.snapshot)
    assert "parked_on" in source
    assert "approvals.size" not in source


def test_the_fake_agent_answers_every_phase():
    """Every phase marker must still match the prompt it belongs to.

    Rewording three prompt openings once left three markers stale: pass-1 fell through
    to the catch-all reply, failed validation three times per chapter, and the log said
    only "parsed but failed validation". This asserts the sniffing still works, phase by
    phase, so that failure mode is a red test instead of a debugging session.
    """
    import json

    from fanyi_dex import fake_agent
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
    from fanyi_dex.project import Project
    from fanyi_dex.prompts import TIER_FULL, beatplan_prompt

    config = str(
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "fixtures/testbook/pipeline/config.json"
    )
    project = Project(config)
    blocks = VoiceBlocks(project)
    chapter = {
        "hui": 12,
        "named": ["Alpha"],
        "verbatim_lines": ["某之一言"],
        "beats": [{"id": "12.1", "tier": TIER_FULL, "beat": "b", "zh_span": "某曰：「善。」"}],
        "zh": "某曰：「善。」",
    }
    segments = [{"beat_id": "12.1", "tier": TIER_FULL, "prose": "p", "zh_span": "某曰：「善。」"}]
    empty = {"calques": [], "archaisms": [], "flattened": [], "fidelity_issues": []}

    prompts = {
        "beatplan": beatplan_prompt(project, 12, "某曰：「善。」"),
        "pass1": pass1_prompt(blocks, chapter),
        "dlg": dialogue_repair_prompt(blocks, ["Alpha"], segments[0]),
        "pass2": pass2_prompt(blocks, project, chapter, segments),
        "audit": audit_prompt(project, LENSES[0], 12, "english", segments, "某曰：「善。」"),
        "remediate": remediate_prompt(blocks, chapter, segments, empty),
        "access": access_prompt(project, "a", "b"),
        "detfix": det_fix_prompt(chapter, segments, {"calques": [], "archaisms": []}),
    }

    for phase, prompt in prompts.items():
        marker = fake_agent._PHASE_MARKERS[phase]
        assert marker in prompt, f"{phase}: marker {marker!r} no longer matches its prompt"
        reply = json.loads(fake_agent.reply_for(prompt))
        assert not reply.get("unmatched_prompt"), f"{phase} fell through to the catch-all"

    # And the replies satisfy the validators the produce chain gates on.
    assert json.loads(fake_agent.reply_for(prompts["beatplan"]))["beats"]
    assert json.loads(fake_agent.reply_for(prompts["pass1"]))["segments"]
    assert json.loads(fake_agent.reply_for(prompts["pass2"]))["segments"]
    assert json.loads(fake_agent.reply_for(prompts["dlg"]))["prose"].strip()
    access = json.loads(fake_agent.reply_for(prompts["access"]))
    assert isinstance(access["version_A_score"], (int, float))
