# What changed in v2, and why

v1 is `fanyi_dex/flow.py` + `fanyi_dex/pass1_flow.py` (`fanyi.py`).
v2 is `fanyi_dex/book/` (`book.py`). Both stay registered.

## Scope

v1 modelled two stages of six and handed off between them by hand. v2 is one
durable execution per volume, from validating the config to a proofed interior PDF.

| Stage | v1 | v2 |
|---|---|---|
| volume initiation | implicit | `InitStep` — validates config, freezes the run plan, seeds a record per chapter |
| beat plans | `FanyiBeatPlan` Flow | `FanyiCurateChapter` SubFlow per chapter |
| GATE 1 tier calls | Channel gate, then the Flow **completed** | Channel gate inside the same execution |
| approved items | **a human** picked a `cutlists/*.json` and passed `--items` | `ApproveItemsStep`, with tier overrides carried on the approval |
| transcreation + QA | `FanyiPass1` Flow, 6 Steps per chapter | `FanyiProduceChapter` SubFlow per chapter, same 6 phases |
| GATE 2 QA scorecard | printed by `pass1-report` | Channel gate |
| harvest to the vault | **refused** — printed the command and parked | `HarvestStep`, after an independent backup |
| .docx / .epub | not modelled | `AssembleStep` |
| 6x9 interior PDF | not modelled | `PrintStep` |
| epubcheck / KDP preflight | not modelled | `QualityStep` + `PrintStep` |
| GATE 3 proof | not modelled | Channel gate, then the volume completes with a manifest |

**Not covered, still:** *project* initiation. `fanyi-init`'s work — reading the
source, the director conversation, authoring `config.json` — is a judgment task, not
a durable pipeline. A volume Flow starts from a config that already exists.

## Machinery that disappeared

`WaveStep` + `WaveJoinStep` + `ChapterFailedStep` + the `chapter-done` Channel +
`chapter_done.for_n(batch)` counting — about 120 lines across the two v1 Flows —
collapse into one `wait_for`:

```python
return Wait.all_of(*[SubFlow.run(self.curate, ChapterJob(...)) for hui in batch])
```

A SubFlow Condition is satisfied by **closure**, so a failed chapter cannot stall
the volume. v1's `ChapterFailedStep` existed only to publish a completion so the
join would not deadlock; that failure mode no longer exists to defend against.

Every book-level Step except `RecoveryGate` itself routes its exhausted retries to
`RecoveryGate`, which is possible because they all take the same input type. (The
recovery gate has none: it *is* the route, and sending it anywhere else would lose the
parked state. It retries uncapped instead.) v1 diverted failures to
whichever gate was nearest — a beat-plan failure and a harvest failure both landed
on the director gate — so "parked on a failure" and "waiting for review" were the
same observable state. Now a failure is its own stage, carrying which stage failed
and what it said, and `resume --stage <stage>` names where to re-enter.

## Resume

v1's resume unit was a **file**: every phase opened with `if not target.is_file()`
and the volume tally was recomputed by globbing `h*.json` and `FAILED.txt`.

v2's resume unit is Dex's own Step history. A chapter SubFlow's ID is derived from
the parent Flow ID, and the default reuse policy attaches to a running chapter,
returns a finished one's result, and restarts only one that ended abnormally. So
re-running a volume resumes it — verified: a full re-run of a finished 3-chapter
volume made **zero** agent calls and re-ran only the deterministic finishing stages.

The corollary is worth knowing: a genuinely clean re-run needs a new identity,
which is what `book.py --generation N` is for.

## State

| | v1 | v2 |
|---|---|---|
| run policy | re-read from `pipeline/config.json` inside every Step | frozen at Init with a config digest; seeded by the Client via `StartFlowOptions.with_attribute` |
| per-chapter status | `AttributeMap[str]`, e.g. `"ok:24beats"` | `AttributeMap[ChapterRecord]`, one typed instance per chapter |
| tallies | recomputed by scanning the filesystem | folded from SubFlow results |
| read model | 7 sequential `get_attribute` calls + a filesystem re-scan | one typed `snapshot` RPC |
| progress | none — "a running chapter is silent until it lands" | a `progress` Stream on all three Flows |

Freezing the plan closed part of a real hole: the chapter list, wave size, gate policy,
efforts, and a digest of `config.json` are now fixed at Init, so a re-run cannot silently
select a different set of chapters or a different gate policy.

**It is only part.** Voice, tier, naming, and verse policy are still read live — every
chapter Step constructs `Project(input.config_path)` and `VoiceBlocks(project)` at
execution time, so editing those sections of `config.json` mid-volume still reaches the
chapters that have not run yet. What the digest gives you is *detection*: `status` prints
`config <sha>`, so a changed config is visible rather than silent. Freezing the policy
itself would mean carrying the resolved prompt blocks in the plan, which is not done.

## Safety

