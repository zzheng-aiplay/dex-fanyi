"""Two-pass transcreation (STEP 1 -> 1b -> 2 -> 3) as a durable Flow.

Ported from the workflow `build_pass2.py` generates. One Flow per volume; within
a volume, chapters run in bounded waves; within a chapter, each of the six phases
is its own durable Step:

    Pass1 -> DialogueRepair -> Pass2 -> Audit -> [Remediate] -> Finalize

Per-phase Steps are the point. Pass-1 and Pass-2 are the expensive calls, so a
failure in the audit must not redo them. Each phase writes its artifact to
`pipeline/run/dex/pass1/book<N>/h<NNN>/`, and a re-run skips any phase whose
artifact is already on disk — so an interrupted volume resumes mid-chapter, not
just mid-volume.

This is the stage where the harness watchdog genuinely bit: the generated
workflow calls every phase with a structured-output `schema:` at
`effort:'medium'`, which is the request shape the project's own notes blame for
stalling at zero tokens. `claude -p` has no schema parameter, so the prompts ask
for JSON as text and parse-retry instead — the shape already proven on the
beat-plan stage — and Dex owns the timeout.

Harvest is deliberately NOT run here: `harvest_reprocess.py` writes
`回NN (edited).md` into the Obsidian vault. The Flow parks at a review gate and
prints the command instead.
"""

from __future__ import annotations

import asyncio
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

from fanyi_dex import detscan
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
from fanyi_dex.project import Project, lift_held, load_items_file
from fanyi_dex.prompts import RETRY_NUDGE, TIER_FULL

# --------------------------------------------------------------------------
# Durable state
# --------------------------------------------------------------------------

stage = Attribute("p1-stage", str, AttributeIndex(IndexType.KEYWORD))
note = Attribute("p1-note", str)
chapters_total = Attribute("p1-chapters-total", int)
chapters_done = Attribute("p1-chapters-done", int)
chapters_failed = Attribute("p1-chapters-failed", int)
attention_count = Attribute("p1-attention", int)
cost_usd = Attribute("p1-cost-usd", str)
chapter_status = AttributeMap("p1-chapter-status", str)

chapter_done = Channel[str]("p1-chapter-done", str)
pass1_reviewed = Channel[str]("p1-reviewed", str)

PLANNING = "planning"
RUNNING = "running-chapters"
COMBINING = "combining"
REVIEW_GATE = "review-gate"
DONE = "done"


# --------------------------------------------------------------------------
# Step inputs
# --------------------------------------------------------------------------


@dataclass
class VolumeInput:
    config_path: str
    book: int
    items_path: str
    only: str = ""
    wave_size: int = 0


@dataclass
class WaveInput:
    config_path: str
    book: int
    pending: str
    wave_size: int
    wave: int


@dataclass
class WaveJoinInput:
    config_path: str
    book: int
    pending: str
    wave_size: int
    wave: int
    batch: int


@dataclass
class ChapterRef:
    config_path: str
    book: int
    hui: int


@dataclass
class VolumeRef:
    config_path: str
    book: int


def _ints(joined: str) -> list[int]:
    return [int(x) for x in joined.split(",") if x.strip()]


def _join(values: list[int]) -> str:
    return ",".join(str(v) for v in values)


def _waiting_options() -> StepOptions:
    """Unlimited retries for waiting/bookkeeping Steps.

    A capped count turns a worker restart into a FAILED flow, and on the wave
    join it would deadlock the volume.
    """
    return StepOptions(
        execute_retry=RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
        )
    )


def _phase_options(config: Config) -> StepOptions:
    return StepOptions(
        execute_method_timeout=config.pass1_timeout,
        execute_retry=RetryPolicy(
            initial_interval=timedelta(seconds=30),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=3,
        ),
    ).on_execute_failure_proceed_to(ChapterFailedStep)


# --------------------------------------------------------------------------
# Artifact helpers
# --------------------------------------------------------------------------


