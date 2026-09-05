"""One volume of one translation project, initiation to final product.

This is the whole business execution: read the Chinese, plan the beats, take the
director's tier calls, transcreate, audit, harvest into the vault, assemble the
manuscript, build the KDP interior, run the checks, take the proof sign-off.

Every Step takes the same input type (`StageRef`). That is deliberate: it makes
`on_execute_failure_proceed_to(RecoveryGate)` legal from every stage and lets one
recovery Step route back into any of them, instead of each stage inventing its own
diversion target.

Chapters are SubFlows. The parent awaits a bounded batch of SubFlow Conditions in
`wait_for`, so there is no wave-join Step, no completion Channel to count, and no
possibility of one bad chapter deadlocking the volume.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dex import (
    Attribute,
    AttributeIndex,
    AttributeMap,
    Channel,
    ChannelMap,
    Context,
    Flow,
    FlowStatus,
    IndexType,
    PersistenceSchema,
    RetryPolicy,
    RPCResult,
    Step,
    StepDecision,
    StepList,
    StepOptions,
    Stream,
    SubFlow,
    Timer,
    Wait,
    go_to,
    graceful_complete,
    rpc,
)

from fanyi_dex.book import finishing
from fanyi_dex.book.curate_chapter import CurateChapterFlow
from fanyi_dex.book.model import (
    APPROVING_ITEMS,
    ASSEMBLING,
    CHECKING,
    CURATING,
    DIRECTOR_GATE,
    DONE,
    GATE_DIRECTOR,
    GATE_PROOF,
    GATE_QA,
    HARVESTING,
    INIT,
    PRINTING,
    PRODUCING,
    PROOF_GATE,
    QA_GATE,
    RECOVERY_GATE,
    Approval,
    ChapterJob,
    ChapterOutcome,
    ChapterRecord,
    Manifest,
    RunPlan,
    Snapshot,
    StageFailure,
    StageRef,
    Tally,
    dumps,
    ints,
    join,
    key,
    loads,
    sha256_file,
)
from fanyi_dex.book.produce_chapter import ProduceChapterFlow
from fanyi_dex.config import Config
from fanyi_dex.project import Project
from fanyi_dex.shell import run_python

# --------------------------------------------------------------------------
# Durable state
# --------------------------------------------------------------------------

stage = Attribute("bk-stage", str, AttributeIndex(IndexType.KEYWORD))
note = Attribute("bk-note", str)
plan = Attribute("bk-plan", RunPlan)
tally = Attribute("bk-tally", Tally)
manifest = Attribute("bk-manifest", Manifest)
failure = Attribute("bk-failure", StageFailure)

# One instance per chapter: each lands independently, so a chapter completing
# never rewrites the whole-volume value (and never invalidates another chapter's
# cached blob).
chapters = AttributeMap("bk-chapter", ChapterRecord)
# Where each chapter SubFlow exported its artifact. Paths, not payloads: SDK 0.2.5
# does not hydrate a blob-backed value carried on a SubFlow completion, so bulk data
# moves through the run directory while Dex holds the coordination state.
#
# The two passes get SEPARATE maps. One shared map meant a produce wave overwrote the
# beat-plan path with the chapter-record path under the same key, so re-entering the
# director gate or the item staging AFTER a produce wave read a finished chapter as if
# it were a plan — silently breaking exactly the recovery paths.
chapter_plans = AttributeMap("bk-chapter-plan-path", str)
chapter_records = AttributeMap("bk-chapter-record-path", str)
chapter_items = AttributeMap("bk-chapter-item", str)

# The director's decision at GATE 1, committed by the Step that actually consumed the
# approval. `Channel.results()` only returns values to the Step execution whose wait
# consumed them, so a later Step reading the Channel gets nothing — which is why
# `--tier-override` silently did nothing until this Attribute existed.
tier_overrides = Attribute("bk-tier-overrides", str)

# Human decisions, keyed by gate. Durable, unlike a Stream, because a gate
# decision is the thing the volume is waiting on.
approvals = ChannelMap("bk-approval", Approval)
resume = Channel("bk-resume", str)

progress = Stream("bk-progress", str, 1 << 18)

# Every stage RecoveryGate can re-enter. Validated before the failure record is
# cleared, so an unrecognised target leaves the diagnosis intact.
RESUME_TARGETS = (
    INIT,
    CURATING,
    DIRECTOR_GATE,
    APPROVING_ITEMS,
    PRODUCING,
    QA_GATE,
    HARVESTING,
    ASSEMBLING,
    PRINTING,
    CHECKING,
    PROOF_GATE,
)


def _bookkeeping() -> StepOptions:
    """Uncapped retries for Steps that only move state.

    A capped count would turn an ordinary Worker restart into a permanently FAILED
    volume.
    """
    return StepOptions(
        execute_retry=RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
        )
    )


# --------------------------------------------------------------------------
# Init
# --------------------------------------------------------------------------


class InitStep(Step[StageRef]):
    """Freeze the run plan, seed one record per chapter, and refuse bad input early."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _bookkeeping().on_execute_failure_proceed_to(RecoveryGate)

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, INIT)
        project = Project(input.config_path)
        project.require_beatplan_support()
        project.require_pass1_support()

        # The Client seeds run policy through StartFlowOptions.with_attribute, so
        # `aggressive`, `auto_approve` and `wave_size` arrive as durable state rather
        # than being re-derived from the Worker's environment — a Worker restart with
        # a different env cannot change the policy a running volume was started under.
        seed = plan.get(context) or RunPlan()
        if seed and seed.chapters:
            # A restarted volume keeps its original plan: the whole point of
            # freezing it is that a config edited mid-run cannot change policy
            # for the chapters that have not run yet.
            frozen = seed
        else:
            only = ints(input.pending)
            selected = project.chapters(input.book, only or None)
            lo, hi = project.chapter_range(input.book)
            frozen = RunPlan(
                project_slug=project.cfg.get("project", {}).get("slug", project.name),
                project_root=str(project.root),
                config_path=project.config_path,
                config_sha256=sha256_file(project.config_path),
                book=input.book,
                book_title=project.book_title(input.book),
                hui_lo=lo,
                hui_hi=hi,
                chapters=join([c.hui for c in selected]),
                aggressive=seed.aggressive,
                auto_approve=seed.auto_approve,
                wave_size=max(1, seed.wave_size or self.config.wave_size),
                curate_effort=seed.curate_effort or self.config.effort,
                produce_effort=seed.produce_effort or self.config.pass1_effort,
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            plan.set(context, frozen)
            for chapter in selected:
                chapters.set(
                    context,
                    key(chapter.hui),
                    ChapterRecord(hui=chapter.hui, local=chapter.local),
                )

        counted = len(frozen.hui_list)
        tally.set(context, Tally(curate_total=counted, produce_total=counted))
        manifest.set(context, Manifest())
        warnings = project.pass1_warnings()
        note.set(
            context,
            f"{project.name} book {frozen.book} ({frozen.book_title}): "
            f"{counted} chapter(s)"
            + (f" | WARNING: {warnings[0]}" if warnings else ""),
        )
        await progress.write(context, f"init: {counted} chapter(s) planned")
        return go_to(
            CurateWaveStep,
            StageRef(
                config_path=input.config_path,
                book=input.book,
                stage=CURATING,
                pending=frozen.chapters,
                wave=1,
            ),
        )


# --------------------------------------------------------------------------
# Curate: beat plans, one SubFlow per chapter, bounded batches
# --------------------------------------------------------------------------


class CurateWaveStep(Step[StageRef]):
    """Awaits one bounded batch of chapter beat-plan SubFlows."""

    def __init__(self, config: Config, curate: CurateChapterFlow) -> None:
        self.config = config
        self.curate = curate

    def get_step_options(self) -> StepOptions:
        return _bookkeeping().on_execute_failure_proceed_to(RecoveryGate)

    def wait_for(self, context: Context, input: StageRef) -> Wait:
        # Stamped on entry, beside the wait it is parked on: a batch of SubFlows
        # can run for an hour, and `execute` does not run until they close.
        stage.set(context, CURATING)
        batch = _batch(context, input)
        if not batch:
            return Wait.skip_immediately()
        frozen = _plan(context)
        return Wait.all_of(
            *[
                SubFlow.run(
                    self.curate,
                    ChapterJob(
                        config_path=input.config_path,
                        book=input.book,
                        hui=hui,
                        local=_local(context, hui),
                        aggressive=frozen.aggressive,
                        effort=frozen.curate_effort,
                    ),
                )
                for hui in batch
            ]
        )

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, CURATING)
        batch = _batch(context, input)
        for index, hui in enumerate(batch):
            result = SubFlow.get_condition_results(context, index)
            record = _record(context, hui)
            record.curate_flow_id = SubFlow.get_flow_id(context, index)
            if result.status is FlowStatus.COMPLETED:
                outcome = _outcome(result)
                record.curate_status = "ok"
                record.beats = outcome.beats
                record.coverage_pct = outcome.coverage_pct
                record.uncertain = outcome.uncertain
                record.cost_usd = outcome.cost_usd
                record.error = ""
                if outcome.export_path:
                    chapter_plans.set(context, key(hui), outcome.export_path)
            else:
                record.curate_status = "failed"
                record.error = (result.error_message or str(result.status))[:400]
            chapters.set(context, key(hui), record)

        remainder = ints(input.pending)[len(batch) :]
        counts = _recount(context)
        tally.set(context, counts)
        note.set(
            context,
            f"curate wave {input.wave}: {counts.curate_done} planned, "
            f"{counts.curate_failed} failed"
            + (f", {len(remainder)} queued" if remainder else ""),
        )
        await progress.write(
            context,
            f"curate wave {input.wave} done: {counts.curate_done}/{counts.curate_total}",
        )
        if remainder:
            return go_to(
                CurateWaveStep,
                StageRef(
                    config_path=input.config_path,
                    book=input.book,
                    stage=CURATING,
                    pending=join(remainder),
                    wave=input.wave + 1,
                ),
            )
        return go_to(
            DirectorGate,
            StageRef(
                config_path=input.config_path,
                book=input.book,
                stage=DIRECTOR_GATE,
            ),
        )


