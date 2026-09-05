# dex-fanyi

The classical-Chinese translation pipeline as durable Dex Flows. Config-driven
like the `fanyi-*` skills — pass `--config <project>/pipeline/config.json`.
Nothing about the tier policy, naming, or voice lives here; it is all read from
that file.

**Two generations live side by side.**

| | Scope | Entry point |
|---|---|---|
| **v2** (`book.py`) | **the whole volume**: source → beat plans → GATE 1 → transcreation + QA → GATE 2 → vault → .docx/.epub → 6x9 interior PDF → checks → GATE 3 | `book.py` |
| v1 (`fanyi.py`) | beat-plan and pass-1 only, handed off between them by a human | `fanyi.py` |

v1 stays registered and unchanged: executions are still open on it, parked at its
gates, and a Step type is part of the durable contract of an open execution. See
[DESIGN.md](DESIGN.md) for the v2 application design and
[V2_CHANGES.md](V2_CHANGES.md) for what differs and why.

## v2 — a whole volume, end to end

Run the two long-lived processes in **their own terminals**, each with an absolute
`cd`. Both need to outlive whatever is driving them:

```bash
# terminal 1
dexcli dev

# terminal 2
cd ~/dex-fanyi && FANYI_PYTHON=/usr/local/bin/python3 uv run python worker.py
```

Do not background the Worker inside an agent session — macOS reaps it under memory
pressure, and it was killed three times that way while this was being built. That
costs nothing durable (Dex Server holds the state; the volume just stops advancing
until a Worker is back), but the volume looks stuck for no visible reason. If
`book.py status` hangs or reports a worker dial error, the Worker is gone: restart
it and the volume continues from its last committed Step.

```bash
C=<project>/pipeline/config.json
uv run python book.py --config $C check  --book 2
uv run python book.py --config $C start  --book 2
uv run python book.py --config $C status --book 2 --chapters
uv run python book.py --config $C watch  --book 2

# the three gates
uv run python book.py --config $C approve --book 2 --gate director "tiers settled" \
    --tier-override 12.7=FULL
uv run python book.py --config $C approve --book 2 --gate qa    "scorecard read"
uv run python book.py --config $C approve --book 2 --gate proof "proof signed off"

# when a stage exhausts its retries the volume parks; this is how it continues
uv run python book.py --config $C resume  --book 2 --stage producing
```

`FANYI_PYTHON` must be a **system** interpreter: the pipeline scripts need
`zhconv` and `pypdf`, which are not in this project's venv. Point it at the venv
and `run_python` refuses by name rather than failing three stages later.

Re-running `start` on the same book is a **resume**, not a fresh run: Dex derives
each chapter SubFlow's ID from the parent Flow ID, so a new run attaches to the
chapters that already finished and re-runs only the rest. Verified — a full re-run
of a finished volume made **zero** agent calls. Pass `--generation N` when you
actually want to start over under a new identity.

### Run the test flow — nothing external required

A fresh clone can run a whole volume end to end. `fixtures/testbook/` is a complete
self-contained project: public-domain Chinese source (三國演義, chapters 1-20) plus a
config and five pipeline scripts written for this repo.

```bash
brew install superdurable/tap/dexcli    # once
uv sync

# terminal 1
dexcli dev
# terminal 2
cd <clone> && FANYI_FAKE_AGENT=1 FANYI_PYTHON=$(which python3) uv run python worker.py
# terminal 3
uv run python tools/verify_e2e.py --human-gates
```

`FANYI_FAKE_AGENT=1` fakes the Claude call and nothing else, so the run needs no API
access and costs nothing while every deterministic script, gate, and artifact contract
runs for real. Drop it to translate for real.

Two modes:

| | Gates | Checks |
|---|---|---|
| `verify_e2e.py` | auto-approved (`skip_immediately`) | **17/17** |
| `verify_e2e.py --human-gates` | each answered by publishing a real `Approval` | **18/18** |

The second is the one that matters for gate logic: a Channel value reaches only the Step
execution whose wait consumed it, so auto-approve never exercises that path. It also
asserts a `--tier-override` carried on the GATE 1 approval reaches the staged item.

The run builds a scratch copy of the fixture in `.run/` (gitignored) and never writes the
fixture itself. A 3-chapter book is 12 pages, under the 24-page print minimum, so it
**parks at the recovery gate instead of calling itself finished** — and
`resume --stage checking` is the human decision that carries it to done. That is the
intended behaviour, and what `--human-gates` demonstrates.

`pandoc`, `typst`, `pypdf`, and `epubcheck` are optional; see
[fixtures/testbook/README.md](fixtures/testbook/README.md) for what each one costs you.

### Point it at your own project

