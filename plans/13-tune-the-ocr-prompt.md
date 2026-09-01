# Plan 13: Measure, then tune, the vision-OCR prompt

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, add a status row for plan 13 to the
> table in `plans/README.md` — unless a reviewer dispatched you and told you
> they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 70d468b..HEAD -- pdf2epub/ocr.py pdf2epub/text.py pdf2epub/checkpoints.py tests/test_text.py tests/test_ocr.py tests/test_jobs.py .gitignore`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M (code) + operator hours (measurement)
- **Risk**: MED — a prompt change alters every future OCR read; mitigated by
  the measurement gate and the contract bump in Step 8
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `70d468b`, 2026-09-01

## Why this matters

Every page OCR'd through Ollama or an OpenAI-compatible vision API is shaped
by one 806-character system prompt, `OCR_PROMPT` (`pdf2epub/ocr.py:22`). A
real 20-page conversion measured three failure modes the current wording does
nothing about: OCR silently substituting a plausible neighbouring word for
the printed one (32 instances across 10 comparable pages, ~3.2/page —
`للتخندق→للخندق`, `النصوص→الصوص`), numerals disagreeing in *value* with the
embedded layer (page 18: embedded `73`, OCR `٩٣`), and the blanket "Page
numbers … omit" rule deleting the page numbers that ARE the content on a
table of contents.

But prompt tuning without measurement is guesswork: vision models are
non-deterministic even at `temperature: 0`, and a wording change that fixes
one page can quietly hurt ten others. This plan therefore builds the
measurement first — an operator-run benchmark harness with objective,
automatic per-page metrics and an explicit accept/reject rule — and only then
proposes wording changes, each landed only if the harness accepts it. After
this plan, "did the prompt change help?" has a number, not an opinion.

## Design decisions (made by this plan — do not revisit during execution)

### The prompt does NOT become a runtime setting

Rejected, for three reasons:

1. **It does not fit the settings machinery.** `OCR_PROMPT` is 806 characters
   (verify: `.venv/bin/python -c "from pdf2epub.ocr import OCR_PROMPT; print(len(OCR_PROMPT))"`
   → `806`); `_validate_value` (`pdf2epub/settings.py:111-114`) caps
   non-secret settings at 512 characters, and the whole settings surface
   (flat env-string key/values, single-line placeholders) is shaped for
   scalars, not a multiline document.
2. **It breaks resume correctness cheaply.** Settings are read live per call
   (`setting_value`), so a mid-job edit would mix two prompts inside one book
   with no record of which pages used which. The guard against that,
   `processing_contract` (`pdf2epub/checkpoints.py:82-101`), is recomputed at
   resume from *stored manifest values plus current code constants*
   (`pdf2epub/web.py:777-785`). A mutable global prompt would have to be
   stored per job to recompute the contract — a manifest schema change, a
   `CHECKPOINT_SCHEMA_VERSION` bump, and migration handling — all to save a
   process restart.
3. **The iteration-speed benefit is delivered better by the harness.** The
   benchmark script in this plan injects any prompt into a provider instance
   directly and *measures while it iterates*. A UI-editable prompt invites
   exactly the unmeasured drift this plan exists to prevent.

Deferred alternative, recorded for the future: if per-book prompts are ever
needed, add the prompt (or its hash) to `ConversionOptions` — options are
hashed into the contract automatically via `asdict(options)`
(`checkpoints.py:95`), so resume safety falls out for free. Not in scope now.

### One prompt, no contents-page variant

`_is_contents_page` (`pdf2epub/analyzer.py:161`) runs inside
`_classify_structure` (`analyzer.py:174`, called at `analyzer.py:764`) —
*after* OCR and candidate selection, so it cannot select a prompt today. The
ordering could change (a pre-OCR detector over the embedded blocks, which do
exist before OCR on text-layer pages), but: it is useless on pure scans —
exactly where OCR matters most; it doubles the measurement matrix (every
variant × two prompts); and a per-page prompt must be recorded per page for
reproducibility and for the contract. Instead, variant 03 rewords the single
prompt's page-number rule to distinguish page furniture from content
(measured by the numeral metric on contents pages). Revisit a variant prompt
only if variant 03 measurably fails.

### Invariants that must survive untouched

- OCR never silently replaces embedded text: `ocr_covers_embedded` and the
  `choose_ocr` expression (`analyzer.py:640-660`) are not modified.
- Findings are review hints, never corrections: `dropped_character_findings`
  and its analyzer wiring are not modified.
- Nothing new blocks publication: `pipeline.py`, `models.py`, and the
  `integrity` section are not touched.
- The pytest suite makes **zero network calls**. The benchmark is an
  operator-run script outside `tests/`; `pyproject.toml:35` pins
  `testpaths = ["tests"]`, so `benchmarks/` is never collected.

## Current state

All excerpts verified against `70d468b` (clean tree, 143 tests passing,
Ruff clean) on 2026-09-01.

Relevant files:

- `pdf2epub/ocr.py` — providers; `OCR_PROMPT` at line 22, sent at lines 327
  (Ollama) and 438 (OpenAI-compatible). `TesseractOcrProvider` (line 234) and
  `MistralDocumentOcrProvider` (line 467) do not use a prompt.
- `pdf2epub/text.py` — text heuristics; home of `word_overlap` (line 50),
  `ocr_covers_embedded` (line 70), `dropped_character_findings` (line 93),
  `normalize_arabic` (line 202), `MIN_COMPARABLE_TEXT = 200` (line 45). The
  new numeral metric goes here.
- `pdf2epub/checkpoints.py` — `OCR_CONTRACT_VERSION = "arabic-ocr-v1"` (line
  30), hashed into every job's contract at line 92.
- `tests/test_ocr.py` — provider tests with a fake `urlopen`; line 74 asserts
  `messages[0]["content"] == OCR_PROMPT`.
- `tests/test_jobs.py` — line 111 monkeypatches `OCR_CONTRACT_VERSION` to
  `"arabic-ocr-v2"` to prove contract sensitivity. **This collides with the
  Step 8 bump and must be updated there.**
- `tests/test_text.py` — unit tests, plain functions; `_FILLER` padding
  constant at line 117 pushes strings over the 200-char comparability floor.

`pdf2epub/ocr.py:22-31` — the prompt being tuned (verbatim):

```python
OCR_PROMPT = """You are an OCR engine for printed Arabic books. Transcribe the page image exactly as printed.
Rules:
- Output ONLY the page text. No preamble, no explanations, no translation, no commentary.
- Keep the text in Arabic, right-to-left, exactly as written. Preserve diacritics (تشكيل), punctuation, and Qur'anic/verse markers as printed.
- Preserve paragraph breaks: one blank line between paragraphs. Do not merge or split paragraphs.
- Mark headings by prefixing with "# " (chapter) or "## " (section).
- Footnotes: in the body put the marker as [^N]; list footnote texts at the end of the page as "[^N]: text".
- Poetry: put each hemistich pair on one line separated by " ... ".
- Page numbers, running headers/footers: omit.
- If part of the page is unreadable, write [غير مقروء] in its place; never guess."""
```

`pdf2epub/ocr.py:300-309` — the Ollama provider's fields (the prompt seam is
added at the end of this list):

```python
@dataclass(slots=True)
class OllamaVisionOcrProvider:
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = 300.0
    num_ctx: int = 8192
    fallback_model: str | None = None
    page_lengths: list[int] = field(default_factory=list)
    name: str = "ollama"
