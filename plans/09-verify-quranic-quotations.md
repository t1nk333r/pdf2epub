# Plan 09: Verify Qur'anic quotations against a reference text

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Step 0 is a go/no-go gate.** This plan is allowed to end at Step 0 with
> "not worth building". That is a successful outcome, not a failure.
>
> **Drift check (run first)**:
> `git diff --stat 5f56abb..HEAD -- pdf2epub/text.py pdf2epub/models.py pdf2epub/pipeline.py`
>
> **Important**: this plan was written against a working tree with **14 modified
> files not yet committed** at `5f56abb`. The excerpts in "Current state" come
> from that working tree, not from the commit. If `git status` is clean and the
> excerpts do not match, the uncommitted work was discarded — that is a STOP
> condition. If `git status` is dirty, compare the excerpts directly.

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: correctness
- **Planned at**: commit `5f56abb` (+ uncommitted work), 2026-08-28
- **Source**: idea adapted from `The_Arabic_OCR` (see "Provenance and licensing")

## Why this matters

The converter can tell that a page's text is *well-formed*, but not that it is
*right*. On one page of the test corpus the two candidate readings disagreed on
a Qur'anic citation: the embedded text layer said `الإسراء: 65` and the OCR read
said `الإسراء: ٢٤`. Al-Isrā' 24 is the correct verse for that āyah, so the OCR
was right — but nothing in the pipeline could know that. Both candidates scored
identically (0.75), because every quality signal measures shape, not truth.

Qur'anic quotations are the one class of content in an Arabic book where an
authoritative reference text exists and an exact check is possible. Verifying
them turns an invisible transcription error into an explicit review signal.

**Honest limit on the evidence**: this rests on **one observed instance**. A
scan of every quality report available at planning time found only 4 quotation
markers, all the same verse from the same page repeated across re-runs of the
same 20-page sample. The frequency of Qur'anic quotations in the wider corpus is
**unmeasured**. Step 0 exists to measure it before any code is written, and to
abandon the plan if the answer is "too rare to matter".

## Current state

### Files involved

- `pdf2epub/text.py` — Arabic text utilities: normalisation, quality scoring.
  New matching helpers belong here.
- `pdf2epub/models.py` — `PageResult` (line 109) and `DocumentResult` (line 128)
  dataclasses; `DocumentResult.report()` (line 136) builds the quality report.
- `pdf2epub/pipeline.py` — `convert()` orchestrates analysis, report writing and
  the integrity gate (lines 60–90).
- `pdf2epub/analyzer.py` — per-page analysis; populates `PageResult.warnings`.

### There is no Qur'an handling anywhere today

```
$ grep -rn "قرآن\|quran\|Quran" --include="*.py" pdf2epub/
(no matches)
```

The only related thing that exists is the ornate-parenthesis pair in the
punctuation set at `pdf2epub/text.py:28`:

```python
NORMAL_PUNCTUATION = frozenset("،؛؟?.!,:()[]{}\"'«»…ـ-–—−/\\+%٪=*_#@&·•﴿﴾")
```

`﴿` (U+FD3F) and `﴾` (U+FD3E) are the conventional delimiters around a Qur'anic
quotation in Arabic typesetting. They are the anchor this plan matches on.

### The warning channels that already exist

`PageResult` carries per-page warnings (`pdf2epub/models.py:119`):

```python
    warnings: list[str] = field(default_factory=list)
```

`DocumentResult` carries document-level warnings (`pdf2epub/models.py:133`),
which `report()` emits verbatim under the `"warnings"` key. `pipeline.py:85` is
an existing example of appending one:

```python
        document.warnings.append(
            "Partial output was explicitly allowed; review the integrity section"
        )
```

Use these channels. Do **not** invent a new top-level report key.

### The observed defect, verbatim

From the page-5 candidates of the test corpus:

```
embedded_text : ﴿ وَقُل رَبِّي ار حَم هُمَا كَمَا رَب َّيَانِي صَغِيرًا﴾   اإلسراء: 65.
ocr:ollama    : ﴿وَقُل رَّبِّ ارْحَمْهُمَا كَمَا رَبَّيَانِي صَغِيرًا﴾    الإسراء: ٢٤.
```

Note both the mangled embedded text and the differing verse number.

