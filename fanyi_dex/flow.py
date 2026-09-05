"""Beat-plan (STEP 0) as a durable Flow: one Flow per volume, one Step per chapter.

What this actually buys, stated honestly:

* **Surviving session death.** Workflow's `resumeFromRunId` cache is same-session
  only, so when a connection dies late in a long volume, every chapter finished so
  far was finished inside something that no longer exists. Each chapter here lands
  in sqlite (and a plan file) the moment it completes, so a worker crash, a
  reboot, or a week's gap costs nothing.
* **Gates that outlive a run.** A Workflow run has to end before the director
  reviews a volume's uncertain tier calls, so the gate is a thing you remember rather
  than part of the pipeline. Here `stage` is durable, queryable state.
* **A timeout this side of the harness.** A separate `claude -p` process has no
  180s first-token watchdog, so Dex sets the budget.

What it does NOT buy: parallel chapter execution and parse-retry, both of which
the Workflow tool already has — `build_beatplan.py` picks a serial loop by
choice, and its 3-attempt retry is ported here rather than invented. And the
watchdog argument is weak *for this stage specifically*: build_beatplan.py's
header records that no-schema + low-effort already fixed the stall. That fix is
honored here (same model, same effort, no schema). The watchdog case is real for
Pass-1, which still runs schemas at medium effort.

What this does NOT change: the prompt (ported verbatim in `prompts.py`), the
tier policy (read from the project config), and the artifact handed downstream —
`harvest_beatplan.py` consumes the emitted `{"plans": [...]}` file unchanged.

Chapters run in bounded waves rather than all at once, which keeps the
rate-limit safety of the serial loop while still overlapping work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from dex import (
    Attribute,
    AttributeIndex,
    AttributeMap,
    Channel,
    Context,
    Flow,
    IndexType,
    PersistenceSchema,
    RetryPolicy,
    RPCResult,
    Step,
    StepDecision,
    StepList,
    StepMovement,
    StepOptions,
    Timer,
    Wait,
    dead_end,
    go_to,
    go_to_many,
    graceful_complete,
    rpc,
)

from fanyi_dex.claude_cli import AgentCallFailed, call_agent_for_json
from fanyi_dex.config import Config
from fanyi_dex.project import Project
from fanyi_dex.prompts import RETRY_NUDGE, beatplan_prompt
from fanyi_dex.shell import run_python

# --------------------------------------------------------------------------
# Durable state
# --------------------------------------------------------------------------

stage = Attribute("stage", str, AttributeIndex(IndexType.KEYWORD))
note = Attribute("note", str)
chapters_total = Attribute("chapters-total", int)
chapters_done = Attribute("chapters-done", int)
chapters_failed = Attribute("chapters-failed", int)
uncertain_count = Attribute("uncertain-count", int)
cost_usd = Attribute("cost-usd", str)
# Keyed per chapter, so the parallel Steps never write the same attribute.
chapter_status = AttributeMap("chapter-status", str)

chapter_done = Channel[str]("chapter-done", str)
tiers_reviewed = Channel[str]("tiers-reviewed", str)

PLANNING = "planning"
RUNNING = "running-chapters"
COMBINING = "combining"
HARVESTING = "harvesting"
DIRECTOR_GATE = "director-gate"
DONE = "done"


# --------------------------------------------------------------------------
# Step inputs. Every field is a primitive: lists travel as comma-joined
# strings so codec derivation stays trivial and the payloads stay small.
# --------------------------------------------------------------------------


@dataclass
class VolumeInput:
    config_path: str
    book: int
    aggressive: bool = False
    only: str = ""
    wave_size: int = 0


@dataclass
class WaveInput:
    config_path: str
    book: int
    aggressive: bool
    pending: str
    wave_size: int
    wave: int


@dataclass
class WaveJoinInput:
    config_path: str
    book: int
    aggressive: bool
    pending: str
    wave_size: int
    wave: int
    batch: int


@dataclass
class ChapterInput:
    config_path: str
    book: int
    hui: int
    local: int
    aggressive: bool


@dataclass
class VolumeRef:
    config_path: str
    book: int


def _ints(joined: str) -> list[int]:
    return [int(x) for x in joined.split(",") if x.strip()]


def _join(values: list[int]) -> str:
    return ",".join(str(v) for v in values)


def _waiting_options() -> StepOptions:
    """Unlimited retries for Steps whose job is to wait or to bookkeep.

    A capped count would turn a worker restart into a FAILED flow, and for the
    wave join it would also deadlock the volume.
    """
    return StepOptions(
        execute_retry=RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
        )
    )


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class StartStep(Step[VolumeInput]):
    """Resolves the chapter list, skipping any chapter already planned."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: VolumeInput) -> StepDecision:  # type: ignore[override]
        stage.set(context, PLANNING)
        project = Project(input.config_path)
        project.require_beatplan_support()

        only = _ints(input.only) or None
        chapters = project.chapters(input.book, only)
        stage_dir = project.stage_dir(input.book)
        stage_dir.mkdir(parents=True, exist_ok=True)

        pending, skipped = [], []
        for chapter in chapters:
            if _plan_is_good(project, input.book, chapter.hui):
                skipped.append(chapter.hui)
                chapter_status.set(context, str(chapter.hui), "cached")
            else:
                pending.append(chapter.hui)

        chapters_total.set(context, len(chapters))
        chapters_done.set(context, len(skipped))
        chapters_failed.set(context, 0)
        cost_usd.set(context, "0.00")
        stage.set(context, RUNNING)
        note.set(
            context,
            f"{len(pending)} chapter(s) to plan, {len(skipped)} already on disk",
        )

        wave_size = input.wave_size or self.config.wave_size
        return go_to(
            WaveStep,
            WaveInput(
                config_path=input.config_path,
                book=input.book,
                aggressive=input.aggressive,
                pending=_join(pending),
                wave_size=max(1, wave_size),
                wave=1,
            ),
        )


