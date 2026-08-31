# Plan 04: Support Python 3.14, eliminate the web-test hang, and show runtime warnings

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report—do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat b3d4ed3..HEAD -- pyproject.toml Dockerfile Jenkinsfile README.md pdf2epub/web.py tests/test_web.py`
> If an in-scope file changed since this plan was written, compare the
> "Current state" excerpts against live code before proceeding. A material
> mismatch is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug, migration, tests, DX
- **Planned at**: commit `b3d4ed3`, 2026-08-27

## Why this matters

The package metadata currently permits Python 3.14, but the README recommends
only 3.11–3.13 and the production image uses 3.13. On the Python 3.14.7 host, 47
tests passed before the suite stalled while executing
`test_remote_ocr_requires_explicit_transfer_acknowledgement`; a 90-second
external timeout terminated the run. The original pytest process remained alive
for more than two hours in `futex_do_wait` with an `asyncio-portal` thread,
while a fresh isolated full run later passed all 54 tests in 0.62 seconds. This
is evidence of nondeterministic unmanaged `TestClient` portal teardown, not a
repeatable endpoint failure. Python 3.14 must not be advertised or deployed
until client ownership is explicit and repeated suites prove clean teardown.

The web app also reports missing EPUBCheck only through a machine-oriented
health boolean. A user can begin a conversion without seeing that publication
must fail, and missing Arabic Tesseract data is only visible indirectly in a
disabled provider option. After this plan, Python 3.14 is an explicitly tested
runtime and the main page gives persistent, actionable Arabic warnings for
missing required/local capabilities before upload.

## Current state

- `pyproject.toml:9` declares `requires-python = ">=3.11"`, which has no upper
  bound and therefore already claims Python 3.14 compatibility.
- `README.md:18` says Python 3.11–3.13 is recommended.
- `Dockerfile:1` uses `python:3.13-slim`.
- `Jenkinsfile:31-44` verifies EPUBCheck and the `ara`/`eng` Tesseract languages
  inside the image, but there is no host-runtime Python matrix or explicit test
  stage.
- `tests/test_web.py:46-51` constructs a temporary `TestClient(web.app)` inline
  for a multipart POST. Under Python 3.14.7 this was the first test that did not
  return in the observed full-suite run.
- `tests/test_web.py:55-59` checks only that `/api/health` returns an
  `epubcheck_available` boolean.
- `pdf2epub/web.py:182-187` currently returns:

  ```python
  return {
      "status": "ok",
      "epubcheck_available": shutil.which("epubcheck") is not None,
  }
  ```

- `pdf2epub/ocr.py:532-550` already owns Tesseract readiness detection and
  distinguishes a missing executable from missing Arabic language data. Reuse
  this through `provider_status()`; do not duplicate subprocess parsing in the
  web layer.
- `pdf2epub/web.py` keeps the HTML, CSS, and plain JavaScript in `INDEX_HTML`.
  Existing UI errors use `.error`, provider readiness comes from
  `/api/ocr/providers`, and all user-facing page text is Arabic. Match those
  conventions rather than introducing a frontend framework.
- Existing tests use plain pytest functions and FastAPI's `TestClient`. For
  deterministic client lifetime, prefer a pytest fixture that yields a client
  from `with TestClient(web.app) as client:` and migrate every web API test to
  it if characterization confirms that lifecycle is involved.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Runtime | `python --version` | Python 3.14.x for the compatibility gate |
| Focused regression | `timeout 30 .venv/bin/pytest -vv tests/test_web.py::test_remote_ocr_requires_explicit_transfer_acknowledgement` | exits 0 promptly; one test passes |
| Web tests | `timeout 60 .venv/bin/pytest -q tests/test_web.py` | exits 0; all web tests pass |
| Full tests | `timeout 180 .venv/bin/pytest -q` | exits 0; 54 or more tests pass |
| Lint | `.venv/bin/ruff check .` | exits 0 with `All checks passed!` |
| Image build | `docker build --tag arabic-pdf2epub:python314-test .` | exits 0 using Python 3.14 |
| Runtime tools | `docker run --rm arabic-pdf2epub:python314-test sh -c 'python --version && epubcheck --version && tesseract --list-langs'` | Python 3.14.x; EPUBCheck succeeds; output contains `ara` and `eng` |

Do not run timeout-wrapped commands as the final CI test mechanism. The timeout
commands above are investigation guards; the final Jenkins test stage must run
pytest normally so a hang fails through the CI system's own stage timeout and
retains useful logs.

## Scope

**In scope** (the only source files to modify):

- `pyproject.toml`
- `Dockerfile`
- `Jenkinsfile`
- `README.md`
- `pdf2epub/web.py`
- `tests/test_web.py`
- `plans/README.md` (status only)

**Out of scope**:

- OCR recognition, retry, model-selection, or prompt behavior in
  `pdf2epub/ocr.py`; consume its existing readiness API only.
- Installing packages or changing services on the host machine.
- Making EPUBCheck optional. It remains mandatory for publication.
- Adding warnings for unconfigured remote providers; Mistral and hosted vision
  are optional and should not make a healthy local installation appear broken.
- Changing conversion integrity, EPUB validation, or job-state semantics.
- Adding a JavaScript framework, template engine, or CSS dependency.
- Pinning around the hang without first proving which dependency/runtime
  interaction causes it.

## Git workflow

- Suggested branch: `advisor/04-python-314-readiness`
- Existing history uses imperative commit subjects such as
  `Tune Ollama Arabic OCR pipeline`; match that style.
- Keep the compatibility/test fix and the readiness-warning UI as separate
  logical commits if commits are requested.
- Do not push or open a pull request unless the operator instructs it.

## Steps

### Step 1: Characterize the Python 3.14 hang before changing code

Run the focused regression command on Python 3.14 with pytest fault-handler
output enabled if it stalls:

```bash
timeout 30 .venv/bin/python -X faulthandler -m pytest -vv -s \
  -o faulthandler_timeout=10 \
  tests/test_web.py::test_remote_ocr_requires_explicit_transfer_acknowledgement
