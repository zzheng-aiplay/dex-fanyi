#!/usr/bin/env python3
"""Application boundary for the end-to-end volume Flow.

    uv run python book.py --config <project>/pipeline/config.json check   --book 1
    uv run python book.py --config ...                          start   --book 1 [--only 1,2,3] [--auto-approve]
    uv run python book.py --config ...                          status  --book 1 [--chapters]
    uv run python book.py --config ...                          watch   --book 1
    uv run python book.py --config ...                          approve --book 1 --gate director "tiers settled"
    uv run python book.py --config ...                          reject  --book 1 --gate qa "chapter 3 is wrong"
    uv run python book.py --config ...                          resume  --book 1 --stage producing

`status` makes exactly one request: the `snapshot` RPC returns the whole read
model. v1 read seven Attributes one at a time and then re-scanned the filesystem
for anything else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta

from dex import (
    FlowAlreadyStartedError,
    FlowNotActiveError,
    FlowNotFoundError,
    StartFlowOptions,
)

from fanyi_dex.app import ClientOnly
from fanyi_dex.book import book_flow
from fanyi_dex.book.model import (
    GATE_DIRECTOR,
    GATE_PROOF,
    GATE_QA,
    Approval,
    RunPlan,
    StageRef,
)
from fanyi_dex.config import Config
from fanyi_dex.project import ConfigUnsupported, Project

GATES = (GATE_DIRECTOR, GATE_QA, GATE_PROOF)


def flow_id(project: Project, book: int, generation: int = 0) -> str:
    """The volume's durable identity.

    Restarting this ID is a *resume*: Dex derives each chapter SubFlow's ID from the
    parent Flow ID (not its run ID), so a new run attaches to chapters that already
    finished and re-runs only the ones that did not. That is the behaviour you want
    after a crash — and it means a genuinely clean re-run needs a new identity, which
    is what `--generation` is for.
    """
    slug = project.cfg.get("project", {}).get("slug", project.name)
    suffix = f"g{generation}" if generation else ""
    return f"book-{slug}-b{book}{suffix}"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def cmd_check(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    print(f"{project.name}  ({project.source_work})")
    missing = sorted(set(project.missing_keys()) | set(project.missing_pass1_keys()))
    if missing:
        print(f"  REFUSED — config is missing: {', '.join(missing)}")
        return 1
    print(f"  books: {', '.join(sorted(project.books, key=int))}")
    if args.book:
        lo, hi = project.chapter_range(args.book)
        chapters = project.chapters(args.book)
        absent = [c.hui for c in chapters if not project.source_path(c.hui).is_file()]
        print(
            f"  book {args.book} ({project.book_title(args.book)}): "
            f"hui {lo}-{hi}, {len(chapters)} chapter(s)"
        )
        if absent:
            print(f"  MISSING source chapters: {absent}")
            return 1
    for warning in project.pass1_warnings():
        print(f"  warning: {warning}")
    print("  OK")
    return 0


async def cmd_start(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    project.require_beatplan_support()
    project.require_pass1_support()
    fid = flow_id(project, args.book, getattr(args, 'generation', 0))

    # Run policy is seeded as durable state, not read from the Worker's env, so a
    # Worker restart under a different environment cannot change the policy this
    # volume was started under.
    seed = RunPlan(
        aggressive=args.aggressive,
        auto_approve=args.auto_approve,
        wave_size=args.wave_size or app.config.wave_size,
        curate_effort=app.config.effort,
        produce_effort=app.config.pass1_effort,
    )
    payload = StageRef(
        config_path=project.config_path,
        book=args.book,
        stage="init",
        pending=args.only or "",
    )
    # Seed the plan ONLY on a first start. Re-seeding on a re-start overwrote the
    # frozen plan with an empty-chapter seed, so InitStep took its fresh-freeze branch
    # and re-derived everything — including a new config digest. That silently undid the
    # freeze the whole design rests on.
    resuming = False
    try:
        await app.client.describe_flow(fid)
        resuming = True
    except FlowNotFoundError:
        pass

    options = (
        StartFlowOptions()
        if resuming
        else StartFlowOptions().with_attribute(book_flow.plan, seed)
    )
    try:
        run_id = await app.client.start_flow(app.book, fid, payload, options)
    except FlowAlreadyStartedError:
        print(f"{fid} is already running — see `status`")
        return 1
    if resuming:
        print(
            f"resuming {fid} (run {run_id}) — keeping the plan frozen at first start; "
            f"finished chapters will not re-run"
        )
        return 0
    mode = "gates AUTO-APPROVED (mock human)" if args.auto_approve else "gates need a human"
    print(f"started {fid} (run {run_id}) — {mode}")
    return 0


async def cmd_status(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    fid = flow_id(project, args.book, getattr(args, 'generation', 0))
    try:
        info = await app.client.describe_flow(fid)
    except FlowNotFoundError:
        print(f"{fid} has never been started")
        return 1

    snapshot = await _snapshot(app, fid, info)
    plan = json.loads(snapshot.plan_json)
    tally = json.loads(snapshot.tally_json)
    manifest = json.loads(snapshot.manifest_json)
    failed = json.loads(snapshot.failure_json)

    print(f"{fid}  {info.status.name}")
    print(f"  stage    : {snapshot.stage}")
    print(f"  note     : {snapshot.note}")
    print(
        f"  plan     : book {plan['book']} \"{plan['title']}\" hui {plan['hui']}, "
        f"wave {plan['wave_size']}, config {plan['config_sha256']}"
        + (", auto-approve" if plan.get("auto_approve") else "")
    )
    print(
        f"  progress : curate {tally['curate']} ({tally['curate_failed']} failed), "
        f"produce {tally['produce']} ({tally['produce_failed']} failed), "
        f"{tally['attention']} needing attention, {tally['uncertain']} uncertain, "
        f"${tally['costUsd']}"
    )
    if failed.get("stage"):
        print(f"  FAILURE  : {failed['stage']} — {failed['detail']}")
        print(f"             resume with: book.py resume --book {args.book} --stage <stage>")
    if snapshot.stage in ("director-gate", "qa-gate", "proof-gate"):
        gate = snapshot.stage.replace("-gate", "")
        print(f"  waiting  : approve --gate {gate}")
    for label, path in (
        ("master", manifest["master_md"]),
        ("docx", manifest["docx"]),
        ("epub", manifest["epub"]),
        ("pdf", manifest["interior_pdf"]),
    ):
        if path:
            print(f"  {label:9}: {path}")
    if manifest["pages"]:
        print(
            f"  checks   : {manifest['pages']} pages, epub {manifest['epubcheck']}, "
            f"preflight {manifest['preflight']}"
        )
    if manifest["checks_note"]:
        print(f"             {manifest['checks_note']}")
    if manifest["vault_backup"]:
        print(f"  backup   : {manifest['vault_backup']}")

    if args.chapters:
        print("\n  hui  curate            produce")
        for row in json.loads(snapshot.chapters_json):
            flags = "".join(
                [
                    " [det]" if row["gate"] else "",
                    "" if row["lift_ok"] or row["produce"] != "ok" else " [lift]",
                    " [dlg]" if row["flattened"] else "",
                ]
            )
            print(
                f"  {row['hui']:>3}  {row['curate']:<8} "
                f"{row['beats']:>3}b {row['coverage']:>3}%  "
                f"{row['produce']:<8} {row['segments']:>3}seg {row['words']:>5}w"
                f"{flags}"
                + (f"  ERROR {row['error'][:60]}" if row["error"] else "")
            )
    return 0


async def _snapshot(app: ClientOnly, fid: str, info):
    """The snapshot RPC, with an Attribute fallback once the volume has closed.

    An RPC requires an active Flow, so a finished volume is read straight from its
    Attributes instead. Both paths return the same shape.
    """
    if info.status.name == "RUNNING":
        return await app.client.invoke_rpc(app.book.snapshot, fid)

    from fanyi_dex.book.model import Snapshot

    async def attribute(definition):
        return await app.client.get_attribute(fid, definition)

    plan = await attribute(book_flow.plan)
    return Snapshot(
        stage=await attribute(book_flow.stage) or "",
        note=await attribute(book_flow.note) or "",
        plan_json=json.dumps(book_flow._plan_dict(plan or RunPlan())),
        tally_json=json.dumps(
            book_flow._tally_dict(await attribute(book_flow.tally) or _empty_tally())
        ),
        chapters_json="[]",
        manifest_json=json.dumps(
            book_flow._manifest_dict(await attribute(book_flow.manifest) or _empty_manifest())
        ),
        failure_json=json.dumps(
            book_flow._failure_dict(await attribute(book_flow.failure) or _empty_failure())
        ),
        gates_pending="",
    )


def _empty_tally():
    from fanyi_dex.book.model import Tally

    return Tally()


def _empty_manifest():
    from fanyi_dex.book.model import Manifest

    return Manifest()


def _empty_failure():
    from fanyi_dex.book.model import StageFailure

    return StageFailure()


async def cmd_watch(app: ClientOnly, args: argparse.Namespace) -> int:
    """Tail the progress Stream. Best-effort by design — `status` is the truth."""
    project = Project(args.config)
    fid = flow_id(project, args.book, getattr(args, 'generation', 0))
    token = ""
    while True:
        try:
            message = await app.client.read_stream(
                fid, book_flow.progress, token, timeout=timedelta(seconds=5)
            )
            print(f"  {message.value}")
            token = message.resume_token
            continue
        except FlowNotFoundError:
            print(f"{fid} has never been started")
            return 1
        except Exception:  # noqa: BLE001 - long-poll expiry is the normal idle path
            pass
        info = await app.client.describe_flow(fid)
        if info.status.name != "RUNNING":
            print(f"{fid} {info.status.name}")
            return 0


async def cmd_approve(app: ClientOnly, args: argparse.Namespace) -> int:
    return await _decide(app, args, "approve")


async def cmd_reject(app: ClientOnly, args: argparse.Namespace) -> int:
    return await _decide(app, args, "reject")


async def _decide(app: ClientOnly, args: argparse.Namespace, decision: str) -> int:
    project = Project(args.config)
    fid = flow_id(project, args.book, getattr(args, 'generation', 0))
    payload = ""
    if args.tier_override:
        payload = json.dumps(
            {
                "tier_overrides": dict(
                    item.split("=", 1) for item in args.tier_override
                )
            }
        )
    value = Approval(
        gate=args.gate,
        decision=decision,
        note=args.note or "",
        payload=payload,
        actor=args.actor,
    )
    try:
        await app.client.publish(fid, book_flow.approvals, args.gate, value)
    except (FlowNotActiveError, FlowNotFoundError):
        print(f"{fid} is not running")
        return 1
    print(f"{decision}d gate '{args.gate}' on {fid}")
    return 0


async def cmd_resume(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    fid = flow_id(project, args.book, getattr(args, 'generation', 0))
    try:
        await app.client.publish(fid, book_flow.resume, args.stage)
    except (FlowNotActiveError, FlowNotFoundError):
        print(f"{fid} is not running")
        return 1
    print(f"asked {fid} to resume at '{args.stage}'")
    return 0


async def cmd_result(app: ClientOnly, args: argparse.Namespace) -> int:
    project = Project(args.config)
    fid = flow_id(project, args.book, getattr(args, 'generation', 0))
    result = await app.client.wait_for_flow(fid, timeout=timedelta(seconds=args.timeout))
    print(f"{fid} {result.status.name}")
    if result.error_message:
        print(f"  {result.error_type}: {result.error_message}")
    for completion in result.completions:
        print(f"  {completion.step_type}: {completion.decode(str)}")
    return 0 if result.status.name == "COMPLETED" else 1


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="book.py", description=__doc__)
    parser.add_argument("--config", required=True, help="<project>/pipeline/config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, book_required: bool = True):
        sub = subparsers.add_parser(name)
        sub.add_argument("--book", type=int, required=book_required)
        sub.add_argument(
            "--generation",
            type=int,
            default=0,
            help="start a clean run under a new volume identity instead of resuming",
        )
        sub.set_defaults(handler=handler)
        return sub

    add("check", cmd_check, book_required=False)

    start = add("start", cmd_start)
    start.add_argument("--only", help="comma-joined hui subset")
    start.add_argument("--aggressive", action="store_true")
    start.add_argument("--wave-size", type=int)
    start.add_argument(
        "--auto-approve",
        action="store_true",
        help="mock the human at every gate — for verification runs",
    )

    status = add("status", cmd_status)
    status.add_argument("--chapters", action="store_true")

    add("watch", cmd_watch)

    for name, handler in (("approve", cmd_approve), ("reject", cmd_reject)):
        sub = add(name, handler)
        sub.add_argument("--gate", choices=GATES, required=True)
        sub.add_argument("note", nargs="?", default="")
        sub.add_argument("--actor", default="human")
        sub.add_argument(
            "--tier-override",
            action="append",
            metavar="BEAT_ID=TIER",
            help="director tier flip carried with a GATE 1 approval",
        )

    resume = add("resume", cmd_resume)
    resume.add_argument("--stage", required=True)

    result = add("result", cmd_result)
    result.add_argument("--timeout", type=int, default=1)
    return parser


async def main() -> int:
    args = build_parser().parse_args()
    app = ClientOnly(Config.from_env())
    try:
        return await args.handler(app, args)
    except ConfigUnsupported as error:
        print(f"REFUSED: {error}")
        return 1
    finally:
        await app.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
