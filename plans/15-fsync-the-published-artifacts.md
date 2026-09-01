# Plan 15: Make the published EPUB and report as durable as the checkpoints

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report. Update this plan's status row in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 63bbceb..HEAD -- pdf2epub/epub.py pdf2epub/pipeline.py pdf2epub/checkpoints.py tests/test_epub.py tests/test_jobs.py`
> A material mismatch with the excerpts below is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: plans/14 (only because every executor should run the suite
  safely; no code dependency)
- **Category**: correctness
- **Planned at**: commit `63bbceb`, 2026-09-01

## Why this matters

The repo contains three independent implementations of "write to a temp file,
then `os.replace`" — and they have silently diverged in durability. The
checkpoint writer fsyncs the file and the parent directory around the rename
(5 fsync calls in `checkpoints.py`); the quality report writer and the final
EPUB writer have **zero**. Measured, not estimated: `grep -c fsync` returns
5 / 0 / 0 for `checkpoints.py` / `pipeline.py` / `epub.py`. So the two
artifacts a user actually downloads have the weakest durability in the system:
after a crash or power loss just after "complete", the visible `.epub` can be
truncated while the job state says finished. The README's claim that EPUBs are
"atomically published" is only half true — visible atomically, durable not.

## Current state

### The correct pattern (`pdf2epub/checkpoints.py:238-260`, `_atomic_json`)

Writes via `tempfile.mkstemp` in the destination directory, fsyncs the file
descriptor, `os.replace`s, then opens the parent directory `O_RDONLY` and
fsyncs it. This is the exemplar; do not modify it, generalize it.

### Gap 1 — the report (`pdf2epub/pipeline.py:43-48`)

```python
            json.dump(
                document.report(), handle, ensure_ascii=False, indent=2, default=str
            )
            handle.write("\n")
        os.replace(temporary, report_path)
    finally:
```

No fsync of the handle before `os.replace`, none of the directory after.

### Gap 2 — the EPUB (`pdf2epub/epub.py:354-357`)

```python
            if validate:
                warnings.extend(_run_epubcheck(temporary))
            os.replace(temporary, output)
        finally:
```

Same gap, on the final deliverable.

### Conventions to match

- 4-space indent, type hints on all signatures, Ruff clean.
- Shared helpers are module-level and underscore-prefixed; docstrings are one
  full sentence. See `_atomic_json` itself for the tone.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0 |

(Stale-shebang note: always `python -m pytest`, from the repo root.)

## Scope

**In scope**:

- `pdf2epub/checkpoints.py` — extract the fsync-replace-fsync sequence into a
  reusable helper in this module (it is the durability module; putting the
  helper here keeps one owner)
- `pdf2epub/pipeline.py`
- `pdf2epub/epub.py`
- `tests/test_jobs.py` and/or `tests/test_epub.py` (tests)
- `plans/README.md` (status row only)

**Out of scope**:

- Changing what is written, when, or the temp-file naming.
- `web.py` — it delegates to these modules.

## Git workflow

- Branch: `advisor/15-fsync-the-published-artifacts`
- Imperative unprefixed commit subjects. No push unless told.

## Steps

### Step 1: Extract the durable-replace helper

In `pdf2epub/checkpoints.py`, add:

```python
def durable_replace(temporary: str | Path, destination: Path) -> None:
    """Fsync the temp file, rename it over destination, fsync the directory.

    The rename alone makes the new name visible; only the surrounding fsyncs
    make it survive power loss. Extracted from _atomic_json so the report and
    EPUB writers share the same guarantee as checkpoints.
    """
```

Implement it by moving the fsync-file → `os.replace` → fsync-dir sequence out
of `_atomic_json`, and have `_atomic_json` call it. Note `_atomic_json`
currently fsyncs the file via its open descriptor *before* closing; preserve
that ordering — the helper may take the already-synced path and do
replace + dir-fsync only, with the file-fsync staying at the write site, OR
open/fsync the file itself. Either is acceptable; pick one, document it in the
docstring, and keep `_atomic_json`'s behaviour byte-identical (its tests in
`tests/test_jobs.py` must pass unmodified).

**Verify**: `.venv/bin/python -m pytest -q tests/test_jobs.py` → all pass,
zero modified assertions.

### Step 2: Use it for the report and the EPUB

- `pipeline.py`: fsync the report handle before it closes (or via the helper)
  and replace the bare `os.replace(temporary, report_path)` with the helper.
- `epub.py`: same for `os.replace(temporary, output)`.

**Verify**: `.venv/bin/python -m pytest -q tests/test_epub.py tests/test_pipeline_ocr.py`
→ all pass.

### Step 3: Pin the guarantee with a test

Durability itself can't be asserted without crash injection, but the *call*
can: add a test that monkeypatches `checkpoints.durable_replace` with a
recording wrapper and runs a conversion (`convert(...)` with
`require_epubcheck=False`, modelled on existing tests in
`tests/test_pipeline_ocr.py`), asserting it was invoked for BOTH the report
path and the epub path. This prevents a future refactor from quietly dropping
back to bare `os.replace`.

**Verify**: the new test fails if you temporarily revert Step 2 in either
file (do the revert, observe the failure, restore — state both results).

### Step 4: Full suite and lint

**Verify**: `.venv/bin/python -m pytest -q` → exit 0 (baseline at your commit
plus new tests); `.venv/bin/ruff check .` → clean.

## Test plan

- `tests/test_jobs.py` existing `_atomic_json` durability tests — unmodified,
  green (regression guard for Step 1).
- New recording-wrapper test from Step 3.

## Done criteria

- [ ] `grep -c fsync pdf2epub/pipeline.py` ≥ 1 and
      `grep -c fsync pdf2epub/epub.py` ≥ 1 (directly or via the helper —
      then instead assert `grep -c durable_replace` ≥ 1 in each).
- [ ] `_atomic_json` behaviour unchanged: `tests/test_jobs.py` passes with
      zero edits.
- [ ] Step 3's revert check performed and reported.
- [ ] Full suite green, ruff clean, no out-of-scope files modified.
- [ ] `plans/README.md` row updated.

## STOP conditions

- Excerpts don't match the live code.
- Any existing `tests/test_jobs.py` assertion needs modification — that means
  the extraction changed `_atomic_json` behaviour; stop rather than adjust
  the test.

## Maintenance notes

- Anyone adding a new "write then replace" site should use `durable_replace`;
  the Step 3 test pattern (recording wrapper) is the cheap way to pin each
  new call site.
- Deliberately not addressed: fsyncing the job `output/` directory when
  multiple artifacts land in one publication — single-file durability is the
  contract for now.