```

Record whether the stall is in the request itself, AnyIO/TestClient portal
startup or shutdown, multipart parsing, or pytest process teardown. Also run
the same test once with a minimal local edit that uses a context-managed
`TestClient`; do not retain that edit unless it fixes the observed stack.

The reviewer has already established that the focused test and one fresh full
suite can pass, while the original stalled process retains an
`asyncio-portal` thread. Treat this as sufficient lifecycle evidence even when
the next focused run does not reproduce; nondeterminism is the regression.

Add a module-local pytest
fixture in `tests/test_web.py` that yields from:

```python
with TestClient(web.app) as client:
    yield client
```

Use that fixture for every test in the module that makes HTTP requests. This
keeps one explicit portal/lifespan boundary per test. Preserve the original
remote acknowledgement POST and its status/detail assertions.

If repeated context-managed runs still hang, use the fault-handler stacks
to identify the smallest compatible FastAPI/Starlette/HTTPX/AnyIO version
combination, confirm the upstream project supports Python 3.14, and update
declared dependency lower bounds only where required. Do not add a narrow exact
pin unless an upstream incompatibility is confirmed and linked in a code
comment or README troubleshooting note.

**Verify**:
`timeout 30 .venv/bin/pytest -vv tests/test_web.py::test_remote_ocr_requires_explicit_transfer_acknowledgement`
→ exits 0 promptly and reports one passed test on Python 3.14.

### Step 2: Make Python 3.14 an explicit tested runtime

Update `README.md` to recommend Python 3.11–3.14. Keep the existing lower bound
in `pyproject.toml` unless investigation proves a dependency requires raising
it; the goal is adding 3.14 support, not unnecessarily abandoning working 3.11
users. Add appropriate Python classifiers in `pyproject.toml` for every version
the project intentionally supports.

Change `Dockerfile` to a Python 3.14 slim image. Do not use floating `latest`.
In `Jenkinsfile`, add a test/lint stage with a bounded Jenkins `timeout` around
the stage and normal `.venv/bin/pytest -q` plus Ruff check commands. The Docker
build and existing runtime-tool verification remain required. If Jenkins does
not provide Python 3.14 on the agent, run tests inside the newly built image or
an explicit Python 3.14 test image; do not silently test with the agent's
unknown Python.

**Verify**:

- `rg -n '3\\.14|3\.13-slim' README.md pyproject.toml Dockerfile Jenkinsfile`
  → 3.14 appears in runtime/docs/CI declarations and `3.13-slim` has no match.
- `docker build --tag arabic-pdf2epub:python314-test .`
  → exits 0.
- The runtime-tools command in the command table reports Python 3.14.x,
  EPUBCheck, `ara`, and `eng`.

### Step 3: Return structured local-capability warnings from health

Keep `/api/health` backward compatible by retaining `status` and
`epubcheck_available`. Add a `warnings` list of structured objects suitable for
display, with stable fields such as `code`, `severity`, `component`, and
Arabic `message`. At minimum:

- `epubcheck_missing`, severity `error`: explain that EPUB publication cannot
  complete until EPUBCheck is installed and available on `PATH`.
- `tesseract_missing`, severity `warning`: explain that local Tesseract OCR is
  unavailable.
- `tesseract_arabic_missing`, severity `warning`: explain that the `ara`
  language data is missing.

Derive the Tesseract warning from the Tesseract item returned by
`provider_status()`. Do not warn merely because optional remote OCR is
unconfigured. Keep `status: "ok"` as the liveness contract so Docker health
checks do not restart a functioning web server; readiness problems belong in
the structured warning list.

Add tests that monkeypatch `shutil.which` and `provider_status` so they do not
depend on the executor machine. Cover no warnings, missing EPUBCheck, missing
Tesseract executable, and missing Arabic data. Assert stable codes as well as
Arabic user-facing messages.

**Verify**:
`.venv/bin/pytest -q tests/test_web.py -k 'health or warning'`
→ all selected tests pass without requiring host EPUBCheck or Tesseract.

### Step 4: Render persistent actionable warnings in the main page

Add an initially hidden warning region near the top of the main card, before
the upload form. It must use `role="alert"` and `aria-live="polite"`, support
multiple messages, and use text-only DOM construction (`textContent`) rather
than interpolating server messages into `innerHTML`.

On page load, request `/api/health` alongside provider readiness. Render every
returned warning in Arabic and leave the region visible until a fresh health
request reports no warnings. Error-severity EPUBCheck messaging must be visually
stronger than warning-severity OCR messaging. Provider settings save/reset
should refresh both providers and warnings because a provider's readiness can
change during the server lifetime.

The warning must explain impact and action, not only name a missing binary. Do
not disable upload globally: an embedded-text-only diagnostic may still be
useful and final publication already fails closed. Continue disabling an
individual OCR provider when its readiness is false.

Extend `test_home_contains_settings_dialog` or add a focused HTML contract test
for the warning-region ID, accessibility attributes, and health-loading hook.
Do not introduce brittle full-document snapshots.

**Verify**:
`.venv/bin/pytest -q tests/test_web.py`
→ all web tests pass, including the hang regression and warning UI contracts.

### Step 5: Update operator documentation and run all gates

Document the warning meanings and installation commands in `README.md`:

- EPUBCheck missing: conversion can analyze but cannot publish a normal EPUB.
- Tesseract or Arabic language data missing: Tesseract OCR is disabled, while a
  ready alternative OCR provider may still be used.
- The Docker image includes both dependencies, while bare-metal installs must
  provide them separately.

Run the full suite under Python 3.14 at least twice to catch teardown/order
dependence, then lint and build the container.

**Verify**:

```bash
timeout 180 .venv/bin/pytest -q
timeout 180 .venv/bin/pytest -q
.venv/bin/ruff check .
docker build --tag arabic-pdf2epub:python314-test .
```

→ both suites exit 0 with 54 or more passing tests and no timeout; Ruff reports
all checks passed; the image builds successfully.

## Test plan

- Preserve the exact remote-transfer acknowledgement behavior: a multipart
  request selecting Mistral without acknowledgement returns HTTP 400 and an
  explanatory detail. The regression is that this test must return promptly on
  Python 3.14, not that its assertion changes.
- Convert all `tests/test_web.py` HTTP tests to an explicit context-managed
  client fixture if Step 1 confirms lifecycle ownership is the cause.
- Add deterministic health tests for:
  - all required local capabilities ready → empty warnings;
  - EPUBCheck absent → `epubcheck_missing` error;
  - Tesseract absent → `tesseract_missing` warning;
  - Tesseract present without `ara` → `tesseract_arabic_missing` warning;
  - optional remote providers unconfigured → no global warning.
- Add a small HTML contract test for the accessible warning region and its
  JavaScript refresh hook.
- Run the complete suite twice on Python 3.14 to expose test-order and cleanup
  failures.

## Done criteria

- [ ] The focused remote-acknowledgement test exits successfully within 30
  seconds on Python 3.14; no sleeps, skipped tests, or weakened assertions were
  added.
- [ ] Two consecutive full suites exit 0 on Python 3.14 with 54 or more tests.
- [ ] Ruff exits 0.
- [ ] `Dockerfile` uses an explicit Python 3.14 slim base and the image builds.
- [ ] README and package metadata consistently state the supported Python
  versions.
- [ ] `/api/health` retains its old keys and adds deterministic structured
  warnings for missing EPUBCheck/Tesseract Arabic readiness.
- [ ] The home page shows accessible Arabic warnings before conversion and
  refreshes them after settings changes.
- [ ] Optional unconfigured remote providers do not produce global warnings.
- [ ] EPUBCheck remains mandatory for publication and Docker liveness remains
  healthy when a bare-metal dependency is missing.
- [ ] `git diff --name-only` contains no source files outside the Scope list.
- [ ] The plan's row in `plans/README.md` is updated.

## STOP conditions

Stop and report back instead of improvising if:

- Three consecutive context-managed full suites do not exit cleanly on Python
  3.14 or leave portal/pytest processes behind. Capture fault-handler evidence
  and report the remaining lifecycle problem rather than claiming it is fixed.
- The hang remains after explicit `TestClient` lifecycle management and the
  stacks implicate an unresolved upstream Python 3.14 incompatibility. Provide
  the stacks and tested dependency versions; do not hide the issue with a skip
  or timeout.
- A Python 3.14 base image is unavailable or any declared runtime dependency
  cannot install on it.
- Supporting Python 3.14 requires dropping Python 3.11–3.13. That is a product
  decision outside this plan.
- Implementing warnings requires changing OCR provider internals or weakening
  conversion/publication integrity gates.
- Any in-scope current-state excerpt has materially drifted from commit
  `b3d4ed3`.

## Maintenance notes

- Reviewers should scrutinize client/portal cleanup and verify the fix addresses
  the captured stack rather than merely changing test order.
- Stable warning codes are the API contract; Arabic wording may evolve without
  forcing clients to parse prose.
- If more required tools are added, expose them through the same warning schema
  rather than adding unrelated booleans.
- A future readiness endpoint may separate liveness from readiness. Until then,
  preserve `status: "ok"` for process health and use `warnings` for actionable
  degradation.
