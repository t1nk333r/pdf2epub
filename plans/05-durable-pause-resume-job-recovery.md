# Plan 05: Make long conversions pauseable, crash-safe, and recoverable

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report—do not improvise. When done, update this plan's row in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat b3d4ed3..HEAD -- pyproject.toml compose.yaml README.md pdf2epub/models.py pdf2epub/analyzer.py pdf2epub/pipeline.py pdf2epub/web.py tests/test_analyzer.py tests/test_pipeline_ocr.py tests/test_web.py tests/test_jobs.py`
> If an in-scope file changed since this plan was written, compare the
> "Current state" excerpts against live code before proceeding. A material
> mismatch is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: `plans/04-python-314-test-readiness-warnings.md`
- **Category**: direction, correctness, architecture, UX
- **Planned at**: commit `b3d4ed3`, 2026-08-28

## Why this matters

OCR of a several-hundred-page Arabic book takes hours. Today, every completed
page exists only in the Uvicorn process until analysis finishes; a power loss,
server restart, browser refresh, or application update can therefore discard
hours of successful Ollama work. A simple in-memory pause flag would help with
temporary GPU sharing but would not survive a crash.

After this plan, each page becomes durable immediately after it is fully
processed. Pause completes the current page, commits its checkpoint, then
releases the worker. Restart recovery never silently consumes the GPU: an
interrupted job appears as recoverable and waits for an explicit Resume action.
The Arabic UI explains what is happening, how much work is safe on disk, what
will repeat after an interruption, and why a job cannot resume when its source
or processing contract has changed.

## Current state

- `pdf2epub/web.py:29` stores jobs under
  `Path(gettempdir()) / "arabic-pdf2epub-jobs"`. `/tmp` is not a durable
  power-loss/reboot contract and may be cleaned by the operating system.
- `pdf2epub/web.py:34-54` defines an in-memory `Job` dataclass. Its state,
  progress, outputs, and logs are not written to disk.
- `pdf2epub/web.py:88-107` can recover only a job that already has one EPUB and
  a quality report. Processing, paused, interrupted, and failed jobs cannot be
  reconstructed.
- `pdf2epub/web.py:132-169` starts one daemon thread and holds no worker handle,
  cancellation token, pause gate, or durable lease.
- `pdf2epub/web.py:210-265` writes the uploaded PDF and launches the worker, but
  does not persist conversion options or a source checksum.
- `pdf2epub/analyzer.py:435-680` opens the PDF, processes pages sequentially,
  appends each `PageResult` to an in-memory `DocumentResult`, then runs
  `_remove_repeated_margins(result)` after all pages. There is no resume index
  or completed-page callback.
- The critical loop currently has this shape (`pdf2epub/analyzer.py:447-449`,
  `657-678`):

  ```python
  for index, page in enumerate(doc):
      if progress:
          progress("Analyzing pages", index, len(doc))
      # extract, OCR, select, structure, and preserve images
      result.pages.append(PageResult(...))
  _remove_repeated_margins(result)
  ```

- `pdf2epub/pipeline.py:35-84` analyzes the entire document, writes one quality
  report atomically, checks integrity, builds the EPUB, and rewrites the report.
  `_write_report()` is the repository's existing atomic-write exemplar: create
  a same-directory temporary file, flush/close it, then call `os.replace()`.
- `pdf2epub/models.py:53-69` defines the typed `PageResult` that must be
  serialized and restored without losing enums, tuples, nested `Block` objects,
  quality signals, OCR metrics, warnings, or candidate evidence.
- `pdf2epub/models.py:72-124` builds document-wide reporting from all pages.
  Resumed output must be indistinguishable from an uninterrupted run.
- `pdf2epub/web.py:317` has one status panel with progress, log, downloads, and
  a restart button. The JavaScript stores the active job ID only in a closure;
  refreshing the browser loses navigation back to the active job even when the
  server continues processing.
- `compose.yaml` already mounts `/data`, while the Docker image sets
  `TMPDIR=/data/tmp`. Introduce an explicit data-root setting rather than
  treating the temporary directory as durable storage.

## Product and state-machine decisions

These decisions are part of the plan and must not be silently changed:

1. **Pause is cooperative and page-boundary safe.** A pause request does not
   cancel an in-flight Ollama HTTP call. The UI immediately shows “pause
   requested” and names the page being finished. The worker enters `paused`
   only after that page checkpoint is atomically committed.
2. **A crash loses at most one page.** A page checkpoint is visible only after
   its complete structured result and referenced extracted images are durable.
   An interrupted/incomplete page is rerun from the beginning.
3. **No automatic GPU restart after boot.** Jobs found in `processing`,
   `pause_requested`, or `building` become `interrupted` on application startup
   and wait for explicit user confirmation.
4. **Completed OCR is immutable under the same processing contract.** Resume
   validates the uploaded PDF hash, page count, checkpoint schema, provider ID,
   model, OCR prompt/adapter version, and all output-affecting options. A
   mismatch blocks resume with a specific reason; it never mixes results from
   different contracts.
5. **Secrets are never checkpointed.** Persist provider ID, model, endpoint
   identity only if safe, and non-secret conversion options. API keys and
   authorization headers remain in environment/runtime memory and readiness is
   rechecked on resume.
6. **One worker owns a job.** Concurrent Resume requests are idempotent and can
   create at most one thread. A durable lease identifies the current process;
   stale leases are recoverable after startup, but a live lease cannot be
   stolen.
7. **Finalization is repeatable.** Whole-document deterministic cleanup,
   integrity evaluation, quality-report creation, EPUB construction, and EPUB
   validation may safely rerun from page checkpoints. A crash during build must
   not redo OCR.
8. **Pause and stop are distinct words.** “Pause” preserves progress and is
   resumable. Do not label deletion or abandonment as pause. Destructive removal
   is deferred from this plan.

## Persistent layout and schemas

Use one directory per job under the configured durable root:

```text
<data-root>/jobs/<job-id>/
  job.json                    # atomic state/manifest snapshot
  source.pdf                  # immutable uploaded source
  checkpoints/
    page-000001.json          # one complete typed PageResult envelope
    page-000002.json
  images/                     # durable approved extracted figures
  output/
    quality-report.json
    <source-stem>.epub
  work/                       # disposable build temporaries only
```

`job.json` must include a schema version, job ID, sanitized original filename,
source SHA-256 and byte size, PDF page count, state, stage, safe-page count,
current/in-flight page, timestamps (`created_at`, `updated_at`, `paused_at`,
`completed_at` as applicable), non-secret options snapshot, processing-contract
fingerprint, output paths relative to the job directory, recoverability/block
reason, bounded log events, and lease metadata.

Each page checkpoint envelope must include its own schema version, one-based
page number, source SHA-256, processing-contract fingerprint, completion
timestamp, fully typed `PageResult`, and relative image references. Reject
absolute paths and `..` components during load. Write JSON to a same-directory
temporary file, flush it, call `os.fsync()` on the file, atomically replace the
target, then fsync the parent directory on Linux when available. The manifest
may lag a page checkpoint after power loss; recovery must scan and validate the
contiguous checkpoint prefix rather than trusting the counter alone.

Use JSON rather than pickle. Checkpoint files are local data but still must be
treated as untrusted after a crash or manual edit: validate types, bounds,
schema versions, job IDs, page numbers, hashes, relative paths, and maximum file
size before constructing model objects.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused persistence tests | `.venv/bin/pytest -q tests/test_jobs.py` | all job/checkpoint tests pass |
| Analyzer resume tests | `.venv/bin/pytest -q tests/test_analyzer.py -k 'checkpoint or resume or pause'` | all selected tests pass |
| Pipeline recovery tests | `.venv/bin/pytest -q tests/test_pipeline_ocr.py -k 'resume or checkpoint or interruption'` | all selected tests pass |
| Web/API tests | `.venv/bin/pytest -q tests/test_web.py -k 'job or pause or resume or recover'` | all selected tests pass |
| Full suite | `timeout 240 .venv/bin/pytest -q` | exits 0 with all tests passing |
| Lint | `.venv/bin/ruff check .` | `All checks passed!` |
| Compose validation | `docker compose config -q` | exits 0 |

Plan 04 must make the full-suite command reliable on Python 3.14 before this
plan is executed. Do not interpret a known TestClient hang as a persistence
regression.