# --------------------------------------------------------------------------
# GATE 1 — the director's tier calls
# --------------------------------------------------------------------------


class DirectorGate(Step[StageRef]):
    """GATE 1: the uncertain tier calls are reviewed before anything is translated."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _bookkeeping().on_execute_failure_proceed_to(RecoveryGate)

    def wait_for(self, context: Context, input: StageRef) -> Wait:
        stage.set(context, DIRECTOR_GATE)
        if _plan(context).auto_approve:
            return Wait.skip_immediately()
        return Wait.any_of(
            approvals.for_one(GATE_DIRECTOR),
            Timer.by_duration(self.config.gate_reminder),
        )

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, DIRECTOR_GATE)
        frozen = _plan(context)
        review = _write_review(context, frozen)

        if frozen.auto_approve:
            note.set(
                context,
                f"GATE 1 auto-approved (mock): {len(review)} uncertain tier call(s) "
                f"accepted as planned",
            )
            await progress.write(context, "GATE 1 auto-approved (mock)")
            return go_to(
                ApproveItemsStep,
                StageRef(input.config_path, input.book, APPROVING_ITEMS),
            )

        # The Channel is read BEFORE the timer is consulted. Testing
        # `has_timer_fired()` first discarded any decision that landed in the same tick
        # as the reminder — and the message was already consumed, so that approval was
        # lost for good and the gate quietly went back to waiting.
        decisions = approvals.results(context, GATE_DIRECTOR)
        if not decisions:
            note.set(
                context,
                f"GATE 1 open: {len(review)} uncertain tier call(s) awaiting review "
                f"— see {_review_path(frozen)}",
            )
            return go_to(DirectorGate, input)

        decision = decisions[0]
        # Commit the payload here: this is the only Step execution whose wait consumed
        # the approval, so it is the only one that can see it.
        tier_overrides.set(context, decision.payload or "")
        if decision.decision != "approve":
            failure.set(
                context,
                StageFailure(
                    stage=DIRECTOR_GATE,
                    detail=f"rejected at GATE 1: {decision.note}",
                    at=_now(),
                ),
            )
            return go_to(
                RecoveryGate, StageRef(input.config_path, input.book, DIRECTOR_GATE)
            )
        note.set(context, f"GATE 1 approved by {decision.actor}: {decision.note}")
        await progress.write(context, f"GATE 1 approved: {decision.note}")
        return go_to(
            ApproveItemsStep,
            StageRef(input.config_path, input.book, APPROVING_ITEMS),
        )


class ApproveItemsStep(Step[StageRef]):
    """Turns approved beat plans into the items the produce pass consumes.

    This is the handoff v1 left to a human: there, the beat-plan Flow completed and
    somebody had to find the right `cutlists/*.json` and pass it to `--items`, with
    `pass1-check` printing candidates because several generations of the file
    disagreed. Here the approved plan is produced and consumed inside one execution.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _bookkeeping().on_execute_failure_proceed_to(RecoveryGate)

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, APPROVING_ITEMS)
        frozen = _plan(context)
        overrides = _tier_overrides(context)

        project = Project(input.config_path)
        staged = 0
        for hui in frozen.hui_list:
            plan_export = chapter_plans.get(context, key(hui))
            raw = finishing.read_json(plan_export)
            if not raw:
                continue
            item = finishing.stage_item(raw, overrides)
            if not item.get("beats"):
                continue
            item_path = project.chapter_dir(input.book, hui) / "item.json"
            item_path.parent.mkdir(parents=True, exist_ok=True)
            item_path.write_text(
                json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            chapter_items.set(context, key(hui), str(item_path))
            staged += 1

        if staged == 0:
            failure.set(
                context,
                StageFailure(
                    stage=APPROVING_ITEMS,
                    detail="no approved beat plans — nothing to translate",
                    at=_now(),
                ),
            )
            return go_to(
                RecoveryGate, StageRef(input.config_path, input.book, APPROVING_ITEMS)
            )

        pending = [h for h in frozen.hui_list if chapter_items.get(context, key(h))]
        note.set(
            context,
            f"{staged} chapter(s) staged for translation"
            + (f", {len(overrides)} tier override(s) applied" if overrides else ""),
        )
        await progress.write(context, f"{staged} chapter(s) staged for translation")
        return go_to(
            ProduceWaveStep,
            StageRef(
                config_path=input.config_path,
                book=input.book,
                stage=PRODUCING,
                pending=join(pending),
                wave=1,
            ),
        )


# --------------------------------------------------------------------------
# Produce: two-pass transcreation + QA, one SubFlow per chapter
# --------------------------------------------------------------------------


class ProduceWaveStep(Step[StageRef]):
    """Awaits one bounded batch of chapter transcreation SubFlows."""

    def __init__(self, config: Config, produce: ProduceChapterFlow) -> None:
        self.config = config
        self.produce = produce

    def get_step_options(self) -> StepOptions:
        return _bookkeeping().on_execute_failure_proceed_to(RecoveryGate)

    def wait_for(self, context: Context, input: StageRef) -> Wait:
        stage.set(context, PRODUCING)
        batch = _batch(context, input)
        if not batch:
            return Wait.skip_immediately()
        frozen = _plan(context)
        return Wait.all_of(
            *[
                SubFlow.run(
                    self.produce,
                    ChapterJob(
                        config_path=input.config_path,
                        book=input.book,
                        hui=hui,
                        local=_local(context, hui),
                        effort=frozen.produce_effort,
                        item_path=chapter_items.get(context, key(hui)),
                    ),
                )
                for hui in batch
            ]
        )

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, PRODUCING)
        batch = _batch(context, input)
        for index, hui in enumerate(batch):
            result = SubFlow.get_condition_results(context, index)
            record = _record(context, hui)
            record.produce_flow_id = SubFlow.get_flow_id(context, index)
            if result.status is FlowStatus.COMPLETED:
                outcome = _outcome(result)
                record.produce_status = "ok"
                record.segments = outcome.segments
                record.words = outcome.words
                record.gate_count = outcome.gate_count
                record.lift_ok = outcome.lift_ok
                record.flattened_after = outcome.flattened_after
                record.unknown_beat_ids = outcome.unknown_beat_ids
                record.missing_beat_ids = outcome.missing_beat_ids
                record.cost_usd = _add(record.cost_usd, outcome.cost_usd)
                record.error = ""
                if outcome.export_path:
                    chapter_records.set(context, key(hui), outcome.export_path)
            else:
                record.produce_status = "failed"
                record.error = (result.error_message or str(result.status))[:400]
            chapters.set(context, key(hui), record)

        remainder = ints(input.pending)[len(batch) :]
        counts = _recount(context)
        tally.set(context, counts)
        note.set(
            context,
            f"produce wave {input.wave}: {counts.produce_done} done, "
            f"{counts.produce_failed} failed, {counts.attention} needing attention"
            + (f", {len(remainder)} queued" if remainder else ""),
        )
        await progress.write(
            context,
            f"produce wave {input.wave} done: "
            f"{counts.produce_done}/{counts.produce_total}",
        )
        if remainder:
            return go_to(
                ProduceWaveStep,
                StageRef(
                    config_path=input.config_path,
                    book=input.book,
                    stage=PRODUCING,
                    pending=join(remainder),
                    wave=input.wave + 1,
                ),
            )
        return go_to(QaGate, StageRef(input.config_path, input.book, QA_GATE))


