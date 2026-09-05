"""CLI for the durable translation stages.

Beat-plan (STEP 0):
    uv run python fanyi.py --config <cfg> check --book 5
    uv run python fanyi.py --config <cfg> start --book 5 --aggressive
    uv run python fanyi.py --config <cfg> status --book 5
    uv run python fanyi.py --config <cfg> reviewed --book 5 "tier calls settled"

Two-pass transcreation (STEP 1 -> 3):
    uv run python fanyi.py --config <cfg> pass1-check  --book 4
    uv run python fanyi.py --config <cfg> pass1-start  --book 4 --items <path>
    uv run python fanyi.py --config <cfg> pass1-status --book 4
    uv run python fanyi.py --config <cfg> pass1-report --book 4
    uv run python fanyi.py --config <cfg> pass1-reviewed --book 4 "looks good"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import os
from pathlib import Path

from dex import (
    FlowAlreadyStartedError,
    FlowNotActiveError,
    FlowNotFoundError,
    StartFlowOptions,
)

from fanyi_dex import pass1_flow
from fanyi_dex.app import ClientOnly
from fanyi_dex.config import Config
from fanyi_dex.flow import PLANNING, VolumeInput
from fanyi_dex.flow import (
    chapters_done,
    chapters_failed,
    chapters_total,
    cost_usd,
    note,
    stage,
    tiers_reviewed,
    uncertain_count,
)
from fanyi_dex.project import ConfigUnsupported, Project
from fanyi_dex.prompts import beatplan_prompt

# The fixture, so `--config` has a working default that assumes nothing about the
# machine. Point it at a real project with `--config`, or set FANYI_CONFIG.
DEFAULT_CONFIG = os.environ.get(
    "FANYI_CONFIG",
    str(Path(__file__).resolve().parent / "fixtures/testbook/pipeline/config.json"),
)


def beatplan_flow_id(project: Project, book: int) -> str:
    return f"beatplan-{project.root.name}-book{book}"


def pass1_flow_id(project: Project, book: int) -> str:
    return f"pass1-{project.root.name}-book{book}"


# -- beat-plan (STEP 0) -------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    project = Project(args.config)
    missing = project.missing_keys()
    print(f"{project.name}  ({project.source_work})")
    print(f"  root   : {project.root}")
    print(f"  books  : {', '.join(sorted(project.books, key=lambda k: int(k)))}")
    if missing:
        print(f"  BEAT-PLAN UNSUPPORTED — config missing: {', '.join(missing)}")
        return 1
    print("  beat-plan config: OK")
    if args.book:
        lo, hi = project.chapter_range(args.book)
        chapters = project.chapters(args.book)
        present = sum(1 for c in chapters if project.source_path(c.hui).is_file())
        print(
            f"  book {args.book}: {project.book_title(args.book)} — hui {lo}-{hi}, "
            f"{len(chapters)} chapter(s), {present} source file(s) present"
        )
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    project = Project(args.config)
    project.require_beatplan_support()
    zh = project.read_source(args.hui)
    text = beatplan_prompt(project, args.hui, zh, args.aggressive)
    if args.count:
        print(f"{len(text)} chars, {len(text.encode('utf-8'))} bytes (source {len(zh)} chars)")
    else:
        print(text)
    return 0


async def cmd_start(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    project.require_beatplan_support()
    fid = beatplan_flow_id(project, args.book)
    payload = VolumeInput(
        config_path=project.config_path,
        book=args.book,
        aggressive=args.aggressive,
        only=args.only or "",
        wave_size=args.wave_size or 0,
    )
    try:
        run_id = await app.client.start_flow(
            app.beatplan, fid, payload, StartFlowOptions().with_attribute(stage, PLANNING)
        )
    except FlowAlreadyStartedError:
        print(f"{fid} is already running — check `status`")
        return 1
    print(f"started {fid} (run {run_id})")
    print(f"  plans : {project.stage_dir(args.book)}")
    return 0


async def cmd_status(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    fid = beatplan_flow_id(project, args.book)
    try:
        info = await app.client.describe_flow(fid)
    except FlowNotFoundError:
        print(f"{fid} has never been started")
        return 1
    print(f"{fid}  {info.status}")
    for label, attribute in (
        ("stage", stage),
        ("note", note),
        ("total", chapters_total),
        ("done", chapters_done),
        ("failed", chapters_failed),
        ("uncertain", uncertain_count),
        ("costUsd", cost_usd),
    ):
        value = await app.client.get_attribute(fid, attribute)
        if value is not None:
            print(f"  {label}: {value}")
    failed = sorted(project.stage_dir(args.book).glob("h*.FAILED.txt"))
    if failed:
        huis = ",".join(p.name[1:4].lstrip("0") for p in failed)
        print(f"  re-run failed: fanyi.py start --book {args.book} --only {huis}")
    return 0


async def cmd_reviewed(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    fid = beatplan_flow_id(project, args.book)
    try:
        await app.client.publish(fid, tiers_reviewed, args.note)
    except (FlowNotActiveError, FlowNotFoundError):
        print(f"{fid} is not running")
        return 1
    print(f"{fid}: director gate opened")
    return 0


# -- two-pass transcreation (STEP 1 -> 3) -------------------------------------


def cmd_pass1_check(args: argparse.Namespace) -> int:
    project = Project(args.config)
    missing = project.missing_pass1_keys()
    print(f"{project.name}  ({project.source_work})")
    if missing:
        print(f"  PASS-1 UNSUPPORTED — config missing: {', '.join(missing)}")
        return 1
    print("  pass-1 config: OK")
    for warning in project.pass1_warnings():
        print(f"  WARNING: {warning}")
    if not args.book:
        return 0

    lo, hi = project.chapter_range(args.book)
    print(
        f"  book {args.book}: {project.book_title(args.book)} — hui {lo}-{hi}, "
        f"{hi - lo + 1} chapters expected"
    )
    candidates = project.items_candidates(args.book)
    if not candidates:
        print("  NO approved beat-plan found in cutlists/ — run STEP 0 first")
        return 1
    print("  approved beat-plan candidates (pass one explicitly with --items):")
    for candidate in candidates:
        print(f"    {candidate.describe()}")
    complete = [c for c in candidates if c.complete]
    if len(complete) == 1:
        print(f"  -> only one complete option: --items {complete[0].path}")
    elif len(complete) > 1:
        print(
            "  -> more than one complete option; they are different editorial "
            "products, so pick deliberately"
        )
    else:
        print("  -> no complete option; every candidate is short of the book range")
    return 0


async def cmd_pass1_start(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    project.require_pass1_support()
    items = Path(args.items).expanduser()
    if not items.is_file():
        print(f"error: --items file not found: {items}")
        return 2

    from fanyi_dex.project import load_items_file

    chapters = load_items_file(items, args.book)
    if not chapters:
        print(f"error: {items.name} bound zero chapters for book {args.book}")
        return 2
    lo, hi = project.chapter_range(args.book)
    expected = hi - lo + 1
    if len(chapters) < expected and not args.allow_partial:
        print(
            f"error: {items.name} has {len(chapters)}/{expected} chapters for book "
            f"{args.book}. Pass --allow-partial to run it anyway, or pick another "
            "file (see pass1-check)."
        )
        return 2

    fid = pass1_flow_id(project, args.book)
    payload = pass1_flow.VolumeInput(
        config_path=project.config_path,
        book=args.book,
        items_path=str(items),
        only=args.only or "",
        wave_size=args.wave_size or 0,
    )
    try:
        run_id = await app.client.start_flow(
            app.pass1,
            fid,
            payload,
            StartFlowOptions().with_attribute(pass1_flow.stage, pass1_flow.PLANNING),
        )
    except FlowAlreadyStartedError:
        print(f"{fid} is already running — check `pass1-status`")
        return 1
    print(f"started {fid} (run {run_id})")
    print(f"  items : {items}  ({len(chapters)} chapters)")
    print(f"  work  : {project.pass1_dir(args.book)}")
    print("  web   : http://127.0.0.1:8802")
    return 0


async def cmd_pass1_status(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    fid = pass1_flow_id(project, args.book)
    try:
        info = await app.client.describe_flow(fid)
    except FlowNotFoundError:
        print(f"{fid} has never been started")
        return 1
    print(f"{fid}  {info.status}")
    for label, attribute in (
        ("stage", pass1_flow.stage),
        ("note", pass1_flow.note),
        ("total", pass1_flow.chapters_total),
        ("done", pass1_flow.chapters_done),
        ("failed", pass1_flow.chapters_failed),
        ("attention", pass1_flow.attention_count),
        ("costUsd", pass1_flow.cost_usd),
    ):
        value = await app.client.get_attribute(fid, attribute)
        if value is not None:
            print(f"  {label}: {value}")
    failed = sorted(project.pass1_dir(args.book).glob("h*/FAILED.txt"))
    if failed:
        huis = ",".join(p.parent.name[1:].lstrip("0") for p in failed)
        print(f"  re-run failed: fanyi.py pass1-start --book {args.book} --only {huis} --items <path>")
    return 0


def cmd_pass1_report(args: argparse.Namespace) -> int:
    """Per-chapter QA scorecard — what to read at the review gate."""
    project = Project(args.config)
    root = project.pass1_dir(args.book)
    records = sorted(root.glob("h*/chapter.json"))
    if not records:
        print(f"no finished chapters under {root}")
        return 1

    print(f"{len(records)} chapter(s) in {root}\n")
    print(f"  {'hui':>4}  {'dlg':>7}  {'det':>5}  {'access':>9}  {'lift':>4}  detail")
    attention = []
    for path in records:
        c = json.loads(path.read_text(encoding="utf-8"))
        det = c.get("det_scan", {})
        det_n = sum(len(det.get(k) or []) for k in ("archaisms", "calques", "unit_leaks", "poem_refs"))
        flat = c.get("flattened_p1_after", 0)
        needs = (not c.get("det_clean", True)) or (not c.get("lift_ok", True)) or flat
        if needs:
            attention.append(c["hui"])
        detail = []
        for key in ("archaisms", "calques", "unit_leaks", "poem_refs", "wrong_roman"):
            hits = det.get(key) or []
            if hits:
                detail.append(f"{key}={len(hits)}")
        print(
            f"  {c['hui']:>4}  {c.get('flattened_p1', 0)}->{flat:<4}  "
            f"{det_n:>5}  {c.get('access_before')}->{c.get('access_after'):<5}  "
            f"{'ok' if c.get('lift_ok') else 'NO':>4}  {', '.join(detail)}"
        )

    combined = project.pass1_combined(args.book)
    print()
    if attention:
        print(f"ATTENTION: hui {', '.join(str(h) for h in attention)}")
    else:
        print("ALL CLEAR: dialogue preserved, deterministic scan clean, accessibility lifted.")
    wrong = [
        c["hui"]
        for p in records
        for c in [json.loads(p.read_text(encoding="utf-8"))]
        if (c.get("det_scan", {}).get("wrong_roman") or [])
    ]
    if wrong:
        print(
            f"WRONG ROMANIZATION (ship-blocking, not auto-fixed): hui "
            f"{', '.join(str(h) for h in wrong)}"
        )
    if combined.is_file():
        print(f"\nharvest into the vault (writes 回NN (edited).md — back up first):")
        print(f"  python3 {project.pipeline_script('harvest_reprocess.py')} \\\n"
              f"      {combined} --config {project.config_path}")
    return 0


async def cmd_pass1_reviewed(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    fid = pass1_flow_id(project, args.book)
    try:
        await app.client.publish(fid, pass1_flow.pass1_reviewed, args.note)
    except (FlowNotActiveError, FlowNotFoundError):
        print(f"{fid} is not running")
        return 1
    print(f"{fid}: review gate opened")
    return 0


# -- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="project pipeline/config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="verify a project can run the beat-plan stage")
    check.add_argument("--book", type=int, default=0)
    check.set_defaults(local=cmd_check)

    prompt = sub.add_parser("prompt", help="print the beat-plan prompt for one chapter")
    prompt.add_argument("--hui", type=int, required=True)
    prompt.add_argument("--aggressive", action="store_true")
    prompt.add_argument("--count", action="store_true")
    prompt.set_defaults(local=cmd_prompt)

    start = sub.add_parser("start", help="beat-plan a volume")
    start.add_argument("--book", type=int, required=True)
    start.add_argument("--aggressive", action="store_true")
    start.add_argument("--only", default="")
    start.add_argument("--wave-size", type=int, default=0)
    start.set_defaults(handler=cmd_start)

    status = sub.add_parser("status", help="beat-plan progress")
    status.add_argument("--book", type=int, required=True)
    status.set_defaults(handler=cmd_status)

    reviewed = sub.add_parser("reviewed", help="open the beat-plan director gate")
    reviewed.add_argument("--book", type=int, required=True)
    reviewed.add_argument("note", nargs="?", default="")
    reviewed.set_defaults(handler=cmd_reviewed)

    p1check = sub.add_parser("pass1-check", help="pass-1 readiness + items candidates")
    p1check.add_argument("--book", type=int, default=0)
    p1check.set_defaults(local=cmd_pass1_check)

    p1start = sub.add_parser("pass1-start", help="transcreate a volume")
    p1start.add_argument("--book", type=int, required=True)
    p1start.add_argument("--items", required=True, help="approved beat-plan file (see pass1-check)")
    p1start.add_argument("--only", default="", help="comma-separated hui")
    p1start.add_argument("--wave-size", type=int, default=0)
    p1start.add_argument(
        "--allow-partial", action="store_true", help="run an items file short of the book range"
    )
    p1start.set_defaults(handler=cmd_pass1_start)

    p1status = sub.add_parser("pass1-status", help="pass-1 progress")
    p1status.add_argument("--book", type=int, required=True)
    p1status.set_defaults(handler=cmd_pass1_status)

    p1report = sub.add_parser("pass1-report", help="per-chapter QA scorecard")
    p1report.add_argument("--book", type=int, required=True)
    p1report.set_defaults(local=cmd_pass1_report)

    p1reviewed = sub.add_parser("pass1-reviewed", help="open the pass-1 review gate")
    p1reviewed.add_argument("--book", type=int, required=True)
    p1reviewed.add_argument("note", nargs="?", default="")
    p1reviewed.set_defaults(handler=cmd_pass1_reviewed)

    return parser


async def main() -> int:
    args = build_parser().parse_args()
    try:
        if hasattr(args, "local"):
            return args.local(args)
        app = ClientOnly(Config.from_env())
        try:
            return await args.handler(app, args)
        finally:
            await app.close()
    except ConfigUnsupported as exc:
        print(f"error: {exc}")
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