### Conventions to match

- 4-space indent, `from __future__ import annotations` at module top, type hints
  on every signature, Ruff default rules clean.
- Module-level helpers in `pdf2epub/text.py` are plain functions with a
  docstring — see `normalize_arabic` at `pdf2epub/text.py:151` for the exemplar:

  ```python
  def normalize_arabic(text: str) -> str:
      """Conservative cleanup: preserve letters, digits, and diacritics."""
  ```

  Note that docstring. It states a design rule this plan must honour: **this
  codebase does not discard information from the text.**
- Tests live in `tests/`, are plain `def test_*` functions, and use `tmp_path` /
  `monkeypatch` fixtures. `tests/test_text.py` is the file for text utilities.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0, all pass |
| Focused tests | `.venv/bin/python -m pytest -q tests/test_text.py` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0, `All checks passed!` |

Note: the console scripts in `.venv/bin/` carry a stale shebang and fail with
"No such file or directory". Always invoke pytest as
`.venv/bin/python -m pytest`. The `ruff` binary works directly.

## Provenance and licensing

The idea comes from the `The_Arabic_OCR` project
(`https://github.com/adam76064/The_Arabic_OCR`), which offers verse lookup
against a bundled Uthmani database.

**That repository has no LICENSE file.** Under default copyright that means all
rights reserved: its code may not be copied, adapted, or vendored into this
project. This plan therefore reimplements an *idea*, which is not copyrightable.

- Do **not** copy any source file, function body, or data file from that
  repository.
- Do **not** add it as a dependency or a git submodule.
- Source the reference text independently (Step 1) and record its licence.

If you find yourself reading their code to decide how to write yours, stop and
report — this plan is specified fully enough not to need it.

## Scope

**In scope** (the only files to create or modify):

- `pdf2epub/quran.py` (create) — reference text loading and matching
- `pdf2epub/text.py` — quotation extraction helper only
- `pdf2epub/analyzer.py` — call the check, append warnings
- `tests/test_quran.py` (create)
- `tests/test_text.py`
- `pyproject.toml` — only if a data file must be declared as package data
- `plans/README.md` (status row only)

**Out of scope**:

- **Correcting the text.** This plan adds a *check*, never a rewrite. A
  mismatch produces a warning; the page's text is left exactly as it was. The
  converter's whole design is that a machine flags and a human decides — see
  the integrity gate at `pdf2epub/pipeline.py:68-84`.
- **Hadith, poetry, or any other quotation type.** Only Qur'anic text has an
  authoritative reference that makes exact matching sound.
- **Blocking publication on a mismatch.** Do not touch
  `ConversionIntegrityError` or the `integrity` section of the report. A verse
  mismatch is a warning, not a gate; wiring it into the gate would block books
  over a footnote.
- **The typography-cleaning features of the source project** (hamza
  normalisation, tashkeel stripping, kasheeda removal). These are explicitly
  rejected — see "Rejected ideas".
- Any network call at conversion time. The reference text is local data.

## Git workflow

- Branch: `advisor/09-verify-quranic-quotations`
- Commit subjects are imperative and unprefixed; recent examples from
  `git log --oneline`: `Guard job state transitions and recover corrupt checkpoints`,
  `Run startup recovery from a lifespan handler`. Match that style.
- Do NOT push or open a pull request unless the operator instructs it.

## Steps

### Step 0: Measure whether this is worth building (GO / NO-GO)

Before writing any feature code, measure how often Qur'anic quotations actually
occur in real conversions.

Gather every `quality-report.json` you can find from past runs, then count
quotation markers:

```bash
.venv/bin/python - <<'EOF'
import json, re, sys, glob
Q = re.compile(r"﴿([^﴾]{3,300})﴾")
total = pages = 0
for path in sys.argv[1:] or glob.glob("**/quality-report.json", recursive=True):
    for p in json.load(open(path))["page_details"]:
        text = "\n".join(b["text"] for b in p["blocks"])
        hits = Q.findall(text)
        if hits:
            pages += 1
            total += len(hits)
print(f"{total} quotations across {pages} pages")
EOF
```

If no reports are available, convert a real Arabic book of at least 100 pages
and run the count on its report.