# --------------------------------------------------------------------------
# GATE 2 — the QA scorecard
# --------------------------------------------------------------------------


class QaGate(Step[StageRef]):
    """GATE 2: the QA signals are read before anything reaches the vault."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _bookkeeping().on_execute_failure_proceed_to(RecoveryGate)

    def wait_for(self, context: Context, input: StageRef) -> Wait:
        stage.set(context, QA_GATE)
        if _plan(context).auto_approve:
            return Wait.skip_immediately()
        return Wait.any_of(
            approvals.for_one(GATE_QA),
            Timer.by_duration(self.config.gate_reminder),
        )

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, QA_GATE)
        frozen = _plan(context)
        counts = _recount(context)
        tally.set(context, counts)
        scorecard = (
            f"{counts.produce_done} done, {counts.produce_failed} failed, "
            f"{counts.attention} needing attention, ${counts.cost_usd}"
        )

        if frozen.auto_approve:
            note.set(context, f"GATE 2 auto-approved (mock): {scorecard}")
            await progress.write(context, "GATE 2 auto-approved (mock)")
            return go_to(
                HarvestStep, StageRef(input.config_path, input.book, HARVESTING)
            )
        decisions = approvals.results(context, GATE_QA)
        if not decisions:
            note.set(context, f"GATE 2 open: {scorecard}")
            return go_to(QaGate, input)

        decision = decisions[0]
        if decision.decision != "approve":
            failure.set(
                context,
                StageFailure(
                    stage=QA_GATE, detail=f"rejected at GATE 2: {decision.note}", at=_now()
                ),
            )
            return go_to(RecoveryGate, StageRef(input.config_path, input.book, QA_GATE))
        note.set(context, f"GATE 2 approved by {decision.actor}: {scorecard}")
        await progress.write(context, "GATE 2 approved")
        return go_to(HarvestStep, StageRef(input.config_path, input.book, HARVESTING))


# --------------------------------------------------------------------------
# Finishing: vault -> manuscript -> print -> checks
# --------------------------------------------------------------------------


class HarvestStep(Step[StageRef]):
    """Writes the finished chapters into the vault, after an independent backup.

    v1 refused to do this at all — it printed the command and parked, because the
    write reaches the Obsidian vault. Here it runs, but only after copying the
    existing book folder aside, and through the project's own
    `harvest_reprocess.py` so the two-pass ship gate is the shipped one.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=timedelta(minutes=30),
            # Stated for the same reason the LLM Steps state it: a pandoc/typst/harvest
            # subprocess is silent for minutes, and SDK 0.2.5 gives no way to heartbeat.
            heartbeat_timeout=timedelta(minutes=30),
            # One attempt at the write itself: a re-run is content-identical, but
            # the backup is not something to take twice in a retry storm.
            execute_retry=RetryPolicy(maximum_attempts=1),
        ).on_execute_failure_proceed_to(RecoveryGate)

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, HARVESTING)
        project = Project(input.config_path)
        frozen = _plan(context)

        records = [
            finishing.read_json(chapter_records.get(context, key(hui)))
            for hui in frozen.hui_list
        ]
        records = [r for r in records if r]
        if not records:
            failure.set(
                context,
                StageFailure(
                    stage=HARVESTING, detail="no finished chapters to harvest", at=_now()
                ),
            )
            return go_to(
                RecoveryGate, StageRef(input.config_path, input.book, HARVESTING)
            )

        combined = project.pass1_combined(input.book)
        combined.parent.mkdir(parents=True, exist_ok=True)
        combined.write_text(finishing.chapters_payload(records), encoding="utf-8")

        backup = finishing.backup_vault_book(project, input.book)
        script = project.pipeline_script("harvest_reprocess.py")
        result = await run_python(
            self.config,
            script,
            [str(combined), "--config", project.config_path],
            cwd=script.parent,
        )
        wrote, skipped = finishing.harvest_tally(result.stdout)

        current = _manifest(context)
        current.harvested = wrote
        current.vault_backup = backup
        manifest.set(context, current)
        note.set(
            context,
            f"harvested {wrote} chapter(s) into the vault"
            + (f", {skipped} skipped by the ship gate" if skipped else "")
            + (f" (backup: {backup})" if backup else ""),
        )
        await progress.write(context, f"harvested {wrote} chapter(s)")
        return go_to(AssembleStep, StageRef(input.config_path, input.book, ASSEMBLING))


