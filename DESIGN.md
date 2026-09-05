# dex-fanyi v2 — application design

Written before the implementation, per the `dex-developer` skill's "model the
application first". This is the *end-to-end* design: one durable execution per
volume, from reading the Chinese source to a proofed KDP interior PDF.

The v1 design (`fanyi_dex/flow.py`, `fanyi_dex/pass1_flow.py`) stays registered
and untouched — executions may still be parked at its gates, and a Step type is
part of the durable contract of an open execution.

## Flow boundary

| Flow type | Business execution | ID |
|---|---|---|
| `FanyiBook` | one volume of one project, initiation → final product | `book-<slug>-b<N>` |
| `FanyiCurateChapter` | one chapter's beat plan | server-generated SubFlow ID |
| `FanyiProduceChapter` | one chapter's two-pass transcreation + QA | server-generated SubFlow ID |

A chapter is a SubFlow, not a Step, because it has its own identity, its own
retry boundary, its own six-phase lifecycle, and because bounded fan-out of
SubFlows is a Dex pattern rather than something to hand-roll. v1's
`WaveStep` / `WaveJoinStep` / `ChapterFailedStep` / `chapter-done` Channel
counting collapses into one `wait_for` that awaits a batch of SubFlow
Conditions.

## `FanyiBook` step graph

Every book-level Step takes the same input type `StageRef`, so any Step can route
its exhausted retries to `RecoveryGate` and `RecoveryGate` can route back.

```
InitStep
  └─▶ CurateWaveStep ──(batch of FanyiCurateChapter SubFlows)──┐
        ▲                                                       │
        └───────────────── next batch ◀─────────────────────────┘
        └─(no pending)─▶ DirectorGate      GATE 1: tier / IN-OUT calls
              └─▶ ApproveItemsStep         stage the approved beat plan
                    └─▶ ProduceWaveStep ──(batch of FanyiProduceChapter)──┐
                          ▲                                               │
                          └──────────── next batch ◀──────────────────────┘
                          └─(no pending)─▶ QaGate    GATE 2: QA scorecard
                                └─▶ HarvestStep      chapters → vault (backed up)
                                      └─▶ AssembleStep   master.md, .docx, .epub
                                            └─▶ PrintStep      6x9 interior PDF
                                                  └─▶ QualityStep  epubcheck
                                                        └─▶ ProofGate  GATE 3
                                                              └─▶ complete(manifest)

any stage except RecoveryGate ──(exhausted retries)──▶ RecoveryGate
RecoveryGate ──(resume message names a stage)──▶ that stage again (including InitStep)
```

### Waits

| Step | `wait_for` |
|---|---|
| `CurateWaveStep`, `ProduceWaveStep` | `skip_immediately()` when the batch is empty, else `Wait.all_of(*SubFlow.run(...))` for it — the empty branch is what a recovery resume takes when nothing is left to do |
| `DirectorGate`, `QaGate`, `ProofGate` | `skip_immediately()` when the plan is auto-approve, else `any_of(approvals[gate].for_one(), Timer(reminder))` |
| `RecoveryGate` | `any_of(resume.for_one(), Timer(reminder))` |
| everything else | none (pure Execute) |

A gate stamps `stage` inside `wait_for`, beside the wait it is parked on — a gate
blocks there, so stamping only in `execute` leaves `stage` stale whenever the
gate is reached by a failure diversion.

## Durable state

Dex owns the state. The filesystem is an **export**, not the resume ledger.