class WaveStep(Step[WaveInput]):
    """Fans out one bounded wave of chapters, plus the join that awaits them."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: WaveInput) -> StepDecision:  # type: ignore[override]
        stage.set(context, RUNNING)
        pending = _ints(input.pending)
        if not pending:
            return go_to(CombineStep, VolumeRef(input.config_path, input.book))

        project = Project(input.config_path)
        locals_by_hui = {c.hui: c.local for c in project.chapters(input.book)}

        batch = pending[: input.wave_size]
        remainder = pending[input.wave_size :]
        note.set(
            context,
            f"wave {input.wave}: chapters {_join(batch)}"
            + (f" ({len(remainder)} still queued)" if remainder else ""),
        )

        movements: list[StepMovement[Any]] = [
            StepMovement.of(
                WaveJoinStep,
                WaveJoinInput(
                    config_path=input.config_path,
                    book=input.book,
                    aggressive=input.aggressive,
                    pending=_join(remainder),
                    wave_size=input.wave_size,
                    wave=input.wave,
                    batch=len(batch),
                ),
            )
        ]
        movements.extend(
            StepMovement.of(
                ChapterStep,
                ChapterInput(
                    config_path=input.config_path,
                    book=input.book,
                    hui=hui,
                    local=locals_by_hui.get(hui, 0),
                    aggressive=input.aggressive,
                ),
            )
            for hui in batch
        )
        return go_to_many(*movements)


class ChapterStep(Step[ChapterInput]):
    """One chapter, one headless Claude call, one plan file."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            # The reason this project exists: no 180s watchdog.
            execute_method_timeout=self.config.chapter_timeout,
            # The parse-retry loop lives inside execute (as in the Workflow
            # script). This outer policy is for infrastructure trouble.
            execute_retry=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=3,
            ),
        ).on_execute_failure_proceed_to(ChapterFailedStep)

    async def execute(self, context: Context, input: ChapterInput) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        zh = project.read_source(input.hui)
        prompt = beatplan_prompt(project, input.hui, zh, input.aggressive)
        label = f"beatplan B{input.book}h{input.hui}"

        parsed, cost = await call_agent_for_json(
            self.config,
            prompt,
            label=label,
            nudge=RETRY_NUDGE,
            is_valid=_has_beats,
        )

        plan = {
            "book": input.book,
            "hui": input.hui,
            "local": input.local,
            "zh": zh,
            "named": parsed.get("named", []),
            "verbatim_lines": parsed.get("verbatim_lines", []),
            "beats": parsed.get("beats", []),
            "uncertain": parsed.get("uncertain", []),
            "_dex": {"cost_usd": round(cost, 6)},
        }
        path = project.plan_path(input.book, input.hui)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        project.failure_path(input.book, input.hui).unlink(missing_ok=True)

        chapter_status.set(
            context, str(input.hui), f"ok:{len(plan['beats'])}beats"
        )
        chapter_done.publish(context, f"{input.hui}:ok")
        return dead_end()