- **`heartbeat_timeout` is stated, not inherited.** SDK 0.2.5 exposes the knob but
  no way to *emit* a heartbeat, and `Stream.write` is once per Step execution. A 90s
  silent Execute was measured to survive under the dev server's
  `STEP_DURABILITY_ASYNC` default (`tests/probe_heartbeat.py`) — but that is a
  property of the durability default, so every LLM Step now names its own budget.
- **`harvest_beatplan.py`'s `main()` is never invoked.** It overwrites
  `cutlists/beatplan_review.json` and a staged-items file unconditionally —
  the hazard v1's README had to open with a bold warning about. v2 imports only its
  deterministic `anchor_beat` and writes the review artifact into the run directory.
- **An independent vault backup** is taken before harvest, separate from the one
  `harvest_reprocess.py` takes of what it is about to write.
- **A failed interior parks the volume.** No PDF, or a failing KDP preflight, routes
  to the recovery gate.
- **`run_python` refuses this project's venv by name.** The pipeline scripts need
  system `zhconv` and `pypdf`; under `uv run` a bare `python3` resolves to the venv.
- **Worker readiness waits for the gRPC handshake.** `AsyncWorker.start` binds the
  port before serving, so v1's TCP probe returned early and Dex's first dispatch was
  refused — visible as every first Step attempt failing and the Flow advancing on
  attempt 2.

## What running it found

Five defects that only a real execution surfaces:

1. **A blob-backed SubFlow output is not hydrated.** Returning the beat plan as an
   outcome field failed every parent Step with `blob-backed Value was not hydrated`.
   Rule adopted: SubFlow inputs and outputs carry only small values; bulk data moves
   through the run directory.
2. **An unset dataclass Attribute reads back as `None`**, not as a default instance —
   it crashed the snapshot RPC.
3. **`harvest_tally`'s regex was a guess.** `harvest_reprocess.py` prints
   `harvested N chapters`, not `wrote N`, so a run that harvested three chapters
   recorded `harvested: 0` while the vault held all three.
4. **The first verification run completed with `preflight fail`** — reporting a
   finished volume with no valid interior. That is what the print gate now catches.
5. **The pipeline scripts died on `No module named 'pypdf'`** because `python3`
   resolved into the venv. Hence the interpreter guard.

## Preserved deliberately

Not churned, because it was already right: the prompts (ported verbatim), `detscan`
importing the project's `build_pass2.py` so blocklists cannot drift, no
structured-output schemas plus parse-retry, dropping an unknown `beat_id` rather than
carrying source-less prose, uncapped retries on bookkeeping Steps, and stamping
`stage` inside `wait_for` beside the wait a gate is parked on.

## Known gaps

- The `fanyi-curate` / `fanyi-produce` **skills are still Workflow-based** and are
  not wired to this. `/fanyi-curate` gets today's behaviour, not v2.
- **Images are not modelled.** `config.review_gates` has a curation/image/proof
  triple; v2 implements the first and third. `assemble.py` picks up
  `assets/bookN/book{N}_image_picks.json` if it is already there.
- **Real-run cost is still unmeasured.** The verification runs use the fake agent, so
  every cost reads `$0.00`. Run one chapter live and read `status` before committing
  to a volume.
- **Gate Channels are never drained.** SDK 0.2.5 exposes no way to list or delete a
  pending Channel message, so a duplicate or superseded `Approval` left on a gate's
  Channel satisfies a later re-entry of that gate without a human. Publish one decision
  per gate.
- **A deterministic stage failure is a loop the operator must break.** `resume --stage X`
  re-enters X with the same input, so if X fails the same way it lands back on the
  recovery gate. That is the intended shape — the operator is meant to fix the cause or
  pick a different stage — but nothing detects the cycle for you.

## The graph is machine-verified

`dexcli visualize` landed in dexcli 0.1.21 (this project was built against 0.1.15,
which did not have it). All three Flows analyse clean:

| Flow | steps | nodes | edges | diagnostics |
|---|---|---|---|---|
| `FanyiBook` | 12 | 91 | 132 | 0 |
| `FanyiCurateChapter` | 2 | 9 | 9 | 0 |
| `FanyiProduceChapter` | 7 | 29 | 57 | 0 |

`valid: true` with zero diagnostics is the check on "no helper hides Dex control
flow" — the analyser turns anything it cannot resolve statically into an Unknown node
and a blocking diagnostic, so the one-Flow-per-file and explicit-dispatch rules are
confirmed rather than merely intended.

- `dexcli visualize fanyi_dex/book/book_flow.py` — interactive Flow Rendering
- `uv run python tools/render_graph.py --check` — assert every Flow still validates
- `uv run python tools/render_graph.py --write GRAPH.md` — regenerate the diagram

`GRAPH.md` is generated from `dexcli visualize --json`, never hand-drawn, so the
picture cannot drift from the code.
