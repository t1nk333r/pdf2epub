# Plan 19: Unblock local OCR model experimentation

> **Executor instructions**: Follow step by step; run every verification
> command; honor STOP conditions; update the status row in `plans/README.md`
> unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 63bbceb..HEAD -- pdf2epub/ocr.py pdf2epub/settings.py pdf2epub/web.py tests/test_ocr.py tests/test_settings.py`
> Material mismatch with the excerpts → STOP.

## Status

- **Priority**: P3
- **Effort**: S/M
- **Risk**: MED — this deliberately loosens two guardrails; the risk is
  contained by keeping safe defaults and explicit opt-ins.
- **Depends on**: plans/14
- **Category**: dx
- **Planned at**: commit `63bbceb`, 2026-09-01

## Why this matters

Two hard-coded gates block trying any new local model, measured against real
attempts this week:

1. The Ollama model allowlist is a frozen tuple. The operator has five models
   pulled; the app accepts three. Pulling Arabic-specific models (Qari OCR
   v0.3 was pulled and confirmed to serve vision via Ollama) dead-ends in
   `"Unsupported Ollama OCR model"`. The gate exists for a real reason —
   thinking-mode models emit `<think>` blocks that poison OCR output — but it
   currently blocks all experimentation, not just thinking models.
2. A LOCAL OpenAI-compatible endpoint (vLLM serving a HuggingFace model)
   demands an API key: readiness requires all three of base-url/model/key, so
   a keyless local server needs a dummy value — a papercut that also
   mis-documents itself ("API key are required").

Both matter because the measured path to better Arabic OCR is comparing
models (second-opinion architecture, prompt benchmark) — and the benchmark
harness from plan 13 can only measure what the providers will accept.

## Current state (verified at `63bbceb`)

### The allowlist (`pdf2epub/ocr.py:39-46`)

```python
OLLAMA_QUALITY_MODEL = "qwen3-vl:32b-instruct-gpu"
OLLAMA_FAST_MODEL = "qwen3-vl:30b-instruct-gpu"
OLLAMA_ALTERNATIVE_MODEL = "qwen3.8:27b-q4_K_M"
OLLAMA_BOOK_MODELS = (
    OLLAMA_QUALITY_MODEL,
    OLLAMA_FAST_MODEL,
    OLLAMA_ALTERNATIVE_MODEL,
)
```

Enforced in four places (`grep -n OLLAMA_BOOK_MODELS pdf2epub/`):
`settings.py` readiness (two sites, "must be an approved non-thinking
model"), `ocr.py:_create/factory` validation, and `web.py` create_job's 400.
The UI dropdown additionally hardcodes the three options in `INDEX_HTML`
(recorded separately as a drift finding).

### The key requirement (`pdf2epub/ocr.py:634-640`)

```python
def _openai_readiness(settings: OcrSettings) -> tuple[bool, bool, str]:
    configured = bool(
        settings.vision_base_url and settings.vision_model and settings.vision_api_key
    )
    if not configured:
        return False, False, "Online vision endpoint, model, and API key are required"
