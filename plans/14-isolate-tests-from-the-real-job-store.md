# Plan 14: Stop the test suite from touching the real job store

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 63bbceb..HEAD -- tests/ pdf2epub/web.py`
> If an in-scope file changed since `63bbceb`, compare the "Current state"
> excerpts against the live code first; a material mismatch is a STOP condition.

## Status

- **Priority**: P1 — execute BEFORE any other open plan; every other plan's
  executor runs this suite against the operator's real data until this lands.
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `63bbceb`, 2026-09-01

## Why this matters

Running `pytest` on a machine with real conversions on disk loads those real
jobs into the application's global registry, and can **rewrite a real job's
`job.json` on disk** as a side effect. This was reproduced, not inferred: on
the audit machine, instantiating the test client loaded the operator's real
job `2c225f2019ef4950a4378adfb0057d94` into `web.jobs`. Startup recovery
contains three `write_manifest` branches; a real job sitting in `processing`,
`pause_requested`, `building`, or `failed`-with-complete-checkpoints state gets
its manifest rewritten by merely running the tests. A test suite must never be
able to corrupt user data.

## Current state

### The fixture that triggers it (`tests/test_web.py:30-33`)

```python
@pytest.fixture
def client():
    with TestClient(web.app) as test_client:
        yield test_client
```

Entering `TestClient` runs the app lifespan, which calls `_recover_jobs()`
(`pdf2epub/web.py:424`). That scans `web.JOB_ROOT`, whose default
(`pdf2epub/web.py:49-58`) resolves to `~/.local/share/pdf2epub/jobs`. The scan
happens at **fixture setup**, before any test body's own
`monkeypatch.setattr(web, "JOB_ROOT", ...)` line runs — so every test using
`client` scans (and possibly rewrites) the real store regardless of what the
test body patches. At least 10 tests use `client` without patching `JOB_ROOT`
at all.

### The shared mutable registry

`web.jobs` is a module-level dict (`pdf2epub/web.py:137`). Tests currently
clean up with ad-hoc `finally: web.jobs.pop(...)` blocks and occasional
`monkeypatch.setattr(web, "jobs", {})` — inconsistently, so a job loaded from
the real store at fixture time stays in the registry for the remainder of the
test session.

### There is no `tests/conftest.py`

```
$ ls tests/conftest.py
ls: cannot access 'tests/conftest.py': No such file or directory
```

### Conventions to match

- Tests are plain `def test_*` functions using `monkeypatch`/`tmp_path`.
- 4-space indent, Ruff default rules clean.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0, 151+ pass |
| Lint | `.venv/bin/ruff check .` | exit 0, `All checks passed!` |

Note: console scripts in `.venv/bin/` have a stale shebang. Always invoke
pytest as `.venv/bin/python -m pytest`, from the repo root.

## Scope

**In scope** (the only files to create or modify):

- `tests/conftest.py` (create)
- `tests/test_web.py` — only if an existing test conflicts with the new
  fixture (see Step 3); otherwise untouched
- `plans/README.md` (status row only)

**Out of scope**:

- `pdf2epub/web.py` and every other source file. The production default of
  `JOB_ROOT` is correct for production; this is purely a test-isolation fix.
- Removing existing per-test `JOB_ROOT`/`jobs` patches — they are redundant
  after this change but harmless; leave them.

## Git workflow

- Branch: `advisor/14-isolate-tests-from-the-real-job-store`
- Commit subjects imperative and unprefixed, e.g. `Guard job state transitions
  and recover corrupt checkpoints`. Do NOT push unless the operator says so.

## Steps

### Step 1: Prove the leak with a failing test

Create `tests/conftest.py` containing, first, a test-support fixture and a
regression test placed in `tests/test_web.py`… no — the regression proof
belongs in the fixture design itself. Do it in this order:

1. In a scratch shell (not a committed file), confirm the leak exists at your
   commit:

```
.venv/bin/python -c "
from pdf2epub import web
from fastapi.testclient import TestClient
before=set(web.jobs)
with TestClient(web.app): pass
print('leaked jobs:', set(web.jobs)-before)"
```

If the machine has no real jobs on disk this prints `set()` — that does NOT
mean the bug is absent; it means the machine is clean. Proceed regardless.

### Step 2: Add the autouse isolation fixture

Create `tests/conftest.py`:

```python
import pytest

from pdf2epub import web


