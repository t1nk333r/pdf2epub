# Plan 06: Stop a late pause request from stranding a fully-OCR'd book in `failed`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat eecb074..HEAD -- pdf2epub/models.py pdf2epub/web.py pdf2epub/pipeline.py tests/test_web.py tests/test_jobs.py`
> If an in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding. A material
> mismatch is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `5f56abb`, 2026-08-28; reconciled to `eecb074`, 2026-08-31

## Why this matters

This application exists to protect hours of GPU OCR against interruption. A
pause request that arrives after the last page has been committed — but before
the EPUB build stage begins — currently drives an illegal
`pause_requested → building` state transition. `require_transition` raises
`ValueError` and the worker's catch-all handler marks the job `failed`.
`POST /api/jobs/{id}/resume` rejects a failed job, so the checkpoints cannot be
reused. `POST /api/jobs/{id}/restart` will accept it, but a restart deliberately
calls `discard_checkpoints()` and reruns from page one. Either way the outcome
is the one the durability work exists to prevent: a book whose every page is
durably checkpointed can only be recovered by redoing the whole conversion.

(Verified still reproducing on `eecb074`: driving a `pause_requested` job
through `web._update(job_id, "Building EPUB", 0, 1)` raises
`ValueError: Illegal job state transition: pause_requested -> building`.)

The window is not a hairline race. On a job resumed after a publication
failure, every page checkpoint already exists, so the analyzer's page loop
body never runs and the cooperative pause check is never reached at all — the
entire EPUB rebuild is exposed. On a first run the window is the time taken by
margin stripping plus a full quality-report write, which for a several-hundred
page book is a multi-megabyte JSON dump.

## Current state

Files involved:

- `pdf2epub/models.py` — `JobState`, the `_JOB_TRANSITIONS` table, and
  `require_transition`.
- `pdf2epub/web.py` — the FastAPI app, the in-memory `Job` record, the progress
  callback `_update`, the worker `_run`, and the pause/resume endpoints.
- `pdf2epub/pipeline.py` — `convert`, which emits the `"Building EPUB"`
  progress event.

### The transition table (`pdf2epub/models.py:44-66`)

```python
_JOB_TRANSITIONS = {
    JobState.QUEUED: {JobState.PROCESSING, JobState.FAILED},
    JobState.PROCESSING: {
        JobState.PAUSE_REQUESTED,
        JobState.BUILDING,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.PAUSE_REQUESTED: {
        JobState.PAUSED,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.PAUSED: {JobState.PROCESSING, JobState.FAILED},
    JobState.BUILDING: {
        JobState.COMPLETE,
        JobState.FAILED,
        JobState.INTERRUPTED,
    },
    JobState.INTERRUPTED: {JobState.PROCESSING},
    JobState.COMPLETE: set(),
    # A failed job keeps its source and options, so it can be started over.
    JobState.FAILED: {JobState.QUEUED},
}
```

`JobState.PAUSE_REQUESTED` has no edge to `JobState.BUILDING`, and
`JobState.FAILED` has exactly one outgoing edge, to `QUEUED`, added for the
restart endpoint. It has no edge to `INTERRUPTED`, which is what Step 3 adds —
add to the set, do not replace it, or restart breaks.

### The progress callback (`pdf2epub/web.py:277-282`)

```python
def _update(job_id: str, stage: str, done: int, total: int) -> None:
    with lock:
        job = jobs[job_id]
        if stage == "Building EPUB":
            _transition_job(job, JobState.BUILDING)
            job.current_page = None
```

`_transition_job` (`pdf2epub/web.py:161-162`) is:

```python
def _transition_job(job: Job, target: JobState) -> None:
    job.state = require_transition(JobState(job.state), target)
```

### The pause endpoint (`pdf2epub/web.py:651-669`)

```python
@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.state == JobState.PAUSE_REQUESTED:
            return job.public()
        if job.state != JobState.PROCESSING:
            raise HTTPException(
                409,
                {"code": "cannot_pause", "message": "لا يمكن إيقاف هذه المهمة مؤقتاً"},
            )
        _transition_job(job, JobState.PAUSE_REQUESTED)
        job.stage = "Pause requested"
        job.pause_event.set()
        _append_log(job, "Pause requested; finishing the current page")
        _persist(job)
        return job.public()
```

The job's state remains `processing` from the last committed page all the way
until `_update("Building EPUB", ...)` runs, so this guard accepts a pause
request throughout that window.

### The resume guard (`pdf2epub/web.py:680-683`)

```python
        if job.state not in {JobState.PAUSED, JobState.INTERRUPTED}:
            raise HTTPException(
                409, {"code": "cannot_resume", "message": "لا يمكن متابعة هذه المهمة"}
            )
```