```

The provider factory imposes the same triple. The request builder attaches
`Authorization: Bearer <key>` unconditionally.

### Existing extension seam

`OcrSettings.from_env` reads `PDF2EPUB_*` variables (`settings.py`), and the
settings panel exposes them; `provider_status()` already returns
`book_models` for the UI. `<think>`-stripping already exists:
`_THINK_BLOCK_RE` (`ocr.py:38`) is applied to Ollama responses — the
allowlist is a second, blunter defense.

### Conventions

4-space indent, typed, Ruff clean; new env vars get a `SettingDefinition` in
`settings.py` AND a commented line in `.env.example` (the audit verified
these two are currently in perfect sync — keep them so).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0 |

(`python -m pytest` from the repo root.)

## Scope

**In scope**: `pdf2epub/ocr.py`, `pdf2epub/settings.py`, `.env.example`,
`README.md` (the two provider sections), `tests/test_ocr.py`,
`tests/test_settings.py`, `plans/README.md` (status row).

**Out of scope**:

- The UI dropdown (`INDEX_HTML`) — making it dynamic is desirable but is the
  recorded TECHDEBT-01 finding; do not fold it in here. The extra models are
  reachable via CLI `--ollama-model` and the API without UI changes.
- Weakening `<think>` stripping, response-size bounds, or timeouts.
- Auto-detecting installed Ollama models (network call at readiness time —
  a design change, not this plan).

## Git workflow

Branch `advisor/19-unblock-local-model-experimentation`; imperative
unprefixed subjects; no push unless told.

## Steps

### Step 1: Make the allowlist extendable via settings, default unchanged

Add env var `PDF2EPUB_OLLAMA_EXTRA_MODELS` (comma-separated model names) as a
non-secret `SettingDefinition`. In `ocr.py`, provide a function
`ollama_allowed_models(settings) -> tuple[str, ...]` returning
`OLLAMA_BOOK_MODELS + parsed-extras` (stripped, deduped, order-preserving),
and use it at ALL four enforcement sites in place of the bare tuple.
Validation message updates to mention the variable. The default — variable
unset — must behave byte-identically to today.

**Verify**:
- `.venv/bin/python -m pytest -q tests/test_ocr.py tests/test_settings.py`
  → all pass (existing allowlist tests unmodified).
- New tests: extras accepted when set; empty/whitespace entries ignored;
  unset → exactly the built-in three.

### Step 2: Make the API key optional for the OpenAI-compatible provider

Readiness: require base-url and model only; message becomes "Online vision
endpoint and model are required (API key only if the endpoint needs one)".
Request builder: attach the `Authorization` header ONLY when a key is set.
Factory: same relaxation. The `remote` flag and the page-image-transfer
acknowledgement stay exactly as they are — a local vLLM is still "remote" to
the app's data-flow warning, which is the conservative and correct default.

**Verify**: new tests — provider ready with url+model and no key; request
built WITHOUT an Authorization header when keyless, WITH it when keyed.
Existing openai-provider tests green unmodified.

### Step 3: Documentation

`.env.example`: add the new variable with a comment naming the thinking-model
hazard ("only add instruct/OCR models; thinking models corrupt output").
README provider sections: one sentence each for the extras variable and the
now-optional key.

**Verify**: `grep -c OLLAMA_EXTRA_MODELS .env.example README.md` → ≥1 each;
the settings/`.env.example` sync property still holds (every
`SettingDefinition` key present in `.env.example` and vice versa — check by
hand, list the diff if any).

### Step 4: Full suite and lint

**Verify**: full suite exit 0 (baseline + ≥4 new); ruff clean.

## Test plan

- Step 1: 3 tests (extras honored / sanitized / default identical).
- Step 2: 3 tests (ready keyless; header conditional both ways).
- Existing provider/readiness tests: green, unmodified — they pin that the
  defaults did not move.

## Done criteria

- [ ] With no new env vars set, `provider_status()` output is identical to
      before (assert in a test by comparing against the literal expected
      structure).
- [ ] `grep -c "OLLAMA_BOOK_MODELS" pdf2epub/settings.py pdf2epub/web.py`
      → 0 direct uses outside `ocr.py` (all go through the function).
- [ ] Keyless local endpoint reaches ready=True; keyed behaviour unchanged.
- [ ] Full suite green; ruff clean; scope respected; index row updated.

## STOP conditions

- Excerpts don't match live code.
- Any existing test needs its assertions weakened — the defaults must not
  move; report instead.
- You find a fifth enforcement site the grep above missed — add it to the
  change, and say so.

## Maintenance notes

- The thinking-model hazard now rests on `_THINK_BLOCK_RE` plus operator
  judgment for extra models. If a hallucination regression appears after an
  extra model is added, check the raw provider output for reasoning tokens
  first.
- When TECHDEBT-01 (dynamic UI model list) is done, the dropdown should call
  `ollama_allowed_models` via `provider_status()` — the seam this plan adds
  is exactly what it needs.
- The benchmark harness (plan 13) accepts any model the providers accept;
  after this plan, `prompt_bench.py run --model <extra>` becomes the
  measurement path for candidate models (Qari v0.3 lost 1-of-4 known words
  vs qwen3-vl's 4-of-4 on a single-page probe — a real comparison needs the
  harness and a corpus).