**Decision rule**: proceed only if the count is **at least 20 quotations across
at least 10 distinct pages** in a corpus of 100+ pages. Below that, the feature
cannot pay for its maintenance and data footprint.

**If below the threshold**: STOP. Record the measured numbers in
`plans/README.md`, mark this plan `REJECTED — insufficient frequency`, and
report back. Do not build it anyway.

**Verify**: the counting command runs and prints a number you have recorded.

### Step 1: Source a licence-clean Uthmani reference text

Obtain the Qur'anic text as data you are permitted to redistribute. Tanzil.net
publishes Uthmani text under stated terms; other sources exist. Requirements:

- Plain text or JSON, one entry per āyah, with sūrah number, āyah number, and
  the text.
- A licence that permits redistribution inside this repository.
- Under 5 MB, so the Docker image stays reasonable.

Record the source URL, version, and licence in a header comment in
`pdf2epub/quran.py` and in `README.md`'s dependency notes.

**If you cannot establish clear redistribution rights**: STOP and report. Do not
ship text of uncertain provenance, and do not fetch it at runtime as a
workaround.

**Verify**: the data file exists, its licence is recorded, and
`.venv/bin/python -c "import json;d=json.load(open('<path>'));print(len(d))"`
prints 6236 (the number of āyāt).

### Step 2: Add the matcher

Create `pdf2epub/quran.py` with:

```python
def normalize_for_match(text: str) -> str:
    """Strip diacritics and orthographic variation for comparison only.

    The returned string is used to compare against the reference text and is
    never written back into the document.
    """
```

This is the one place where lossy normalisation is correct, because the output
is a comparison key, not content. Strip tashkeel, tatweel, and hamza variation,
and collapse whitespace.

Then:

```python
def find_reference(quotation: str) -> tuple[int, int] | None:
    """Return (surah, ayah) for an exact normalized match, else None."""
```

Build a lookup from the normalized reference text once, at module import, and
cache it. Match the normalized quotation as a substring of a normalized āyah, or
an āyah as a substring of the quotation (books quote partial verses and run
several together).

Add to `pdf2epub/text.py`:

```python
QURAN_QUOTE_RE = re.compile(r"﴿([^﴾]{3,300})﴾")


def quranic_quotations(text: str) -> list[str]:
    """Return the text inside each ﴿…﴾ pair."""
```

**Verify**: `.venv/bin/python -m pytest -q tests/test_quran.py` → exit 0.

### Step 3: Check the citation that follows the quotation

A citation like `الإسراء: ٢٤` normally follows the closing `﴾`. Parse the sūrah
name and āyah number from the 80 characters after the quotation, map the name to
its number, and compare against what `find_reference` returned.

Handle both Arabic-Indic (`٠١٢٣٤٥٦٧٨٩`) and Latin digits — the corpus contains
both, sometimes on the same page.

Report exactly three outcomes:

- quotation matched and citation agrees → nothing
- quotation matched and citation disagrees → warning naming both
- quotation not found in the reference → warning that it could not be verified

**Verify**: a unit test asserts each of the three outcomes.

### Step 4: Wire it into per-page analysis

In `pdf2epub/analyzer.py`, after a page's blocks are final, run the check over
the page text and append any warnings to that page's `warnings` list — the same
list used at `pdf2epub/models.py:119`. Do not modify block text.

Guard the whole thing so a failure in this check can never fail a conversion:

```python
    try:
        ...
    except Exception:  # noqa: BLE001 - verification must never break a conversion
        pass
```

That bare guard is justified here and only here: a reference-data problem must
degrade to "no verification", never to a failed book.

**Verify**: `.venv/bin/python -m pytest -q` → exit 0, all tests pass.

### Step 5: Full suite and lint

**Verify**:
- `.venv/bin/python -m pytest -q` → exit 0, at least 121 tests pass.
- `.venv/bin/ruff check .` → exit 0, `All checks passed!`

## Test plan

In `tests/test_quran.py` (create):

- `test_exact_verse_is_matched` — a known āyah returns its (sūrah, āyah).
- `test_partial_quotation_is_matched` — a fragment of an āyah resolves.
- `test_diacritics_do_not_affect_matching` — the same āyah with and without
  tashkeel resolves identically.