## Scope

**In scope** (the only source files to modify or create):

- `pdf2epub/models.py`
- `pdf2epub/checkpoints.py` (new: schemas, atomic persistence, recovery)
- `pdf2epub/analyzer.py`
- `pdf2epub/pipeline.py`
- `pdf2epub/web.py`
- `pdf2epub/settings.py` only if the data-root setting follows its existing
  definition/validation pattern
- `.env.example`
- `compose.yaml`
- `README.md`
- `tests/test_jobs.py` (new)
- `tests/test_analyzer.py`
- `tests/test_pipeline_ocr.py`
- `tests/test_web.py`
- `plans/README.md` (status only)

**Out of scope**:

- Modifying the currently running conversion or attempting to retrofit its
  in-memory pages into checkpoints.
- Interrupting an Ollama request mid-generation or claiming instant pause.
- Parallel OCR, multiple worker processes, Celery/Redis, distributed leases, or
  multi-host shared storage.
- User accounts, authentication, multi-tenant authorization, or exposing the
  localhost app publicly.
- Encrypting uploaded PDFs or checkpoint text at rest. Document that the data
  root contains book content and requires normal filesystem protection.
- A destructive Delete/Discard API, automated retention cleanup, or storage
  quotas. These need a separate lifecycle policy because accidental deletion
  is materially different from pause/resume.
- Editing OCR prompts or model quality/fallback rules.
- Changing EPUB validity or content-integrity requirements.
- Resuming with changed output-affecting settings. Start a new job instead.
- CLI pause/resume. The storage/pipeline APIs should not prevent it later, but
  this plan delivers the web workflow first.

## Git workflow

- Suggested branch: `advisor/05-durable-pause-resume`
- Existing history uses imperative subjects such as
  `Add live conversion log stream`; match that style.
- Recommended logical commits: persistence primitives/models; resumable
  analyzer/pipeline; job state/API; Arabic UI/UX; documentation/tests.
- Do not push or open a pull request unless the operator instructs it.

## Steps

### Step 1: Define a closed job state machine and typed persistent schemas

In `pdf2epub/models.py`, introduce a `JobState` `StrEnum` (or place web-specific
state types in `checkpoints.py` if that avoids a circular import) with exactly:

```text
queued -> processing -> pause_requested -> paused -> processing
queued/processing/pause_requested/paused/building -> failed
processing -> building -> complete
processing/pause_requested/building -> interrupted (startup recovery only)
interrupted -> processing (explicit resume only)
```

Reject all transitions not listed. `complete` and `failed` are terminal in this
plan. Represent transition validation as a pure function with table-driven
tests. Separate `safe_pages` (durable contiguous checkpoints) from
`current_page` (possibly in flight); never derive persisted safety from the UI
percentage alone.

In new `pdf2epub/checkpoints.py`, add versioned dataclasses/Pydantic models for
the job manifest and page envelope plus explicit serialization/deserialization
for `PageResult`, `Block`, `TextQuality`, and their enums/tuples. Preserve
unknown future fields only if doing so is deliberate and tested; otherwise fail
with a clear unsupported-schema reason.

Define a deterministic processing-contract fingerprint from:

- source SHA-256 and page count;
- all output-affecting `ConversionOptions` fields;
- provider ID and selected model;
- a named OCR prompt/adapter contract version constant;
- checkpoint schema version.

Do not include credentials or transient timeout values unless they can alter
output. If endpoint identity is included, strip credentials and query secrets.

**Verify**:
`.venv/bin/pytest -q tests/test_jobs.py -k 'state or schema or roundtrip or fingerprint'`
→ transition table, typed round-trip, secret exclusion, invalid enum/path, and
fingerprint-change tests all pass.

### Step 2: Implement crash-safe atomic checkpoint storage

Add a small `CheckpointStore` responsible for path construction, atomic JSON
writes, checkpoint validation, contiguous-prefix discovery, manifest repair,
and document reconstruction. It must be the only module that interprets the
on-disk layout.

Required behaviors:

- write one page envelope only after all referenced accepted images exist;
- never overwrite a valid page checkpoint with different content;
- tolerate an orphan temporary file after power failure;
- recover a newly committed page when `job.json.safe_pages` lags behind;
- ignore/quarantine a truncated next-page checkpoint while preserving the last
  valid contiguous page;
