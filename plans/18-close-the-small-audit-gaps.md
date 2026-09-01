# Plan 18: Close the small audit gaps in one sweep

> **Executor instructions**: Follow step by step; run every verification
> command; honor STOP conditions; update the status row in `plans/README.md`
> unless a reviewer says they maintain the index. The steps are independent —
> commit each as its own logical commit in the order given.
>
> **Drift check (run first)**:
> `git diff --stat 63bbceb..HEAD -- pyproject.toml README.md pdf2epub/web.py pdf2epub/cli.py pdf2epub/text.py pdf2epub/checkpoints.py tests/`
> Material mismatch with any excerpt → skip THAT step, report it, continue
> with the rest.

## Status

- **Priority**: P2
- **Effort**: S (six independent S-items)
- **Risk**: LOW
- **Depends on**: plans/14
- **Category**: mixed (deps, docs, correctness, debt)
- **Planned at**: commit `63bbceb`, 2026-09-01

## Why this matters

Six verified, one-to-few-line defects from the 2026-09-01 deep audit, none
worth a plan alone, all cheap and all real: an undeclared runtime dependency,
a README instruction that sends Docker users to the wrong port, a JS state
map missing an enum member and never translated, raw tracebacks from the CLI
for its own domain errors, a duplicated constant that can silently diverge,
and an Arabic-range mismatch inside the quality scorer.

## Current state (each verified at `63bbceb`)

1. **pydantic undeclared** — `pdf2epub/web.py:19` `from pydantic import
   BaseModel`; `grep -c pydantic pyproject.toml` → 0.
2. **README Docker port wrong** — `README.md:114` says
   `http://host.docker.internal:11434`; `compose.yaml:13` compiles the
   default `http://host.docker.internal:11435` via
   `${PDF2EPUB_OLLAMA_URL_DOCKER:-…}`, a variable README never names.
3. **JS state maps** — `web.py`'s `INDEX_HTML` has `stateLabels` (8 keys,
   Arabic only — English mode shows Arabic badges) and a `messages` object
   with 7 of 8 `JobState` keys — `queued` missing, masked by `||data.stage`.
   `JobState` members (`pdf2epub/models.py:34-42`): queued, processing,
   pause_requested, paused, building, complete, failed, interrupted.
4. **CLI tracebacks** — `pdf2epub/cli.py:47-54` calls `convert(...)` bare;
   `ConversionIntegrityError` / `ConversionPublicationError` (both carry
   `.report_path`) and `MissingDependency` escape as raw tracebacks. The web
   layer handles all three (`web.py` worker `_run`); the CLI is the only
   consumer that doesn't. (Reconfirms 2026-08-28 finding #7.)
5. **`MAX_JOB_LOGS` duplicated** — `pdf2epub/checkpoints.py:32` and
   `pdf2epub/web.py:60` both define `MAX_JOB_LOGS = 2_000` independently;
   only the web copy is tested.
6. **`spaced_arabic` narrower range** — `pdf2epub/text.py:177` hardcodes
   `[؀-ۿ]` while `ARABIC_RE` (`text.py:9`) also covers
   `ݐ-ݿ` and `ࢠ-ࣿ`; Supplement/Extended-A letters in a
   disconnected run escape the detector.

Conventions: 4-space indent, typed signatures, Ruff clean; i18n keys follow
the existing `STRINGS` table pattern in `INDEX_HTML` (`'key':'English'`, with
`t('key')||'العربية'` fallback at use sites).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0 |

(`python -m pytest` from the repo root; stale console-script shebangs.)

## Scope

**In scope**: `pyproject.toml`, `README.md`, `pdf2epub/web.py` (INDEX_HTML
string-table/JS only), `pdf2epub/cli.py`, `pdf2epub/text.py`,
`pdf2epub/checkpoints.py` OR `pdf2epub/web.py` for the constant (one import
line), `tests/test_web.py`, `tests/test_text.py`, `tests/test_analyzer.py`,
`plans/README.md` (status row).

**Out of scope**: any endpoint logic, the selection policy, the prompt, the
Ollama allowlist (that is plan 19), lockfile work (recorded separately).

## Git workflow

Branch `advisor/18-close-the-small-audit-gaps`; one commit per step,
imperative unprefixed subjects; no push unless told.

## Steps

### Step 1: Declare pydantic

