"""One chapter's two-pass transcreation + QA, as its own durable execution.

Ported from `pass1_flow.py`'s phase chain with one structural change: the resume
ledger is Dex's own Step history, not files on disk. v1 guarded every phase with
`if not target.is_file()` because the volume Flow re-drove the whole chain from
Pass-1 on each re-run. Here the chapter is a SubFlow, and the parent's default
reuse policy attaches to a running chapter, returns a finished chapter's result,
and restarts only one that ended abnormally — so an interrupted volume resumes
mid-chapter with no filesystem interrogation at all.

Phase artifacts still land as files at the end, because `harvest_reprocess.py`
and the human both read files. They are exports.

Two deviations from the generated Workflow script are kept from v1, both
deliberate: no structured-output schemas (parse-retry instead, at medium effort,
outside any first-token watchdog), and an unknown `beat_id` is dropped rather
than becoming a segment with an empty `zh_span` — invented prose with no source.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from dex import (
    Attribute,
    AttributeIndex,
    Context,
    Flow,
    IndexType,
    PersistenceSchema,
    RetryPolicy,
    Step,
    StepDecision,
    StepList,
    StepOptions,
    Stream,
    force_fail,
    go_to,
    graceful_complete,
)

from fanyi_dex import detscan
from fanyi_dex.book.model import (
    ChapterJob,
    ChapterOutcome,
    dumps,
    loads,
)
from fanyi_dex.claude_cli import call_agent_for_json
from fanyi_dex.config import Config
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
from fanyi_dex.project import Project, lift_held
from fanyi_dex.prompts import RETRY_NUDGE, TIER_FULL

phase = Attribute("pp-phase", str, AttributeIndex(IndexType.KEYWORD))
item_json = Attribute("pp-item", str)
p1_json = Attribute("pp-p1", str)
repaired_json = Attribute("pp-repaired", str)
p2_json = Attribute("pp-p2", str)
audit_json = Attribute("pp-audit", str)
remediated_json = Attribute("pp-remediated", str)
final_json = Attribute("pp-final", str)
cost_usd = Attribute("pp-cost", str)
detail = Attribute("pp-detail", str)

progress = Stream("pp-progress", str, 1 << 16)


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------


class Pass1Step(Step[ChapterJob]):
    """STEP 1: transcreate each beat from the Chinese, applying its tier."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=self.config.pass1_timeout,
            heartbeat_timeout=self.config.pass1_timeout,
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(ProduceFailedStep)

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        phase.set(context, "pass1")
        project = Project(input.config_path)
        # Read once, then hold it in this SubFlow's own durable state: later phases
        # must not re-read a file the parent could have rewritten mid-chapter.
        item = loads(_read_text(input.item_path), {})
        if not item.get("beats"):
            return force_fail(
                f"hui {input.hui}: approved item at {input.item_path} carries no beats"
            )
        item_json.set(context, dumps(item))

        parsed, cost = await call_agent_for_json(
            self.config,
            pass1_prompt(VoiceBlocks(project), item),
            label=f"pass1 h{input.hui}",
            nudge=RETRY_NUDGE,
            is_valid=_has_segments,
            effort=input.effort or self.config.pass1_effort,
        )

        by_id = {b["id"]: b for b in item["beats"]}
        returned = {
            r.get("beat_id"): r for r in parsed["segments"] if r.get("beat_id")
        }
        unknown = sorted(set(returned) - set(by_id))
        segments = [
            {
                "beat_id": beat["id"],
                "tier": returned.get(beat["id"], {}).get("tier") or beat.get("tier"),
                "prose": returned.get(beat["id"], {}).get("prose") or "",
                "zh_span": beat.get("zh_span", ""),
                "is_verse": beat.get("is_verse"),
                "verse_kind": beat.get("verse_kind"),
            }
            for beat in item["beats"]
        ]
        missing = [
            b["id"]
            for b in item["beats"]
            # Only the tier that promises full rendering owes prose; the compressed tier
            # is allowed to come back empty.
            if b.get("tier") == TIER_FULL
            and not (returned.get(b["id"], {}).get("prose") or "").strip()
        ]

        p1_json.set(
            context,
            dumps(
                {
                    "hui": input.hui,
                    "title_en": parsed.get("title_en", ""),
                    "segments": segments,
                    "unknown_beat_ids": unknown,
                    "missing_beat_ids": missing,
                }
            ),
        )
        _add_cost(context, cost)
        detail.set(context, f"pass1: {len(segments)} segments")
        await progress.write(context, f"h{input.hui}: pass1 {len(segments)} segments")
        return go_to(DialogueRepairStep, input)