def _read(path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write(path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _load_item(project: Project, book: int, hui: int) -> dict[str, Any]:
    return _read(project.phase_path(book, hui, "item"))


def _segments_by_id(segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["beat_id"]: s for s in segments if s.get("beat_id")}


def _reattach(
    base: list[dict[str, Any]], returned: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay returned prose onto `base` by beat_id, keeping base order/metadata.

    The agent never echoes zh_span (echoing ~400 chars of Chinese per beat stalls
    generation), so the source span and verse flags are re-attached here — and a
    segment the agent dropped keeps its previous prose rather than vanishing.
    """
    got = _segments_by_id(returned)
    out = []
    for seg in base:
        replacement = got.get(seg["beat_id"])
        prose = (replacement or {}).get("prose")
        out.append({**seg, "prose": prose if prose is not None else seg.get("prose", "")})
    return out


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


def _bump_cost(project: Project, book: int, hui: int, phase: str, cost: float) -> None:
    path = project.phase_path(book, hui, "cost")
    ledger = {}
    if path.is_file():
        try:
            ledger = _read(path)
        except json.JSONDecodeError:
            ledger = {}
    ledger[phase] = round(ledger.get(phase, 0.0) + cost, 6)
    _write(path, ledger)


async def _gather_bounded(config: Config, factories: list[Any]) -> list[Any]:
    """Run coroutine factories with a concurrency cap.

    Bounds the fan-outs *inside* one chapter (dialogue repairs, audit lenses) so
    a single chapter cannot spawn a dozen Claude processes at once.
    """
    semaphore = asyncio.Semaphore(max(1, config.inner_concurrency))

    async def run(factory):
        async with semaphore:
            return await factory()

    return await asyncio.gather(*(run(f) for f in factories))


# --------------------------------------------------------------------------
# Volume orchestration
# --------------------------------------------------------------------------


class Pass1StartStep(Step[VolumeInput]):
    """Validates config + items, stages one item.json per chapter, plans the waves."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: VolumeInput) -> StepDecision:  # type: ignore[override]
        stage.set(context, PLANNING)
        project = Project(input.config_path)
        project.require_pass1_support()

        chapters = load_items_file(input.items_path, input.book)
        if not chapters:
            raise RuntimeError(
                f"{input.items_path} bound zero chapters for book {input.book}"
            )
        only = set(_ints(input.only))
        if only:
            chapters = [c for c in chapters if c.get("hui") in only]

        pending, cached = [], []
        for chapter in chapters:
            hui = chapter["hui"]
            _write(project.phase_path(input.book, hui, "item"), chapter)
            if project.phase_path(input.book, hui, "chapter").is_file():
                cached.append(hui)
                chapter_status.set(context, str(hui), "cached")
            else:
                pending.append(hui)

        chapters_total.set(context, len(chapters))
        chapters_done.set(context, len(cached))
        chapters_failed.set(context, 0)
        attention_count.set(context, 0)
        cost_usd.set(context, "0.00")
        stage.set(context, RUNNING)
        warnings = project.pass1_warnings()
        note.set(
            context,
            f"{len(pending)} chapter(s) to transcreate, {len(cached)} already done"
            + (f" | WARNING: {warnings[0]}" if warnings else ""),
        )

        return go_to(
            WaveStep,
            WaveInput(
                config_path=input.config_path,
                book=input.book,
                pending=_join(sorted(pending)),
                wave_size=max(1, input.wave_size or self.config.wave_size),
                wave=1,
            ),
        )


class WaveStep(Step[WaveInput]):
    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: WaveInput) -> StepDecision:  # type: ignore[override]
        stage.set(context, RUNNING)
        pending = _ints(input.pending)
        if not pending:
            return go_to(CombineStep, VolumeRef(input.config_path, input.book))

        batch = pending[: input.wave_size]
        remainder = pending[input.wave_size :]
        note.set(
            context,
            f"wave {input.wave}: chapters {_join(batch)}"
            + (f" ({len(remainder)} queued)" if remainder else ""),
        )

        movements: list[StepMovement[Any]] = [
            StepMovement.of(
                WaveJoinStep,
                WaveJoinInput(
                    config_path=input.config_path,
                    book=input.book,
                    pending=_join(remainder),
                    wave_size=input.wave_size,
                    wave=input.wave,
                    batch=len(batch),
                ),
            )
        ]
        movements.extend(
            StepMovement.of(Pass1Step, ChapterRef(input.config_path, input.book, hui))
            for hui in batch
        )
        return go_to_many(*movements)


class WaveJoinStep(Step[WaveJoinInput]):
    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    def wait_for(self, context: Context, input: WaveJoinInput) -> Wait:
        return Wait.until(chapter_done.for_n(input.batch))

    async def execute(self, context: Context, input: WaveJoinInput) -> StepDecision:  # type: ignore[override]
        stage.set(context, RUNNING)
        project = Project(input.config_path)
        done, failed, cost, attention = _tally(project, input.book)
        chapters_done.set(context, done)
        chapters_failed.set(context, failed)
        attention_count.set(context, attention)
        cost_usd.set(context, f"{cost:.2f}")
        note.set(
            context,
            f"wave {input.wave} complete: {done} done, {failed} failed, {attention} need attention",
        )
        return go_to(
            WaveStep,
            WaveInput(
                config_path=input.config_path,
                book=input.book,
                pending=input.pending,
                wave_size=input.wave_size,
                wave=input.wave + 1,
            ),
        )


class CombineStep(Step[VolumeRef]):
    """Writes the `{"chapters": [...]}` file harvest_reprocess.py consumes."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: VolumeRef) -> StepDecision:  # type: ignore[override]
        stage.set(context, COMBINING)
        project = Project(input.config_path)
        chapters = []
        for directory in sorted(project.pass1_dir(input.book).glob("h*")):
            record = directory / "chapter.json"
            if not record.is_file():
                continue
            try:
                chapters.append(_read(record))
            except json.JSONDecodeError:
                continue
        combined = project.pass1_combined(input.book)
        _write(combined, {"chapters": chapters})

        done, failed, cost, attention = _tally(project, input.book)
        chapters_done.set(context, done)
        chapters_failed.set(context, failed)
        attention_count.set(context, attention)
        cost_usd.set(context, f"{cost:.2f}")
        stage.set(context, REVIEW_GATE)
        note.set(
            context,
            f"combined {len(chapters)} chapter(s) -> {combined}"
            + (f" | {attention} need attention" if attention else "")
            + (f" | {failed} failed" if failed else "")
            # "all clear" only when something was actually produced and nothing
            # is flagged — not when the volume produced nothing at all.
            + ("" if (attention or failed or not chapters) else " | all clear"),
        )
        return go_to(ReviewGate, input)


class ReviewGate(Step[VolumeRef]):
    """GATE: review the QA signals before anything is written into the vault.

    Harvest is not run automatically because `harvest_reprocess.py` overwrites
    `回NN (edited).md` in the Obsidian vault.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    def wait_for(self, context: Context, input: VolumeRef) -> Wait:
        # Stamped in wait_for, not execute: a gate blocks here, so execute does
        # not run until the gate opens, and a failure diversion would otherwise
        # leave `stage` reporting the phase it was leaving.
        stage.set(context, REVIEW_GATE)
        return Wait.any_of(
            pass1_reviewed.for_one(),
            Timer.by_duration(self.config.gate_reminder),
        )

    async def execute(self, context: Context, input: VolumeRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        if context.has_timer_fired():
            note.set(
                context,
                f"awaiting review of {project.pass1_combined(input.book)} "
                f"(then: harvest_reprocess.py <that file> --config {project.config_path})",
            )
            return go_to(ReviewGate, input)
        stage.set(context, DONE)
        note.set(context, "reviewed — ready to harvest into the vault")
        return graceful_complete(f"pass1:book{input.book}")


class ChapterFailedStep(Step[ChapterRef]):
    """Records a chapter that exhausted retries, then releases the wave.

    Publishing here is what stops one bad chapter from deadlocking a volume: the
    wave join counts completions, not successes.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _waiting_options()

    async def execute(self, context: Context, input: ChapterRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        marker = project.chapter_dir(input.book, input.hui) / "FAILED.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        phases = sorted(
            p.stem for p in project.chapter_dir(input.book, input.hui).glob("*.json")
        )
        marker.write_text(
            f"chapter {input.hui} exhausted its retries.\n"
            f"phases completed: {', '.join(phases) or 'none'}\n"
            f"re-run with --only {input.hui} (completed phases are reused)\n",
            encoding="utf-8",
        )
        chapter_status.set(context, str(input.hui), "failed")
        chapter_done.publish(context, f"{input.hui}:failed")
        return dead_end()


# --------------------------------------------------------------------------
# Chapter phases
# --------------------------------------------------------------------------


class Pass1Step(Step[ChapterRef]):
    """STEP 1: transcreate each beat from the Chinese, applying its tier."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _phase_options(self.config)

    async def execute(self, context: Context, input: ChapterRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        target = project.phase_path(input.book, input.hui, "p1")
        if not target.is_file():
            item = _load_item(project, input.book, input.hui)
            blocks = VoiceBlocks(project)
            parsed, cost = await call_agent_for_json(
                self.config,
                pass1_prompt(blocks, item),
                label=f"pass1 h{input.hui}",
                nudge=RETRY_NUDGE,
                is_valid=_has_segments,
                effort=self.config.pass1_effort,
            )
            by_id = {b["id"]: b for b in item["beats"]}
            # Keep the PLAN's order and membership, not the reply's. A beat_id the
            # plan never had means invented prose with no source span behind it,
            # so it is dropped rather than carried into the book; a beat the reply
            # skipped is recorded as missing instead of silently vanishing.
            returned_by_id = {
                r.get("beat_id"): r for r in parsed["segments"] if r.get("beat_id")
            }
            unknown = sorted(set(returned_by_id) - set(by_id))
            segments = []
            for beat in item["beats"]:
                returned = returned_by_id.get(beat["id"], {})
                segments.append(
                    {
                        "beat_id": beat["id"],
                        "tier": returned.get("tier") or beat.get("tier"),
                        "prose": returned.get("prose") or "",
                        "zh_span": beat.get("zh_span", ""),
                        "is_verse": beat.get("is_verse"),
                        "verse_kind": beat.get("verse_kind"),
                    }
                )
            missing = [
                b["id"]
                for b in item["beats"]
                if b.get("tier") == TIER_FULL
                and not (returned_by_id.get(b["id"], {}).get("prose") or "").strip()
            ]
            _write(
                target,
                {
                    "hui": input.hui,
                    "title_en": parsed.get("title_en", ""),
                    "segments": segments,
                    "unknown_beat_ids": unknown,
                    "missing_beat_ids": missing,
                },
            )
            _bump_cost(project, input.book, input.hui, "pass1", cost)
            chapter_status.set(context, str(input.hui), f"pass1:{len(segments)}seg")
        return go_to(DialogueRepairStep, input)


class DialogueRepairStep(Step[ChapterRef]):
    """STEP 1b: re-render any beat that flattened direct speech into narration."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _phase_options(self.config)

    async def execute(self, context: Context, input: ChapterRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        target = project.phase_path(input.book, input.hui, "p1_repaired")
        if not target.is_file():
            item = _load_item(project, input.book, input.hui)
            p1 = _read(project.phase_path(input.book, input.hui, "p1"))
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
                        effort=self.config.pass1_effort,
                    )
                    return index, parsed["prose"], cost

                results = await _gather_bounded(
                    self.config, [lambda i=i, s=s: repair(i, s) for i, s in bad]
                )
                for index, prose, cost in results:
                    segments[index] = {**segments[index], "prose": prose}
                    total_cost += cost

            remaining = sum(1 for s in segments if detscan.is_flattened(s))
            _write(
                target,
                {
                    "hui": input.hui,
                    "title_en": p1.get("title_en", ""),
                    "segments": segments,
                    "flattened_p1": len(bad),
                    "flattened_p1_after": remaining,
                },
            )
            _bump_cost(project, input.book, input.hui, "dlg_repair", total_cost)
            chapter_status.set(
                context, str(input.hui), f"dlg:{len(bad)}->{remaining}"
            )
        return go_to(Pass2Step, input)


class Pass2Step(Step[ChapterRef]):
    """STEP 2: fluency rewrite into the shipping voice."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _phase_options(self.config)

    async def execute(self, context: Context, input: ChapterRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        target = project.phase_path(input.book, input.hui, "p2")
        if not target.is_file():
            item = _load_item(project, input.book, input.hui)
            repaired = _read(project.phase_path(input.book, input.hui, "p1_repaired"))
            blocks = VoiceBlocks(project)
            parsed, cost = await call_agent_for_json(
                self.config,
                pass2_prompt(blocks, project, item, repaired["segments"]),
                label=f"pass2 h{input.hui}",
                nudge=RETRY_NUDGE,
                is_valid=_has_segments,
                effort=self.config.pass1_effort,
            )
            segments = _reattach(repaired["segments"], parsed["segments"])
            _write(target, {"hui": input.hui, "segments": segments})
            _bump_cost(project, input.book, input.hui, "pass2", cost)
            chapter_status.set(context, str(input.hui), "pass2")
        return go_to(AuditStep, input)


class AuditStep(Step[ChapterRef]):
    """STEP 3a: three adversarial lenses — calque, archaism, fidelity."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _phase_options(self.config)

    async def execute(self, context: Context, input: ChapterRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        target = project.phase_path(input.book, input.hui, "audit")
        if not target.is_file():
            item = _load_item(project, input.book, input.hui)
            p2 = _read(project.phase_path(input.book, input.hui, "p2"))
            segments = p2["segments"]
            english = detscan.assemble(segments)

            async def run_lens(lens: dict[str, str]):
                parsed, cost = await call_agent_for_json(
                    self.config,
                    audit_prompt(
                        project, lens, input.hui, english, segments, item.get("zh", "")
                    ),
                    label=f"audit:{lens['name']} h{input.hui}",
                    nudge=RETRY_NUDGE,
                    is_valid=_is_audit,
                    effort=self.config.pass1_effort,
                )
                return parsed, cost

            results = await _gather_bounded(
                self.config, [lambda L=L: run_lens(L) for L in LENSES]
            )
            merged = {
                "calques": [c for parsed, _ in results for c in parsed.get("calques") or []],
                "archaisms": [a for parsed, _ in results for a in parsed.get("archaisms") or []],
                "flattened": [f for parsed, _ in results for f in parsed.get("flattened") or []],
                "fidelity_issues": [
                    f for parsed, _ in results for f in parsed.get("fidelity_issues") or []
                ],
            }
            _write(target, merged)
            _bump_cost(
                project, input.book, input.hui, "audit", sum(c for _, c in results)
            )
            findings = sum(len(v) for v in merged.values())
            chapter_status.set(context, str(input.hui), f"audit:{findings}")

        merged = _read(target)
        if sum(len(v) for v in merged.values()) > 0:
            return go_to(RemediateStep, input)
        return go_to(FinalizeStep, input)


class RemediateStep(Step[ChapterRef]):
    """STEP 3b: fix the flagged spans, then re-audit with the first two lenses."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _phase_options(self.config)

    async def execute(self, context: Context, input: ChapterRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        target = project.phase_path(input.book, input.hui, "remediated")
        if not target.is_file():
            item = _load_item(project, input.book, input.hui)
            p2 = _read(project.phase_path(input.book, input.hui, "p2"))
            merged = _read(project.phase_path(input.book, input.hui, "audit"))
            blocks = VoiceBlocks(project)

            parsed, cost = await call_agent_for_json(
                self.config,
                remediate_prompt(blocks, item, p2["segments"], merged),
                label=f"remediate h{input.hui}",
                nudge=RETRY_NUDGE,
                is_valid=_has_segments,
                effort=self.config.pass1_effort,
            )
            segments = _reattach(p2["segments"], parsed["segments"])
            english = detscan.assemble(segments)

            async def run_lens(lens: dict[str, str]):
                reparsed, recost = await call_agent_for_json(
                    self.config,
                    audit_prompt(
                        project, lens, input.hui, english, segments, item.get("zh", "")
                    ),
                    label=f"reaudit:{lens['name']} h{input.hui}",
                    nudge=RETRY_NUDGE,
                    is_valid=_is_audit,
                    effort=self.config.pass1_effort,
                )
                return reparsed, recost

            # The generated workflow re-audits with the first two lenses only.
            results = await _gather_bounded(
                self.config, [lambda L=L: run_lens(L) for L in LENSES[:2]]
            )
            after = {
                "calques": [c for r, _ in results for c in r.get("calques") or []],
                "archaisms": [a for r, _ in results for a in r.get("archaisms") or []],
                "flattened": [],
                "fidelity_issues": [],
            }
            _write(target, {"hui": input.hui, "segments": segments, "audit_after": after})
            _bump_cost(
                project,
                input.book,
                input.hui,
                "remediate",
                cost + sum(c for _, c in results),
            )
            chapter_status.set(
                context,
                str(input.hui),
                f"remediated:{len(after['calques']) + len(after['archaisms'])}",
            )
        return go_to(FinalizeStep, input)


class FinalizeStep(Step[ChapterRef]):
    """STEP 3c: accessibility-lift gate, deterministic scan, targeted fix, record."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def get_step_options(self) -> StepOptions:
        return _phase_options(self.config)

    async def execute(self, context: Context, input: ChapterRef) -> StepDecision:  # type: ignore[override]
        project = Project(input.config_path)
        record_path = project.phase_path(input.book, input.hui, "chapter")
        if record_path.is_file():
            chapter_done.publish(context, f"{input.hui}:cached")
            return dead_end()

        item = _load_item(project, input.book, input.hui)
        repaired = _read(project.phase_path(input.book, input.hui, "p1_repaired"))
        remediated_path = project.phase_path(input.book, input.hui, "remediated")
        audit = _read(project.phase_path(input.book, input.hui, "audit"))
        if remediated_path.is_file():
            remediated = _read(remediated_path)
            final_segments = remediated["segments"]
            audit_after = remediated["audit_after"]
        else:
            final_segments = _read(project.phase_path(input.book, input.hui, "p2"))["segments"]
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
            effort=self.config.pass1_effort,
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
                effort=self.config.pass1_effort,
            )
            total_cost += fix_cost
            final_segments = _reattach(final_segments, parsed["segments"])
            det = detscan.scan(project.config_path, detscan.assemble(final_segments))
        det_clean = detscan.gate_count(det) == 0

        flattened_after = sum(1 for s in final_segments if detscan.is_flattened(s))
        record = {
            "hui": input.hui,
            "local": item.get("local"),
            "book": input.book,
            "final": final_segments,
            "flattened_p1": repaired.get("flattened_p1", 0),
            "unknown_beat_ids": _read(project.phase_path(input.book, input.hui, "p1")).get(
                "unknown_beat_ids", []
            ),
            "missing_beat_ids": _read(project.phase_path(input.book, input.hui, "p1")).get(
                "missing_beat_ids", []
            ),
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
        _write(record_path, record)
        # Clear any prior failure marker, or a chapter that succeeded on re-run
        # would keep being counted as failed forever.
        (project.chapter_dir(input.book, input.hui) / "FAILED.txt").unlink(missing_ok=True)
        _bump_cost(project, input.book, input.hui, "finalize", total_cost)

        flags = "".join(
            [
                "" if det_clean else " [det]",
                "" if lift_ok else " [lift]",
                " [dlg]" if flattened_after else "",
            ]
        )
        chapter_status.set(
            context, str(input.hui), ("done" + flags).strip() or "done"
        )
        chapter_done.publish(context, f"{input.hui}:done")
        return dead_end()


# --------------------------------------------------------------------------
# Tally
# --------------------------------------------------------------------------


def _tally(project: Project, book: int) -> tuple[int, int, float, int]:
    """(done, failed, cost, needing-attention) read off disk."""
    done = failed = attention = 0
    cost = 0.0
    root = project.pass1_dir(book)
    if not root.is_dir():
        return 0, 0, 0.0, 0
    for directory in sorted(root.glob("h*")):
        record = directory / "chapter.json"
        if record.is_file():
            done += 1
            try:
                payload = _read(record)
            except json.JSONDecodeError:
                payload = {}
            if (
                not payload.get("det_clean", True)
                or not payload.get("lift_ok", True)
                or payload.get("flattened_p1_after", 0)
            ):
                attention += 1
        if (directory / "FAILED.txt").is_file():
            failed += 1
        ledger = directory / "cost.json"
        if ledger.is_file():
            try:
                cost += sum(float(v) for v in _read(ledger).values())
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    return done, failed, cost, attention


# --------------------------------------------------------------------------
# Flow
# --------------------------------------------------------------------------


class Pass1Flow(Flow[VolumeInput]):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.start = Pass1StartStep(config)
        self.wave = WaveStep(config)
        self.wave_join = WaveJoinStep(config)
        self.pass1 = Pass1Step(config)
        self.dialogue_repair = DialogueRepairStep(config)
        self.pass2 = Pass2Step(config)
        self.audit = AuditStep(config)
        self.remediate = RemediateStep(config)
        self.finalize = FinalizeStep(config)
        self.chapter_failed = ChapterFailedStep(config)
        self.combine = CombineStep(config)
        self.review_gate = ReviewGate(config)

    def get_flow_type(self) -> str:
        return "FanyiPass1"

    def get_steps(self) -> StepList[VolumeInput]:
        return StepList.start_step(self.start).other_steps(
            self.wave,
            self.wave_join,
            self.pass1,
            self.dialogue_repair,
            self.pass2,
            self.audit,
            self.remediate,
            self.finalize,
            self.chapter_failed,
            self.combine,
            self.review_gate,
        )

    def get_persistence_schema(self) -> PersistenceSchema:
        return PersistenceSchema.of(
            stage,
            note,
            chapters_total,
            chapters_done,
            chapters_failed,
            attention_count,
            cost_usd,
            chapter_status,
            chapter_done,
            pass1_reviewed,
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
                    "attention": attention_count.get(context),
                    "costUsd": cost_usd.get(context),
                },
                ensure_ascii=False,
            )
        )
