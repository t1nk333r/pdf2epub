# Plan 16: Bound the quality-report reads and stop re-parsing them per view

> **Executor instructions**: Follow this plan step by step, run every
> verification command, honor STOP conditions, and update this plan's status
> row in `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 63bbceb..HEAD -- pdf2epub/web.py pdf2epub/pipeline.py pdf2epub/checkpoints.py pdf2epub/models.py tests/test_web.py`
> Material mismatch with the excerpts → STOP.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: plans/14 (suite safety only)
- **Category**: security/perf
- **Planned at**: commit `63bbceb`, 2026-09-01

## Why this matters

Every persisted-JSON read in the app goes through a 16 MB size guard
(`MAX_CHECKPOINT_BYTES`, enforced by `CheckpointStore._read_json`) — except
two: the quality-report reads. The report is the largest artifact the app
writes (it embeds full per-page candidate text for the whole book; multi-MB on
real conversions), and it is parsed **unbounded** and **on the request thread,
on every view of a finished job**. Separately, the conversion worker calls
`document.report()` (a full recursive dataclass dump of every page) two to
three times per conversion where once suffices. None of this is a new
capability — it is applying the codebase's own established bound and removing
repeated work.

## Current state

### The guarded pattern everyone else uses (`pdf2epub/checkpoints.py:272-285`)

`_read_json(path, limit)` rejects files over the limit before `json.loads`.

### Unbounded read 1 — worker, once per finished job (`pdf2epub/web.py:338`)

```python
        report = json.loads(outputs["report"].read_text(encoding="utf-8"))
```

### Unbounded read 2 — request path, every status view (`pdf2epub/web.py:933-935`)

```python
            data["quality"] = _quality_summary(
                json.loads(report.read_text(encoding="utf-8"))
            )
```

`_quality_summary` (`web.py:222-227`) then drops `page_details` — i.e. the
multi-MB payload is parsed only to be discarded.

### The repeated builds (`pdf2epub/pipeline.py:67-68` and `:104-106`)

`_write_report(document, report_path)` serializes `document.report()`; the
next line calls `document.report()` again just for `["integrity"]`; after
`build_epub` appends warnings, `_write_report` runs again (that second write
is legitimate — the warnings changed — but the extra `.report()` for
integrity is pure rework).

### Conventions to match

- 4-space indent, typed signatures, Ruff clean; module-level underscore
  helpers (see `_quality_summary` in `web.py`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0 |

(`python -m pytest` from the repo root; console-script shebangs are stale.)

## Scope

**In scope**: `pdf2epub/web.py`, `pdf2epub/pipeline.py`, `tests/test_web.py`,
`plans/README.md` (status row).

**Out of scope**:

- Shrinking the report itself or removing `page_details` — that is the
  long-recorded "report size" finding (#3, 2026-08-28) and a schema decision.
- `checkpoints.py` — its guard is the exemplar, not a change site.
- Any change to `schema_version` or report content.

## Git workflow

Branch `advisor/16-bound-and-cache-the-report-reads`; imperative unprefixed
subjects; no push unless told.

## Steps

### Step 1: Write the summary sidecar once, at report-write time

In `pipeline.py`, after the final `_write_report`, also write
`quality-summary.json` next to it containing exactly what
`web._quality_summary` returns today (report minus `page_details`). Reuse the
same atomic-write path `_write_report` uses. Compute `document.report()`
**once** into a local and reuse it for: the report write, the integrity check
(`pipeline.py:68`), and the summary — eliminating the duplicate `.report()`
calls (the post-`build_epub` rewrite keeps its own single call).

**Verify**: `.venv/bin/python -m pytest -q tests/test_pipeline_ocr.py` → all
pass; a conversion output dir now contains both files (assert in Step 3's
test).

### Step 2: Make the web layer read the summary, bounded

- `web.py:933-935`: read `quality-summary.json` (derive its path from the
  report path) with a size guard (reuse/import the checkpoint limit or define
  `MAX_REPORT_SUMMARY_BYTES = 1 * 1024 * 1024`); fall back to the old
  full-report parse **only if the sidecar is missing** (jobs converted before
  this change), and bound that fallback with `MAX_CHECKPOINT_BYTES`,
  degrading to `data["quality"] = None` over the limit rather than erroring.
- `web.py:338`: bound the read the same way (this one needs the full report
  for the integrity status; keep reading the full file, just guarded).

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py` → all pass.

### Step 3: Tests

1. `test_status_view_uses_the_summary_sidecar` — build a completed job whose
   outputs include both files, make the full report unreadable (e.g. replace
   its content with oversized junk), assert `GET /api/jobs/{id}` still
   returns the quality summary — proving the request path no longer touches
   the big file.
2. `test_oversized_report_degrades_instead_of_erroring` — legacy job with
   only a full report larger than the cap: status endpoint returns 200 with
   `quality` null/absent, not a 500.
3. Existing `test_quality_summary_omits_large_page_details`
   (`tests/test_web.py:36` area) stays green unmodified.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k "sidecar or oversized"`
→ 2 passed.

### Step 4: Full suite and lint

**Verify**: full suite exit 0; ruff clean.

## Done criteria

- [ ] `grep -n "json.loads(report.read_text" pdf2epub/web.py` → no match
      (replaced by the guarded/sidecar path).
- [ ] `grep -c "document.report()" pdf2epub/pipeline.py` → exactly 2
      (one per write site; the integrity re-call is gone).
- [ ] Step 3's three tests green; full suite green; ruff clean.
- [ ] No out-of-scope files modified; index row updated.

## STOP conditions

- Excerpts don't match live code.
- The sidecar's content cannot equal `_quality_summary`'s output without
  changing `_quality_summary` — report the difference instead of changing
  the function's shape.

## Maintenance notes

- The sidecar is derived data; if report content ever changes, regenerate it
  in the same commit. The fallback path covers old jobs, so no migration.
- This deliberately leaves finding #3 (report size itself) open — the sidecar
  makes the request path immune to it, which lowers but does not remove the
  value of eventually splitting `page_details` out.