class DialogueRepairStep(Step[ChapterJob]):
    """STEP 1b: re-render any beat that flattened speech into narration."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=self.config.pass1_timeout,
            heartbeat_timeout=self.config.pass1_timeout,
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(ProduceFailedStep)

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        phase.set(context, "dialogue-repair")
        project = Project(input.config_path)
        item = loads(item_json.get(context), {})
        p1 = loads(p1_json.get(context), {})
        segments = p1["segments"]
        blocks = VoiceBlocks(project)

        bad = [(i, s) for i, s in enumerate(segments) if detscan.is_flattened(s)]
        total_cost = 0.0
        if bad:

            async def repair(index: int, segment: dict[str, Any]):
                parsed, cost = await call_agent_for_json(
                    self.config,
                    dialogue_repair_prompt(blocks, item.get("named", []), segment),
                    label=f"dlg-repair h{input.hui} seg{index}",
                    nudge=RETRY_NUDGE,
                    is_valid=_has_prose,
                    effort=input.effort or self.config.pass1_effort,
                )
                return index, parsed["prose"], cost

            for index, prose, cost in await _bounded(
                self.config, [lambda i=i, s=s: repair(i, s) for i, s in bad]
            ):
                segments[index] = {**segments[index], "prose": prose}
                total_cost += cost

        remaining = sum(1 for s in segments if detscan.is_flattened(s))
        repaired_json.set(
            context,
            dumps(
                {
                    "hui": input.hui,
                    "title_en": p1.get("title_en", ""),
                    "segments": segments,
                    "flattened_p1": len(bad),
                    "flattened_p1_after": remaining,
                }
            ),
        )
        _add_cost(context, total_cost)
        detail.set(context, f"dialogue repair: {len(bad)} -> {remaining}")
        await progress.write(
            context, f"h{input.hui}: dialogue repair {len(bad)} -> {remaining}"
        )
        return go_to(Pass2Step, input)


class Pass2Step(Step[ChapterJob]):
    """STEP 2: fluency rewrite into the shipping voice."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=self.config.pass1_timeout,
            heartbeat_timeout=self.config.pass1_timeout,
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(ProduceFailedStep)

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        phase.set(context, "pass2")
        project = Project(input.config_path)
        item = loads(item_json.get(context), {})
        repaired = loads(repaired_json.get(context), {})

        parsed, cost = await call_agent_for_json(
            self.config,
            pass2_prompt(VoiceBlocks(project), project, item, repaired["segments"]),
            label=f"pass2 h{input.hui}",
            nudge=RETRY_NUDGE,
            is_valid=_has_segments,
            effort=input.effort or self.config.pass1_effort,
        )
        segments = _reattach(repaired["segments"], parsed["segments"])
        p2_json.set(context, dumps({"hui": input.hui, "segments": segments}))
        _add_cost(context, cost)
        detail.set(context, "pass2 complete")
        await progress.write(context, f"h{input.hui}: pass2 complete")
        return go_to(AuditStep, input)