- `test_wrong_citation_is_reported` — the page-5 case: the āyah
  `وَقُل رَّبِّ ارْحَمْهُمَا كَمَا رَبَّيَانِي صَغِيرًا` cited as `الإسراء: 65`
  produces a warning naming 24 as the reference answer.
- `test_non_quranic_text_in_ornate_parens_is_not_a_false_match` — arbitrary
  Arabic inside `﴿…﴾` reports "could not verify", not a wrong match.

In `tests/test_text.py`:

- `test_quranic_quotations_are_extracted` — including two quotations on one
  line, and text with no quotations returning `[]`.

Model the file structure on `tests/test_text.py`, which is plain functions with
no class wrappers and no fixtures.

## Done criteria

ALL must hold:

- [ ] Step 0 was run and its measured numbers are recorded in `plans/README.md`.
- [ ] `.venv/bin/python -m pytest -q` exits 0 with at least 121 tests passing.
- [ ] `.venv/bin/ruff check .` exits 0 with `All checks passed!`.
- [ ] The reference text's source and licence are recorded in `pdf2epub/quran.py`.
- [ ] `grep -rn "text = .*quran\|block.text =" pdf2epub/quran.py` returns no
      match — the checker never rewrites document text.
- [ ] A conversion of a book with no Qur'anic quotations produces a byte-identical
      EPUB to one produced before this change.
- [ ] `git status` shows no modified file outside the In scope list.
- [ ] `plans/README.md` status row for plan 09 updated.

## STOP conditions

Stop and report back instead of improvising if:

- Step 0's measured frequency is below the threshold. Mark the plan REJECTED.
- You cannot establish redistribution rights for a reference text.
- The excerpts in "Current state" do not match the live code.
- You find yourself needing to read `The_Arabic_OCR`'s source to proceed.
- Matching produces false positives on ordinary Arabic prose — that means the
  `﴿…﴾` anchor is unreliable in this corpus and the design needs rethinking.
- You are tempted to auto-correct a mismatched verse. That is out of scope and
  changes the project's contract from "flag for a human" to "silently edit".

## Maintenance notes

- The lossy normalisation in `normalize_for_match` is deliberately quarantined
  in `pdf2epub/quran.py`. If it ever leaks into `pdf2epub/text.py`'s
  `normalize_arabic`, it will silently destroy the hamza distinctions that the
  reversed-ligature detector depends on. A reviewer should check that boundary
  specifically.
- The reference data is the largest asset in the package. If image size becomes
  a concern, it is the first thing to make optional.
- Deliberately deferred: verifying hadith, and verifying that a quotation's
  *text* matches the reference rather than only locating it. The latter would
  let the converter say "this quotation differs from the reference by 3 words",
  which is a stronger signal — but it needs a diff-and-threshold design that
  Step 0's evidence does not yet justify.

## Rejected ideas from the same source project

Recorded so a future session does not re-litigate them:

- **Typography cleaning (`normalize_hamza`, `remove_all_tashkeel`,
  `remove_kasheeda`)** — REJECTED, and it would cause a regression. These are
  lossy: `normalize_hamza` maps `أ إ آ → ا`. This project's reversed-ligature
  detector (`REVERSED_LIGATURE_RE` in `pdf2epub/text.py`) distinguishes exactly
  those characters to find transposed lam-alef ligatures. Adopting hamza
  normalisation would disable it outright, and would collapse `الأول` and
  `الاول` in the reader's text. It also contradicts `normalize_arabic`'s stated
  contract: "preserve letters, digits, and diacritics".
- **`fix_punctuation` / `clean_double_spaces`** — already implemented, at
  `pdf2epub/text.py:162-165`.
- **Scan preprocessing (deskew, Otsu/Sauvola/Wolf binarisation, spread
  splitting)** — deferred, not rejected. Real capability, but no measured need:
  the scanned pages in the test corpus OCR'd cleanly without it. It would also
  require adding `numpy`, which is not currently a dependency (the project
  declares six). Revisit if a book arrives whose scans OCR badly.
- **Multi-column reading-order detection** — deferred. `_reading_order` at
  `pdf2epub/analyzer.py:126` is a crude two-column split, but no reading-order
  defect has been observed. Worth revisiting with a genuinely multi-column book.
- **Reusing any of their code** — blocked by the absent licence. See
  "Provenance and licensing".