- block resume on a corrupt checkpoint inside the previously safe prefix;
- prevent symlink/path traversal outside the job directory;
- keep logs bounded to `MAX_JOB_LOGS` or a storage-layer equivalent;
- use relative paths in persisted state and resolve them only after validation.

Do not fsync after every log line. Page checkpoint and important state
transitions (`paused`, `interrupted`, `building`, terminal states) require
durability; routine progress/log updates may be coalesced to avoid excessive
disk writes.

**Verify**:
`.venv/bin/pytest -q tests/test_jobs.py -k 'atomic or corrupt or traversal or contiguous or manifest'`
→ simulated interrupted writes recover through the last valid page and invalid
paths/schema/content block resume safely.

### Step 3: Make page analysis resumable without changing output

Refactor `analyze_pdf()` to accept an optional resume/checkpoint collaborator,
not web globals. It should:

1. reconstruct `DocumentResult.pages` and durable image paths from validated
   contiguous checkpoints;
2. begin the PDF loop at the first missing page;
3. process that page using the existing extraction/OCR/selection/structure code;
4. persist the complete page and its images atomically;
5. append the same `PageResult` to memory;
6. invoke a cooperative control hook after the checkpoint commit;
7. stop cleanly at that boundary when pause was requested;
8. after all pages, run `_remove_repeated_margins()` exactly once on the fully
   reconstructed document.

Extract the existing single-page body into a private function if needed, but do
not change its quality decisions. Ensure resumed and uninterrupted documents
serialize identically apart from timestamps/job metadata. Because repeated
margin removal is document-wide, store the pre-margin-removal page result and
rerun the deterministic cleanup after reconstruction; do not checkpoint a
partially finalized mixture.

Introduce a typed cooperative pause result/exception handled by the pipeline;
it is expected control flow, not a conversion failure. Always close the PyMuPDF
document in `finally`, including pause and exceptions.

**Verify**:

- `.venv/bin/pytest -q tests/test_analyzer.py -k 'checkpoint or resume or pause'`
  → tests prove pause occurs after a committed page, resume starts at the next
  page, completed fake OCR calls are not repeated, and the PDF handle closes.
- Add a parity test comparing normalized `DocumentResult.report()` from one
  uninterrupted run with a pause/reconstruct/resume run → equal after removing
  only checkpoint/job timestamps.

### Step 4: Make pipeline finalization restartable and idempotent

Extend `convert()` with an optional checkpoint/control context. Distinguish
`paused` from `failed`; a cooperative pause must not emit an integrity failure
or partial EPUB. If all pages exist, reconstruct the document and enter
`building`. Build into temporary output as today and publish only after all
existing integrity/EPUBCheck gates pass.

On resume from `building` or `interrupted` with all page checkpoints present,
rerun deterministic document cleanup, report generation, EPUB construction,
and validation without calling OCR. Clean stale build temporaries only inside
that job's `work/` or output temporary names. Never delete page checkpoints
after success; they are the recovery/audit record until a future retention
policy explicitly removes the job.

**Verify**:
`.venv/bin/pytest -q tests/test_pipeline_ocr.py -k 'resume or checkpoint or interruption'`
→ pause emits no EPUB, resume does not repeat OCR, interruption during build
reruns build only, and final reports/EPUB content match uninterrupted output.

### Step 5: Persist web jobs and enforce single-worker ownership

Replace the web-only ephemeral `Job` storage with a runtime object backed by the
persisted manifest. Keep locks/events/thread handles in memory only; serialize
only stable state. Configure the data root as:

- `PDF2EPUB_DATA_DIR` when set;
- otherwise an XDG user-data location for bare-metal execution;
- `/data` through `compose.yaml` in Docker.

Do not default durable jobs to `/tmp`. On app startup, scan only direct child
directories with valid job IDs. Validate manifests/checkpoints before exposing
them. Convert stale `processing`, `pause_requested`, and `building` jobs to
`interrupted` with an Arabic recoverability message; do not auto-launch them.

Add idempotent endpoints:

- `GET /api/jobs` — newest-first summaries for the local job dashboard;
- `POST /api/jobs/{job_id}/pause` — `processing` becomes
  `pause_requested`; repeated calls return the same state;
- `POST /api/jobs/{job_id}/resume` — resume `paused` or `interrupted` only after
  compatibility/readiness validation; repeated/concurrent calls create one
  worker;
- retain `GET /api/jobs/{job_id}` and download routes with backward-compatible
  fields, adding `safe_pages`, `current_page`, `can_pause`, `can_resume`,
  `recoverable`, `blocked_reason`, and timestamps.

Use HTTP 409 for valid but impossible state transitions and HTTP 422 for a
checkpoint/contract compatibility block. Return stable machine codes plus
Arabic-safe UI detail; do not expose filesystem paths, secrets, or raw provider
responses. Recheck the selected OCR provider before Resume. If unavailable,
keep the job paused/interrupted and explain how to restore readiness.

Persist the uploaded PDF hash/options/initial manifest before launching the
worker. If thread creation fails, transition to `failed` atomically. Use a
process UUID plus worker token/lease under the existing lock to make concurrent
Resume requests idempotent.

**Verify**:
`.venv/bin/pytest -q tests/test_web.py -k 'job or pause or resume or recover'`
→ API tests cover legal transitions, 409/422 responses, two simultaneous Resume
requests creating one worker, startup interruption recovery, provider-not-ready,
and absence of secrets/absolute paths.

### Step 6: Build an Arabic job dashboard that survives refresh

Extend the existing server-rendered HTML/plain JavaScript; do not introduce a
framework. Add a compact “التحويلات” section on the main card that loads
`GET /api/jobs` at startup and shows active/recoverable recent jobs. Each row or
card should include:

- sanitized book filename and creation time;
- state badge with distinct color/icon and text (never color alone);
- durable progress: “تم حفظ 244 من 356 صفحة بأمان”;
- current activity separately: “تتم معالجة الصفحة 245”;
- last update time and a human-readable elapsed/paused duration;
- primary action appropriate to state: View, Pause, Resume, or Download;
- a short blocked reason and settings link when Resume is unavailable.

Keep the active job addressable across refresh by placing its ID in a URL query
or fragment and also recording it in `localStorage` only as a convenience. The
server list is authoritative. Loading an unknown/invalid job ID must return to
the dashboard with an Arabic error rather than a blank spinner.

For long logs, retain the existing bounded server list and render only new IDs.
Do not embed the full log history in `GET /api/jobs`; summaries should include
only the most recent event. Preserve the detailed log on the job view.

**Verify**:
`.venv/bin/pytest -q tests/test_web.py -k 'home or dashboard or refresh or history'`
→ contract tests find the accessible dashboard, URL restoration hook, stable
state labels, and bounded summary response.

### Step 7: Design pause/resume interaction states explicitly

Within the active job panel:

- **Processing**: show an enabled “إيقاف مؤقت” button and explanatory helper
  text that the current page will finish first.
- **Pause requested**: disable the button, show a spinner and “سيتم الإيقاف بعد
  حفظ الصفحة الحالية”, keep polling, and show both current page and last safe
  page.
- **Paused**: freeze the progress animation, show a prominent “متابعة التحويل”
  primary button, display when it paused, retained safe pages, estimated work
  remaining, and “يمكن إغلاق المتصفح أو إعادة تشغيل الجهاز؛ سيبقى التقدم
  المحفوظ”. Do not promise that an uncommitted in-flight page survived.
- **Interrupted**: use a neutral recovery banner, explain that the last complete
  checkpoint is safe, and require explicit Resume.
- **Resume validation blocked**: keep Resume disabled, display the exact
  actionable category (source mismatch, model unavailable, settings mismatch,
  unsupported schema, corrupt checkpoint), and never suggest deleting data as
  the first action.
- **Building**: disable Pause because OCR is already complete; state that the
  EPUB is being assembled/validated and can be rebuilt from saved pages after a
  restart.
- **Complete/failed**: preserve existing download/report actions. A publication
  failure after OCR should remain resumable at the build stage once the missing
  validator is installed, without rerunning OCR.