@pytest.fixture(autouse=True)
def isolated_job_store(tmp_path, monkeypatch):
    """Point every test at an empty job store and registry.

    The client fixture's lifespan runs startup recovery at fixture-setup
    time, before a test body can patch JOB_ROOT itself — so without this,
    the suite scans (and can rewrite) the real store under
    ~/.local/share/pdf2epub. Individual tests may still re-patch JOB_ROOT
    to their own tmp directory; this fixture only guarantees the default
    is never the real one.
    """
    monkeypatch.setattr(web, "JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(web, "jobs", {})
    yield
```

`autouse=True` plus pytest's fixture-ordering rule (autouse fixtures run
before same-scope non-autouse fixtures that don't depend on them — verify this
holds by Step 4's check rather than trusting it) makes the patch active before
`client` enters the lifespan.

**Verify**: `.venv/bin/python -m pytest -q` → all tests pass. If any test
fails, see Step 3 before changing the fixture.

### Step 3: Reconcile tests that conflicted

Expected conflict class: a test that relied on `web.jobs` retaining an entry
it inserted **before** calling `client` (ordering), or a test asserting
against the real `JOB_ROOT` default. Fix the test, not the fixture — every
such reliance is exactly the leak this plan removes. List every test you had
to touch in your report. If more than 5 tests need edits, STOP and report:
the fixture ordering may not be what Step 2 assumes.

### Step 4: Prove isolation, in a committed test

Add to `tests/conftest.py` (or `tests/test_web.py`, matching its style) a
test that seeds a fake "real" store and proves the suite never sees it:

```python
def test_client_lifespan_does_not_touch_the_default_store(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    decoy = tmp_path / "decoy-jobs"
    decoy.mkdir()
    (decoy / "a" * 32).mkdir()
    monkeypatch.setattr(web, "JOB_ROOT", decoy)
    # the autouse fixture already re-pointed JOB_ROOT for this test; this
    # test then points it at a decoy explicitly and confirms recovery scans
    # only what a test sets, never a hardcoded real path
    with TestClient(web.app):
        pass
    assert set(web.jobs) == set()  # decoy dir has no valid manifest -> skipped
```

Additionally add the sharper guard: assert that no test can reach the real
default path at all —

```python
def test_default_job_root_is_never_the_real_one_during_tests():
    from pathlib import Path

    assert not str(web.JOB_ROOT).startswith(str(Path.home() / ".local"))
```

**Verify**: `.venv/bin/python -m pytest -q tests/ -k "default_job_root or lifespan_does_not_touch"`
→ 2 passed.

### Step 5: Full suite and lint

**Verify**:
- `.venv/bin/python -m pytest -q` → exit 0, at least 153 tests pass
  (151 baseline + 2 new; more if Step 3 added any).
- `.venv/bin/ruff check .` → `All checks passed!`

## Test plan

- The two committed tests of Step 4.
- The whole existing suite is itself the test: it must pass unmodified except
  for Step 3's documented reconciliations.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m pytest -q` exits 0, ≥153 passing.
- [ ] `.venv/bin/ruff check .` exits 0.
- [ ] `tests/conftest.py` exists with the autouse fixture patching BOTH
      `web.JOB_ROOT` and `web.jobs`.
- [ ] Step 1's scratch probe, re-run after the change **with the suite's
      conftest active** (`.venv/bin/python -m pytest -q tests/test_web.py -k client -x`
      on a machine with a real job present), leaves the real store's mtimes
      untouched: capture `stat -c %Y ~/.local/share/pdf2epub/jobs/*/job.json`
      before and after a suite run and diff — identical, or no real jobs exist
      to check (state which in the report).
- [ ] `git status` shows no modified file outside the In scope list.
- [ ] `plans/README.md` status row updated.

## STOP conditions

- The excerpts above don't match the live code.
- More than 5 existing tests need edits (fixture-ordering assumption wrong).
- The autouse fixture does NOT run before `client`'s lifespan (Step 4's decoy
  test fails in a way that shows the real path was scanned) — report; do not
  try session-scoped patching without review.

## Maintenance notes

- Future fixtures that need a populated store should build it under the
  autouse fixture's `tmp_path`, never by pointing at a shared location.
- If the app ever grows a `PDF2EPUB_DATA_DIR`-style env override read at
  import time, the fixture must patch that too — note it here when it happens.
