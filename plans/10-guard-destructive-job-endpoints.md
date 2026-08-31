# Plan 10: Stop unauthenticated callers from deleting every conversion

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 3a33d7b..HEAD -- pdf2epub/web.py compose.yaml README.md`
>
> If any in-scope file changed since `3a33d7b`, compare the "Current state"
> excerpts against the live code before proceeding; a material mismatch is a
> STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `3a33d7b`, 2026-08-31

## Why this matters

The application has no authentication of any kind — no dependency guards, no
middleware, no session. That was an acceptable trade while every route was
either read-only or scoped to one job, and while the server was bound to
loopback.

Two changes broke that balance:

1. Two destructive routes were added. `DELETE /api/jobs` deletes **every**
   non-running job and recursively removes its storage — source PDFs,
   checkpoints, and finished EPUBs — in a single unauthenticated request with
   no parameters.
2. `compose.yaml` no longer binds to loopback. A container built from the
   current file publishes the port on all interfaces.

Either alone is defensible. Together, any host that can route to the machine can
destroy every conversion on it, including books that took hours of GPU OCR. The
project's own README states the rule this violates.

This plan does not add authentication — that is a larger decision. It restores
the invariant the README already promises, and makes the destructive route
require an explicit, deliberate call.

## Current state

### There is no authentication anywhere

```
$ grep -n "Depends(\|middleware\|APIKeyHeader\|HTTPBasic\|CORSMiddleware" pdf2epub/web.py
(no matches)
```

Every route is open to anyone who can reach the port.

### The destructive routes (`pdf2epub/web.py:769-822`)

```python
_LIVE_STATES = {JobState.PROCESSING, JobState.PAUSE_REQUESTED, JobState.BUILDING}


def _delete_job_storage(job: Job) -> bool:
    """Remove one job's directory. False when it sits outside the job root."""
    root = job.store.root.resolve()
    if root.parent != JOB_ROOT.resolve() or root.is_symlink():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    ...


@app.delete("/api/jobs")
def clear_jobs() -> dict:
    """Delete every job that is not currently running."""
```

`delete_job` targets one job and requires knowing its 32-hex id. `clear_jobs`
takes no parameters and removes everything. They are not equally dangerous and
should not be equally easy to call.

The path guard inside `_delete_job_storage` is a **correct and deliberate**
safety check. Do not remove or weaken it.

### The network binding (`compose.yaml:8`)

```yaml
    ports:
      - "${PDF2EPUB_PORT:-8000}:8000"
```

Docker publishes on `0.0.0.0` when no host address is given. The committed
version of this line was `"127.0.0.1:${PDF2EPUB_PORT:-8000}:8000"`.

### What the README promises

`README.md:87`:

```
Keep the application bound to localhost unless you add authentication and TLS.
```

`README.md:100`:

```
Open <http://127.0.0.1:8000>. The Compose file binds only to localhost and stores
```

The second sentence is **now false**. A reader who trusts it will deploy an
open service. Stale documentation that is actively wrong is worse than none.

### Conventions to match

- Errors are raised as `HTTPException(status, {"code": ..., "message": ...})`
  with an Arabic `message`; see `pdf2epub/web.py:785-792` for the exemplar shape.
- 4-space indent, type hints on every signature, Ruff default rules clean.
- Tests live in `tests/test_web.py` and use the `client` fixture at
  `tests/test_web.py:30`; job fixtures use `_persistent_job` in the same file.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0, all pass |
| Focused tests | `.venv/bin/python -m pytest -q tests/test_web.py` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0, `All checks passed!` |
| Compose parses | `.venv/bin/python -c "import yaml;yaml.safe_load(open('compose.yaml'));print('ok')"` | prints `ok` |

Note: the console scripts in `.venv/bin/` carry a stale shebang and fail with
"No such file or directory". Always invoke pytest as
`.venv/bin/python -m pytest`. The `ruff` binary works directly.

## Scope

**In scope** (the only files to modify):

- `pdf2epub/web.py`
- `compose.yaml`
- `README.md`
- `tests/test_web.py`
- `plans/README.md` (status row only)

**Out of scope**:

- **Adding authentication, sessions, API keys, or TLS.** That is a product
  decision with its own design, and this plan must not pre-empt it by inventing
  a scheme. If you believe the fix requires auth, STOP and report.
- Removing or weakening the path guard in `_delete_job_storage`. It is a
  security check.
- `DELETE /api/settings` (`pdf2epub/web.py`) — pre-existing, resets in-memory
  overrides only, destroys no user data.
- Changing what `delete_job` does to a single job.

## Git workflow

- Branch: `advisor/10-guard-destructive-job-endpoints`
- Commit subjects are imperative and unprefixed; recent examples from
  `git log --oneline`: `Guard job state transitions and recover corrupt checkpoints`,
  `Run startup recovery from a lifespan handler`. Match that style.
- Do NOT push or open a pull request unless the operator instructs it.

## Steps

### Step 1: Restore the loopback binding in Compose

Change `compose.yaml:8` back to publishing on loopback only:

```yaml
      - "127.0.0.1:${PDF2EPUB_PORT:-8000}:8000"