`dex-fanyi` holds no content and no editorial policy — a project supplies both, through
`<project>/pipeline/config.json` and five scripts beside it. `fixtures/testbook/` is the
smallest project satisfying that contract, so it doubles as the contract's documentation.

## v1 — beat-plan and pass 1

## ⚠️ Before pointing v1 at a real project

`harvest_beatplan.py` writes `<project>/cutlists/beatplan_review.json`
**unconditionally**, overwriting whatever is there — including a pending tier-call
review a human has not finished. Back it up first:

```bash
cp <project>/cutlists/beatplan_review.json \
   <project>/cutlists/beatplan_review.json.bak
```

v1 also treats only artifacts under `<project>/pipeline/run/dex/` as cached, so plans
produced by any other path are invisible to it and will be regenerated from scratch.

v2 does not have this hazard: it never invokes that script's `main()`, and writes its
review artifact into the run directory instead.

## What this actually buys

Stated honestly, because the first version of this argument was wrong:

* **Surviving session death.** Workflow's `resumeFromRunId` cache is same-session
  only. A connection death late in a long volume means every chapter finished so far
  was finished inside a run that no longer exists. Here each chapter writes its
  plan file the moment it lands, and a re-run skips it — verified: after deleting
  one plan file from a finished volume, a fresh run made exactly one agent call.
* **A gate that outlives the run.** A Workflow run must end before a human reviews
  a volume's uncertain tier calls, so the gate is something you remember. Here the volume
  literally sits at `stage: director-gate` until you run `reviewed`.
* **A timeout outside the harness.** A separate `claude -p` process has no 180s
  first-token watchdog; Dex sets a 30-minute per-chapter budget.

**What it does not buy.** The Workflow tool already has parallel fan-out,
parse-retry, and same-session resume — `build_beatplan.py` picks a serial loop by
choice, and its 3-attempt retry is ported here rather than invented. And the
watchdog argument is weak *for this stage*: build_beatplan.py's own header
records that no-schema + low-effort already fixed the stall, and that shape is
honored here. **The watchdog case properly belongs to Pass-1**, which in the
generated workflow runs `SEG_SCHEMA`/`AUDIT_SCHEMA` at `effort:'medium'` — see the
Pass-1 section below, which drops the schemas for exactly that reason.

## Setup

```bash
dexcli dev                   # Dex server :8801, Web :8802 — leave running
cd ~/dex-fanyi && uv sync
uv run python worker.py      # hosts both Flows — leave running, restart freely
```

## Beat-plan (STEP 0)

```bash
C=<project>/pipeline/config.json
uv run python fanyi.py --config $C check --book 1      # can this project run the stage?
uv run python fanyi.py --config $C prompt --hui 86 --count
uv run python fanyi.py --config $C start --book 5 --aggressive
uv run python fanyi.py --config $C status --book 5
uv run python fanyi.py --config $C reviewed --book 5 "tier calls settled"
```

`check` refuses projects that lack the config this stage needs, rather than
failing mid-flow:

| Project shape | Result |
|---|---|
| abridged, with tier policy and a volume plan | OK |
| abridged, tier policy not yet written | refused: `structure.tier_handling`, `structure.verbatim_policy` |
| unabridged (no curation, so no tiers) | refused: `books` + all tier keys |

Chapters a volume wants compressed harder take `--aggressive`, the same directive the
project's own beat-plan builder injects. Re-run stragglers with `--only 12,13`.

### The graph

```
StartStep ──▶ WaveStep ──┬──▶ ChapterStep ×N ──┬──▶ WaveJoinStep ──┐
   (skips already-       │      (claude -p)     │   (awaits N)     │
    planned chapters)    │           └─fails─▶ ChapterFailedStep ──┘
                         │                                          │
                         └◀──────── next wave ──────────────────────┘
                         │
              (no pending) ──▶ CombineStep ──▶ HarvestStep ──▶ DirectorGate ──▶ done
                                                                  ▲
                                              (0 plans, or harvest failed) ─┘
```

Chapters run in bounded waves (`--wave-size`, default 4) rather than all at once,
keeping the rate-limit safety of the serial loop while overlapping work.

`ChapterFailedStep` exists so one bad chapter cannot deadlock a volume: the wave
join counts *completions*, not successes, so a failed chapter still releases the
wave. Verified by pointing `FANYI_CLAUDE_BIN` at a binary that always exits 1 —
all chapters failed, the volume still reached the gate with an actionable note
instead of hanging.

### Integration seam

This replaces only the execution engine. The prompt is ported verbatim into
`prompts.py`, and the emitted `{"plans": [...]}` file is consumed by the
project's own unmodified `harvest_beatplan.py`, which stages
its staged items file for the next pass exactly as before.

