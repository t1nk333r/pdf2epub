# Content Integrity and Source Selection

## Summary

Prevent corrupted or missing Arabic text from being published as a successful EPUB. Replace the current one-way threshold decision with calibrated candidate evaluation, preserve both embedded-text and OCR candidates on questionable pages, and fail the conversion when required page content cannot be recovered.

This plan covers audit findings 1, 2, and 4. It should be completed before tuning chapter, footnote, or styling heuristics because those stages currently receive unreliable text.

## Current State

- `pdf2epub/text.py::evaluate_text` scores mostly from visible-text length and Arabic character ratio. Long Arabic-looking gibberish can receive a perfect score.
- `pdf2epub/analyzer.py::analyze_pdf` routes a page to OCR when the embedded score is below one threshold, then replaces the embedded blocks with the OCR result without comparing the two candidates.
- OCR output is represented as one full-page paragraph, so no layout or region provenance survives.
- If OCR is unavailable, a scan can become an empty page with a warning. `pdf2epub/pipeline.py::convert` still builds and returns a successful EPUB.
- The quality report records warnings and review pages but has no conversion-level integrity verdict or explicit missing-content count.

## Intended Change

Introduce an explicit source-selection stage with these outcomes for every content-bearing page:

1. accept embedded text;
2. accept OCR text;
3. select the better of both candidates while retaining comparison evidence;
4. require human review; or
5. fail because no usable candidate exists.

No conversion may enter the `complete` state while a required page has outcome 5. Review-required output must be visibly labeled partial and must not be presented as a normal successful EPUB.

## Decisions and Constraints

- Never use an LLM to decide quality based only on plausibility; semantic fluency can hide omissions or hallucinations.
- Preserve exact candidate text, provenance, metrics, and selection reasons in the quality report.
- Keep normalization conservative. Candidate evaluation must not silently rewrite source text.
- Avoid using a single scalar score as the complete decision. Store independent signals and explicit rejection reasons.
- Calibrate thresholds against a labeled Arabic fixture corpus rather than a single book.
- Treat pages that are intentionally blank separately from pages whose content could not be extracted.
- Preserve the existing `OcrProvider` injection point so tests can use deterministic fake providers.

## Model and API Changes

### `pdf2epub/models.py`

- Add a candidate model containing source, text/blocks, quality signals, provider confidence, and rejection reasons.
- Add a page-selection enum such as `embedded`, `ocr`, `comparison`, `review`, `unavailable`, and `intentional_blank`.
- Extend `PageResult` with selected-candidate metadata and optional alternative-candidate summaries.
- Add document-level integrity fields: unavailable pages, review-required pages, partial-output status, and an overall verdict.
- Keep report serialization backward-readable where practical; add a report schema version.

### `pdf2epub/text.py`

- Split quality evaluation into independently testable signals:
  - Unicode replacement/control corruption;
  - detached-letter and excessive token fragmentation;
  - improbable character and punctuation sequences;
  - word-order and line-order anomalies;
  - repeated/duplicated substrings;
  - disagreement between independent extractors or between embedded text and an OCR probe;
  - coverage metrics such as characters and words per occupied text area.
- Return signal values and reasons, not only a weighted score.
- Add a policy function that converts signals into `accept`, `compare`, or `reject`.
- Do not make dictionary membership the sole gate; classical, technical, and diacritized Arabic must remain valid.

### `pdf2epub/analyzer.py`

- Separate extraction into `build_embedded_candidate`, `build_ocr_candidate`, and `select_candidate` steps.
- On questionable pages, retain embedded blocks while OCR runs instead of overwriting them immediately.
- Compare candidates using corruption signals, coverage, normalized alignment, and provider confidence.
- If OCR returns empty or materially lower-coverage text, retain the embedded candidate or mark the page for review.
- Distinguish true blank pages using text, drawing, and image evidence.
- Preserve region/block provenance. Whole-page OCR may remain a fallback, but its block must be clearly marked as layout-unresolved.
- Close the PyMuPDF document in `try/finally` or a context manager so failures do not leak handles.

### `pdf2epub/pipeline.py`

- Add an integrity gate after analysis and before EPUB construction.
- Refuse normal publication when any nonblank page is unavailable.
- Define an explicit opt-in `allow_partial` option for diagnostic/review artifacts; default it to false in CLI and web flows.
- Write EPUB and report to temporary names and publish them atomically only after integrity and EPUB validation succeed.

### `pdf2epub/web.py` and `pdf2epub/cli.py`

- Surface preflight failures, missing OCR configuration, unavailable page numbers, and review-required status clearly.
- Add `allow_partial` only as an advanced explicit option, not an implicit fallback.
- Ensure a failed integrity gate sets the web job to `failed`, with the quality report still downloadable when available.

## Implementation Steps

1. Add versioned candidate, selection, and document-integrity models.
2. Create a labeled fixture manifest with representative clean, corrupt, reversed, fragmented, scanned, blank, and mixed Arabic pages.
3. Refactor `evaluate_text` into signal extraction plus a separately configurable decision policy.
4. Add regression fixtures based on the observed perfect-score gibberish case without committing copyrighted full books.
5. Refactor page analysis to create candidates before selection.
6. Implement deterministic candidate comparison and retain alternative metadata in reports.
7. Add blank-page detection and explicit unavailable-page handling.
8. Add the pipeline integrity gate and partial-output policy.
9. Update web/CLI states and Arabic messages.
10. Update README documentation with the decision outcomes and failure semantics.

## Tests

- Unit tests for every quality signal and boundary value.
- A regression test proving Arabic-looking gibberish does not receive `accept`.
- Tests where OCR is empty, worse than embedded text, better than embedded text, unavailable, and times out.
- Tests for intentional blank pages versus missing-content pages.
- Pipeline tests proving unavailable required pages prevent a successful EPUB.
- Tests proving partial mode is explicit and reported prominently.
- Golden tests for page-selection metadata and report schema compatibility.
- Corpus evaluation reporting false-accept and false-reject rates; define initial release thresholds before implementation is considered complete.

## Acceptance Criteria

- The known gibberish pattern is rejected or routed to comparison/review, never accepted as perfect text.
- A scan with no functioning OCR provider cannot produce a normal successful EPUB.
- OCR never overwrites a higher-quality embedded candidate without recorded comparison evidence.
- Each page has a documented selection outcome and source provenance.
- The report exposes unavailable/review pages and an overall integrity verdict.
- Existing clean embedded-text PDFs continue to avoid unnecessary OCR.

## Risks and Mitigations

- **False rejection of valid historical Arabic:** calibrate on varied fixtures and use review rather than destructive replacement near thresholds.
- **OCR cost and latency:** run dual candidates only on questionable pages and cache page render hashes.
- **Report size:** store full alternatives in the downloadable report while returning a compact web summary.
- **Behavioral migration:** version the report schema and document threshold changes.

## Dependencies and Ordering

- Complete this plan before structural reconstruction improvements.
- The provider registry plan can proceed in parallel, but source selection must consume only the stable `OcrProvider`/candidate interface.
- The EPUB validation plan should be integrated with the final atomic publication gate.
