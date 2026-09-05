"""Pure helpers for the finishing stages: approved items, harvest, assemble, print, checks.

Nothing here touches Dex. The Steps that call these live in `book_flow.py`, so the
Flow graph — every wait, movement, and recovery target — is readable in one file.

The rule throughout: reuse the project's own scripts rather than reimplementing
them, and never invoke a script whose side effects reach outside the run.
`harvest_beatplan.py` is imported for its deterministic flag-anchoring but its
`main()` is never called, because it overwrites the project's review artifact and its
staged-items file unconditionally — the hazard v1's README had to warn about in bold.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from fanyi_dex.project import Project


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Approved items
# --------------------------------------------------------------------------


def stage_item(plan: dict[str, Any], decisions: dict[str, str]) -> dict[str, Any]:
    """Turn one beat plan into the item the produce pass consumes.

    Beat ids are `{hui}.{n}` 1-based, the same canonical ids `harvest_beatplan.py`
    assigns, so a tier flip or a re-run cannot drift the identity of a beat.
    `decisions` maps beat id -> tier, and is how a director override at the gate
    reaches the translation.
    """
    hui = plan.get("hui")
    beats = []
    for n, beat in enumerate(plan.get("beats", []), 1):
        beat_id = f"{hui}.{n}"
        beats.append(
            {
                "id": beat_id,
                "beat": beat.get("beat", ""),
                "tier": decisions.get(beat_id) or beat["tier"],
                "zh_span": beat.get("zh_span", ""),
                **(
                    {"is_verse": beat["is_verse"]}
                    if beat.get("is_verse") is not None
                    else {}
                ),
                **(
                    {"verse_kind": beat["verse_kind"]}
                    if beat.get("verse_kind") is not None
                    else {}
                ),
            }
        )
    return {
        "book": plan.get("book"),
        "hui": hui,
        "local": plan.get("local"),
        "named": plan.get("named", []),
        "beats": beats,
        "verbatim_lines": plan.get("verbatim_lines", []),
        "zh": plan.get("zh", ""),
    }


def uncertain_review(project: Project, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anchor each planner-flagged uncertain tier call to a beat, deterministically.

    Reuses `harvest_beatplan.anchor_beat` — the planner's self-reported beat_index
    is wrong about a quarter of the time, and that resolution logic is the project's,
    not this runner's. Falls back to the planner's index when the script is absent.
    """
    anchor = None
    script = project.pipeline_script("harvest_beatplan.py")
    if script.is_file():
        try:
            anchor = _load_module(script, "_fanyi_harvest_beatplan").anchor_beat
        except Exception:  # noqa: BLE001 - a missing optional dep must not fail the volume
            anchor = None

    review: list[dict[str, Any]] = []
    for plan in plans:
        hui = plan.get("hui")
        beats = plan.get("beats", [])
        for flagged in plan.get("uncertain", []):
            text = flagged.get("beat_zh") or flagged.get("zh", "")
            if anchor is not None:
                index, method = anchor(text, beats)
            else:
                index, method = flagged.get("beat_index"), "planner_index"
            review.append(
                {
                    "book": plan.get("book"),
                    "hui": hui,
                    "beat_id": f"{hui}.{index + 1}" if index is not None else None,
                    "tier": flagged.get("tier"),
                    "alt_tier": flagged.get("alt_tier"),
                    "zh": flagged.get("zh", ""),
                    "rationale_zh": flagged.get("rationale_zh", ""),
                    "anchor_method": method,
                    "beat_zh_span": beats[index].get("zh_span", "") if index is not None else "",
                    "beat_syn": beats[index].get("beat", "") if index is not None else "",
                    "planner_beat_index": flagged.get("beat_index"),
                }
            )
    return review


# --------------------------------------------------------------------------
# Output parsing. The pipeline scripts report what they wrote on stdout; these
# read it rather than guessing paths, so a script that changes its layout is
# noticed instead of silently producing a manifest pointing at nothing.
# --------------------------------------------------------------------------


def read_json(path: str) -> dict[str, Any] | None:
    """Read a chapter SubFlow's exported artifact, or None if it is not there."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def existing(*candidates: Path) -> str:
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def scrape_int(stdout: str, pattern: str) -> int:
    match = re.search(pattern, stdout)
    return int(match.group(1)) if match else 0


def harvest_tally(stdout: str) -> tuple[int, int]:
    """`harvest_reprocess.py` prints `harvested N chapters` plus a SKIPPED block.

    Matched against the script's actual output, not a guess at it: an earlier guess
    of `wrote N` parsed to zero, so a run that harvested three chapters recorded
    `harvested: 0` on the manifest while the vault held all three.
    """
    wrote = scrape_int(stdout, r"harvested\s+(\d+)\s+chapter")
    skipped = scrape_int(stdout, r"SKIPPED\s+(\d+)")
    return wrote, skipped


def backup_vault_book(project: Project, book: int) -> str:
    """Copy the vault chapter files aside before harvest overwrites them.

    `harvest_reprocess.py` makes its own `cutlists/twopass_backup/`, but that is a
    backup of what it is about to write, taken by the thing doing the writing. This
    is an independent copy taken before it runs, named by run.
    """
    import shutil
    import time

    vault = project.cfg.get("project", {}).get("vault_book_dir", "")
    if not vault:
        return ""
    lo, hi = project.chapter_range(book)
    base = Path(vault).expanduser()
    source = None
    for candidate in sorted(base.glob("Book *")):
        if f"回{lo}-{hi}" in candidate.name or f"回{lo:02d}-{hi:02d}" in candidate.name:
            source = candidate
            break
    if source is None or not source.is_dir():
        return ""
    target = (
        project.root
        / "pipeline"
        / "run"
        / "dex"
        / "backup"
        / f"book{book}-{time.strftime('%Y%m%dT%H%M%S')}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return str(target)


def missing_final_artifacts(harvested: int, master_md: str, interior_pdf: str) -> list[str]:
    """What the volume still lacks before anyone can sign off on a proof.

    The proof gate is the only exit from the volume Flow, and RecoveryGate accepts
    `proof-gate` as a resume target — so without this check an operator could complete
    a volume that never harvested, assembled, or printed anything.
    """
    return [
        label
        for label, value in (
            ("harvested chapters", harvested),
            ("master markdown", master_md),
            ("interior PDF", interior_pdf),
        )
        if not value
    ]


def one_line(text: str, limit: int = 400) -> str:
    """Collapse a script tail into one status line.

    Script tails carry grpc's stderr chatter and multi-line check reports, which turn
    a status readout into a wall of text; the full output stays in the Worker log.
    """
    lines = [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip()
        and "ev_poll_posix" not in line
        and not line.startswith(("I0", "W0", "E0"))
    ]
    interesting = [line for line in lines if "FAIL" in line or "Error" in line] or lines
    return " | ".join(interesting)[:limit]


def epubcheck_verdict(stdout: str, stderr: str, code: int) -> tuple[str, str]:
    merged = (stdout + "\n" + stderr).strip()
    if code == 0:
        return "pass", "no errors"
    fatal = [line for line in merged.splitlines() if "ERROR" in line or "FATAL" in line]
    return "fail", "; ".join(fatal[:3]) or merged[-300:]


def chapters_payload(records: list[dict[str, Any]]) -> str:
    """The `{"chapters": [...]}` file `harvest_reprocess.py` consumes, unchanged."""
    return json.dumps({"chapters": records}, ensure_ascii=False, indent=1)