### The catch-all that converts the `ValueError` into a dead job (`pdf2epub/web.py:377-384`)

```python
    except Exception as exc:  # noqa: BLE001 - background job boundary records all failures
        with lock:
            job = jobs[job_id]
            _transition_job(job, JobState.FAILED)
            job.stage = "Failed"
            job.error = str(exc)
            _append_log(job, f"Conversion failed — {exc}", "error")
            job.worker_token = None
```

### Reproduction confirmed before this plan was written

Driving `_update` with a job in `pause_requested` produces:

```
ValueError: Illegal job state transition: pause_requested -> building
```

and a job in `failed` is refused by the resume guard.

### Conventions to match

- The package is 4-space indented, `from __future__ import annotations` at the
  top of every module, and type hints on every function signature. Ruff's
  default rule set must stay clean.
- User-facing API messages are Arabic strings inside a
  `{"code": ..., "message": ...}` dict, raised via `HTTPException`. See
  `pdf2epub/web.py:658-661` for the shape. Internal log lines added via
  `_append_log` are English. Follow both conventions exactly.
- Job state is mutated only while holding the module-level `lock`, and
  persisted with `_persist(job)` in the same critical section.
- Tests use `TestClient` through the `client` fixture in `tests/test_web.py:30`
  and build persistent jobs with the `_persistent_job` helper in the same file.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0, all pass |
| Focused tests | `.venv/bin/python -m pytest -q tests/test_web.py` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0, `All checks passed!` |

Note: the console scripts in `.venv/bin/` carry a stale shebang and fail with
"No such file or directory". Always invoke pytest as
`.venv/bin/python -m pytest`. The `ruff` binary works directly.

## Scope

**In scope** (the only files to modify):

- `pdf2epub/models.py`
- `pdf2epub/web.py`
- `tests/test_web.py`
- `plans/README.md` (status row only)

**Out of scope**:

- `pdf2epub/analyzer.py` and `pdf2epub/pipeline.py` — the cooperative
  page-boundary pause and the ordering of durability writes are correct and
  must not be restructured by this plan.
- `pdf2epub/checkpoints.py` — the durability layer is not implicated.
- Adding cancellation of an in-flight OCR request. Pause remains cooperative
  and page-boundary based; that decision is recorded in `plans/README.md` under
  "Findings considered and rejected".
- Making `failed` a general-purpose resumable state. Only the narrow recovery
  path described in Step 3 is in scope.

## Git workflow

- Branch: `advisor/06-pause-during-build-window`
- Commit subjects are imperative and unprefixed; recent examples from
  `git log --oneline`: `Guard job state transitions and recover corrupt checkpoints`,
  `Run startup recovery from a lifespan handler`. Match that style.
- Do NOT push or open a pull request unless the operator instructs it.

## Steps

### Step 1: Write the failing regression tests first

Add to `tests/test_web.py`, modelled on the existing
`test_pause_is_idempotent_and_sets_page_boundary_event`
(`tests/test_web.py:307`) for structure and on `_persistent_job` for setup:

1. `test_pause_after_last_page_does_not_fail_the_job` — build a job in
   `JobState.PROCESSING`, set its state to `JobState.PAUSE_REQUESTED` to model
   a pause click that landed during the report-writing window, then call
   `web._update(job.id, "Building EPUB", 0, 1)`. Assert it does not raise and
   that the resulting state is one the job can be driven out of — either
   `building` (the pause was too late and is dropped) or `pause_requested`
   preserved for the build stage to honor. Pick whichever your Step 2
   implementation produces and assert it exactly.
2. `test_pause_is_rejected_once_the_build_stage_owns_the_job` — a job already
   in `JobState.BUILDING` must get a 409 from `POST /api/jobs/{id}/pause` with
   `detail["code"] == "cannot_pause"`.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k "pause"` →
the two new tests fail (the first with
`ValueError: Illegal job state transition: pause_requested -> building`), and
every pre-existing pause test still passes.

### Step 2: Make the late pause request safe

Decide and implement one of these two designs. Both are acceptable; pick the
first unless you find a concrete reason it cannot work, and state your choice
in the commit message.

**Design A (recommended) — drop a pause that arrives too late.** In
`_update`, when `stage == "Building EPUB"` and the job is in
`JobState.PAUSE_REQUESTED`, clear `job.pause_event`, log via `_append_log` that
the pause arrived after the last page was saved and the EPUB build will finish,
and transition `pause_requested → building`. This requires adding
`JobState.BUILDING` to the `JobState.PAUSE_REQUESTED` entry in
`_JOB_TRANSITIONS` in `pdf2epub/models.py`. The user gets a finished book
rather than a dead job, which matches the plan-05 principle that committed work
is never discarded.

**Design B — honor the pause at the build boundary.** Leave the transition
table alone; in `_update`, when the job is in `JobState.PAUSE_REQUESTED` and
the build stage is starting, raise `PauseRequested` so `_run`'s existing
handler parks the job in `paused`. Every page is already committed, so a later
resume rebuilds the EPUB from checkpoints. If you choose this, confirm by
reading `pdf2epub/pipeline.py:88-96` that raising from the progress callback
cannot leave the manifest in `building` with no worker.

Whichever design you choose, `pause_job` must additionally reject a pause once
the job has reached `JobState.BUILDING`, returning the existing 409
`cannot_pause` shape. The current guard already does this because it requires
`JobState.PROCESSING`; add the explicit test from Step 1 rather than new code
if it already holds.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k "pause"` →
exit 0, all pass including the two new tests.