Artifacts are namespaced under `<project>/pipeline/run/dex/beatplan/book<N>/` so
they can never be confused with Workflow-path output:

```
h086.json …            one chapter plan each (the resume unit)
h086.FAILED.txt        a chapter that exhausted its retries
beatplan_output.json   the combined file handed to harvest
```

If you edit the prompt template in `build_beatplan.py`, edit `prompts.py` too —
`tests/` asserts the structural markers still line up, but it cannot detect
reworded instructions.

## Retry policy

Both Flows share this shape.

**Gates and bookkeeping steps retry forever** (`maximum_attempts` unset). A
capped count turns an ordinary worker restart into a permanently FAILED flow,
and on the wave join it would deadlock the volume.

**Chapter steps get 3 attempts** (30s → 5min backoff) on top of the 3 in-handler
parse attempts, then divert to `ChapterFailedStep`.

`stage` is stamped at step *entry* — including inside `DirectorGate.wait_for`,
because a gate blocks there and its `execute` does not run until the gate opens.
Without that, a volume reached by a failure diversion reports the stage it was
leaving rather than the gate it is parked at. That bug was real and is fixed.

### Beat-plan cost

Measured on a deliberately tiny fixture chapter: **$0.045/chapter** with
`--bare`. A real 5,400-character chapter costs materially more, since output
scales with beat count (~24-30 beats each carrying its `zh_span`). `--bare`
matters: it skips CLAUDE.md, hooks, and auto-memory, cutting per-call overhead
from ~14,400 to ~1,600 cache-creation tokens.

The running total is on the flow as `costUsd`, so run one real chapter with
`--only 86` and read the number before committing to 35.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `DEX_FLOW_SERVICE_ADDRESS` | `127.0.0.1:8801` | |
| `DEX_WORKER_BIND_ADDRESS` | `127.0.0.1:8812` | dex-yt uses 8811 |
| `FANYI_MODEL` / `FANYI_EFFORT` | `opus` / `low` | matches the Workflow agent options |
| `FANYI_BARE` | on | see Cost |
| `FANYI_WAVE_SIZE` | `4` | chapters in flight |
| `FANYI_CHAPTER_TIMEOUT_S` | `1800` | per-chapter budget |
| `FANYI_PARSE_ATTEMPTS` | `3` | in-handler retry, as in the JS |
| `FANYI_FAKE_AGENT` | off | fake the Claude call, run the real scripts — validates the seam for free |
| `FANYI_DRY_RUN` | off | fake everything |
| `FANYI_PYTHON` | `python3` | interpreter for pipeline scripts (harvest imports zhconv) |
| `FANYI_CLAUDE_BIN` | `claude` | |
| `FANYI_PASS1_EFFORT` | `medium` | pass-1 phases; matches the generated workflow |
| `FANYI_PASS1_TIMEOUT_S` | `2700` | per-phase budget (45 min) |
| `FANYI_INNER_CONCURRENCY` | `3` | cap on fan-outs *inside* one chapter (dialogue repairs, audit lenses) |
| `FANYI_FAKE_FAIL_PHASE` | unset | test hook: `pass1`/`dlg`/`pass2`/`audit`/`remediate`/`access`/`detfix` returns unparseable text |

## Pass 1 — two-pass transcreation (STEP 1 → 3)

Ported from `build_pass2.py`, which despite its name emits the whole chain:
STEP 1 Pass-1 transcreate → STEP 1b dialogue repair → STEP 2 Pass-2 fluency →
STEP 3 QA (3-lens audit → remediate → re-audit → accessibility gate →
deterministic ship gate). Only the `final` segments are harvestable, so all of it
is ported, not just the phase named "Pass-1".

```bash
C=<project>/pipeline/config.json
uv run python fanyi.py --config $C pass1-check  --book 4
uv run python fanyi.py --config $C pass1-start  --book 4 --items <path>
uv run python fanyi.py --config $C pass1-status --book 4
uv run python fanyi.py --config $C pass1-report --book 4     # QA scorecard
uv run python fanyi.py --config $C pass1-reviewed --book 4 "looks good"
```

### Each phase is its own durable Step

```
Pass1 ──▶ DialogueRepair ──▶ Pass2 ──▶ Audit ──┬─(findings)─▶ Remediate ──┐
                                                └─(clean)─────────────────┴──▶ Finalize
```

Artifacts land per chapter in `pipeline/run/dex/pass1/book<N>/h<NNN>/`:
`item.json`, `p1.json`, `p1_repaired.json`, `p2.json`, `audit.json`,
`remediated.json`, `chapter.json`, `cost.json`.