```

`pdf2epub/ocr.py:326-327` — the only Ollama reference (the 32B fallback goes
through the same `_request_model`, so one edit covers both attempts):

```python
                        "messages": [
                            {"role": "system", "content": OCR_PROMPT},
```

`pdf2epub/ocr.py:419-426` — the OpenAI-compatible provider's fields; its
single `OCR_PROMPT` reference is at line 438:

```python
@dataclass(slots=True)
class OpenAiCompatibleVisionOcrProvider:
    """Adapter for hosted services exposing an OpenAI-compatible vision API."""

    base_url: str
    model: str
    api_key: str
    timeout: float = 300.0
    name: str = "openai_compatible"
```

Both provider dataclasses are `slots=True` but **not frozen** — fields can be
assigned after construction, which the benchmark script relies on.

`pdf2epub/ocr.py:374-384` — `_fallback_reason`: any Latin letter in the
output triggers a re-run on the 32B model. Relevant twice: a prompt change
that induces Latin commentary doubles GPU cost, so the harness counts Latin
leakage; and the harness disables the fallback (`fallback_model = None`) so
one prompt is measured against one model.

`pdf2epub/analyzer.py:323-330` — how a page becomes an image (the harness
renders identically):

```python
def _ocr_page(
    page, dpi: int, language: str, provider: OcrProvider
) -> tuple[list[Block], float | None, str, dict]:
    scale = dpi / 72
    pix = page.get_pixmap(
        matrix=_fitz().Matrix(scale, scale), colorspace=_fitz().csRGB, alpha=False
    )
    result = provider.recognize(pix.tobytes("png"), language)
```

Measured failure data this plan is built on (from a real 20-page conversion;
recorded in `plans/README.md` under "Plan 12"):

- 32 dropped-character findings across 10 comparable pages (~3.2/page);
  `dropped_character_findings` detects this class at ~50% precision.
- Page 18: embedded `73` vs OCR `٩٣` — different values, not digit systems.
- Contents pages lose their destination numbers to the "omit" rule.

Conventions:

- Ruff (default config, 88 columns): `.venv/bin/ruff check .` is clean at
  baseline; keep it clean — the new `benchmarks/` directory is checked too.
- Tests are plain functions; model numeral tests on
  `test_dropped_character_is_reported_with_both_forms`
  (`tests/test_text.py:120`) and prompt tests on
  `test_ollama_sends_native_vision_request` (`tests/test_ocr.py:44`) and
  `test_openai_compatible_sends_data_url` (`tests/test_ocr.py:168`).
- Commit messages: imperative and unprefixed, e.g.
  `Stop dot leaders reading as dropped OCR content`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests (full) | `.venv/bin/python -m pytest -q` | `151 passed` when done (baseline: `143 passed`) |
| Tests (numeral metric) | `.venv/bin/python -m pytest -q -k "numeral"` | `6 passed, 143 deselected` after Step 3 (`0` matched at baseline) |
| Tests (prompt seam) | `.venv/bin/python -m pytest -q -k "prompt_override"` | `2 passed, 149 deselected` after Step 4 (`0` matched at baseline) |
| Lint | `.venv/bin/ruff check .` | `All checks passed!` |
| Harness selftest | `.venv/bin/python benchmarks/prompt_bench.py selftest` | last line `selftest: PASS`, exit 0 |
| Branch check | `git branch --show-current` | `advisor/13-tune-the-ocr-prompt` |

Warnings, both verified:

- The console scripts in `.venv/bin/` have a stale shebang — pytest MUST be
  invoked as `.venv/bin/python -m pytest`, never as `.venv/bin/pytest`. The
  `ruff` binary works directly.
- `pdf2epub` only imports with the repo root as the working directory. Run
  every command (including the benchmark) from the repo root; the benchmark
  script also inserts its own repo root into `sys.path` (Step 5) so it works
  when invoked by absolute path.

## Scope

**In scope** (the only files you may modify or create):

- `benchmarks/prompt_bench.py` (create) — the operator-run harness.
- `benchmarks/prompts/00-baseline.txt` (create) — byte-copy of `OCR_PROMPT`.
- `benchmarks/prompts/01-letter-fidelity.txt` (create)
- `benchmarks/prompts/02-numerals.txt` (create)
- `benchmarks/prompts/03-toc-page-numbers.txt` (create)
- `.gitignore` — one added line: `benchmarks/results/`.
- `pdf2epub/text.py` — add `NUMERAL_RE`, `_WESTERN_DIGITS`,
  `numeral_mismatches`; change nothing existing.
- `pdf2epub/ocr.py` — add a `prompt` field to the two vision providers and
  reference it; Step 8 only: the accepted `OCR_PROMPT` wording.
- `tests/test_text.py` — extend the import, add six test functions.
- `tests/test_ocr.py` — add two test functions.
- `pdf2epub/checkpoints.py` — Step 8 only: bump `OCR_CONTRACT_VERSION`.
- `tests/test_jobs.py` — Step 8 only: the sentinel at line 111.
- `plans/README.md` — status row for plan 13 only (final step).

**Out of scope** (do NOT touch, even though they look related):

- `pdf2epub/analyzer.py` — selection logic, coverage gate, contents-page
  detection, and the dropped-character wiring all stay as they are. Wiring
  `numeral_mismatches` into the analyzer as a page warning is explicitly
  deferred (Maintenance notes).
- `pdf2epub/settings.py` and `pdf2epub/web.py` — no prompt setting, no UI
  field, no new endpoint (decision above).
- `pdf2epub/pipeline.py`, `pdf2epub/models.py`, `pdf2epub/epub.py`,
  `pdf2epub/cli.py`.
- `TesseractOcrProvider` and `MistralDocumentOcrProvider` — they take no
  prompt; do not invent one.
- The existing functions `ocr_covers_embedded`, `word_overlap`,
  `dropped_character_findings` and the constants `MIN_COMPARABLE_TEXT`,
  `MIN_LENGTH_RATIO`, `MIN_WORD_OVERLAP` — reuse, never edit.
- Any existing test (except the Step 8 sentinel edit in `tests/test_jobs.py`,
  which is specified exactly).
- No new pytest test may open a socket. Provider tests use the existing
  `monkeypatch` fake-`urlopen` pattern.

## Git workflow

- Branch: `advisor/13-tune-the-ocr-prompt` (created off current `main`).
- Commit per step; message style imperative and unprefixed (see
  `git log --oneline -5`). Suggested messages are given in the steps.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the branch

```
git checkout -b advisor/13-tune-the-ocr-prompt
```

**Verify**: `git branch --show-current` → `advisor/13-tune-the-ocr-prompt`

### Step 2: Add the numeral metric to `pdf2epub/text.py`

Insert the following immediately after `dropped_character_findings` (i.e.
after line 123, before `def evaluate_text`). Do not modify anything already
in the file. `Counter` must be imported at the top of the file
(`from collections import Counter`) alongside the existing imports.

```python
# Digits in either direction: Western, Arabic-Indic, and Eastern
# Arabic-Indic (Persian) forms all appear in printed Arabic books.
NUMERAL_RE = re.compile(r"[0-9٠-٩۰-۹]+")
_WESTERN_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩"
    "۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def numeral_mismatches(embedded: str, ocr: str) -> tuple[list[str], list[str]]:
    """Numbers whose *value* differs between the embedded layer and OCR.

    Both texts' digit runs are mapped to Western digits before comparison, so
    `٩٣` and `93` agree while `٩٣` and `73` do not — a measured failure on a
    real conversion (page 18: embedded `73`, OCR read `٩٣`). Returns
    (numbers only the embedded layer has, numbers only the OCR text has) as
    sorted multisets. Like `dropped_character_findings`, this is a review
    signal, not a correction: either side can be the wrong one.
    """
    if len(embedded.strip()) < MIN_COMPARABLE_TEXT:
        return ([], [])

    def tokens(text: str) -> Counter[str]:
        return Counter(
            match.translate(_WESTERN_DIGITS)
            for match in NUMERAL_RE.findall(normalize_arabic(text))
        )

    left, right = tokens(embedded), tokens(ocr)
    return (sorted((left - right).elements()), sorted((right - left).elements()))
```

This exact implementation was probed against the real package before this
plan was written; the expected outputs in Step 3 are its real outputs.

**Verify**: `.venv/bin/python -c "from pdf2epub.text import numeral_mismatches; print(numeral_mismatches('x'*200 + ' صفحة 73', 'صفحة ٩٣'))"`
→ `(['73'], ['93'])`

**Verify**: `.venv/bin/ruff check .` → `All checks passed!`

### Step 3: Unit-test the numeral metric in `tests/test_text.py`

Add `numeral_mismatches` to the `from pdf2epub.text import (...)` block at
the top. Reuse the existing `_FILLER` constant (line 117). Add these six
tests (names must contain `numeral` so the `-k` filter matches); every
expected value below was executed against the Step 2 implementation:

```python
def test_numeral_mismatch_reports_different_values():
    # The measured page-18 failure: embedded 73, OCR read ٩٣.
    embedded = _FILLER + "انظر صفحة 73 وطبعة عام 1985."
    ocr = _FILLER + "انظر صفحة ٩٣ وطبعة عام ١٩٨٥."
    assert numeral_mismatches(embedded, ocr) == (["73"], ["93"])


def test_numeral_mismatch_ignores_digit_system_differences():
    embedded = _FILLER + "في عام ١٤٠٥ هجرية."
    ocr = _FILLER + "في عام 1405 هجرية."
    assert numeral_mismatches(embedded, ocr) == ([], [])


def test_numeral_mismatch_identical_numbers_cancel():
    embedded = _FILLER + "الباب 3 والفصل 12."
    ocr = _FILLER + "الباب 3 والفصل 12."
    assert numeral_mismatches(embedded, ocr) == ([], [])


def test_numeral_mismatch_reports_omitted_contents_numbers():
    # A contents entry whose destination number OCR omitted.
    embedded = _FILLER + "المقدمة 9 الفصل الأول 15"
    ocr = _FILLER + "المقدمة 9 الفصل الأول"
    assert numeral_mismatches(embedded, ocr) == (["15"], [])


def test_numeral_mismatch_requires_comparable_text():
    assert numeral_mismatches("صفحة 73", "صفحة 93") == ([], [])


def test_numeral_mismatch_normalizes_persian_digits():
    embedded = _FILLER + "طبع سنة ۱۳۶۲ شمسية."
    ocr = _FILLER + "طبع سنة 1362 شمسية."
    assert numeral_mismatches(embedded, ocr) == ([], [])
```

**Verify**: `.venv/bin/python -m pytest -q -k "numeral"` →
`6 passed, 143 deselected`

**Verify**: `.venv/bin/python -m pytest -q` → `149 passed`

Commit: `Add a numeral-value mismatch metric to the text heuristics`

### Step 4: Add the prompt seam to the two vision providers

In `pdf2epub/ocr.py`:

1. Add `prompt: str = OCR_PROMPT` as the **last** field of
   `OllamaVisionOcrProvider` (after `name: str = "ollama"`, line 309).
2. Add `prompt: str = OCR_PROMPT` as the **last** field of
   `OpenAiCompatibleVisionOcrProvider` (after
   `name: str = "openai_compatible"`, line 426).
3. Change line 327 from `{"role": "system", "content": OCR_PROMPT},` to
   `{"role": "system", "content": self.prompt},`.
4. Change line 438 the same way.

Nothing else changes. The default binds to the module constant, so app
behavior is byte-identical — the existing test at `tests/test_ocr.py:74`
(`messages[0] == {"role": "system", "content": OCR_PROMPT}`) must still pass
untouched, which is itself the guard that the default did not drift.

Add two tests to `tests/test_ocr.py` (names must contain `prompt_override`;
model on `test_ollama_sends_native_vision_request` line 44 and
`test_openai_compatible_sends_data_url` line 168, reusing `FakeResponse` and
`image_bytes`):

```python
def test_ollama_prompt_override_is_sent(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data)
        return FakeResponse({"message": {"content": "نص"}})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OllamaVisionOcrProvider(
        "http://localhost:11434", OLLAMA_QUALITY_MODEL, prompt="PROBE PROMPT"
    )
    provider.recognize(image_bytes((100, 100)), "ara")
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": "PROBE PROMPT",
    }


