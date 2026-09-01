# Plan 12: Flag likely dropped characters when OCR replaces embedded text

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, add a status row for plan 12 to the
> table in `plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 7546ca9..HEAD -- pdf2epub/text.py pdf2epub/analyzer.py tests/test_text.py tests/test_pipeline_ocr.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `a3b97ca`, 2026-09-01; baseline re-stamped to `7546ca9` (133 tests), 2026-09-01

## Why this matters

When a page has both an embedded PDF text layer and an OCR read, the analyzer
picks one, and OCR often wins because the embedded layer has broken glyph
mapping. But OCR sometimes drops a single character and produces a *real word
with a different meaning* — on page 15 of a real converted book, the embedded
layer (score 0.650) contained `للتخندق` ("to entrenchment", correct) while the
chosen `ocr:ollama` candidate (score 1.000) read `للخندق` ("to the trench",
wrong). No quality score can see this: both are fluent Arabic. The existing
bulk-loss gate `ocr_covers_embedded` cannot catch it either — a one-character
drop is invisible to a length-ratio + 50%-word-overlap check, by design.

A measured cross-check over a real 20-page conversion found **32 such
candidates across 10 comparable pages (3.2 per page)** by asking, for each
embedded word absent from the OCR word set, whether the same word minus
exactly one character appears in the OCR set. True positives included
`للتخندق→للخندق`, `النصوص→الصوص` (texts→thieves), `التنوع→النوع`
(diversity→type), `يخاطب→يخطب` (addresses→preaches), `اعتراف→اعترا`
(truncated), `التطور→التطو` (truncated). **Roughly half were noise** —
tanween-alef orthographic variants such as `سلفا→سلف`, `بديال→بديل`,
`مترددا→متردد` — so precision is around 50%.

This plan surfaces those pairs as a per-page review hint. After it lands, a
human reviewing the quality report sees exactly which words to check and what
the embedded layer said, instead of silently shipping a corrupted word.

### Design constraints (must hold — do not "improve" on these)

1. **Review hint only, never an automatic correction.** At ~50% precision,
   auto-correcting would corrupt text. The page keeps whatever text was
   selected; the finding is only reported.
2. **Must not block publication.** Do not touch `ConversionIntegrityError`,
   `pipeline.py`, or the `integrity` section of the report. (Per-page
   `warnings` do not feed the `integrity` section — verified below.)
3. **Must not change candidate selection.** `ocr_covers_embedded`, its
   thresholds, and the `choose_ocr` expression stay byte-for-byte as they are.
   `ocr_covers_embedded` is a bulk-loss detector and that is its job.
4. **Do not "simplify" the check into a plain word diff.** A plain diff of the
   two word sets yields 30+ items per page, drowned in the embedded layer's
   own mid-word ligature damage (`الإسالم`, `والانغالق`). The
   exactly-one-character-drop constraint is what makes the list short (~3 per
   page) and reviewable.
5. **Implement exactly the measured rule.** The 32-candidate / ~50%-precision
   numbers above were measured for the plain "word minus exactly one
   character" rule. Do not add noise filters (e.g. ignoring dropped final
   alef) — that would invalidate the measured numbers. Precision improvements
   are deferred to Maintenance notes and require re-measurement first.

### Where the findings surface (decision made by this plan)

Two surfaces, neither requiring any schema or dataclass change:

- **Per-page `PageResult.warnings`** (the human surface): one warning string
  per affected page listing the pairs. This flows automatically into the
  report's `page_details[*].warnings` and puts the page number in the
  top-level `review_pages` list — which, for this scenario, it already is:
  when OCR replaces non-empty embedded text the page's `page_type` is already
  `BROKEN_TEXT` (`analyzer.py:664`), and `report()` already lists
  `BROKEN_TEXT` pages under `review_pages` (`models.py:148`). So the warning
  adds text, not new review pages.
- **A structured key inside the existing free-form per-candidate dict** (the
  machine surface): `candidates["ocr:<provider>"]["dropped_character_candidates"]`
  holding `[{"embedded": ..., "ocr": ...}, ...]`. `candidates` is
  `dict[str, dict[str, Any]]` (`models.py:126`), is serialized by `asdict` into
  `page_details`, and round-trips through checkpoints untouched
  (`checkpoints.py:150` uses `dict(value.get("candidates", {}))`), so no
  change to `models.py` or `checkpoints.py` is needed and resumed jobs with
  old checkpoints simply lack the key.

Rejected alternative: a new top-level report field aggregating pairs across
pages. Trade-off: an aggregate would let a reviewer see all pairs in one
place without walking `page_details`, but it means touching the report schema
(`schema_version: 2` is pinned by `tests/test_pipeline_ocr.py:79`), inventing
a document-level shape for per-page data, and duplicating information that
`review_pages` + `page_details` already locate. If an aggregate is wanted
later it can be derived from `page_details` without a converter change.

## Current state

All excerpts verified against `a3b97ca` (working tree clean, 132 tests
passing) on 2026-09-01.

Relevant files:

- `pdf2epub/text.py` — text-quality heuristics; home of `word_overlap`,
  `ocr_covers_embedded`, `normalize_arabic`, `WORD_RE`. The new detector goes
  here.
- `pdf2epub/analyzer.py` — `analyze_pdf` page loop; contains the `choose_ocr`
  decision and the per-page `warnings` list. The single new call site goes
  here.
- `tests/test_text.py` — unit tests for `pdf2epub/text.py`; plain test
  functions, no fixtures. New unit tests go here.
- `tests/test_pipeline_ocr.py` — end-to-end pipeline tests with fake OCR
  providers and a `_text_pdf` helper (line 356). One new end-to-end test goes
  here.

`pdf2epub/text.py:45-47` — thresholds the detector reuses (`MIN_COMPARABLE_TEXT`)
and must not change:

```python
MIN_COMPARABLE_TEXT = 200
MIN_LENGTH_RATIO = 0.6
MIN_WORD_OVERLAP = 0.5
```

`pdf2epub/text.py:50-56` — the word-set idiom to mirror:

```python
def word_overlap(left: str, right: str) -> float:
    """Share of the left text's words that also appear in the right text."""
    left_words = {w for w in WORD_RE.findall(normalize_arabic(left)) if len(w) > 1}
    if not left_words:
        return 1.0
    right_words = {w for w in WORD_RE.findall(normalize_arabic(right)) if len(w) > 1}
    return len(left_words & right_words) / len(left_words)