| Definition | Kind | Purpose |
|---|---|---|
| `stage` | Attribute[str], KEYWORD index | current business stage, searchable |
| `note` | Attribute[str] | one human-readable line |
| `plan` | Attribute[RunPlan] | frozen at Init: chapter list, config digest, gate policy, wave size, efforts |
| `tier_overrides` | Attribute[str] | the director's GATE 1 payload, committed by the Step that consumed the approval |
| `tally` | Attribute[Tally] | counts + cost per pass |
| `chapters` | AttributeMap[ChapterRecord] keyed `h001` | per-chapter outcome, written independently |
| `chapter_plans` / `chapter_records` | AttributeMap[str] keyed `h001` | where each pass's chapter SubFlow exported its artifact — **separate maps**, because one shared map let a produce wave overwrite the beat-plan path |
| `manifest` | Attribute[Manifest] | the final product: master/docx/epub/pdf paths, page count, gate verdicts |
| `failure` | Attribute[StageFailure] | what `RecoveryGate` is parked on |
| `approvals` | ChannelMap[Approval] keyed by gate name | durable human decisions |
| `resume` | Channel[str] | operator recovery decisions |
| `progress` | Stream[str] | best-effort progress, one message per Step execution |

`plan` is frozen once: the chapter list, wave size, gate policy, and efforts cannot
change under a running volume, and a digest of `config.json` is recorded so an edit is
*visible* in `status` rather than silent. Voice/tier/naming policy is still resolved at
execution time from the config file — freezing that too would mean carrying the resolved
prompt blocks in the plan, which is not done.

A Channel value reaches only the Step execution whose wait consumed it, so a gate
decision a later Step needs must be committed to an Attribute by the gate itself —
that is what `tier_overrides` is for.

Per-chapter *payloads* (beats, segments, prose) live in the chapter SubFlow's own
Attributes, blob-backed and hydrated through the BlobCache the app already opens.
The parent keeps only the summary record, so one chapter landing does not rewrite
a whole-volume value.

## Failure behavior

| Concern | Decision |
|---|---|
| LLM phase | 3 attempts, 30s→5min backoff, `heartbeat_timeout` set explicitly to the method timeout |
| chapter SubFlow fails | terminal FAILED result; the parent records it and the batch still advances |
| gates and bookkeeping | uncapped retries — a capped count turns a Worker restart into a FAILED volume — *and* a route to `RecoveryGate`, so a deterministic exception parks the volume instead of burning the retry budget and failing it |
| the proof gate | refuses to sign off unless the manifest carries harvested chapters, a master, and an interior PDF — it is the only exit, and `resume --stage proof-gate` can reach it directly |
| vault harvest | backup first, then **one** attempt — the write is content-idempotent but the backup is not something to take twice in a retry storm, so a failure parks rather than retries |
| assemble / print / quality | exhausted retries route to `RecoveryGate`, never fail the volume |
| unknown `beat_id` from an agent | dropped and recorded, never carried into the book as source-less prose |

`heartbeat_timeout` is explicit because SDK 0.2.5 exposes the knob but no way to
*emit* a heartbeat, and `Stream.write` is once per Step execution. A silent 90s
Execute was measured to survive under the dev server's `STEP_DURABILITY_ASYNC`
default (`tests/probe_heartbeat.py`), but that is a property of the durability
default, not of the application — so the budget is stated rather than inherited.

## Read model

One typed `snapshot` RPC returns stage, note, plan, tally, every chapter record,
the gate the volume is parked on, and the manifest. v1 returned a JSON *string*
of scalars and the CLI re-scraped the filesystem for anything else.

## Verification

`tools/make_sandbox.py` builds a self-contained project (a config repointed,
chapters 1-3, real front/back matter, real pipeline scripts) so the end-to-end
run cannot touch the shipped book, `cutlists/beatplan_review.json`, or the vault.
`FANYI_FAKE_AGENT=1` fakes only the Claude call, so every deterministic script,
gate, and artifact contract runs for real.

Two modes, because they cover different paths:

- default — every gate auto-approved (`skip_immediately`), 17 checks
- `--human-gates` — each gate answered by publishing a real `Approval` to its Channel,
  18 checks, including that a `--tier-override` carried on the GATE 1 payload actually
  reaches the staged item

The second mode is the one that matters for gate logic: a Channel value reaches only the
Step execution whose wait consumed it, and auto-approve never consumes one.

### Known limitation

Gate Channels are never drained. SDK 0.2.5 exposes no way to list or delete a pending
Channel message, so a duplicate or superseded `Approval` left on a gate's Channel will
satisfy a later re-entry of that gate without a human. Publish one decision per gate.