```

If the operator genuinely needs LAN access, that is a deliberate local edit they
make knowingly — the committed default must be the safe one.

**Verify**:
- `.venv/bin/python -c "import yaml;yaml.safe_load(open('compose.yaml'));print('ok')"` → prints `ok`
- `grep -n "127.0.0.1:" compose.yaml` → one match, on the ports line

### Step 2: Require an explicit confirmation on the bulk delete

`clear_jobs` currently deletes everything on a bare `DELETE /api/jobs`. Add a
required query parameter so the destructive interpretation cannot be reached by
accident — a stray request, a crawler, or a mistyped path:

```python
@app.delete("/api/jobs")
def clear_jobs(confirm: bool = False) -> dict:
    """Delete every job that is not currently running."""
    if not confirm:
        raise HTTPException(
            400,
            {
                "code": "confirmation_required",
                "message": "أضف confirm=true لحذف كل التحويلات",
            },
        )
```

Keep the rest of the function exactly as it is, including the per-job path guard
and the `skipped` list.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k "clear"` →
existing clear tests fail (they call it without `confirm`). That failure is
expected here; Step 4 updates them.

### Step 3: Update the browser caller

The UI calls the endpoint from the embedded JavaScript in `INDEX_HTML`. Find it:

```
grep -n "'/api/jobs',{method:'DELETE'}" pdf2epub/web.py
```

Change that one call to `'/api/jobs?confirm=true'`. The existing
`confirm(...)` dialog in front of it stays — it is the human check; the query
parameter is the machine check. Do not remove either.

**Verify**: `grep -c "api/jobs?confirm=true" pdf2epub/web.py` → `1`

### Step 4: Update and extend the tests

In `tests/test_web.py`:

- Update the existing bulk-clear test to pass `?confirm=true`.
- Add `test_clear_jobs_requires_confirmation`: `client.delete("/api/jobs")`
  returns 400 with `detail["code"] == "confirmation_required"`, **and** the job
  storage directory still exists afterwards. Assert the directory — asserting
  only the status code would pass even if the deletion happened anyway.
- Add `test_clear_jobs_skips_storage_outside_the_job_root`: register a job whose
  store root is outside `JOB_ROOT`, call with `?confirm=true`, and assert its id
  appears in `skipped`, its directory still exists, and the response is 200.
  This path exists in the code today and has no test.

Model the fixtures on `_persistent_job` in the same file, and remember to
`monkeypatch.setattr(web, "jobs", {})` so the module-level registry does not
leak jobs between tests — an existing test needed exactly that.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py` → exit 0, three
clear-related tests pass.

### Step 5: Correct the README

Two edits:

- `README.md:100` — the sentence "The Compose file binds only to localhost" is
  true again after Step 1. Confirm it, do not delete it.
- Document the two delete routes and the `confirm=true` requirement in the
  section that describes the API surface, and state plainly that the
  application has no authentication and must not be exposed beyond localhost.

**Verify**: `grep -n "confirm=true" README.md` → at least one match.

### Step 6: Full suite and lint

**Verify**:
- `.venv/bin/python -m pytest -q` → exit 0, at least 119 tests pass.
- `.venv/bin/ruff check .` → exit 0, `All checks passed!`

## Test plan

- `test_clear_jobs_requires_confirmation` — 400, and storage survives.
- `test_clear_jobs_skips_storage_outside_the_job_root` — the untested branch.
- Updated existing bulk-clear test, now passing `?confirm=true`.
- Existing coverage that must stay green without modification:
  `test_delete_job_removes_its_storage`, `test_a_running_job_cannot_be_deleted`.
- Structural pattern: `_persistent_job` in `tests/test_web.py`.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m pytest -q` exits 0 with at least 119 tests passing.
- [ ] `.venv/bin/ruff check .` exits 0 with `All checks passed!`.
- [ ] `grep -n "127.0.0.1:" compose.yaml` matches the ports line.
- [ ] `DELETE /api/jobs` without `confirm` returns 400 and deletes nothing —
      proven by a test that asserts the directory still exists.
- [ ] The path guard in `_delete_job_storage` is unchanged
      (`git diff pdf2epub/web.py` shows no edit to that function).
- [ ] `README.md` no longer claims anything about the binding that the Compose
      file contradicts.
- [ ] `git status` shows no modified file outside the In scope list.
- [ ] `plans/README.md` status row for plan 10 updated.

## STOP conditions

Stop and report back instead of improvising if:

- The excerpts in "Current state" do not match the working tree, or
  `clear_jobs` does not exist (see the drift note at the top).
- You conclude the fix needs authentication. That is out of scope by design —
  report the reasoning instead of inventing a scheme.
- Restoring the loopback binding breaks the operator's workflow in a way you can
  observe (for instance, a documented remote-access setup elsewhere in the repo).
- Any existing delete test fails for a reason other than the new `confirm`
  parameter.

## Maintenance notes

- `confirm=true` is a guard against accidents, **not** a security control. Anyone
  who can reach the port can still pass it. The only real fix is authentication,
  and this plan deliberately defers that decision — a reviewer should not read
  this as "the endpoint is now safe to expose".
- If authentication is added later, this parameter can stay: confirmation and
  authorization answer different questions.
- The `skipped` list returned by `clear_jobs` exists so one misplaced job cannot
  abort the whole sweep. If a caller ever starts treating a non-empty `skipped`
  as success, that silently hides jobs that could not be removed.