class ChapterFailedStep(Step[ChapterInput]):
    """Records a chapter that exhausted its retries, then releases the wave.

    Publishing here is what stops one bad chapter from deadlocking the volume:
    the wave join is counting completions, not successes.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: ChapterInput) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        marker = project.failure_path(input.book, input.hui)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"chapter {input.hui} exhausted its retries; re-run with "
            f"--only {input.hui}\n",
            encoding="utf-8",
        )
        chapter_status.set(context, str(input.hui), "failed")
        chapter_done.publish(context, f"{input.hui}:failed")
        return dead_end()


class WaveJoinStep(Step[WaveJoinInput]):
    """Awaits this wave's completions, then starts the next wave."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    def wait_for(self, context: Context, input: WaveJoinInput) -> Wait:
        return Wait.until(chapter_done.for_n(input.batch))

    async def execute(self, context: Context, input: WaveJoinInput) -> StepDecision:  # type: ignore[override]
        stage.set(context, RUNNING)
        project = Project(input.config_path)
        done, failed, cost = _tally(project, input.book)
        chapters_done.set(context, done)
        chapters_failed.set(context, failed)
        cost_usd.set(context, f"{cost:.2f}")
        note.set(context, f"wave {input.wave} complete: {done} planned, {failed} failed")

        return go_to(
            WaveStep,
            WaveInput(
                config_path=input.config_path,
                book=input.book,
                aggressive=input.aggressive,
                pending=input.pending,
                wave_size=input.wave_size,
                wave=input.wave + 1,
            ),
        )


class CombineStep(Step[VolumeRef]):
    """Writes the `{"plans": [...]}` file that harvest_beatplan.py expects."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: VolumeRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, COMBINING)
        project = Project(input.config_path)
        plans = []
        for path in sorted(project.stage_dir(input.book).glob("h*.json")):
            try:
                plans.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        combined = project.combined_path(input.book)
        combined.write_text(
            json.dumps({"plans": plans}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        done, failed, cost = _tally(project, input.book)
        chapters_done.set(context, done)
        chapters_failed.set(context, failed)
        cost_usd.set(context, f"{cost:.2f}")
        note.set(context, f"combined {len(plans)} plan(s) -> {combined}")
        if not plans:
            note.set(
                context,
                f"no chapter plans produced ({failed} failed) — nothing to harvest; "
                f"re-run the failed chapters with --only",
            )
            stage.set(context, DIRECTOR_GATE)
            return go_to(DirectorGate, input)
        return go_to(HarvestStep, input)


class HarvestStep(Step[VolumeRef]):
    """Runs the project's own harvest_beatplan.py, unmodified."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return StepOptions(
            execute_method_timeout=timedelta(minutes=30),
            execute_retry=RetryPolicy(maximum_attempts=2),
            # A harvest problem should park the volume for inspection, not kill
            # the Flow — the chapter plans are already on disk.
        ).on_execute_failure_proceed_to(DirectorGate)

    async def execute(self, context: Context, input: VolumeRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, HARVESTING)
        project = Project(input.config_path)
        script = project.pipeline_script("harvest_beatplan.py")
        result = await run_python(
            self.config,
            script,
            [str(project.combined_path(input.book))],
            cwd=script.parent,
        )
        uncertain = _scrape_uncertain(result.stdout)
        uncertain_count.set(context, uncertain)
        stage.set(context, DIRECTOR_GATE)
        _, failed, _ = _tally(project, input.book)
        summary = f"{uncertain} uncertain tier call(s) to review"
        if failed:
            summary += f"; {failed} chapter(s) failed — re-run those with --only"
        note.set(context, summary)
        return go_to(DirectorGate, input)


