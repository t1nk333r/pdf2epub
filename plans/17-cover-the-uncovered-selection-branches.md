# Plan 17: Test the candidate-selection branches that have zero coverage

> **Executor instructions**: Follow step by step; run every verification
> command; honor STOP conditions; update the status row in `plans/README.md`
> unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 63bbceb..HEAD -- pdf2epub/analyzer.py tests/test_pipeline_ocr.py`
> Material mismatch with the excerpts → STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW (pure test additions)
- **Depends on**: plans/14
- **Category**: tests
- **Planned at**: commit `63bbceb`, 2026-09-01

## Why this matters

The candidate-selection policy in `analyze_pdf` — which text ends up in the
published book — has four whole outcome branches with **zero** test coverage:
intentional-blank pages, image-heavy page typing, the two retained-embedded
REVIEW outcomes, and OCR-returned-nothing UNAVAILABLE. A regression in any of
them silently changes published content and no test fails. This is also the
prerequisite for the recorded direction finding "extract the selection policy
into a pure function": characterization tests must exist before that refactor
is safe. Grep evidence: `intentional_blank`, `IMAGE_HEAVY`/`HYBRID`
(analyzer-produced), `"clearly safer"`, `"OCR did not account"`, and
`"OCR returned no usable text"` have no hits under `tests/` today.

## Current state (all verified at `63bbceb`)

### Branch A — intentional blank (`pdf2epub/analyzer.py:551-558`)

```python
            try:
                intentional_blank = (
                    options.mode != "force_ocr"
                    and not embedded
                    and not images
                    and not page.get_drawings()
                )
```

Selected pages get `PageSelection.INTENTIONAL_BLANK` and
`"No text, images, or vector drawings were present"`.

### Branch B — image-heavy typing (`pdf2epub/analyzer.py:761-762`)

```python
            elif image_heavy:
                kind = PageType.HYBRID if embedded else PageType.IMAGE_HEAVY
```

`image_heavy` is computed earlier from summed image rect area
`> page_area * 0.35`.

### Branch C — retained-embedded REVIEW (two reasons, `analyzer.py:696-717`)

When OCR ran but `choose_ocr` is false and embedded text exists, the page is
kept as embedded with `PageSelection.REVIEW` and one of two reasons:
`"OCR did not account for the text already on the page"` (coverage gate
refused) or `"OCR did not provide a clearly safer candidate"`.

### Branch D — UNAVAILABLE after successful-but-empty OCR (`analyzer.py:718-724`)

No embedded text, OCR returns no usable blocks →
`PageSelection.UNAVAILABLE`, warning `"OCR returned no usable text"`, and the
integrity gate later fails the conversion (`pipeline.py` raises
`ConversionIntegrityError` "No usable text could be recovered from pages").

### Existing harness to reuse (`tests/test_pipeline_ocr.py`)

`FakeOcrProvider` / `FailingOcrProvider` (top of file), `_text_pdf` helper,
and the established pattern: build a small `pymupdf` doc, run
`convert(source, out, ConversionOptions(...), ocr_provider=...)`, assert on
`json.loads(outputs["report"].read_text())["page_details"]`. Use Latin filler
text for embedded layers — `pymupdf.insert_text` is not Arabic-shaping-safe
(recorded plan-13 constraint); the selection logic under test is
script-agnostic. Note `evaluate_text` scores Latin text too (length +
`[A-Za-z]` script score 0.65 path) — pick text lengths so the branch you
target actually fires; verify with a scratch probe before finalizing each
test, and put the measured score in a comment.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Focused | `.venv/bin/python -m pytest -q tests/test_pipeline_ocr.py` | exit 0 |
| Full | `.venv/bin/python -m pytest -q` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0 |

(`python -m pytest` from the repo root.)

## Scope

**In scope**: `tests/test_pipeline_ocr.py` (or a new
`tests/test_selection.py` if the file passes ~700 lines), `plans/README.md`
(status row).

**Out of scope**: ANY change to `pdf2epub/` source. These are
characterization tests: they pin what the code does today. If a test reveals
behaviour that looks wrong, record it in your report as a finding — do not
fix the source and do not write the test to assert the "should" behaviour.

## Git workflow

Branch `advisor/17-cover-the-uncovered-selection-branches`; imperative
unprefixed subjects; no push unless told.

## Steps

Each step: write the test, run it, confirm it passes against unmodified
source, and confirm it FAILS if you temporarily flip the branch's outcome
(e.g. comment the branch condition) — then restore. Report each flip result.

### Step 1: `test_truly_blank_page_is_intentional_blank`

A `pymupdf` doc with one entirely empty page between two text pages.
Assert page 2: `selection == "intentional_blank"`, `page_type == "TEXT"`,
no OCR call for it (assert the fake provider's call count).

### Step 2: `test_image_heavy_page_types` (two cases)

Insert a large raster (>35% of page area — build with PIL, insert via
`page.insert_image`) on a page WITH embedded text → `page_type == "HYBRID"`;
on a page WITHOUT text but where OCR returns text (so the page isn't
unavailable) → assert the analyzer-produced type for that configuration as
observed (characterize; the interaction with the OCR branch may yield SCAN —
assert what it actually does and comment why).

### Step 3: `test_ocr_that_fails_coverage_leaves_embedded_for_review`
### and `test_ocr_no_better_than_embedded_leaves_review`

Two fake providers: one returning a tiny fraction of a long embedded page
(coverage gate refusal → reason `"OCR did not account for the text already on
the page"`), one returning text scoring no better with no severe-issue
difference (reason `"OCR did not provide a clearly safer candidate"`).
Convert with `allow_partial=True` so the run completes; assert
`selection == "review"`, `source == "embedded_text"`, and the exact
`selection_reason` strings.

### Step 4: `test_empty_ocr_with_no_embedded_text_is_unavailable`

Provider returning whitespace-only text on a page with no embedded layer.
Assert conversion raises `ConversionIntegrityError` matching
`"No usable text"` and that the written report shows
`selection == "unavailable"` and the warning string.

### Step 5: Full suite and lint

**Verify**: full suite exit 0 (baseline + ≥5 new); ruff clean.

## Test plan

The five tests above ARE the plan; model structure on
`test_short_clean_page_is_not_held_back_for_review` in the same file.

## Done criteria

- [ ] ≥5 new tests; each flip-check performed and reported.
- [ ] `grep -l "intentional_blank\|OCR returned no usable text\|clearly safer\|did not account" tests/`
      → at least one test file.
- [ ] Zero `pdf2epub/` source modifications (`git diff --stat` proves it).
- [ ] Full suite green; ruff clean; index row updated.

## STOP conditions

- Excerpts don't match live code.
- A branch cannot be reached with any fake-provider/PDF combination after two
  honest attempts — report the recipe you tried; unreachable code is itself a
  finding, not something to force.
- A characterization test exposes behaviour you believe is a bug — report it;
  do not fix.

## Maintenance notes

- These tests are the safety net for the planned selection-policy extraction;
  whoever does that refactor should run exactly this file first and last.
- The flip-checks (mutation-style) are one-off manual proofs; if the team
  ever adopts mutation testing, this file is the first candidate.
