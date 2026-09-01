# Plan 20: Flag same-length word substitutions in OCR output

> **Executor instructions**: Follow step by step; run every verification
> command; honor STOP conditions; update the status row in `plans/README.md`
> unless a reviewer says they maintain the index.
>
> **Step 0 is a measurement gate.** This plan may legitimately end at Step 0
> with REJECTED if the noise rate makes the hint useless. That is a valid
> outcome, not a failure.
>
> **Drift check (run first)**:
> `git diff --stat 63bbceb..HEAD -- pdf2epub/text.py pdf2epub/analyzer.py tests/test_text.py`
> Material mismatch with the excerpts → STOP.

## Status

- **Priority**: P3 (high value, but gated on measurement)
- **Effort**: M
- **Risk**: MED — the hint's precision is unknown; shipping a noisy hint
  erodes trust in the report. Hence the gate.
- **Depends on**: plans/14
- **Category**: correctness
- **Planned at**: commit `63bbceb`, 2026-09-01

## Why this matters

The vision model sometimes reads a rare word as a frequent same-length
neighbour. Two confirmed instances on ONE line of a real conversion (page 9):
`القيوم` (Al-Qayyūm, a divine attribute) read as `القائم`, and
`الصفرية` ("zero-sum") read as `الصغيرة` ("small") — both real words, both
meaning-destroying. Verified against the shipped detectors: this class is
**invisible to all of them** — `dropped_character_findings` returns nothing
(no character was dropped; substitution, not deletion), word overlap measures
0.94 (far above every floor), and the quality score sees fluent Arabic.

The dropped-character hint (plan 12) proved the pattern: detect against the
embedded candidate, report as a review hint, never correct. This plan extends
it to edit-distance-1 **substitutions** — with one honest difference: the
noise floor is expected to be worse than drops' ~50%, because Arabic is dense
in legitimate 1-letter-apart word pairs, and OCR *correctly fixing* the
embedded layer's own errors also shows up as a "substitution". That is why
measurement comes first.

## Current state (verified at `63bbceb`)

### The sibling detector to extend (`pdf2epub/text.py`, `dropped_character_findings`)

Compares embedded-word set vs OCR-word set; for each embedded word absent
from OCR, checks whether the word minus one character appears in the OCR set.
Guards: `MIN_COMPARABLE_TEXT` (200) page floor,
`MIN_DROPPED_CHAR_WORD_LENGTH` (4). Reported via `PageResult.warnings` and
`candidates[...]["dropped_character_candidates"]` inside the `choose_ocr`
branch of `analyzer.py` (wired by plan 12; the wiring site has a
`dropped = dropped_character_findings(embedded, ocr_text)` call inside
`if embedded:` after OCR is selected).

### The confirmed real-world case, verbatim from the report

```
embedded (rejected): ...واإلله الموجه،، المتحكم القيوم، تراجع...
ocr:ollama (chosen): ...والإله الموجه، المتحكم، القائم، تراجع...
```

Note the embedded side carries ligature damage (`اإلله`) — the deterministic
repair regex `ا([اأإآ])ل → ال\1` from the recorded ligature work normalizes
comparisons; substitution detection must compare against the REPAIRED
embedded word, or the embedded layer's own damage will flood the results.

### Conventions

Same as plan 12's implementation: pure function in `text.py`, constants
module-level with a rationale comment, review-hint-only wiring in
`analyzer.py`, tests in `tests/test_text.py` plain functions.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0 |

(`python -m pytest` from the repo root.)

## Scope

**In scope**: `pdf2epub/text.py`, `pdf2epub/analyzer.py` (hint wiring only,
inside the existing plan-12 block), `tests/test_text.py`,
`tests/test_pipeline_ocr.py`, `plans/README.md` (status row).

**Out of scope**:

- ANY correction of text. Hint only, like plan 12.
- Blocking publication, `integrity`, `ocr_covers_embedded`, `choose_ocr`.
- Semantic/dictionary lookups, language models, frequency lists — this plan
  is pure string comparison; smarter filtering is a follow-up if the gate
  passes marginally.

## Steps

### Step 0: Measure the noise rate (GATE)

Write a THROWAWAY script (scratch, not committed) implementing the candidate
detector:

For each embedded word (length ≥ 5 — one longer than the drops' floor,
because same-length pairs are noisier) absent from the OCR word set, after
applying the ligature repair to the embedded side: report OCR words at
Hamming distance exactly 1 (same length, one differing position). Exclude
pairs whose difference is only a tanween/diacritic-class character or a
`ه/ة` or `ي/ى` final-form alternation — these are orthographic variants, the
known-noise classes from plan 12's data.

Run it over EVERY `quality-report.json` you can find under
`~/.local/share/pdf2epub/jobs/*/output/` (there is at least one recent one;
if none exist, ask the operator to convert a book first — that is a
BLOCKED outcome, record it). For every page with both candidates, print the
pairs.

**Decision rule**: manually classify the first 30 pairs (or all, if fewer)
into real-error / OCR-fixed-embedded / noise. Proceed only if real-errors are
≥ 25% of pairs AND the per-page pair count averages ≤ 5. Below that, mark
this plan REJECTED with the measured numbers in `plans/README.md` and stop —
the hint would bury its own signal. Record the classification table in your
report either way. The known-real pairs `القيوم→القائم` and
`الصفرية→الصغيرة` must appear in the output on the page-9 data if that
report is still on disk (if it is and they don't appear, your detector is
wrong — fix it before classifying).

### Step 1: Implement `substituted_word_findings` in `text.py`

Pure function, signature and guards mirroring `dropped_character_findings`,
with the Step 0 exclusions as named constants
(`SUBSTITUTION_MIN_WORD_LENGTH = 5`, an `ORTHOGRAPHIC_PAIRS` frozenset for
the variant classes). Docstring states the measured precision from Step 0.

**Verify**: unit tests — the two real pairs detected (using padded page-like
inputs, ≥200 chars); tanween variant excluded; `ه/ة` excluded; identical
texts → empty; short page → empty; different-length words never paired
(that's plan 12's territory — assert no overlap by feeding a known drop and
asserting THIS function ignores it).

### Step 2: Wire the hint

Inside the existing plan-12 block in `analyzer.py` (the `if embedded:` after
OCR selection), call the new function on the repaired embedded text, append
`substituted_word_candidates` next to `dropped_character_candidates` in the
same candidates dict, and extend the warning line (or add a parallel one)
capped at 8 pairs, same style. The bare-except guard around the block covers
it (deliberate: hints must never break a conversion).

**Verify**: end-to-end test in `tests/test_pipeline_ocr.py` mirroring
plan 12's — a fake provider substituting one letter in one word; assert the
pair surfaces in the report's warnings and candidates, and
`integrity.status` is unaffected.

### Step 3: Full suite and lint

**Verify**: full suite exit 0 (baseline + ≥7 new); ruff clean; and the
plan-12 flip-style check — stub the new function to return `[]`, confirm
exactly the new end-to-end test fails, restore.

## Test plan

Steps 1–2's tests as listed; existing plan-12 tests must stay green
unmodified (the two detectors share wiring but not behaviour).

## Done criteria

- [ ] Step 0's classification table is in the report AND summarized in
      `plans/README.md` (numbers, not adjectives) — whether the outcome is
      proceed or REJECTED.
- [ ] If proceeding: both real pairs detected by unit test; orthographic
      exclusions tested; end-to-end hint surfaces; integrity untouched;
      flip-check reported.
- [ ] Full suite green; ruff clean; scope respected; index row updated.

## STOP conditions

- Excerpts don't match live code.
- No quality report exists on disk and the operator hasn't provided one —
  BLOCKED, like plan 09.
- Step 0's gate fails — REJECTED, with numbers.
- The exclusion list grows past ~6 character-classes during Step 1 —
  that is dictionary-building by another name; stop and report, the
  follow-up (frequency-based filtering) needs its own design.

## Maintenance notes

- If this ships and the operator reports noise, the tuning knobs are, in
  order: `SUBSTITUTION_MIN_WORD_LENGTH` up, `ORTHOGRAPHIC_PAIRS` additions
  (sparingly — see the STOP condition), and per-page cap down. Never
  auto-correct as a "fix" for noise.
- The real long-term answer to this failure class is the recorded
  second-opinion direction finding (two independent models disagreeing);
  this hint is the single-model stopgap and stays useful afterwards as the
  cross-check trigger.
