# testbook — the fixture project

A complete, self-contained translation project, here so a fresh clone can run the whole
volume Flow without any external assets.

```bash
uv run python tools/verify_e2e.py --human-gates
```

That builds a scratch copy of this fixture, runs a volume through every stage, answers
the three gates and the recovery gate, and asserts the final product. Nothing in this
folder is written by a run.

## What is here

| | |
|---|---|
| `source/chapters/hui_NNN.txt` | Chinese source, one file per chapter |
| `pipeline/config.json` | the project contract: books, policy, paths |
| `pipeline/build_pass2.py` | the deterministic ship gate (blocklists) |
| `pipeline/harvest_beatplan.py` | anchors an uncertain tier call back to its beat |
| `pipeline/harvest_reprocess.py` | writes finished chapters into the book folder |
| `pipeline/assemble.py` | chapters → master markdown, .docx, .epub |
| `pipeline/assemble_print.py` | master → 6x9 interior PDF + preflight |
| `vault/Book N — .../` | front and back matter, in the folder layout `assemble.py` expects |

## Provenance and licensing

**The Chinese source is public domain.** *Sanguo Yanyi* (三國演義) is attributed to Luo
Guanzhong and was written in the fourteenth century. The files are the plain source text,
unmodified.

**Everything else in this folder was written for this repository.** The config, the five
pipeline scripts, and the front and back matter are fixture implementations. They are not
copied from any real book project, and they deliberately encode no editorial policy: every
policy string in `config.json` is marked `PLACEHOLDER` and says in one plain line what a
real project would spend a page on.

That separation is the whole design of `dex-fanyi`. The runner holds no content and no
policy; a project supplies both. This fixture is the smallest project that satisfies the
contract, which makes it double as the contract's documentation — if you want to point
`dex-fanyi` at your own work, this folder is the list of what has to exist.

## Optional tools

The fixture degrades rather than failing when a tool is missing:

| Tool | Without it |
|---|---|
| `pandoc` | no `.docx` or `.epub`; the master markdown is still written and the run still passes |
| `typst` | no interior PDF, so the volume parks at the recovery gate — which is the correct behaviour, and what the `--human-gates` run demonstrates |
| `pypdf` | the interior is still built; the font and trim-size checks are skipped |
| `epubcheck` | the epub check reports `not-run` |

Install them with `brew install pandoc typst epubcheck` and `pip install pypdf` for the
full path.

## Chapter count

Twenty chapters are included so a run can be sized to taste. `tools/verify_e2e.py`
defaults to three, which is enough to exercise every stage; `--chapters 1,2,3,4,5` or
more costs proportionally more agent calls. With `FANYI_FAKE_AGENT=1` on the Worker every
call is free, so the only cost of a longer run is wall-clock.
