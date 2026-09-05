#!/usr/bin/env python3
"""One command that proves the whole volume pipeline works, end to end.

Builds a sandbox project, starts the volume Flow, mocks the human at every gate,
and asserts the final product exists. Needs `dexcli dev` and `worker.py` running —
each in its own terminal, because a backgrounded Worker gets reaped under memory
pressure and the volume then just stops advancing.

    dexcli dev
    cd ~/dex-fanyi && FANYI_FAKE_AGENT=1 FANYI_PYTHON=/usr/local/bin/python3 uv run python worker.py
    uv run python tools/verify_e2e.py                  # gates auto-approved   -> 17 checks
    uv run python tools/verify_e2e.py --human-gates    # real Channel decisions -> 18 checks

Whether the Claude calls are real is the *Worker's* setting, not this script's:
start the Worker without FANYI_FAKE_AGENT to spend real money. Do that with one
chapter first (`--chapters 1`) and read the cost off `book.py status`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dex import FlowAlreadyStartedError, StartFlowOptions  # noqa: E402

from fanyi_dex.app import ClientOnly  # noqa: E402
from fanyi_dex.book import book_flow  # noqa: E402
from fanyi_dex.book.model import (  # noqa: E402
    APPROVING_ITEMS,
    DIRECTOR_GATE,
    DONE,
    GATE_DIRECTOR,
    GATE_PROOF,
    GATE_QA,
    PRINTING,
    PROOF_GATE,
    QA_GATE,
    RECOVERY_GATE,
    Approval,
    RunPlan,
    StageRef,
)
from fanyi_dex.config import Config  # noqa: E402
from fanyi_dex.project import Project  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "fixtures/testbook/pipeline/config.json"),
        help="a project's pipeline/config.json; defaults to the committed fixture, so a "
        "fresh clone can run this with no external assets",
    )
    parser.add_argument("--book", type=int, default=1)
    parser.add_argument("--chapters", default="1,2,3")
    parser.add_argument("--generation", type=int, default=int(time.time()) % 100000)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--keep-sandbox", action="store_true")
    parser.add_argument(
        "--human-gates",
        action="store_true",
        help="answer the three gates by publishing Approvals instead of auto-approving, "
        "and assert a --tier-override reaches the translation",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    print("1. building the sandbox project")
    built = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "make_sandbox.py"),
            "--config",
            str(Path(args.config).expanduser()),
            "--book",
            str(args.book),
            "--chapters",
            args.chapters,
            "--force",
        ],
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        print(built.stdout + built.stderr)
        return 1
    sandbox_config = built.stdout.strip().splitlines()[-1].split()[-1]
    project = Project(sandbox_config)
    huis = [int(x) for x in args.chapters.split(",")]
    print(f"   {project.root}")

    app = ClientOnly(Config.from_env())
    fid = f"book-{project.cfg['project']['slug']}-b{args.book}g{args.generation}"
    try:
        mode = (
            "mock human answering each gate by hand"
            if args.human_gates
            else "every gate auto-approved (mock human)"
        )
        print(f"\n2. starting {fid} — {mode}")
        seed = RunPlan(
            auto_approve=not args.human_gates,
            wave_size=len(huis),
            curate_effort=app.config.effort,
            produce_effort=app.config.pass1_effort,
        )
        try:
            await app.client.start_flow(
                app.book,
                fid,
                StageRef(config_path=project.config_path, book=args.book),
                StartFlowOptions().with_attribute(book_flow.plan, seed),
            )
        except FlowAlreadyStartedError:
            print(f"   {fid} already running — pass a different --generation")
            return 1

        print("\n3. driving the volume, answering whatever it parks on")
        override_beat, override_tier = "1.1", "FULL"
        gate_for_stage = {
            DIRECTOR_GATE: GATE_DIRECTOR,
            QA_GATE: GATE_QA,
            PROOF_GATE: GATE_PROOF,
        }
        answered_recovery = False
        seen_failures: set[str] = set()
        stage, snapshot = await _settle(app, fid, args.timeout)

        # One loop, because the volume can park on things in more than one order: the
        # KDP preflight sends it to the recovery gate BETWEEN the QA gate and the proof
        # gate, so a fixed gate-then-recovery sequence hangs at whichever comes second.
        while stage in (gate_for_stage if args.human_gates else {}) or stage == RECOVERY_GATE:
            print(f"   stage: {stage}")
            if stage == RECOVERY_GATE:
                failure = json.loads(snapshot.failure_json)
                failed_stage = failure.get("stage", "")
                if not answered_recovery:
                    check(
                        "parks on a failed print preflight instead of reporting itself finished",
                        failed_stage == PRINTING,
                        failure.get("detail", "")[:110],
                    )
                    answered_recovery = True

                # Resume PAST the stage that failed, not at a fixed one. Always resuming
                # at 'checking' skipped harvest/assemble/print whenever something earlier
                # failed, and then looped forever against the proof gate's guard — the
                # volume kept arriving at GATE 3 with an empty manifest.
                resume_at = {PRINTING: "checking"}.get(failed_stage)
                if resume_at is None:
                    check(
                        f"volume parked at an unexpected stage: {failed_stage}",
                        False,
                        failure.get("detail", "")[:160],
                    )
                    break
                if failed_stage in seen_failures:
                    check(
                        f"resuming past {failed_stage} did not clear it — looping",
                        False,
                        failure.get("detail", "")[:160],
                    )
                    break
                seen_failures.add(failed_stage)
                # A short book is under the print minimum. Accepting that is exactly the
                # human decision this gate exists for.
                print(f"   mock human accepts the {failed_stage} failures: resume at '{resume_at}'")
                await app.client.publish(fid, book_flow.resume, resume_at)
            else:
                gate = gate_for_stage[stage]
                payload = (
                    json.dumps({"tier_overrides": {override_beat: override_tier}})
                    if gate == GATE_DIRECTOR
                    else ""
                )
                print(
                    f"   mock human approves gate '{gate}'"
                    + (f" with {override_beat}={override_tier}" if payload else "")
                )
                await app.client.publish(
                    fid,
                    book_flow.approvals,
                    gate,
                    Approval(
                        gate=gate,
                        decision="approve",
                        note="verified by tools/verify_e2e.py",
                        payload=payload,
                        actor="verify_e2e",
                    ),
                )
            leaving = stage
            stage, snapshot = await _settle(app, fid, args.timeout, leaving=leaving)

            if leaving == DIRECTOR_GATE:
                # The director's payload has to survive the hop from the gate that
                # consumed it to the Step that stages the items.
                item_path = project.chapter_dir(args.book, huis[0]) / "item.json"
                item = (
                    json.loads(item_path.read_text(encoding="utf-8"))
                    if item_path.is_file()
                    else {}
                )
                tiers = {b["id"]: b["tier"] for b in item.get("beats", [])}
                check(
                    f"--tier-override {override_beat}={override_tier} reached the staged item",
                    tiers.get(override_beat) == override_tier,
                    f"{override_beat} is {tiers.get(override_beat)!r}",
                )
        print(f"   stage: {stage}")

        print("\n5. asserting the durable state and the final product")
        tally = json.loads(snapshot.tally_json)
        manifest = json.loads(snapshot.manifest_json)
        expected = len(huis)

        check("volume reached 'done'", stage == DONE, stage)
        check(
            f"all {expected} chapter(s) planned",
            tally["curate"] == f"{expected}/{expected}",
            tally["curate"],
        )
        check(
            f"all {expected} chapter(s) transcreated",
            tally["produce"] == f"{expected}/{expected}",
            tally["produce"],
        )
        check("no chapter failed", tally["curate_failed"] == 0 and tally["produce_failed"] == 0)
        check(
            f"{expected} chapter(s) harvested into the vault",
            manifest["harvested"] == expected,
            str(manifest["harvested"]),
        )
        check("an independent vault backup was taken", bool(manifest["vault_backup"]))

        vault_book = next(
            (project.root / "vault").glob("Book *"), project.root / "vault" / "missing"
        )
        written = sorted(p.name for p in vault_book.glob("回* (edited).md"))
        check(f"vault holds {expected} chapter file(s)", len(written) == expected, ", ".join(written))

        for label, path_key in (
            ("master markdown", "master_md"),
            (".docx", "docx"),
            (".epub", "epub"),
            ("6x9 interior PDF", "interior_pdf"),
        ):
            path = manifest[path_key]
            check(f"{label} built", bool(path) and Path(path).is_file(), path)

        check("interior has pages", manifest["pages"] > 0, f"{manifest['pages']} pages")
        check("epubcheck passed", manifest["epubcheck"] == "pass", manifest["epubcheck"])

        # Every chapter phase left its export behind for the human and for harvest.
        for hui in huis:
            directory = project.chapter_dir(args.book, hui)
            phases = sorted(p.stem for p in directory.glob("*.json"))
            check(
                f"hui {hui} exported its phase artifacts",
                {"item", "p1", "p1_repaired", "p2", "audit", "chapter"} <= set(phases),
                ", ".join(phases),
            )

        failed = [name for name, ok, _ in CHECKS if not ok]
        print(
            f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed"
            + (f" — FAILED: {', '.join(failed)}" if failed else "")
        )
        if not args.keep_sandbox:
            print(f"\nsandbox kept at {project.root} (delete when done)")
        return 1 if failed else 0
    finally:
        await app.close()


async def _settle(app: ClientOnly, fid: str, timeout: int, stop_at_gate: bool = True,
                  leaving: str = ""):
    """Poll until the volume is terminal, or parked at a gate awaiting a decision.

    `leaving` is the gate just answered: its `stage` Attribute still reads the old value
    while the consuming Step commits, so returning on it would hand back a snapshot from
    mid-transition. `stop_at_gate=False` ignores gates entirely and waits for a terminal
    state — used after answering the recovery gate.
    """
    parked = {DIRECTOR_GATE, QA_GATE, PROOF_GATE, RECOVERY_GATE}
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        info = await app.client.describe_flow(fid)
        if info.status.name != "RUNNING":
            stage = await app.client.get_attribute(fid, book_flow.stage)
            return stage or "", await _closed_snapshot(app, fid)
        snapshot = await app.client.invoke_rpc(app.book.snapshot, fid)
        if snapshot.stage != last:
            print(f"   .. {snapshot.stage}: {snapshot.note[:90]}")
            last = snapshot.stage
        if stop_at_gate and snapshot.stage in parked and snapshot.stage != leaving:
            # `leaving` is the gate we just answered: its stage Attribute still reads the
            # old value while the consuming Step commits, so returning on it would hand
            # back a snapshot from mid-transition.
            return snapshot.stage, snapshot
        await asyncio.sleep(3)
    raise TimeoutError(f"{fid} did not settle within {timeout}s (last stage: {last})")


async def _closed_snapshot(app: ClientOnly, fid: str):
    """A closed Flow rejects RPCs, so read the same values off its Attributes."""
    from types import SimpleNamespace

    from fanyi_dex.book.model import Manifest, StageFailure, Tally

    plan = await app.client.get_attribute(fid, book_flow.plan)
    tally = await app.client.get_attribute(fid, book_flow.tally)
    manifest = await app.client.get_attribute(fid, book_flow.manifest)
    failure = await app.client.get_attribute(fid, book_flow.failure)
    return SimpleNamespace(
        stage=await app.client.get_attribute(fid, book_flow.stage) or "",
        note=await app.client.get_attribute(fid, book_flow.note) or "",
        plan_json=json.dumps(book_flow._plan_dict(plan or RunPlan())),
        tally_json=json.dumps(book_flow._tally_dict(tally or Tally())),
        manifest_json=json.dumps(book_flow._manifest_dict(manifest or Manifest())),
        failure_json=json.dumps(book_flow._failure_dict(failure or StageFailure())),
        chapters_json="[]",
        gates_pending="",
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