Add `"pydantic>=2",` to `[project] dependencies` in `pyproject.toml`.
**Verify**: `.venv/bin/python -c "import tomllib;d=tomllib.load(open('pyproject.toml','rb'));print([x for x in d['project']['dependencies'] if 'pydantic' in x])"`
→ one entry.

### Step 2: Fix the README Docker port and name the variable

Change `README.md:114` to port `11435` and add one sentence naming
`PDF2EPUB_OLLAMA_URL_DOCKER` as the Compose-side override.
**Verify**: `grep -n "11434" README.md` → no match on the Docker line
(the bare-metal default 11434 elsewhere is CORRECT — do not touch it);
`grep -c "PDF2EPUB_OLLAMA_URL_DOCKER" README.md` ≥ 1.

### Step 3: Complete and translate the JS state maps

In `INDEX_HTML`: add the missing `queued` entry to `messages`; route
`stateLabels` and `messages` values through the i18n table (add
`state.label.*` / existing `state.*` keys to `STRINGS.en`; keep the Arabic
literals as fallbacks in the established `t('…')||'…'` pattern — note
`stateLabels` is currently a const built at parse time, so convert it to a
function or rebuild it inside `applyLang()`/`renderJobState` so a language
switch takes effect without reload). Then pin the enum sync: add
`test_js_state_maps_cover_every_job_state` extracting the keys of both maps
from `INDEX_HTML` with a regex and asserting equality with
`{s.value for s in JobState}`.
**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k state_maps`
→ 1 passed; flip-check: remove one key, observe failure, restore.

### Step 4: Catch the CLI's domain exceptions

Wrap the `convert(...)` call in `cli.py`:

```python
    try:
        outputs = convert(...)
    except (ConversionIntegrityError, ConversionPublicationError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        if getattr(exc, "report_path", None):
            print(f"report: {exc.report_path}", file=sys.stderr)
        raise SystemExit(2)
    except MissingDependency as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(3)
```

(Adjust imports; `MissingDependency` lives in `pdf2epub.analyzer`.)
Add `test_cli_reports_integrity_failures_without_traceback` in
`tests/test_analyzer.py` (monkeypatch `cli.convert` to raise, assert
`SystemExit.code == 2` and the message reached stderr via `capsys`).
**Verify**: the test passes; and passes only WITH the handler (flip-check).

### Step 5: Single source for `MAX_JOB_LOGS`

In `pdf2epub/web.py`, replace the local definition with
`from .checkpoints import MAX_JOB_LOGS` (keep the name exported from
`checkpoints.py`). Confirm the existing truncation test still passes and add
one asserting a persisted manifest's `logs` are truncated to
`checkpoints.MAX_JOB_LOGS` (seed >limit entries, `write_manifest`,
`load_manifest`, assert length).
**Verify**: `grep -c "MAX_JOB_LOGS = " pdf2epub/web.py` → 0.

### Step 6: Widen `spaced_arabic` to the shared Arabic class

Build the run-detection pattern in `text.py` from the same ranges as
`ARABIC_RE` (define the class string once, use it in both). Add
`test_disconnected_extended_arabic_is_detected` using a run of
Supplement-block letters (e.g. `ݐ ݑ ݒ ݓ ݔ`) asserting
`"disconnected Arabic letters" in reasons`.
**Verify**: new test passes; the existing disconnected-letters test
unmodified and green.

### Step 7: Full suite and lint

**Verify**: full suite exit 0 (baseline + ≥4 new tests); ruff clean.

## Done criteria

- [ ] All six steps' individual verifies pass (or a step was skipped for
      drift and reported).
- [ ] Flip-checks for Steps 3 and 4 performed and reported.
- [ ] Six commits (or fewer if steps were skipped), one per step.
- [ ] Full suite green; ruff clean; no out-of-scope modifications; index row
      updated.

## STOP conditions

- A step's excerpt doesn't match — skip that step, report, continue.
- Step 3's `stateLabels` rework requires touching `renderJobState` beyond
  label lookup — report the shape before proceeding if the diff would exceed
  ~20 lines of JS.

## Maintenance notes

- Step 3's sync test is the template for pinning any Python↔JS mirror; the
  Ollama model list (plan 19's territory) deserves the same test once that
  list becomes dynamic.
- Step 5 leaves `MAX_CHECKPOINT_BYTES` and friends where they are — only the
  duplicated constant moves.