class AssembleStep(Step[StageRef]):
    """Runs the project's own assemble.py: master markdown, .docx, .epub."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=30),
            execute_retry=RetryPolicy(maximum_attempts=2),
        ).on_execute_failure_proceed_to(RecoveryGate)

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, ASSEMBLING)
        project = Project(input.config_path)
        script = project.pipeline_script("assemble.py")
        await run_python(
            self.config,
            script,
            [str(input.book), "--config", project.config_path],
            cwd=script.parent,
        )

        exports = project.root / "exports"
        current = _manifest(context)
        current.master_md = finishing.existing(exports / f"Book{input.book}_master.md")
        current.docx = finishing.existing(exports / f"Book{input.book}.docx")
        current.epub = finishing.existing(exports / f"Book{input.book}.epub")
        manifest.set(context, current)

        if not current.master_md:
            failure.set(
                context,
                StageFailure(
                    stage=ASSEMBLING,
                    detail="assemble.py produced no master markdown",
                    at=_now(),
                ),
            )
            return go_to(
                RecoveryGate, StageRef(input.config_path, input.book, ASSEMBLING)
            )

        note.set(
            context,
            "assembled "
            + ", ".join(
                p.rsplit("/", 1)[-1]
                for p in (current.master_md, current.docx, current.epub)
                if p
            ),
        )
        await progress.write(context, "manuscript assembled")
        return go_to(PrintStep, StageRef(input.config_path, input.book, PRINTING))


class PrintStep(Step[StageRef]):
    """Runs assemble_print.py: the 6x9 KDP interior PDF, preflight included."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=timedelta(minutes=60),
            heartbeat_timeout=timedelta(minutes=60),
            execute_retry=RetryPolicy(maximum_attempts=2),
        ).on_execute_failure_proceed_to(RecoveryGate)

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, PRINTING)
        project = Project(input.config_path)
        script = project.pipeline_script("assemble_print.py")
        result = await run_python(
            self.config, script, [str(input.book)], cwd=script.parent, check=False
        )

        current = _manifest(context)
        current.interior_pdf = finishing.existing(
            project.root / "exports" / "print" / f"Book{input.book}_interior_6x9.pdf"
        )
        current.pages = finishing.scrape_int(result.stdout, r"(\d+)\s+pages")
        current.preflight = (
            "pass" if result.code == 0 and current.interior_pdf else "fail"
        )
        if current.preflight == "fail":
            current.checks_note = finishing.one_line(result.tail)
        manifest.set(context, current)
        note.set(
            context,
            f"interior {'built' if current.interior_pdf else 'NOT built'}"
            + (f", {current.pages} pages" if current.pages else ""),
        )
        await progress.write(context, f"print: {current.preflight}")

        # The interior is the final product, and assemble_print.py runs the KDP
        # preflight. A missing PDF or a failing check parks the volume rather than
        # walking on to the proof gate and reporting itself finished — which is what
        # the first verification run did, completing with `preflight fail`.
        # `resume --stage checking` is the deliberate way past it, for the cases where
        # a human reads the failures and accepts them.
        if current.preflight != "pass":
            reason = (
                "no interior PDF"
                if not current.interior_pdf
                else "KDP preflight failed"
            )
            failure.set(
                context,
                StageFailure(
                    stage=PRINTING,
                    detail=f"{reason}: {finishing.one_line(result.tail, 300)}",
                    at=_now(),
                ),
            )
            return go_to(
                RecoveryGate, StageRef(input.config_path, input.book, PRINTING)
            )
        return go_to(QualityStep, StageRef(input.config_path, input.book, CHECKING))