Button requests must disable immediately to prevent double clicks, recover on
network failure, and reconcile with the next server response. Use `aria-live`
for state messages, `aria-busy` while transitions are pending, visible focus,
and Arabic text labels in addition to icons. Confirmation is not required for
Pause or Resume because both are reversible and nondestructive.

Estimate remaining time from a robust recent-page window (for example median
duration of the last 20 completed OCR pages), label it approximate, and omit it
when insufficient data exists. Paused time must not inflate active processing
ETA. The backend should expose the inputs or the calculated estimate so a page
refresh remains consistent.

**Verify**:
`.venv/bin/pytest -q tests/test_web.py -k 'pause or resume or accessibility or eta'`
→ HTML/API contracts cover every state, double-click/idempotency behavior,
accessible labels/live regions, and ETA omission with insufficient data.

### Step 8: Document recovery guarantees and operator workflow

Update `.env.example`, `compose.yaml`, and `README.md` with:

- `PDF2EPUB_DATA_DIR` and the Docker `/data` mapping;
- storage sensitivity: uploaded PDFs, extracted text, images, and checkpoints
  remain on disk;
- the exact guarantee: at most the currently in-flight page repeats after a
  sudden failure once its previous checkpoint is durable;
- pause is cooperative, not instantaneous;
- startup never auto-resumes GPU work;
- compatibility checks and how to restore a missing Ollama model/provider;
- backup guidance for the data root;
- no automatic cleanup/quota exists yet, so operators must monitor disk usage;
- deployment warning: one application process only until a distributed lease
  design exists. Do not run multiple Uvicorn workers against the same data root.

Ensure Docker stores jobs under the named volume rather than a temporary-only
subdirectory. Keep temporary build files separate from durable checkpoints.

**Verify**:

- `docker compose config -q` → exits 0 and resolves the durable data-root value.
- `rg -n 'PDF2EPUB_DATA_DIR|إيقاف مؤقت|متابعة التحويل|interrupted|checkpoint' README.md .env.example compose.yaml pdf2epub/web.py`
  → configuration, UX text, and recovery behavior are documented/present.

### Step 9: Run fault-injection and full regression gates

Add deterministic tests that simulate failure at every durability boundary by
injecting failures rather than killing real processes:

1. before page checkpoint temporary write;
2. after temporary fsync but before rename;
3. after page rename but before manifest update;
4. during pause request with an OCR call in flight;
5. after all pages but during report write;
6. during EPUB construction/validation;
7. two Resume requests racing;
8. restart with a stale worker lease;
9. source/options/model/prompt-version mismatch;
10. corrupt/traversing/symlinked checkpoint input.

For each, assert the recovered safe-page prefix, next OCR page, public state,
and absence of a falsely published EPUB. Then run all focused suites, the full
suite, Ruff, and Compose validation.

**Verify**:

```bash
.venv/bin/pytest -q tests/test_jobs.py
.venv/bin/pytest -q tests/test_analyzer.py tests/test_pipeline_ocr.py tests/test_web.py
timeout 240 .venv/bin/pytest -q
.venv/bin/ruff check .
docker compose config -q
```

→ all commands exit 0; no test performs real Ollama/network calls; the complete
suite has no hang and no leaked worker threads.

## Test plan

### Persistence and security

- Typed `PageResult` round-trip preserves all current fields and enums.
- Atomic write recovery covers orphan temporary, lagging manifest, truncated
  next page, corrupt safe page, unexpected schema, overlarge JSON, absolute
  paths, `..`, and symlink escape.
- Manifest and public responses contain no API keys, authorization headers, or
  secret settings.
- Processing fingerprint changes for every output-affecting option, model,
  prompt contract, PDF hash, and schema version.

### Analyzer and pipeline correctness

- Pause requested during fake OCR finishes and checkpoints that page before
  pausing.
- Resume begins at the first missing page; fake provider call counts prove
  earlier OCR is not repeated.
- Interrupted current page repeats exactly once.
- Reconstructed images remain inside the job and EPUB references resolve.
- Whole-document repeated-margin removal produces parity between continuous
  and resumed runs.
- Build/report/EPUBCheck interruption never causes OCR repetition or exposes an
  invalid final EPUB.

### API concurrency and recovery

