"""Typed durable state for the end-to-end volume Flow.

Every value here is a Dex Attribute payload, so the fields stay flat: primitives
and JSON strings only. Nested payloads (beats, segments, prose) travel as JSON
text inside a single field, which the SDK stores as a blob and hydrates through
the BlobCache — no application-side blob layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Stage names. These are durable: they land in the indexed `stage` Attribute and
# `RecoveryGate` routes on them, so they are a contract, not display strings.
# --------------------------------------------------------------------------

INIT = "init"
CURATING = "curating"
DIRECTOR_GATE = "director-gate"
APPROVING_ITEMS = "approving-items"
PRODUCING = "producing"
QA_GATE = "qa-gate"
HARVESTING = "harvesting"
ASSEMBLING = "assembling"
PRINTING = "printing"
CHECKING = "checking"
PROOF_GATE = "proof-gate"
RECOVERY_GATE = "recovery-gate"
DONE = "done"

# Gate names double as ChannelMap instance keys.
GATE_DIRECTOR = "director"
GATE_QA = "qa"
GATE_PROOF = "proof"


@dataclass
class StageRef:
    """The one input type every book-level Step takes.

    A single input type is what makes `on_execute_failure_proceed_to(RecoveryGate)`
    legal from every stage and lets RecoveryGate route back into any of them.

    `pending` is a comma-joined hui list — the batching cursor, carried in the Step
    input rather than recomputed from the filesystem.
    """

    config_path: str
    book: int
    stage: str = INIT
    pending: str = ""
    wave: int = 0


@dataclass
class RunPlan:
    """Frozen at Init and never rewritten.

    v1 rebuilt this from `pipeline/config.json` inside every Step, so editing the
    config mid-run applied different policy to later chapters with no record.
    """

    project_slug: str = ""
    project_root: str = ""
    config_path: str = ""
    config_sha256: str = ""
    book: int = 0
    book_title: str = ""
    hui_lo: int = 0
    hui_hi: int = 0
    chapters: str = ""  # comma-joined hui
    aggressive: bool = False
    auto_approve: bool = False
    wave_size: int = 4
    curate_effort: str = ""
    produce_effort: str = ""
    started_at: str = ""

    @property
    def hui_list(self) -> list[int]:
        return ints(self.chapters)


@dataclass
class Tally:
    curate_total: int = 0
    curate_done: int = 0
    curate_failed: int = 0
    produce_total: int = 0
    produce_done: int = 0
    produce_failed: int = 0
    attention: int = 0
    uncertain: int = 0
    cost_usd: str = "0.00"


@dataclass
class ChapterRecord:
    """One AttributeMap instance per chapter, so chapters never rewrite each other."""

    hui: int = 0
    local: int = 0
    curate_status: str = "pending"  # pending | ok | failed
    curate_flow_id: str = ""
    beats: int = 0
    coverage_pct: int = 0
    uncertain: int = 0
    produce_status: str = "pending"
    produce_flow_id: str = ""
    segments: int = 0
    words: int = 0
    gate_count: int = 0
    lift_ok: bool = False
    flattened_after: int = 0
    unknown_beat_ids: str = ""
    missing_beat_ids: str = ""
    cost_usd: str = "0.00"
    error: str = ""


@dataclass
class Manifest:
    """The final product, as durable state rather than 'look on disk'."""

    master_md: str = ""
    docx: str = ""
    epub: str = ""
    interior_pdf: str = ""
    pages: int = 0
    epubcheck: str = "not-run"  # not-run | pass | fail
    preflight: str = "not-run"
    checks_note: str = ""
    harvested: int = 0
    vault_backup: str = ""


@dataclass
class StageFailure:
    stage: str = ""
    detail: str = ""
    at: str = ""


@dataclass
class Approval:
    """A human decision at a gate. `payload` is free-form JSON for gate-specific data."""

    gate: str = ""
    decision: str = "approve"  # approve | reject
    note: str = ""
    payload: str = ""
    actor: str = "human"


@dataclass
class ChapterJob:
    """SubFlow input for one chapter of either pass."""

    config_path: str
    book: int
    hui: int
    local: int
    aggressive: bool = False
    effort: str = ""
    # Produce only: path to the approved item (beats + zh) exported by the parent.
    #
    # A path, not the body. SDK 0.2.5 does not hydrate blob-backed values carried on
    # a SubFlow completion, and a 30 KB item is over the blob threshold — so the rule
    # here is that SubFlow inputs and outputs carry only small values, and bulk data
    # moves through the run directory. Verified the hard way: returning the beat plan
    # as an outcome field failed every parent Step with
    # "blob-backed Value was not hydrated".
    item_path: str = ""


@dataclass
class ChapterOutcome:
    """SubFlow output. The parent folds this into its ChapterRecord."""

    hui: int = 0
    ok: bool = False
    detail: str = ""
    beats: int = 0
    coverage_pct: int = 0
    uncertain: int = 0
    segments: int = 0
    words: int = 0
    gate_count: int = 0
    lift_ok: bool = False
    flattened_after: int = 0
    unknown_beat_ids: str = ""
    missing_beat_ids: str = ""
    cost_usd: str = "0.00"
    # Where the SubFlow exported its artifact, for the parent to read. See the note
    # on ChapterJob.item_path for why this is a path and not the payload.
    export_path: str = ""


@dataclass
class Snapshot:
    """One cohesive read model, returned by the `snapshot` RPC."""

    stage: str = ""
    note: str = ""
    plan_json: str = ""
    tally_json: str = ""
    chapters_json: str = ""
    manifest_json: str = ""
    failure_json: str = ""
    gates_pending: str = ""


# --------------------------------------------------------------------------
# Small helpers shared by the Flows. Deliberately free of Dex semantics: a
# helper that hid a wait, a movement, or a recovery target would make the Flow
# unreadable to `dexcli visualize`.
# --------------------------------------------------------------------------


def ints(joined: str) -> list[int]:
    return [int(x) for x in str(joined).split(",") if x.strip()]


def join(values: list[int]) -> str:
    return ",".join(str(v) for v in values)


def sha256_file(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return ""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(text: str, fallback: Any = None) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def key(hui: int) -> str:
    """AttributeMap instance key: a stable domain identifier, zero-padded so the
    server's ascending key order matches chapter order."""
    return f"h{hui:03d}"


def cjk_count(text: str) -> int:
    return sum(1 for ch in text or "" if "一" <= ch <= "鿿")


def coverage(beats: list[dict[str, Any]], zh: str) -> int:
    """Do the beat spans tile the source? Same metric harvest_beatplan.py prints."""
    total = cjk_count(zh)
    if not total:
        return 0
    covered = sum(cjk_count(b.get("zh_span", "")) for b in beats)
    return round(covered / total * 100)