class QualityStep(Step[StageRef]):
    """Validates the epub. The print preflight already ran inside assemble_print.py."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=15),
            execute_retry=RetryPolicy(maximum_attempts=2),
        ).on_execute_failure_proceed_to(RecoveryGate)

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, CHECKING)
        current = _manifest(context)
        if current.epub:
            result = await _epubcheck(self.config, current.epub)
            current.epubcheck, detail = finishing.epubcheck_verdict(*result)
            if current.epubcheck == "fail":
                current.checks_note = (current.checks_note + " | epub: " + detail)[:600]
        else:
            current.epubcheck = "not-run"
        manifest.set(context, current)
        note.set(
            context,
            f"checks: epub {current.epubcheck}, print preflight {current.preflight}",
        )
        await progress.write(context, f"checks: epub {current.epubcheck}")
        return go_to(ProofGate, StageRef(input.config_path, input.book, PROOF_GATE))


# --------------------------------------------------------------------------
# GATE 3 — proof sign-off, then the volume is done
# --------------------------------------------------------------------------


class ProofGate(Step[StageRef]):
    """GATE 3: the human reads the proof before the volume is called finished."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _bookkeeping().on_execute_failure_proceed_to(RecoveryGate)

    def wait_for(self, context: Context, input: StageRef) -> Wait:
        stage.set(context, PROOF_GATE)
        if _plan(context).auto_approve:
            return Wait.skip_immediately()
        return Wait.any_of(
            approvals.for_one(GATE_PROOF),
            Timer.by_duration(self.config.gate_reminder),
        )

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, PROOF_GATE)
        frozen = _plan(context)
        current = _manifest(context)
        summary = (
            f"{current.pages} pages, epub {current.epubcheck}, "
            f"preflight {current.preflight}"
        )

        if not frozen.auto_approve:
            decisions = approvals.results(context, GATE_PROOF)
            if not decisions:
                note.set(context, f"GATE 3 open: proof awaiting sign-off — {summary}")
                return go_to(ProofGate, input)
            decision = decisions[0]
            if decision.decision != "approve":
                failure.set(
                    context,
                    StageFailure(
                        stage=PROOF_GATE,
                        detail=f"rejected at GATE 3: {decision.note}",
                        at=_now(),
                    ),
                )
                return go_to(
                    RecoveryGate, StageRef(input.config_path, input.book, PROOF_GATE)
                )

        # Guard the only exit. RecoveryGate accepts `proof-gate` as a resume target, so
        # without this an operator could complete a volume that never assembled: the
        # first version of this Step trusted whatever manifest happened to be there.
        incomplete = finishing.missing_final_artifacts(
            current.harvested, current.master_md, current.interior_pdf
        )
        if incomplete:
            failure.set(
                context,
                StageFailure(
                    stage=PROOF_GATE,
                    detail=f"cannot sign off: missing {', '.join(incomplete)}",
                    at=_now(),
                ),
            )
            note.set(context, f"GATE 3 blocked: missing {', '.join(incomplete)}")
            return go_to(
                RecoveryGate, StageRef(input.config_path, input.book, PROOF_GATE)
            )

        stage.set(context, DONE)
        counts = _recount(context)
        tally.set(context, counts)
        note.set(
            context,
            f"book {frozen.book} finished: {summary}, ${counts.cost_usd}"
            + (" (gates auto-approved)" if frozen.auto_approve else ""),
        )
        await progress.write(context, f"book {frozen.book} finished")
        return graceful_complete(dumps(_manifest_dict(current)))