class AuditStep(Step[ChapterJob]):
    """STEP 3a: three adversarial lenses — calque, archaism, fidelity."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=self.config.pass1_timeout,
            heartbeat_timeout=self.config.pass1_timeout,
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(ProduceFailedStep)

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        phase.set(context, "audit")
        project = Project(input.config_path)
        item = loads(item_json.get(context), {})
        segments = loads(p2_json.get(context), {})["segments"]
        english = detscan.assemble(segments)

        async def run_lens(lens: dict[str, str]):
            return await call_agent_for_json(
                self.config,
                audit_prompt(project, lens, input.hui, english, segments, item.get("zh", "")),
                label=f"audit:{lens['name']} h{input.hui}",
                nudge=RETRY_NUDGE,
                is_valid=_is_audit,
                effort=input.effort or self.config.pass1_effort,
            )

        results = await _bounded(self.config, [lambda L=L: run_lens(L) for L in LENSES])
        merged = _merge_audit(results)
        audit_json.set(context, dumps(merged))
        _add_cost(context, sum(c for _, c in results))

        findings = sum(len(v) for v in merged.values())
        detail.set(context, f"audit: {findings} finding(s)")
        await progress.write(context, f"h{input.hui}: audit {findings} finding(s)")
        if findings > 0:
            return go_to(RemediateStep, input)
        return go_to(FinalizeStep, input)


class RemediateStep(Step[ChapterJob]):
    """STEP 3b: fix the flagged spans, then re-audit with the first two lenses."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=self.config.pass1_timeout,
            heartbeat_timeout=self.config.pass1_timeout,
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(ProduceFailedStep)

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        phase.set(context, "remediate")
        project = Project(input.config_path)
        item = loads(item_json.get(context), {})
        p2_segments = loads(p2_json.get(context), {})["segments"]
        merged = loads(audit_json.get(context), {})

        parsed, cost = await call_agent_for_json(
            self.config,
            remediate_prompt(VoiceBlocks(project), item, p2_segments, merged),
            label=f"remediate h{input.hui}",
            nudge=RETRY_NUDGE,
            is_valid=_has_segments,
            effort=input.effort or self.config.pass1_effort,
        )
        segments = _reattach(p2_segments, parsed["segments"])
        english = detscan.assemble(segments)

        async def run_lens(lens: dict[str, str]):
            return await call_agent_for_json(
                self.config,
                audit_prompt(project, lens, input.hui, english, segments, item.get("zh", "")),
                label=f"reaudit:{lens['name']} h{input.hui}",
                nudge=RETRY_NUDGE,
                is_valid=_is_audit,
                effort=input.effort or self.config.pass1_effort,
            )

        # The generated workflow re-audits with the first two lenses only.
        results = await _bounded(
            self.config, [lambda L=L: run_lens(L) for L in LENSES[:2]]
        )
        after = _merge_audit(results)
        after["flattened"] = []
        after["fidelity_issues"] = []
        remediated_json.set(
            context,
            dumps({"hui": input.hui, "segments": segments, "audit_after": after}),
        )
        _add_cost(context, cost + sum(c for _, c in results))

        left = len(after["calques"]) + len(after["archaisms"])
        detail.set(context, f"remediated, {left} left")
        await progress.write(context, f"h{input.hui}: remediated, {left} left")
        return go_to(FinalizeStep, input)