- Every legal and illegal state transition has an expected HTTP result.
- Repeated Pause/Resume requests are idempotent.
- Concurrent Resume requests start one worker.
- Startup marks stale active states interrupted and never auto-starts work.
- Provider unavailable and processing-contract mismatch produce stable blocked
  codes with actionable details.
- Job list is newest-first, bounded/paginated if needed, and excludes detailed
  log arrays.

### UI/UX

- Dashboard and active job restore after browser refresh without relying solely
  on `localStorage`.
- Processing, pause-requested, paused, interrupted, blocked, building,
  complete, and failed states each render the right primary action and Arabic
  explanation.
- UI distinguishes current/in-flight page from safely saved page count.
- Controls have text labels, keyboard focus, `aria-live`, and `aria-busy` where
  appropriate.
- ETA is approximate, excludes paused duration, and disappears when data is
  insufficient.

## Done criteria

- [ ] A completed page is atomically durable before progress reports it as
  safely saved.
- [ ] Simulated power loss at every listed boundary loses at most the current
  uncommitted page and never publishes a corrupt EPUB.
- [ ] Restart reconstructs interrupted jobs without automatically resuming GPU
  work.
- [ ] Explicit Resume continues from the first missing page and does not repeat
  committed fake OCR calls.
- [ ] Pause requested during OCR waits for that page, commits it, then reaches
  `paused` with the UI/API still responsive.
- [ ] Concurrent Resume requests create exactly one worker.
- [ ] Changed source/settings/model/prompt/schema or corrupt checkpoints block
  resume with a stable actionable reason.
- [ ] No secrets or unsafe filesystem paths appear in checkpoints or public
  responses.
- [ ] Browser refresh restores the active job and the dashboard lists
  recoverable work.
- [ ] Arabic UI clearly distinguishes processing, pausing, paused,
  interrupted, blocked, building, completed, and failed states.
- [ ] The UI reports both current page and durably saved page count.
- [ ] `/api/jobs` summaries do not contain full logs or quality page details.
- [ ] Docker uses persistent `/data` storage and README documents protection,
  backup, disk monitoring, cooperative pause, and single-process limits.
- [ ] All focused tests, full pytest suite, Ruff, and Compose validation pass.
- [ ] No files outside the Scope list are modified.
- [ ] `plans/README.md` marks Plan 05 `DONE` only after every gate passes.

## STOP conditions

Stop and report back instead of improvising if:

- Plan 04's Python 3.14 test hang is unresolved; a reliable test baseline is a
  prerequisite for this concurrency/persistence work.
- The current production conversion is still running. Do not reload, restart,
  patch, signal, or migrate its process.
- Refactoring the per-page loop changes OCR text, selection, structure, image
  preservation, integrity, or EPUB output in the parity tests.
- Reliable recovery would require serializing provider credentials.
- The deployment uses more than one Uvicorn process or multiple hosts against
  one data root. This plan's local lease is insufficient for distributed work.
- Filesystem semantics do not support same-directory atomic replace; do not
  claim crash safety on that target.
- A valid checkpoint inside the durable prefix cannot be decoded after a schema
  change. Add an explicit migration or block with a compatibility message; do
  not silently discard completed OCR.
- Resume would require combining checkpoints from different source hashes,
  options, models, prompt contracts, or schema versions.
- A step requires deleting user job data or changing the mandatory EPUB
  integrity/validation policy.

## Maintenance notes

- Reviewers should focus on durability ordering: images, page checkpoint,
  manifest safe-page update, and public progress must occur in that order.
- The manifest is a cache of state; validated contiguous page checkpoints are
  the source of truth for completed work after a crash.
- Any future change to `PageResult`, OCR prompting, candidate selection, image
  classification, or output-affecting options must bump/migrate the relevant
  schema or processing-contract version.
- Keep pause checks at page boundaries unless providers later gain a proven
  cancellation protocol. Mid-request cancellation can waste GPU work and create
  ambiguous output.
- Do not scale to multiple Uvicorn workers until job ownership uses a database
  or robust cross-process lock with tested failure semantics.
- Retention, deletion, storage quotas, and checkpoint compaction are deliberately
  deferred. They require explicit UX and recoverability rules and should be a
  separate plan.