def test_openai_prompt_override_is_sent(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data)
        return FakeResponse({"choices": [{"message": {"content": "نص"}}]})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OpenAiCompatibleVisionOcrProvider(
        "https://vision.example/v1", "vision-model", "secret", prompt="PROBE PROMPT"
    )
    provider.recognize(image_bytes((100, 100)), "ara")
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": "PROBE PROMPT",
    }
```

**Verify**: `.venv/bin/python -m pytest -q -k "prompt_override"` →
`2 passed, 149 deselected`

**Verify**: `.venv/bin/python -m pytest -q` → `151 passed`
(no existing test changed — confirm with `git diff --stat tests/test_ocr.py`
showing only additions)

Commit: `Let the vision providers accept a per-instance OCR prompt`

### Step 5: Build the benchmark harness

Create `benchmarks/prompt_bench.py`, an operator-run CLI. Append one line,
`benchmarks/results/`, to `.gitignore`. The script must never be importable
by pytest collection (`pyproject.toml:35` pins `testpaths = ["tests"]` — do
not change that).

Script skeleton and hard requirements:

```python
"""Benchmark OCR prompt variants against a corpus of PDFs with embedded text.

Operator-run; never invoked by pytest. Usage (from the repo root):

    .venv/bin/python benchmarks/prompt_bench.py index --corpus ~/pdf2epub-bench-corpus
    .venv/bin/python benchmarks/prompt_bench.py run --corpus ~/pdf2epub-bench-corpus \
        --prompt benchmarks/prompts/00-baseline.txt --runs 3 \
        --out benchmarks/results/baseline
    .venv/bin/python benchmarks/prompt_bench.py compare \
        benchmarks/results/baseline benchmarks/results/v01 --primary dropped
    .venv/bin/python benchmarks/prompt_bench.py selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

then import from `pdf2epub.text`: `MIN_COMPARABLE_TEXT`,
`dropped_character_findings`, `evaluate_text`, `normalize_arabic`,
`numeral_mismatches`, `ocr_covers_embedded`, `word_overlap`; and from
`pdf2epub.ocr`: `OCR_PROMPT`, `OcrSettings`, `create_ocr_provider`.

Four subcommands (use `argparse` with subparsers):

**`index --corpus DIR`** — writes `DIR/corpus-manifest.json`. For each
`*.pdf` in DIR (sorted): sha256 of the file, page count, and the list of
*comparable* pages — pages whose embedded text (`page.get_text("text")`,
then `normalize_arabic`) has length `>= MIN_COMPARABLE_TEXT` **and**
`evaluate_text(embedded).score >= 0.48`. Store per-file
`{"sha256": ..., "pages": N, "comparable": [1-based page numbers]}` plus a
top-level `"total_comparable"` count. Print a summary line per file and, if
`total_comparable < 40` or fewer than 2 files, print
`WARNING: corpus is underpowered (see plan 13); results are directional only.`
Exit 2 (refuse) only when there are zero PDFs.

**`run --corpus DIR --prompt FILE --runs N --out DIR [--provider ollama] [--dpi 300] [--model NAME]`** —
requires `corpus-manifest.json` to exist and every file hash to still match
(refuse with exit 2 otherwise: measurements across a mutated corpus are not
comparable). Prompt text is `FILE.read_text(encoding="utf-8").rstrip("\n")`.
If the file's basename is `00-baseline.txt` and the text differs from
`pdf2epub.ocr.OCR_PROMPT`, refuse with exit 2 — a stale baseline file
invalidates every comparison. Build the provider with
`create_ocr_provider(args.provider, OcrSettings.from_env())` (apply
`--model` by constructing `OcrSettings.from_env()` and overriding
`ollama_model` before the call), then set `provider.prompt = prompt_text`
and, when the provider has a `fallback_model` attribute, set it to `None` so
exactly one model answers for the prompt (the Latin-letter fallback at
`pdf2epub/ocr.py:379` would otherwise silently swap models mid-benchmark).
Refuse providers without a `prompt` attribute (tesseract, mistral). For each
run 1..N, for each comparable page: render exactly as
`analyzer._ocr_page` does (`Matrix(dpi/72, dpi/72)`, `csRGB`, `alpha=False`,
`pix.tobytes("png")`), call `provider.recognize(png, "ara+eng")`, and record
per page:

- `dropped`: `len(dropped_character_findings(embedded, ocr))` and the pairs
- `overlap`: `word_overlap(embedded, ocr)`
- `numerals_missing` / `numerals_added`: from `numeral_mismatches`
- `covers`: `ocr_covers_embedded(embedded, ocr)`
- `score`: `evaluate_text(ocr).score`
- `latin`: `bool(re.search(r"[A-Za-z]", ocr))`
- `ocr_text` itself (so metrics can be recomputed and humans can diff without
  re-running OCR)

Write one JSON per run to `OUT/run-NN.json` with header fields
`prompt_file`, `prompt_sha256`, `provider`, `model`, `dpi`,
`corpus_manifest_sha256`, `started`, and print a one-line per-run summary.
Crash-tolerance: write each run file atomically (temp file + `os.replace`)
when the run completes; a crashed run leaves no partial file.

**`compare BASELINE_DIR VARIANT_DIR --primary dropped|numerals`** — loads all
`run-*.json` from both dirs; refuses (exit 2) if the two sides disagree on
`corpus_manifest_sha256`, provider, model, or dpi, or if either side has
fewer than 2 runs. Per run compute: `total_dropped` (sum over pages),
`total_numerals` (sum of `len(missing) + len(added)`), `mean_overlap`,
`mean_score`, `pages_not_covered`, `pages_latin`. Then apply this decision
rule (implement it as a pure function, exactly this logic):

```python
def decide(baseline: list[RunSummary], variant: list[RunSummary], primary: str) -> dict[str, bool]:
    def totals(runs, key):
        return [getattr(run, key) for run in runs]

    key = "total_dropped" if primary == "dropped" else "total_numerals"
    b, v = totals(baseline, key), totals(variant, key)
    rules = {
        # Non-deterministic runs: require the variant's WORST run to beat the
        # baseline's BEST run, so noise alone cannot produce an accept.
        "primary_disjoint": max(v) < min(b),
        # ...and the improvement must be material, not marginal.
        "primary_magnitude": mean(v) <= 0.8 * mean(b),
    }
    guards = {
        "total_dropped": "lower",
        "total_numerals": "lower",
        "pages_not_covered": "lower",
        "pages_latin": "lower",
        "mean_overlap": "higher",
        "mean_score": "higher",
    }
    del guards[key]
    for name, direction in guards.items():
        b_vals, v_vals = totals(baseline, name), totals(variant, name)
        spread = max(b_vals) - min(b_vals)  # the baseline's own run-to-run noise
        if direction == "lower":
            rules[f"guard_{name}"] = mean(v_vals) <= max(b_vals) + spread
        else:
            rules[f"guard_{name}"] = mean(v_vals) >= min(b_vals) - max(spread, 0.01)
    return rules
```

Print each rule with its numbers and PASS/FAIL, then a final line
`verdict: ACCEPT` iff all rules pass, else `verdict: REJECT`. Also print the
per-page pairs the variant *gained* and *lost* relative to the baseline's
first run — that list is the input to the human spot check in Step 7. Exit 0
on ACCEPT, 1 on REJECT (so shell scripts can branch), 2 on refusal.

**`selftest`** — end-to-end plumbing check with **no network and no corpus**:
in a temp dir, build a 3-page PDF with `pymupdf` (`page.insert_text` with
English filler lines of the style of `tests/test_pipeline_ocr.py:468-475` —
ASCII text round-trips reliably through `insert_text`/`get_text`, Arabic
through the default font does not, and the selftest exercises plumbing, not
Arabic fidelity; include digits in the text). Use a fake in-process provider
object (a tiny class with `name`, `prompt`, and a `recognize` returning the
page's own embedded text with one letter dropped from one word and one
numeral altered — deterministic). Run `index`, two `run`s with 2 runs each
(baseline = faithful echo provider, "variant" = the mutilating provider),
then `compare --primary dropped` and assert it prints `verdict: REJECT`
(the mutilator is worse), and compare in the other direction asserts
`verdict: ACCEPT`. Print `selftest: PASS` and exit 0 only if every assertion
held.

Create `benchmarks/prompts/00-baseline.txt` containing the current
`OCR_PROMPT` **byte-for-byte** (a single trailing newline from the editor is
tolerated by the `rstrip("\n")` read). Generate it, don't type it:

```
.venv/bin/python -c "from pdf2epub.ocr import OCR_PROMPT; open('benchmarks/prompts/00-baseline.txt','w',encoding='utf-8').write(OCR_PROMPT + '\n')"
```

**Verify**: `.venv/bin/python benchmarks/prompt_bench.py --help` → usage text
listing `index`, `run`, `compare`, `selftest`; exit 0

**Verify**: `.venv/bin/python benchmarks/prompt_bench.py selftest` → last
line `selftest: PASS`; exit 0

**Verify**: `.venv/bin/python -c "from pathlib import Path; from pdf2epub.ocr import OCR_PROMPT; assert Path('benchmarks/prompts/00-baseline.txt').read_text(encoding='utf-8').rstrip('\n') == OCR_PROMPT; print('baseline ok')"`
→ `baseline ok`

**Verify**: `.venv/bin/ruff check .` → `All checks passed!`

**Verify**: `.venv/bin/python -m pytest -q` → `151 passed` (proves pytest
does not collect `benchmarks/`)

Commit: `Add an operator-run benchmark harness for OCR prompt variants`

### Step 6: Write the three prompt variants

Each variant changes exactly one thing relative to the baseline
(single-variable experiments — a combined prompt is only assembled in Step 7
from individually accepted changes). Create these files with exactly the
baseline text plus the stated edit:

`benchmarks/prompts/01-letter-fidelity.txt` — targets the dropped-character
substitutions. Insert this rule immediately after the
"Keep the text in Arabic…" line:

```
- Copy every word letter-for-letter as printed. Never replace a printed word with a more common or similar-looking word; if a word looks unusual or misspelled, transcribe it exactly as it appears anyway.
```

`benchmarks/prompts/02-numerals.txt` — targets numeral value errors. Insert
this rule immediately after the "Keep the text in Arabic…" line:

```
- Transcribe every number exactly as printed: the same digits, in the same digit system (Arabic-Indic ٠١٢٣٤٥٦٧٨٩ or Western 0123456789), with the same value. Never convert, translate, or correct a number.
```

`benchmarks/prompts/03-toc-page-numbers.txt` — targets contents pages.
Replace the line `- Page numbers, running headers/footers: omit.` with:

```
- Omit running headers, running footers, and the isolated page number of the page itself. Keep page numbers that are part of the content, such as the destination numbers listed in a table of contents or an index.
```

**Verify**: `ls benchmarks/prompts/` →
`00-baseline.txt  01-letter-fidelity.txt  02-numerals.txt  03-toc-page-numbers.txt`

**Verify** (each variant differs from baseline by exactly the intended
lines): `diff benchmarks/prompts/00-baseline.txt benchmarks/prompts/01-letter-fidelity.txt | grep -c "^>"` → `1`;
same for `02` → `1`; for `03`: `diff ... | grep -c "^[<>]"` → `2`

Commit: `Add single-variable OCR prompt variants for benchmarking`

### Step 7: Measure (operator-run; cannot be verified automatically)

**This step is honest about its nature: it needs a real corpus, a live
Ollama endpoint, and roughly 1–3 hours of GPU time per prompt comparison
(≈40 pages × 3 runs × 5–20 s/page per side). No CI command can stand in for
it.** What the executor does here is (a) confirm the corpus gate, and (b) if
it passes, hand the operator the exact commands and the acceptance procedure
below; the executor may drive the runs if the operator says so.

**Corpus gate.** The plan does NOT assume a corpus exists — this project's
test PDFs have been deleted before (see `plans/README.md`, "Plan 09 Step 0
— BLOCKED"). Requirements: a directory (recommended:
`~/pdf2epub-bench-corpus/`, outside the repo so `git clean` can never eat
it) holding **at least 2 different Arabic books' PDFs with embedded text
layers, totalling at least 40 comparable pages** as counted by
`index`. Scanned-only PDFs contribute nothing here (no reference layer) —
they are measured only indirectly, via the guards on pages the corpus does
cover. If no such PDFs are available: STOP after Step 6, commit what exists,
and record the plan as
`DONE (harness landed); measurement BLOCKED — no corpus`, exactly as plan 09
did. Do not fabricate a corpus and do not proceed to Step 8.

**Procedure** (all from the repo root, Ollama reachable, settings via env):

```
.venv/bin/python benchmarks/prompt_bench.py index --corpus ~/pdf2epub-bench-corpus
.venv/bin/python benchmarks/prompt_bench.py run --corpus ~/pdf2epub-bench-corpus \
    --prompt benchmarks/prompts/00-baseline.txt --runs 3 --out benchmarks/results/baseline
.venv/bin/python benchmarks/prompt_bench.py run --corpus ~/pdf2epub-bench-corpus \
    --prompt benchmarks/prompts/01-letter-fidelity.txt --runs 3 --out benchmarks/results/v01
.venv/bin/python benchmarks/prompt_bench.py compare benchmarks/results/baseline benchmarks/results/v01 --primary dropped
```

then variants 02 and 03, each with `--primary numerals`.

**What counts as real improvement versus noise** (this is the decision rule
from Step 5, restated as policy): the baseline's three runs define the noise
band for every metric. A variant is accepted only when its *worst* run beats
the baseline's *best* run on the primary metric AND its mean is at least 20%
better AND no guard metric degrades beyond the baseline's own run-to-run
spread. With ~40 pages at the measured ~3.2 findings/page, the baseline
carries ~128 dropped findings, so the 20% bar is ~26 findings — well above
observed single-run jitter at `temperature: 0`, which is set for both
providers (`ocr.py` options) but does not make GPU inference reproducible.
If the baseline's own runs are so spread out that `primary_disjoint` can
never fire, the corpus is underpowered: add runs (`--runs 5`) or pages, and
say so in the report rather than lowering the bar.

**Human judgment component** (unautomatable, and stated as such): the
`compare` output lists the dropped-character pairs gained and lost. Before
accepting a variant, a human who reads Arabic must spot-check ~10 changed
pages' stored `ocr_text` against the page images and confirm the direction
of change is real — the metric is ~50%-precision against an imperfect
embedded layer, and both prompts are scored against the same layer, so the
comparison is fair, but only a human can confirm the winning side reads
*better* rather than merely *closer to a flawed reference*. Record their
verdict in the run report.

If ACCEPT: individually accepted edits are assembled into one combined
prompt file (`benchmarks/prompts/04-combined.txt`), which must itself be
benchmarked against the baseline and accepted before Step 8 — single
variables do not compose for free.

**Verify**: not automatable — the gate is the `compare` verdict lines plus
the recorded human spot-check. Attach the `compare` outputs to the report.

### Step 8: Land the accepted prompt (conditional — only on a Step 7 ACCEPT)

Skip this step entirely (and say so in the report) if Step 7 was blocked or
every variant was rejected. Otherwise:

1. In `pdf2epub/ocr.py`, replace the `OCR_PROMPT` text (lines 22-31) with
   the accepted combined prompt, byte-identical to the accepted
   `benchmarks/prompts/04-combined.txt` (or the single accepted variant
   file).
2. In `pdf2epub/checkpoints.py` line 30, bump
   `OCR_CONTRACT_VERSION = "arabic-ocr-v1"` → `"arabic-ocr-v2"`. This is the
   resume-correctness answer: the contract is recomputed at resume from the
   *current* constant (`web.py:777-785`), so every job checkpointed under
   the old prompt now resumes into `contract_mismatch` / blocked instead of
   silently mixing two prompts inside one book. That is the designed
   behavior — a paused job predating the prompt change must be restarted,
   not resumed.
3. In `tests/test_jobs.py` line 111, the monkeypatch
   `monkeypatch.setattr(checkpoints, "OCR_CONTRACT_VERSION", "arabic-ocr-v2")`
   would now patch the version to its own real value and the inequality
   assertion below it would fail. Change the sentinel to a value that can
   never be the live version: `"arabic-ocr-test-sentinel"`. Touch nothing
   else in that test.
4. Regenerate `benchmarks/prompts/00-baseline.txt` from the new constant
   (same one-liner as Step 5) so the next tuning round starts from the
   truth.

**Verify**: `.venv/bin/python -m pytest -q` → `151 passed`

**Verify**: `command grep -n "arabic-ocr-v2" pdf2epub/checkpoints.py` → line 30 only

**Verify**: the Step 5 baseline-file check one-liner → `baseline ok`

**Verify**: `.venv/bin/ruff check .` → `All checks passed!`

Commit: `Tune the vision OCR prompt and bump the processing contract`

### Step 9: Update the plans index

Add a row for plan 13 to the table in `plans/README.md` with the honest
outcome: `DONE` only if Steps 1-6 landed **and** Step 7 ran to a verdict;
`DONE (harness); measurement BLOCKED — no corpus` if Step 7's gate failed;
mention explicitly whether Step 8 executed.

**Verify**: `command grep -n "| 13 |" plans/README.md` → one row

## Test plan

- `tests/test_text.py`: the six `numeral` tests of Step 3 — the measured
  page-18 value error, digit-system equivalence, cancellation, contents-page
  omission, the comparability floor, Persian digits. Model after
  `test_dropped_character_is_reported_with_both_forms` (line 120).
- `tests/test_ocr.py`: the two `prompt_override` tests of Step 4, proving
  the seam carries an arbitrary prompt to the wire for both vision
  providers. The untouched existing test at line 44/74 proves the default is
  still `OCR_PROMPT`. Model after the fake-`urlopen` tests at lines 44 and
  168. No network in any test.
- The harness itself is guarded by its offline `selftest` (Step 5), which
  exercises index → run → compare → decision rule in both directions with a
  deterministic fake provider.
- Verification: `.venv/bin/python -m pytest -q` → `151 passed` (baseline
  143 + 8 new).

## Done criteria

Machine-checkable. ALL must hold (items 8-10 only when Step 8 executed):

- [ ] `.venv/bin/python -m pytest -q` → `151 passed`
- [ ] `.venv/bin/python -m pytest -q -k "numeral"` → `6 passed`
- [ ] `.venv/bin/python -m pytest -q -k "prompt_override"` → `2 passed`
- [ ] `.venv/bin/ruff check .` → `All checks passed!`
- [ ] `.venv/bin/python benchmarks/prompt_bench.py selftest` → `selftest: PASS`, exit 0
- [ ] `command grep -c "self.prompt" pdf2epub/ocr.py` → `2`, and
      `command grep -n '"content": OCR_PROMPT' pdf2epub/ocr.py` → no matches
- [ ] Baseline prompt file matches the constant (Step 5 one-liner → `baseline ok`)
- [ ] `git status --porcelain` shows no modified files outside the Scope list
- [ ] (Step 8 only) `command grep -n "arabic-ocr-v2" pdf2epub/checkpoints.py` → line 30
- [ ] (Step 8 only) `command grep -n "arabic-ocr-test-sentinel" tests/test_jobs.py` → line 111
- [ ] (Step 8 only) the accepted `compare` output (verdict: ACCEPT) and the
      human spot-check note are attached to the final report
- [ ] `plans/README.md` has a plan-13 row stating the honest outcome

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows any in-scope file changed since `70d468b`, or the
  `OCR_PROMPT` text differs from the excerpt above (someone tuned it
  already — this plan's baseline measurements would be against the wrong
  prompt).
- A step's verification fails twice after a reasonable fix attempt.
- `pymupdf`'s `insert_text`/`get_text` round-trip in the selftest does not
  reproduce the inserted ASCII text (environment problem, not a plan
  problem).
- **Step 7's corpus gate fails** — fewer than 2 books or fewer than 40
  comparable pages available. Land Steps 1-6, record BLOCKED, stop. Never
  proceed to Step 8 without an ACCEPT verdict.
- The baseline's three runs are so noisy that `primary_disjoint` is
  unachievable — report the measured spread instead of weakening the rule.
- Any new test would need a network call, or you find yourself wanting to
  edit `analyzer.py`, `settings.py`, or `web.py` — all out of scope.
- The operator asks for the prompt to be settings-editable — that reverses a
  design decision recorded here; send them to this plan's decision section
  rather than implementing it ad hoc.

## Maintenance notes

- **Every future prompt edit must go through the harness.** The workflow is
  now: copy `00-baseline.txt`, make one change, `run` + `compare`, human
  spot-check, land with an `OCR_CONTRACT_VERSION` bump (v3, v4, …) and a
  matching `00-baseline.txt` regeneration. An unmeasured prompt edit is a
  regression risk with no detector.
- **The contract bump is deliberate friction.** After Step 8, paused jobs
  from before the change resume into `contract_mismatch` and must be
  restarted. If an operator complains, that is the feature working: half a
  book OCR'd under prompt A and half under prompt B is worse.
- `numeral_mismatches` lives in `pdf2epub/text.py` but is only consumed by
  the harness. Wiring it into `analyze_pdf` as a per-page review warning
  (next to the dropped-character hint) is a natural follow-up — deferred
  because its precision on real books is unmeasured; the harness results
  will measure it for free. If wired in later, keep it a hint: either side
  can be wrong.
- The stored `ocr_text` in run files makes old runs re-scorable when a new
  metric is added — recompute instead of re-running OCR where possible.
- A contents-page prompt variant (per-page prompt selection) remains
  deferred; reconsider only if variant 03's measured numbers are
  insufficient, and note it then requires pre-OCR detection and per-page
  prompt recording (see the design decision section).
- Reviewer scrutiny: Step 4's diff must show the two dataclasses gaining a
  trailing field and exactly two `OCR_PROMPT` → `self.prompt` swaps, nothing
  else in `ocr.py`; Step 8's diff must show the prompt text, the version
  constant, and the test sentinel — any other change is scope creep.
