"""One chapter's beat plan, as its own durable execution.

A chapter is a SubFlow rather than a Step in the volume Flow because it has its
own identity, its own retry boundary, and its own failure meaning. The volume
awaits a bounded batch of these as Conditions, which is what removes v1's
hand-rolled wave/join/failed-chapter/Channel-counting machinery.

The plan lands in a durable Attribute first and in a file second. v1 used the
file as the resume ledger, which meant the durable execution's own state and the
truth on disk could disagree.
"""

from __future__ import annotations

import json
from datetime import timedelta

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
    graceful_complete,
)

from fanyi_dex.book.model import (
    ChapterJob,
    ChapterOutcome,
    coverage,
    dumps,
)
from fanyi_dex.claude_cli import call_agent_for_json
from fanyi_dex.config import Config
from fanyi_dex.project import Project
from fanyi_dex.prompts import RETRY_NUDGE, beatplan_prompt

phase = Attribute("cc-phase", str, AttributeIndex(IndexType.KEYWORD))
plan_json = Attribute("cc-plan", str)
cost_usd = Attribute("cc-cost", str)
detail = Attribute("cc-detail", str)

progress = Stream("cc-progress", str, 1 << 16)


class PlanChapterStep(Step[ChapterJob]):
    """The one expensive call: source Chinese in, beat plan out."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=self.config.chapter_timeout,
            # Stated, not inherited: SDK 0.2.5 has no way to emit a heartbeat, so
            # a silent call's survival would otherwise depend on the server's
            # durability default rather than on this application's intent.
            heartbeat_timeout=self.config.chapter_timeout,
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(PlanFailedStep)

    async def execute(self, context: Context, input: ChapterJob) -> StepDecision:  # type: ignore[override]
        phase.set(context, "planning")
        project = Project(input.config_path)
        zh = project.read_source(input.hui)

        parsed, cost = await call_agent_for_json(
            self.config,
            beatplan_prompt(project, input.hui, zh, input.aggressive),
            label=f"beatplan B{input.book}h{input.hui}",
            nudge=RETRY_NUDGE,
            is_valid=_has_beats,
            effort=input.effort or self.config.effort,
        )

        beats = parsed.get("beats", [])
        plan = {
            "book": input.book,
            "hui": input.hui,
            "local": input.local,
            "zh": zh,
            "named": parsed.get("named", []),
            "verbatim_lines": parsed.get("verbatim_lines", []),
            "beats": beats,
            "uncertain": parsed.get("uncertain", []),
        }
        covered = coverage(beats, zh)

        plan_json.set(context, dumps(plan))
        cost_usd.set(context, f"{cost:.6f}")
        phase.set(context, "planned")
        detail.set(context, f"{len(beats)} beats, {covered}% coverage")

        # The export: how the plan reaches the human, and how it reaches the parent
        # Flow (a SubFlow outcome can only carry small values — see ChapterJob).
        # It is not the resume unit: this SubFlow's own Step history is.
        export = project.plan_path(input.book, input.hui)
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
        )

        await progress.write(
            context, f"h{input.hui}: {len(beats)} beats, coverage {covered}%"
        )
        return graceful_complete(
            ChapterOutcome(
                hui=input.hui,
                ok=True,
                detail=f"{len(beats)} beats",
                beats=len(beats),
                coverage_pct=covered,
                uncertain=len(plan["uncertain"]),
                cost_usd=f"{cost:.6f}",
                export_path=str(export),
            )
        )


class PlanFailedStep(Step[ChapterJob]):
    """Exhausted retries end the chapter as a failure, not as a quiet success.

    The parent's batch wait is satisfied by *closure*, so failing here cannot
    stall the volume — which is why v1's ChapterFailedStep (whose only job was to
    publish a completion so the join would not deadlock) has no counterpart.
    """

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
        phase.set(context, "failed")
        reason = f"beat plan for hui {input.hui} exhausted its retries"
        detail.set(context, reason)
        await progress.write(context, f"h{input.hui}: FAILED")
        return force_fail(reason)


class CurateChapterFlow(Flow[ChapterJob]):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.plan = PlanChapterStep(config)
        self.plan_failed = PlanFailedStep(config)

    def get_flow_type(self) -> str:
        return "FanyiCurateChapter"

    def get_steps(self) -> StepList[ChapterJob]:
        return StepList.start_step(self.plan).other_steps(self.plan_failed)

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(phase, plan_json, cost_usd, detail, progress)


def _has_beats(parsed: dict) -> bool:
    beats = parsed.get("beats")
    return isinstance(beats, list) and len(beats) > 0