class FinalizeStep(Step[ChapterJob]):
    """STEP 3c: accessibility-lift gate, deterministic ship gate, targeted fix."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=self.config.pass1_timeout,
            heartbeat_timeout=self.config.pass1_timeout,
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(ProduceFailedStep)

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        phase.set(context, "finalize")
        project = Project(input.config_path)
        item = loads(item_json.get(context), {})
        p1 = loads(p1_json.get(context), {})
        repaired = loads(repaired_json.get(context), {})
        audit = loads(audit_json.get(context), {})
        remediated = loads(remediated_json.get(context), None)

        if remediated:
            final_segments = remediated["segments"]
            audit_after = remediated["audit_after"]
        else:
            final_segments = loads(p2_json.get(context), {})["segments"]
            audit_after = audit

        total_cost = 0.0

        # Accessibility lift: a newcomer scores Pass-1 (A) against the final (B).
        access, cost = await call_agent_for_json(
            self.config,
            access_prompt(
                project,
                detscan.assemble(repaired["segments"]),
                detscan.assemble(final_segments),
            ),
            label=f"access h{input.hui}",
            nudge=RETRY_NUDGE,
            is_valid=_is_access,
            effort=input.effort or self.config.pass1_effort,
        )
        total_cost += cost
        score_a = float(access["version_A_score"])
        score_b = float(access["version_B_score"])
        lift_ok = lift_held(project, score_a, score_b)

        # Deterministic ship gate, using the project's own blocklists.
        det = detscan.scan(project.config_path, detscan.assemble(final_segments))
        if detscan.gate_count(det) > 0:
            parsed, fix_cost = await call_agent_for_json(
                self.config,
                det_fix_prompt(item, final_segments, det),
                label=f"det-fix h{input.hui}",
                nudge=RETRY_NUDGE,
                is_valid=_has_segments,
                effort=input.effort or self.config.pass1_effort,
            )
            total_cost += fix_cost
            final_segments = _reattach(final_segments, parsed["segments"])
            det = detscan.scan(project.config_path, detscan.assemble(final_segments))
        det_clean = detscan.gate_count(det) == 0

        flattened_after = sum(1 for s in final_segments if detscan.is_flattened(s))
        record = {
            "hui": input.hui,
            "local": item.get("local") or input.local,
            "book": input.book,
            "final": final_segments,
            "flattened_p1": repaired.get("flattened_p1", 0),
            "unknown_beat_ids": p1.get("unknown_beat_ids", []),
            "missing_beat_ids": p1.get("missing_beat_ids", []),
            "flattened_p1_after": flattened_after,
            "audit_before": audit,
            "audit_after": audit_after,
            "det_scan": det,
            "det_clean": det_clean,
            "access_before": score_a,
            "access_after": score_b,
            "lift_ok": lift_ok,
            "access_friction": access.get("top_friction_B", ""),
        }
        final_json.set(context, dumps(record))
        _add_cost(context, total_cost)
        phase.set(context, "done")

        # Export the phase artifacts for the human and for harvest_reprocess.py.
        directory = project.chapter_dir(input.book, input.hui)
        directory.mkdir(parents=True, exist_ok=True)
        for name, payload in (
            ("item", item),
            ("p1", p1),
            ("p1_repaired", repaired),
            ("p2", loads(p2_json.get(context), {})),
            ("audit", audit),
            ("remediated", remediated),
            ("chapter", record),
        ):
            if payload is None:
                continue
            (directory / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )

        words = len(detscan.assemble(final_segments).split())
        flags = "".join(
            [
                "" if det_clean else " [det]",
                "" if lift_ok else " [lift]",
                " [dlg]" if flattened_after else "",
            ]
        )
        detail.set(context, ("done" + flags).strip())
        await progress.write(context, f"h{input.hui}: {('done' + flags).strip()}")
        return graceful_complete(
            ChapterOutcome(
                hui=input.hui,
                ok=True,
                detail=("done" + flags).strip(),
                segments=len(final_segments),
                words=words,
                gate_count=detscan.gate_count(det),
                lift_ok=lift_ok,
                flattened_after=flattened_after,
                unknown_beat_ids=",".join(p1.get("unknown_beat_ids", [])),
                missing_beat_ids=",".join(p1.get("missing_beat_ids", [])),
                cost_usd=cost_usd.get(context) or "0.00",
                # A path: the parent reads this to build the `{"chapters": [...]}`
                # file harvest_reprocess.py consumes. See ChapterJob.item_path.
                export_path=str(directory / "chapter.json"),
            )
        )


class ProduceFailedStep(Step[ChapterJob]):
    """Exhausted retries end the chapter as a failure, recording how far it got."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=1),
            )
        )

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        reached = phase.get(context) or "pass1"
        reason = f"hui {input.hui} exhausted its retries in phase '{reached}'"
        phase.set(context, "failed")
        detail.set(context, reason)
        await progress.write(context, f"h{input.hui}: FAILED in {reached}")
        return force_fail(reason)