# --------------------------------------------------------------------------
# Operator recovery
# --------------------------------------------------------------------------


class RecoveryGate(Step[StageRef]):
    """Every stage's exhausted-retry target: park, record, wait for a decision.

    v1 gave each failure its own ad hoc diversion (a beat-plan failure landed on the
    director gate with a note; a harvest failure landed there too), so 'the volume is
    parked' and 'the volume is waiting for review' were the same state. Here the
    failure is its own state, carrying which stage failed and what it said, and the
    operator's `resume` message names the stage to re-enter.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _bookkeeping()

    def wait_for(self, context: Context, input: StageRef) -> Wait:
        stage.set(context, RECOVERY_GATE)
        # Recorded on entry, beside the wait. Synthesising it in `execute` meant a
        # parked volume reported no failure at all until the reminder timer fired —
        # up to a day of `status` showing a gate with nothing to explain it.
        if not (failure.get(context) or StageFailure()).stage:
            failure.set(
                context,
                StageFailure(stage=input.stage, detail=_last_failure(input.stage), at=_now()),
            )
        return Wait.any_of(
            resume.for_one(), Timer.by_duration(self.config.gate_reminder)
        )

    async def execute(self, context: Context, input: StageRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, RECOVERY_GATE)
        recorded = failure.get(context) or StageFailure()
        if not recorded.stage:
            recorded = StageFailure(
                stage=input.stage, detail=_last_failure(input.stage), at=_now()
            )
            failure.set(context, recorded)

        # Channel before timer, as at the gates.
        messages = resume.results(context)
        if not messages:
            note.set(
                context,
                f"parked after {recorded.stage} failed: {recorded.detail[:200]}",
            )
            return go_to(RecoveryGate, input)

        target = (messages[0] or "").strip() or recorded.stage
        if target not in RESUME_TARGETS:
            # Do NOT clear the failure record for an unrecognised target: a typo used to
            # erase the diagnosis and leave the volume parked with nothing to explain why.
            note.set(
                context,
                f"unknown resume target '{target}' — still parked after "
                f"{recorded.stage} failed. Valid: {', '.join(sorted(RESUME_TARGETS))}",
            )
            return go_to(RecoveryGate, input)
        failure.set(context, StageFailure())
        note.set(context, f"resuming at {target}")
        await progress.write(context, f"resuming at {target}")
        ref = StageRef(input.config_path, input.book, target)

        # Explicit dispatch, one movement per stage: a table lookup would hide the
        # graph from anyone reading this Flow.
        if target == INIT:
            return go_to(InitStep, StageRef(input.config_path, input.book, INIT))
        if target == CURATING:
            return go_to(
                CurateWaveStep,
                StageRef(
                    input.config_path,
                    input.book,
                    CURATING,
                    pending=_unplanned(context),
                    wave=1,
                ),
            )
        if target == DIRECTOR_GATE:
            return go_to(DirectorGate, ref)
        if target == APPROVING_ITEMS:
            return go_to(ApproveItemsStep, ref)
        if target == PRODUCING:
            return go_to(
                ProduceWaveStep,
                StageRef(
                    input.config_path,
                    input.book,
                    PRODUCING,
                    pending=_unproduced(context),
                    wave=1,
                ),
            )
        if target == QA_GATE:
            return go_to(QaGate, ref)
        if target == HARVESTING:
            return go_to(HarvestStep, ref)
        if target == ASSEMBLING:
            return go_to(AssembleStep, ref)
        if target == PRINTING:
            return go_to(PrintStep, ref)
        if target == CHECKING:
            return go_to(QualityStep, ref)
        if target == PROOF_GATE:
            return go_to(ProofGate, ref)
        # Unreachable: RESUME_TARGETS is validated above. Kept as the explicit
        # else-branch so every path out of this Step is visible in the graph.
        return go_to(RecoveryGate, input)


# --------------------------------------------------------------------------
# Flow
# --------------------------------------------------------------------------


class BookFlow(Flow[StageRef]):
    def __init__(
        self,
        config: Config,
        curate: CurateChapterFlow,
        produce: ProduceChapterFlow,
    ) -> None:
        self.config = config
        self.init = InitStep(config)
        self.curate_wave = CurateWaveStep(config, curate)
        self.director_gate = DirectorGate(config)
        self.approve_items = ApproveItemsStep(config)
        self.produce_wave = ProduceWaveStep(config, produce)
        self.qa_gate = QaGate(config)
        self.harvest = HarvestStep(config)
        self.assemble = AssembleStep(config)
        self.print = PrintStep(config)
        self.quality = QualityStep(config)
        self.proof_gate = ProofGate(config)
        self.recovery_gate = RecoveryGate(config)

    def get_flow_type(self) -> str:
        return "FanyiBook"

    def get_steps(self) -> StepList[StageRef]:
        return StepList.start_step(self.init).other_steps(
            self.curate_wave,
            self.director_gate,
            self.approve_items,
            self.produce_wave,
            self.qa_gate,
            self.harvest,
            self.assemble,
            self.print,
            self.quality,
            self.proof_gate,
            self.recovery_gate,
        )

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(
            stage,
            note,
            plan,
            tally,
            manifest,
            failure,
            chapters,
            chapter_plans,
            chapter_records,
            chapter_items,
            tier_overrides,
            approvals,
            resume,
            progress,
        )

    @rpc
    def snapshot(self, context: Context) -> RPCResult[Snapshot]:
        """One cohesive read model, so a UI or CLI needs exactly one request."""
        records = []
        for instance in chapters.get_all_instance_keys(context):
            record = chapters.get(context, instance)
            records.append(
                {
                    "hui": record.hui,
                    "local": record.local,
                    "curate": record.curate_status,
                    "beats": record.beats,
                    "coverage": record.coverage_pct,
                    "uncertain": record.uncertain,
                    "produce": record.produce_status,
                    "segments": record.segments,
                    "words": record.words,
                    "gate": record.gate_count,
                    "lift_ok": record.lift_ok,
                    "flattened": record.flattened_after,
                    "cost": record.cost_usd,
                    "error": record.error,
                }
            )
        # The gate this volume is actually parked on, not "every gate whose Channel is
        # empty" — which was true for all three gates for the whole life of the volume,
        # and inverted besides (a pending message means the gate is about to proceed).
        parked_on = {
            DIRECTOR_GATE: GATE_DIRECTOR,
            QA_GATE: GATE_QA,
            PROOF_GATE: GATE_PROOF,
        }.get(stage.get(context) or "")
        frozen = _plan(context)
        return RPCResult(
            Snapshot(
                stage=stage.get(context) or "",
                note=note.get(context) or "",
                plan_json=dumps(_plan_dict(frozen)),
                tally_json=dumps(_tally_dict(_recount(context))),
                chapters_json=dumps(records),
                manifest_json=dumps(_manifest_dict(_manifest(context))),
                failure_json=dumps(_failure_dict(failure.get(context) or StageFailure())),
                gates_pending=parked_on or "",
            )
        )


# --------------------------------------------------------------------------
# Helpers. Pure, and none of them hide a wait, a movement, or a recovery target.
# --------------------------------------------------------------------------


def _plan(context: Context) -> RunPlan:
    """An unset dataclass Attribute reads back as None, not as a default instance."""
    return plan.get(context) or RunPlan()


def _manifest(context: Context) -> Manifest:
    return manifest.get(context) or Manifest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _batch(context: Context, input: StageRef) -> list[int]:
    return ints(input.pending)[: max(1, _plan(context).wave_size)]


def _record(context: Context, hui: int) -> ChapterRecord:
    try:
        return chapters.get(context, key(hui))
    except KeyError:
        return ChapterRecord(hui=hui)


def _local(context: Context, hui: int) -> int:
    return _record(context, hui).local


def _outcome(result) -> ChapterOutcome:
    return result.single_output(ChapterOutcome)


def _add(left: str, right: str) -> str:
    return f"{float(left or 0.0) + float(right or 0.0):.6f}"


def _recount(context: Context) -> Tally:
    counts = Tally()
    cost = 0.0
    for instance in chapters.get_all_instance_keys(context):
        record = chapters.get(context, instance)
        counts.curate_total += 1
        counts.produce_total += 1
        counts.uncertain += record.uncertain
        cost += float(record.cost_usd or 0.0)
        if record.curate_status == "ok":
            counts.curate_done += 1
        elif record.curate_status == "failed":
            counts.curate_failed += 1
        if record.produce_status == "ok":
            counts.produce_done += 1
            if record.gate_count or not record.lift_ok or record.flattened_after:
                counts.attention += 1
        elif record.produce_status == "failed":
            counts.produce_failed += 1
    counts.cost_usd = f"{cost:.2f}"
    return counts


def _tier_overrides(context: Context) -> dict[str, str]:
    """Tier flips the director sent with the GATE 1 approval, as {beat_id: tier}.

    Read from the Attribute DirectorGate committed, not from the Channel: a Channel
    value is delivered only to the Step execution whose wait consumed it, so reading
    `approvals.results(...)` from a later Step returns nothing and every `--tier-override`
    was silently discarded.
    """
    payload = loads(tier_overrides.get(context), {}) or {}
    overrides = payload.get("tier_overrides") or {}
    return {str(k): str(v) for k, v in overrides.items()}


def _review_path(frozen: RunPlan) -> Path:
    return (
        Path(frozen.project_root)
        / "pipeline"
        / "run"
        / "dex"
        / "book"
        / f"book{frozen.book}"
        / "uncertain_review.json"
    )


def _write_review(context: Context, frozen: RunPlan) -> list[dict[str, Any]]:
    """Export the uncertain tier calls for the human, and count them on the tally."""
    project = Project(frozen.config_path)
    plans = [
        finishing.read_json(chapter_plans.get(context, key(hui)))
        for hui in frozen.hui_list
    ]
    review = finishing.uncertain_review(project, [p for p in plans if p])
    path = _review_path(frozen)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = _recount(context)
    counts.uncertain = len(review)
    tally.set(context, counts)
    return review


def _unplanned(context: Context) -> str:
    return join(
        [
            chapters.get(context, instance).hui
            for instance in chapters.get_all_instance_keys(context)
            if chapters.get(context, instance).curate_status != "ok"
        ]
    )


def _unproduced(context: Context) -> str:
    return join(
        [
            chapters.get(context, instance).hui
            for instance in chapters.get_all_instance_keys(context)
            if chapters.get(context, instance).produce_status != "ok"
        ]
    )


def _last_failure(input_stage: str) -> str:
    return f"exhausted retries in {input_stage}"


async def _epubcheck(config: Config, epub: str) -> tuple[str, str, int]:
    if config.dry_run:
        return "[dry-run] epubcheck", "", 0
    process = await asyncio.create_subprocess_exec(
        "epubcheck",
        epub,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    return (
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        process.returncode or 0,
    )


def _plan_dict(value: RunPlan) -> dict[str, Any]:
    return {
        "project": value.project_slug,
        "root": value.project_root,
        "config_sha256": value.config_sha256,
        "book": value.book,
        "title": value.book_title,
        "hui": [value.hui_lo, value.hui_hi],
        "chapters": value.chapters,
        "aggressive": value.aggressive,
        "auto_approve": value.auto_approve,
        "wave_size": value.wave_size,
        "curate_effort": value.curate_effort,
        "produce_effort": value.produce_effort,
        "started_at": value.started_at,
    }


def _tally_dict(value: Tally) -> dict[str, Any]:
    return {
        "curate": f"{value.curate_done}/{value.curate_total}",
        "curate_failed": value.curate_failed,
        "produce": f"{value.produce_done}/{value.produce_total}",
        "produce_failed": value.produce_failed,
        "attention": value.attention,
        "uncertain": value.uncertain,
        "costUsd": value.cost_usd,
    }


def _manifest_dict(value: Manifest) -> dict[str, Any]:
    return {
        "master_md": value.master_md,
        "docx": value.docx,
        "epub": value.epub,
        "interior_pdf": value.interior_pdf,
        "pages": value.pages,
        "epubcheck": value.epubcheck,
        "preflight": value.preflight,
        "checks_note": value.checks_note,
        "harvested": value.harvested,
        "vault_backup": value.vault_backup,
    }


def _failure_dict(value: StageFailure) -> dict[str, Any]:
    return {"stage": value.stage, "detail": value.detail, "at": value.at}