```

`pdf2epub/text.py:59-72` is `ocr_covers_embedded(embedded, ocr) -> bool` — do
not modify it. It is called once, at `pdf2epub/analyzer.py:639`:

```python
                    ocr_covers_page = ocr_covers_embedded(embedded, ocr_text)
                    choose_ocr = bool(ocr_blocks) and ocr_covers_page and (
```

`pdf2epub/analyzer.py:660-670` — the branch where OCR wins; the new call goes
at the end of this block (`warnings` is the per-page list defined at
`analyzer.py:574`; `embedded` is the embedded layer's text; `ocr_text` and
`candidates` are in scope here):

```python
                    if choose_ocr:
                        blocks = ocr_blocks
                        selected_quality = ocr_quality
                        source = f"ocr:{provider_name}"
                        kind = PageType.SCAN if not embedded else PageType.BROKEN_TEXT
                        selection = PageSelection.OCR
                        selection_reason = (
                            "OCR was the only usable candidate"
                            if not embedded
                            else "OCR passed quality checks that embedded text failed"
                        )
```

Because the call site sits inside `choose_ocr`, coverage has already passed
(`ocr_covers_page` is a conjunct of `choose_ocr`), so the detector never runs
against an OCR read that dropped half the page.

`pdf2epub/analyzer.py:21-28` — the import block to extend:

```python
from .text import (
    BROKEN_EXTRACTION_REASONS,
    HEADING_RE,
    SEVERE_TEXT_REASONS,
    evaluate_text,
    normalize_arabic,
    ocr_covers_embedded,
)
```

`pdf2epub/models.py:122` — `warnings: list[str]` on `PageResult`, already
surfaced: `report()` at `models.py:148` adds any page with `warnings` (or
`page_type == BROKEN_TEXT`) to the top-level `review_pages`, and
`page_details` carries the full `PageResult` via `asdict` (`models.py:184`).
The `integrity` section (`models.py:177-183`) is built **only** from
`PageSelection.UNAVAILABLE` / `PageSelection.REVIEW` selections — per-page
warnings never affect it, so this plan cannot block publication
(`pipeline.py:68-85` gates on `integrity` only).

Conventions:

- Warning strings are short English sentences, e.g.
  `"OCR dropped or replaced page content; retained embedded text"`
  (`analyzer.py:681-683`).
- Ruff (default 88-column config, `.venv/bin/ruff check .`) is clean at
  baseline; keep it clean.
- Tests are plain functions; unit tests in `tests/test_text.py` model the
  style of `test_ocr_that_drops_half_the_page_does_not_cover_it`
  (`tests/test_text.py:71`); the pipeline test models
  `test_defect_flagged_embedded_text_loses_a_tie_to_ocr`
  (`tests/test_pipeline_ocr.py:400`).

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests (full) | `.venv/bin/python -m pytest -q` | `140 passed` at the end (baseline before this plan: `133 passed`) |
| Tests (this plan only) | `.venv/bin/python -m pytest -q -k "dropped_character"` | `7 passed, 132 deselected` when done (`0` matched at baseline) |
| Lint | `.venv/bin/ruff check .` | `All checks passed!` |
| Branch check | `git branch --show-current` | `advisor/12-flag-dropped-characters-in-ocr` |

Warning: the console scripts in `.venv/bin/` have a stale shebang — pytest
MUST be invoked as `.venv/bin/python -m pytest`, never as `.venv/bin/pytest`.
The `ruff` binary works directly.

## Scope

**In scope** (the only files you may modify):

- `pdf2epub/text.py` — add one constant and one pure function; change nothing
  existing.
- `pdf2epub/analyzer.py` — extend one import list, add one guarded block
  inside the `if choose_ocr:` branch; change nothing else.
- `tests/test_text.py` — extend the top import, add one module constant and
  six test functions.
- `tests/test_pipeline_ocr.py` — add one provider class, two module constants,
  one test function.
- `plans/README.md` — status row for plan 12 only (final step).

**Out of scope** (do NOT touch, even though they look related):

- `pdf2epub/models.py` — no new dataclass fields; `warnings` and `candidates`
  already carry everything. No `schema_version` bump.
- `pdf2epub/pipeline.py` — the integrity gate must not learn about this
  warning; the hint must never block publication.
- `pdf2epub/checkpoints.py` — the chosen surfaces round-trip through the
  existing serialization untouched.
- `pdf2epub/web.py`, `pdf2epub/epub.py`, `pdf2epub/ocr.py`, `pdf2epub/cli.py`.
- The existing functions `ocr_covers_embedded`, `word_overlap`, and the
  constants `MIN_COMPARABLE_TEXT`, `MIN_LENGTH_RATIO`, `MIN_WORD_OVERLAP` —
  reuse, never edit.
- The `choose_ocr` expression and every `selection` / `selection_reason`
  assignment in `analyzer.py`.
- Any existing test.

## Git workflow

- Branch: `advisor/12-flag-dropped-characters-in-ocr` (created off current
  `main`).
- Commit per logical unit; message style is imperative and unprefixed, e.g.
  `Guard job state transitions and recover corrupt checkpoints` (see
  `git log --oneline -5`). Two commits are specified in the steps.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the branch

```
git checkout -b advisor/12-flag-dropped-characters-in-ocr
```

**Verify**: `git branch --show-current` →
`advisor/12-flag-dropped-characters-in-ocr`

### Step 2: Add the detector to `pdf2epub/text.py`

Insert the following immediately after the `ocr_covers_embedded` function
(i.e. after line 72, before `def evaluate_text`). Do not modify anything
already in the file.

```python
# A word must be at least this long before its one-character-shorter form is
# specific enough to mean anything: three-letter words shrink into two-letter
# particles that occur on every page. None of the 32 candidates measured on a
# real conversion involved an embedded word shorter than four characters.
MIN_DROPPED_CHAR_WORD_LENGTH = 4


def dropped_character_findings(embedded: str, ocr: str) -> list[tuple[str, str]]:
    """Embedded words that reappear in the OCR text minus exactly one character.

    OCR sometimes drops a single letter and produces a real word with a
    different meaning ("للتخندق" read as "للخندق"), which neither a quality
    score nor the bulk-loss gate `ocr_covers_embedded` can see. This
    cross-check is a review hint only: measured precision is roughly 50%
    (tanween-alef spelling variants such as "سلفا"/"سلف" look identical to
    real drops), so callers must report the pairs, never auto-correct with
    them. The exactly-one-character constraint is what keeps the list short
    (~3 per page on a measured conversion); a plain word diff drowns in the
    embedded layer's own ligature damage (30+ items per page).
    """
    if len(embedded.strip()) < MIN_COMPARABLE_TEXT:
        return []
    embedded_words = {
        word
        for word in WORD_RE.findall(normalize_arabic(embedded))
        if len(word) >= MIN_DROPPED_CHAR_WORD_LENGTH
    }
    ocr_words = {
        word for word in WORD_RE.findall(normalize_arabic(ocr)) if len(word) > 1
    }
    findings: list[tuple[str, str]] = []
    for word in sorted(embedded_words - ocr_words):
        for position in range(len(word)):
            shorter = word[:position] + word[position + 1 :]
            if shorter in ocr_words:
                findings.append((word, shorter))
                break
    return findings
```

Notes for the executor: `sorted()` makes output deterministic;
`break` records each embedded word at most once (leftmost dropped position);
the `MIN_COMPARABLE_TEXT` guard reuses the same "too little text to compare"
floor as `ocr_covers_embedded`.

**Verify** (all three):

1. `.venv/bin/ruff check .` → `All checks passed!`
2. `.venv/bin/python -m pytest -q` → `133 passed` (nothing existing broke)
3. Behavior probe:

```
.venv/bin/python -c "
from pdf2epub.text import dropped_character_findings
f = 'هذا نص عربي سليم لاختبار جودة استخراج الكلمات من ملف الكتاب المطبوع. ' * 3
print(dropped_character_findings(f + 'كان الانحياز للتخندق الفكري واضحا.', f + 'كان الانحياز للخندق الفكري واضحا.'))
"
```

→ `[('للتخندق', 'للخندق')]`

### Step 3: Unit tests in `tests/test_text.py`

Change line 1 of `tests/test_text.py` from:

```python
from pdf2epub.text import evaluate_text, normalize_arabic, wrap_ltr
```

to:

```python
from pdf2epub.text import (
    dropped_character_findings,
    evaluate_text,
    normalize_arabic,
    wrap_ltr,
)
```

Then append the following at the end of the file. The filler exists to clear
the 200-character `MIN_COMPARABLE_TEXT` floor (`len(_FILLER)` is 207).

```python
_FILLER = "هذا نص عربي سليم لاختبار جودة استخراج الكلمات من ملف الكتاب المطبوع. " * 3


def test_dropped_character_is_reported_with_both_forms():
    embedded = _FILLER + "كان الانحياز للتخندق الفكري واضحا."
    ocr = _FILLER + "كان الانحياز للخندق الفكري واضحا."
    assert dropped_character_findings(embedded, ocr) == [("للتخندق", "للخندق")]


def test_dropped_character_hint_reports_known_orthographic_variants():
    # "سلفا"/"سلف" is a tanween-alef spelling variant, not an OCR error.
    # Measured precision is ~50% precisely because such pairs are reported.
    # That is accepted behavior: this is a review hint, and any noise filter
    # must be re-measured on a real conversion before it is added. Do not
    # "fix" this test by filtering.
    embedded = _FILLER + "قلنا ذلك سلفا في الفصل الأول."
    ocr = _FILLER + "قلنا ذلك سلف في الفصل الأول."
    assert dropped_character_findings(embedded, ocr) == [("سلفا", "سلف")]


def test_dropped_character_check_skips_short_pages():
    # Below MIN_COMPARABLE_TEXT the comparison means nothing, same as the
    # coverage gate.
    assert dropped_character_findings("للتخندق", "للخندق") == []


def test_dropped_character_check_ignores_words_shorter_than_four_letters():
    embedded = _FILLER + "زرع فيه الأمل."
    ocr = _FILLER + "زع فيه الأمل."
    assert dropped_character_findings(embedded, ocr) == []


def test_dropped_character_check_requires_exactly_one_missing_character():
    # "اعتر" is "اعتراف" minus two characters; only exactly-one drops are
    # reported — this constraint is what keeps the list reviewable.
    embedded = _FILLER + "تحدث عن اعتراف الشهود."
    ocr = _FILLER + "تحدث عن اعتر الشهود."
    assert dropped_character_findings(embedded, ocr) == []


def test_dropped_character_check_is_silent_on_identical_texts():
    embedded = _FILLER + "كان الانحياز للتخندق الفكري واضحا."
    assert dropped_character_findings(embedded, embedded) == []
```

**Verify**:

1. `.venv/bin/python -m pytest -q -k "dropped_character"` →
   `6 passed, 132 deselected`
2. `.venv/bin/python -m pytest -q` → `139 passed`
3. `.venv/bin/ruff check .` → `All checks passed!`

Then commit:

```
git add pdf2epub/text.py tests/test_text.py
git commit -m "Add a dropped-character cross-check for OCR candidates"
```

### Step 4: Wire the detector into the analyzer

In `pdf2epub/analyzer.py`:

1. Extend the `from .text import (...)` block (lines 21-28, quoted in Current
   state) by adding `dropped_character_findings,` in alphabetical position —
   between `SEVERE_TEXT_REASONS,` and `evaluate_text,`.

2. Inside the `if choose_ocr:` branch (lines 660-670, quoted in Current
   state), append the following directly after the `selection_reason = (...)`
   assignment's closing parenthesis, at the same indentation as
   `selection_reason` (24 spaces; its body 28/32 — this code sits inside
   `elif use_ocr:` → `try:` → `if choose_ocr:`):

```python
                        if embedded:
                            dropped = dropped_character_findings(embedded, ocr_text)
                            if dropped:
                                # Review hint only (~50% precision): the pairs
                                # are reported, never applied to the text.
                                candidates[f"ocr:{provider_name}"][
                                    "dropped_character_candidates"
                                ] = [
                                    {"embedded": kept, "ocr": read}
                                    for kept, read in dropped
                                ]
                                shown = "; ".join(
                                    f"{kept}→{read}" for kept, read in dropped[:8]
                                )
                                more = (
                                    f"; and {len(dropped) - 8} more"
                                    if len(dropped) > 8
                                    else ""
                                )
                                warnings.append(
                                    "OCR may have dropped a character in "
                                    f"{len(dropped)} word(s) "
                                    f"(embedded→ocr): {shown}{more}"
                                )
```

Notes:

- The `if embedded:` guard matters — scan-only pages have no embedded layer
  to compare against.
- `dropped[:8]` caps the warning string; the full list is always in
  `dropped_character_candidates`. A measured page averages 3.2 candidates, so
  the cap only trims pathological pages.
- Do not compute findings outside this branch, and do not touch the `elif
  embedded:` (OCR rejected) branch — when embedded text is retained, the
  suspect OCR read was discarded and there is nothing to hint about.

**Verify**:

1. `.venv/bin/python -m pytest -q` → `139 passed` (the wiring must not break
   any existing test; the tie-break test at `tests/test_pipeline_ocr.py:400`
   exercises this exact branch with >200 chars of embedded text, but its
   "words" are single letters, below `MIN_DROPPED_CHAR_WORD_LENGTH`, so no
   warning appears)
2. `.venv/bin/ruff check .` → `All checks passed!`
3. `grep -n "dropped_character_findings" pdf2epub/analyzer.py` → exactly two
   matches: one in the import block, one call site inside `if choose_ocr:`

### Step 5: End-to-end test in `tests/test_pipeline_ocr.py`

The pipeline test uses Latin words deliberately: `pymupdf.insert_text` with
Arabic is not shaping-safe in this suite (every existing `_text_pdf` caller
uses Latin), and the detector is script-agnostic (`WORD_RE` matches any word
characters). Append at the end of the file:

```python
_DROP_FILLER_LINES = [
    "This paragraph exists only to push the embedded text layer of",
    "the page over the two hundred character comparability floor",
    "that guards the dropped character cross check from meaningless",
    "short pages of front matter or bare headings in the document.",
]
_DROP_EMBEDDED_LINES = _DROP_FILLER_LINES + [
    "Careful planning prevents failure during conversion runs.",
]


class DroppedCharacterOcrProvider:
    """Re-reads the page faithfully except one word loses one letter."""

    name = "dropper"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        text = "\n".join(_DROP_FILLER_LINES)
        text += "\nCareful planing prevents failure during conversion runs."
        return OcrResult(text, 0.95, self.name, model="drop-model")


def test_dropped_character_hint_does_not_block_publication(tmp_path):
    # OCR wins the page but read "planning" as "planing". The page must keep
    # the OCR text, carry a review warning with both forms, expose the pair
    # in the candidates dict, and still publish: integrity is untouched.
    source = tmp_path / "drop.pdf"
    _text_pdf(source, _DROP_EMBEDDED_LINES)

    outputs = convert(
        source,
        tmp_path / "output",
        ConversionOptions(
            mode="force_ocr", preserve_images=False, require_epubcheck=False
        ),
        ocr_provider=DroppedCharacterOcrProvider(),
    )

    report = json.loads(outputs["report"].read_text())
    detail = report["page_details"][0]
    assert detail["selection"] == "ocr"
    assert detail["candidates"]["ocr:dropper"]["dropped_character_candidates"] == [
        {"embedded": "planning", "ocr": "planing"}
    ]
    assert any(
        warning.startswith("OCR may have dropped a character")
        for warning in detail["warnings"]
    )
    assert report["integrity"]["status"] == "passed"
    assert outputs["epub"].exists()
```

(`OcrResult`, `ConversionOptions`, `convert`, `json`, and `_text_pdf` are all
already imported/defined in this file — add no imports.)

**Verify**:

1. `.venv/bin/python -m pytest -q -k "dropped_character"` →
   `7 passed, 132 deselected`
2. `.venv/bin/python -m pytest -q` → `140 passed`
3. `.venv/bin/ruff check .` → `All checks passed!`

Then commit:

```
git add pdf2epub/analyzer.py tests/test_pipeline_ocr.py
git commit -m "Flag likely dropped characters when OCR replaces embedded text"
```

### Step 6: Update the plan index

Add one row to the status table in `plans/README.md` (columns:
Plan | Title | Priority | Effort | Depends on | Status), e.g.:

```
| 12 | Flag likely dropped characters when OCR replaces embedded text | P2 | S | — | DONE — `advisor/12-flag-dropped-characters-in-ocr`; 139 pass |
```

**Verify**: `git status --porcelain` after committing the README change shows
no modified tracked files outside the five in-scope paths.

## Test plan

New tests (all names contain `dropped_character`, so the
`-k "dropped_character"` filter in the commands table collects exactly these
— verified against the baseline suite: the filter currently matches 0 of the
132 existing tests):

- `tests/test_text.py` (model after
  `test_ocr_that_drops_half_the_page_does_not_cover_it` in the same file):
  1. happy path — `للتخندق`/`للخندق` reported with both forms;
  2. known ~50%-precision noise (`سلفا`/`سلف`) is still reported, pinned
     deliberately so nobody silently adds an unmeasured filter;
  3. short pages (< 200 chars) produce no findings;
  4. words shorter than 4 characters are ignored;
  5. a two-character drop is not reported (exactly-one constraint);
  6. identical texts produce no findings.
- `tests/test_pipeline_ocr.py` (model after
  `test_defect_flagged_embedded_text_loses_a_tie_to_ocr`):
  7. end-to-end: OCR wins, the warning and the structured
     `dropped_character_candidates` both appear in the report, `integrity` is
     `passed`, and the EPUB is built — the regression this plan exists to
     prevent is the hint ever becoming a publication blocker or a text
     mutation.

Verification: `.venv/bin/python -m pytest -q` → `140 passed` (133 baseline +
7 new).

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `.venv/bin/ruff check .` exits 0 (`All checks passed!`)
- [ ] `.venv/bin/python -m pytest -q` ends with `140 passed`
- [ ] `.venv/bin/python -m pytest -q -k "dropped_character"` ends with
      `7 passed, 132 deselected`
- [ ] `grep -c "dropped_character_findings" pdf2epub/analyzer.py` → `2`
- [ ] `git diff main -- pdf2epub/models.py pdf2epub/pipeline.py pdf2epub/checkpoints.py pdf2epub/web.py` is empty (selection, integrity, schema, and checkpoints untouched)
- [ ] `git diff main -- pdf2epub/text.py | grep -E "^-" | grep -v "^---"` shows no removed lines (nothing existing in `text.py` was edited)
- [ ] `git status --porcelain` shows no modifications outside the five in-scope files
- [ ] `plans/README.md` has a status row for plan 12

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows changes to any in-scope file since `a3b97ca`, or any
  "Current state" excerpt does not match the live code (especially the
  `if choose_ocr:` block at `analyzer.py:660-670` — the 2026-08-31 audit
  flagged the selection policy as a candidate for extraction into a pure
  function; if that refactor has happened, this plan's call site is wrong).
- Any of the 133 baseline tests fails after Step 4. In particular, if
  `test_defect_flagged_embedded_text_loses_a_tie_to_ocr` or any integrity
  test fails, the wiring changed selection or gating behavior — that is
  forbidden, not fixable by editing the test.
- A step's verification fails twice after a reasonable fix attempt.
- Correct behavior appears to require touching `models.py`, `pipeline.py`,
  `checkpoints.py`, `web.py`, `ocr_covers_embedded`, or the `choose_ocr`
  expression.
- You find yourself adding a noise filter, an auto-correction, or replacing
  the exactly-one-character rule with a general diff — all three are
  explicitly rejected in "Design constraints".
- The Step 2 behavior probe prints anything other than
  `[('للتخندق', 'للخندق')]`.

## Maintenance notes

- **Precision improvements are deferred and gated on re-measurement.** Two
  candidate filters looked promising in the measured data but were not
  validated: (a) ignoring pairs where the dropped character is a final alef
  (would remove `سلفا→سلف` and `مترددا→متردد` but not the mid-word noise
  `بديال→بديل`), and (b) ignoring pairs whose shorter form also appears in
  the embedded text. Either changes the measured 32-candidate/~50%-precision
  numbers, so re-run the cross-check over a real conversion before adopting
  one, and update `test_dropped_character_hint_reports_known_orthographic_variants`
  in the same change.
- **If the selection policy is extracted from `analyze_pdf`** (direction
  finding in the 2026-08-31 audit), move this call along with the
  `choose_ocr` branch — it must stay conditioned on "OCR won over non-empty
  embedded text".
- **Audit finding #3** (the report embeds every candidate's full text)
  proposes slimming the `candidates` dict; whoever does that must preserve
  the `dropped_character_candidates` key — the end-to-end test here will
  catch its loss.
- **Reviewer scrutiny points**: `git diff` of `analyzer.py` must show only
  the import line and the new guarded block — no change to `choose_ocr`, any
  `selection`/`selection_reason`, or `ocr_covers_embedded`; the warning is
  appended only in the branch where OCR won; and no code path writes any
  finding back into page text.
- **Explicitly deferred**: any document-level aggregate report field (derive
  from `page_details` if wanted); running the check when embedded text is
  retained and OCR discarded (nothing actionable — the suspect read was not
  used); UI display of the pairs in `web.py`.