class ProduceChapterFlow(Flow[ChapterJob]):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.pass1 = Pass1Step(config)
        self.dialogue_repair = DialogueRepairStep(config)
        self.pass2 = Pass2Step(config)
        self.audit = AuditStep(config)
        self.remediate = RemediateStep(config)
        self.finalize = FinalizeStep(config)
        self.failed = ProduceFailedStep(config)

    def get_flow_type(self) -> str:
        return "FanyiProduceChapter"

    def get_steps(self) -> StepList[ChapterJob]:
        return StepList.start_step(self.pass1).other_steps(
            self.dialogue_repair,
            self.pass2,
            self.audit,
            self.remediate,
            self.finalize,
            self.failed,
        )

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(
            phase,
            item_json,
            p1_json,
            repaired_json,
            p2_json,
            audit_json,
            remediated_json,
            final_json,
            cost_usd,
            detail,
            progress,
        )


# --------------------------------------------------------------------------
# Helpers. Pure functions only — nothing here hides a wait, a movement, or a
# recovery target, so the Flow graph stays readable in one file.
# --------------------------------------------------------------------------


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _add_cost(context: Context, amount: float) -> None:
    current = float(cost_usd.get(context) or 0.0)
    cost_usd.set(context, f"{current + amount:.6f}")


def _has_segments(parsed: dict[str, Any]) -> bool:
    segments = parsed.get("segments")
    return isinstance(segments, list) and len(segments) > 0


def _has_prose(parsed: dict[str, Any]) -> bool:
    return isinstance(parsed.get("prose"), str) and bool(parsed["prose"].strip())


def _is_audit(parsed: dict[str, Any]) -> bool:
    return any(
        isinstance(parsed.get(key), list)
        for key in ("calques", "archaisms", "flattened", "fidelity_issues")
    )


def _is_access(parsed: dict[str, Any]) -> bool:
    return isinstance(parsed.get("version_A_score"), (int, float)) and isinstance(
        parsed.get("version_B_score"), (int, float)
    )


def _merge_audit(results: list[tuple[dict[str, Any], float]]) -> dict[str, list]:
    return {
        "calques": [c for parsed, _ in results for c in parsed.get("calques") or []],
        "archaisms": [a for parsed, _ in results for a in parsed.get("archaisms") or []],
        "flattened": [f for parsed, _ in results for f in parsed.get("flattened") or []],
        "fidelity_issues": [
            f for parsed, _ in results for f in parsed.get("fidelity_issues") or []
        ],
    }


def _reattach(
    base: list[dict[str, Any]], returned: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay returned prose onto `base` by beat_id, keeping base order/metadata.

    The agent never echoes zh_span (echoing ~400 chars of Chinese per beat stalls
    generation), so the source span and verse flags are re-attached here — and a
    segment the agent dropped keeps its previous prose rather than vanishing.
    """
    got = {s["beat_id"]: s for s in returned if s.get("beat_id")}
    out = []
    for seg in base:
        prose = (got.get(seg["beat_id"]) or {}).get("prose")
        out.append({**seg, "prose": prose if prose is not None else seg.get("prose", "")})
    return out


async def _bounded(config: Config, factories: list[Any]) -> list[Any]:
    """Cap the fan-outs *inside* one phase (dialogue repairs, audit lenses)."""
    semaphore = asyncio.Semaphore(max(1, config.inner_concurrency))

    async def run(factory):
        async with semaphore:
            return await factory()

    return await asyncio.gather(*(run(f) for f in factories))