A re-run skips any phase whose artifact exists, so an interrupted volume resumes
**mid-chapter**. This is the point of the split: Pass-1 and Pass-2 are the
expensive calls, and an audit failure must not redo them. Verified — after
deleting `p2.json` onward, a re-run made zero `pass1` and zero `dlg-repair` calls
and only re-ran Pass-2 forward. Same when recovering from a real mid-phase
failure: `FAILED.txt` records which phases completed, and the re-run reuses them.

### `--items` is explicit on purpose

Pass 1 consumes the **director-approved** beat plan, whose canonical location
differs per book after several generations of artifacts. `pass1-check` reports
every candidate with its coverage against the config's book range:

```
book N: hui LO-HI, 22 chapters expected
  <candidate-a>.json     22/22 (hui LO-HI)      complete
  <candidate-b>.json     21/22 (hui LO-HI-1)    PARTIAL
  -> only one complete option

book M: hui LO-HI, 35 chapters expected
  <candidate-c>.json     35/35 (hui LO-HI)      complete
  <candidate-d>.json     35/35 (hui LO-HI)      complete
  <candidate-e>.json     27/35 (hui LO-HI-1)    PARTIAL
  -> more than one complete option; pick deliberately
```

Two files can both be complete and still not be interchangeable — a compressed
edition and an uncompressed one cover the same chapter range differently — so the
tool reports and refuses to guess. A file short of the book range needs
`--allow-partial`.

### Two deviations from the generated workflow, both deliberate

**No structured-output schemas.** Every phase in the JS passes `schema:` at
`effort:'medium'` — the exact request shape the project's own notes blame for
stalling past the 180s watchdog. `claude -p` has no schema parameter, so the
prompts ask for one JSON object as text and parse-retry, the shape already proven
on the beat-plan stage. Effort stays medium; a separate process has no
first-token watchdog. **This is the stage where the watchdog argument is real.**

**Unknown `beat_id`s are dropped.** The JS does `byId[s.beat_id]||{}`, so a
`beat_id` the plan never had becomes a segment with an empty `zh_span` — invented
prose with no source behind it, carried into the book. Here the plan's order and
membership win: unknown ids are dropped and recorded in `unknown_beat_ids`, and a
non-CUT beat that came back with no prose is recorded in `missing_beat_ids`. Both
land on the chapter record.

### The deterministic gate is reused, not copied

`detscan.py` imports the project's `build_pass2.py` (the same importlib trick
`harvest_reprocess.py` uses) so the archaism/calque/unit-leak/poem-ref/
wrong-romanization blocklists cannot drift from the gate that actually decides
whether a chapter ships. `gate_count()` matches the JS `detN` — four categories;
`wrong_roman` is reported and surfaced by `pass1-report` but excluded from the
count, matching the shipped gate even though build_pass2.py calls it
ship-blocking.

### Harvest is NOT run automatically

`harvest_reprocess.py` writes `回NN (edited).md` into the Obsidian vault. The Flow
stops at a review gate and `pass1-report` prints the command instead. Back up the
book folder before running it.

### Cost is unmeasured

The beat-plan stage measured $0.045/chapter on a tiny fixture. Pass 1 is
6–10 Claude calls per chapter at medium effort with whole-chapter prose in and
out, so it is materially more — but I have not run a real chapter, so I have no
number for it. Run one first and read `costUsd`:

```bash
uv run python fanyi.py --config $C pass1-start --book 4 --only 64 \
    --items <project>/cutlists/<approved-beat-plan>.json
```

The specific risk worth watching on that first chapter: Pass-1 returns ~24-30
prose segments as a single JSON object, which is a large reply to keep valid.
Parse failures retry (`FANYI_PARSE_ATTEMPTS`, default 3) and then divert to the
fix gate rather than corrupting anything, but if it fails repeatedly that is the
signal to reconsider the no-schema tradeoff.

## Not built — in v1

These are limitations of **v1**, the section above. v2 addressed the last two: it
implements the curation and proof gates and streams progress. See
[V2_CHANGES.md](V2_CHANGES.md#known-gaps) for what is still open in v2.

- **The `fanyi-curate` / `fanyi-produce` skills are untouched.** They still emit
  Workflow scripts, so `/fanyi-curate` gets today's behavior, not this — and this is
  still true of v2. Note also that `fanyi-curate` drives the older *scene* cut-list
  model (KEEP-FULL/COMPRESS/CUT), while this ports the newer *beat* model from
  `build_beatplan.py` — which by design does not reuse those scene decisions.
- Curation GATE_1, image GATE_2, and proof GATE_3 from `config.review_gates`
  (v2 has the curation and proof gates; the image gate is open in both).
- No progress `Stream`, so a running chapter is silent until it lands
  (v2 has one on all three Flows).