class DirectorGate(Step[VolumeRef]):
    """GATE: the director reviews the uncertain tier calls before Pass-1."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    def wait_for(self, context: Context, input: VolumeRef) -> Wait:
        # Stamped here, not in execute: a gate blocks in wait_for, so execute
        # does not run until the gate opens. Stamping on entry is what keeps
        # `stage` truthful when this gate is reached by a failure diversion.
        stage.set(context, DIRECTOR_GATE)
        return Wait.any_of(
            tiers_reviewed.for_one(),
            Timer.by_duration(self.config.gate_reminder),
        )

    async def execute(self, context: Context, input: VolumeRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, DIRECTOR_GATE)
        project = Project(input.config_path)
        if context.has_timer_fired():
            review = project.root / "cutlists" / "beatplan_review.json"
            note.set(context, f"awaiting director review: {review}")
            return go_to(DirectorGate, input)
        stage.set(context, DONE)
        note.set(context, "tier calls reviewed — ready for Pass-1 (build_pass2.py)")
        return graceful_complete(f"beatplan:book{input.book}")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _has_beats(parsed: dict[str, Any]) -> bool:
    beats = parsed.get("beats")
    return isinstance(beats, list) and len(beats) > 0


def _plan_is_good(project: Project, book: int, hui: int) -> bool:
    path = project.plan_path(book, hui)
    if not path.is_file():
        return False
    try:
        return _has_beats(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return False


def _tally(project: Project, book: int) -> tuple[int, int, float]:
    stage_dir = project.stage_dir(book)
    done = 0
    cost = 0.0
    for path in stage_dir.glob("h*.json"):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if _has_beats(plan):
            done += 1
        cost += float(plan.get("_dex", {}).get("cost_usd") or 0.0)
    failed = len(list(stage_dir.glob("h*.FAILED.txt")))
    return done, failed, cost


def _scrape_uncertain(stdout: str) -> int:
    """harvest_beatplan.py prints `uncertain tier calls -> <path> (N to review)`."""
    for line in stdout.splitlines():
        if "uncertain tier calls ->" in line and "(" in line:
            fragment = line.rsplit("(", 1)[1]
            digits = "".join(ch for ch in fragment if ch.isdigit())
            if digits:
                return int(digits)
    return 0


# --------------------------------------------------------------------------
# Flow
# --------------------------------------------------------------------------


class BeatPlanFlow(Flow[VolumeInput]):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.start = StartStep(config)
        self.wave = WaveStep(config)
        self.chapter = ChapterStep(config)
        self.chapter_failed = ChapterFailedStep(config)
        self.wave_join = WaveJoinStep(config)
        self.combine = CombineStep(config)
        self.harvest = HarvestStep(config)
        self.director_gate = DirectorGate(config)

    def get_flow_type(self) -> str:
        return "FanyiBeatPlan"

    def get_steps(self) -> StepList[VolumeInput]:
        return StepList.start_step(self.start).other_steps(
            self.wave,
            self.chapter,
            self.chapter_failed,
            self.wave_join,
            self.combine,
            self.harvest,
            self.director_gate,
        )

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(
            stage,
            note,
            chapters_total,
            chapters_done,
            chapters_failed,
            uncertain_count,
            cost_usd,
            chapter_status,
            chapter_done,
            tiers_reviewed,
        )

    @rpc
    def status(self, context: Context) -> RPCResult[str]:
        return RPCResult(
            json.dumps(
                {
                    "stage": stage.get(context),
                    "note": note.get(context),
                    "total": chapters_total.get(context),
                    "done": chapters_done.get(context),
                    "failed": chapters_failed.get(context),
                    "uncertain": uncertain_count.get(context),
                    "costUsd": cost_usd.get(context),
                },
                ensure_ascii=False,
            )
        )