### Step 3: Give a `failed` job with intact checkpoints a way back

A job that reached `failed` through this bug — or any other post-OCR
exception — is currently unrecoverable even though every page checkpoint is
valid. Extend startup recovery in `_recover_jobs` (`pdf2epub/web.py:398`) so a
manifest in `JobState.FAILED` whose `safe_pages == page_count` and whose
`store.contiguous_pages(manifest)` succeeds is reported as `interrupted` with
`blocked_code = "publication_failed"` and the existing Arabic blocked reason
used at `pdf2epub/web.py:366-367`.

This requires adding `JobState.INTERRUPTED` to the `JobState.FAILED` entry in
`_JOB_TRANSITIONS`. Do not add any other outgoing edge from `failed`, and do
not make a failed job with incomplete checkpoints recoverable — a genuine
integrity failure must stay failed.

**Verify**: add `test_failed_job_with_complete_checkpoints_recovers_as_interrupted`
to `tests/test_web.py` (write a manifest in `JobState.FAILED` with
`safe_pages == page_count` and all page checkpoints present, call
`web._recover_jobs()`, assert the job is `interrupted` and `can_resume` is
true). Then
`.venv/bin/python -m pytest -q tests/test_web.py` → exit 0.

### Step 4: Full suite and lint

**Verify**:
- `.venv/bin/python -m pytest -q` → exit 0, 85 or more tests pass.
- `.venv/bin/ruff check .` → exit 0, `All checks passed!`

## Test plan

New tests, all in `tests/test_web.py`, structured after
`test_pause_is_idempotent_and_sets_page_boundary_event` and using the
`_persistent_job` helper:

- `test_pause_after_last_page_does_not_fail_the_job` — the regression this plan
  exists for; must fail before Step 2 and pass after.
- `test_pause_is_rejected_once_the_build_stage_owns_the_job` — 409
  `cannot_pause` for a job in `building`.
- `test_failed_job_with_complete_checkpoints_recovers_as_interrupted` — the
  Step 3 recovery path.
- Confirm no existing test regresses; the suite is 85 tests at commit `5f56abb`.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m pytest -q` exits 0 with at least 126 tests passing.
- [ ] `.venv/bin/ruff check .` exits 0 with `All checks passed!`.
- [ ] Driving `_update(job_id, "Building EPUB", 0, 1)` on a job in
      `pause_requested` no longer raises.
- [ ] `POST /api/jobs/{id}/pause` on a job in `building` returns 409 with
      `code == "cannot_pause"`.
- [ ] A `failed` job whose checkpoints are complete and valid is reported as
      `interrupted` by `_recover_jobs` and accepts a resume.
- [ ] A `failed` job whose checkpoints are incomplete is still reported as
      `failed`.
- [ ] `git status` shows no modified file outside the In scope list.
- [ ] `plans/README.md` status row for plan 06 updated.

## STOP conditions

Stop and report back instead of improvising if:

- The excerpts in "Current state" do not match the live code.
- A production conversion is currently running against this data root. Do not
  restart, signal, or migrate that process.
- Design A turns out to discard a pause the user can still observe as pending
  in the UI without any log line explaining it — that is a worse failure than
  the bug, so report rather than ship it.
- Making the `failed → interrupted` recovery work appears to require relaxing
  the checkpoint-integrity checks in `pdf2epub/checkpoints.py`.
- Any change you need would touch `pdf2epub/analyzer.py`'s page loop.

## Maintenance notes

- The reviewer should scrutinize one thing above all: that no new edge in
  `_JOB_TRANSITIONS` lets a job leave `failed` while its checkpoints are
  incomplete or its contract has changed.
- Any future stage added between the last page commit and the EPUB build widens
  the same window. If such a stage is added, it must either emit a progress
  event that `_update` recognizes or explicitly re-check the pause flag.
- Deliberately deferred: making pause interrupt an in-flight OCR request, and
  surfacing a distinct UI state for "pause arrived too late". Both were judged
  larger than this fix warrants.
