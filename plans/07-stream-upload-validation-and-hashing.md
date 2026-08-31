# Plan 07: Stop loading whole PDFs into memory to check five bytes and to hash

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 3a33d7b..HEAD -- pdf2epub/web.py tests/test_web.py`
> If an in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding. A material
> mismatch is a STOP condition.
>
> **Amended 2026-08-31**: a third whole-file read was added to `pdf2epub/web.py`
> after this plan was first written — the restart endpoint hashes the source the
> same way resume does. Step 2 and the Done criteria now cover both call sites.
> If `grep -c "read_bytes()" pdf2epub/web.py` returns anything other than 3
> before you start, the file has drifted again: find every site and treat any
> extra one as in scope.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: perf
- **Planned at**: commit `3a33d7b`, amended 2026-08-31

## Why this matters

Uploads are accepted up to 500 MB (`MAX_UPLOAD_BYTES` at
`pdf2epub/web.py:59`). The upload itself is streamed in 1 MiB chunks and hashed
incrementally, which is correct. Immediately afterwards, two code paths throw
that care away and read the entire file into a single `bytes` object: the PDF
magic-number check reads the whole upload to look at five bytes, and every
resume re-reads the whole source PDF to recompute its SHA-256. On a large
scanned book this allocates hundreds of megabytes in the request thread, on a
machine that is simultaneously expected to be running local GPU OCR. Both are
avoidable with no change in behavior: the magic number needs 5 bytes, and the
resume hash can be computed in chunks.

## Current state

### The upload path already streams correctly (`pdf2epub/web.py:559-571`)

```python
    digest = hashlib.sha256()
    size = 0
    with pdf.open("wb") as target:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                target.close()
                shutil.rmtree(store.root)
                raise HTTPException(413, "PDF exceeds the 500 MB upload limit")
            target.write(chunk)
            digest.update(chunk)
        target.flush()
        os.fsync(target.fileno())
```

### Then the whole file is read back for five bytes (`pdf2epub/web.py:573-575`)

```python
    if pdf.read_bytes()[:5] != b"%PDF-":
        shutil.rmtree(store.root)
        raise HTTPException(400, "The uploaded file is not a PDF")
```

### And the whole file is read again on every resume (`pdf2epub/web.py:711-718`)

```python
        source = job.store.root / "source.pdf"
        if (
            not source.is_file()
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest()
            != job.manifest.source_sha256
        ):
            job.blocked_code = "source_mismatch"
```

### And a third time on every restart (`pdf2epub/web.py:838-848`)

```python
        source = job.store.root / "source.pdf"
        if (
            not source.is_file()
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest()
            != job.manifest.source_sha256
        ):
            job.blocked_code = "source_mismatch"
```

This is the same check as the resume path, in `restart_job`. It was added after
this plan was first written. Both call sites take the same fix.

The symlink and existence checks in that condition are deliberate security
checks and must be preserved exactly, including their short-circuit ordering:
`source.is_file()` and `source.is_symlink()` must both still be evaluated
before the file is opened.

### Conventions to match

- 4-space indentation, `from __future__ import annotations` at module top, type
  hints on every function signature, Ruff default rules clean.
- Small shared helpers in `pdf2epub/web.py` are module-level, underscore-
  prefixed, and typed — see `_safe_pdf_name` at `pdf2epub/web.py:271-275` for
  the exemplar shape.
- Tests live in `tests/test_web.py` and use the `client` fixture at
  `tests/test_web.py:30`.

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

- `pdf2epub/web.py`
- `tests/test_web.py`
- `plans/README.md` (status row only)

**Out of scope**:

- Changing `MAX_UPLOAD_BYTES` or the 413 behavior.
- Changing which bytes constitute a valid PDF. The check stays exactly
  `b"%PDF-"` at offset 0 — do not "improve" it into a full PDF parse; the
  subsequent `pymupdf.open` call at `pdf2epub/web.py:585-589` is the real
  validation.
- Removing or reordering the `is_file()` / `is_symlink()` guards on the resume
  path. They are security checks.
- `pdf2epub/checkpoints.py` — its own reads are bounded by
  `MAX_CHECKPOINT_BYTES` and are not in scope.

## Git workflow

- Branch: `advisor/07-stream-upload-validation-and-hashing`
- Commit subjects are imperative and unprefixed; recent examples from
  `git log --oneline`: `Guard job state transitions and recover corrupt checkpoints`,
  `Run startup recovery from a lifespan handler`. Match that style.
- Do NOT push or open a pull request unless the operator instructs it.

## Steps

### Step 1: Validate the PDF header without reading the file

Replace the `pdf.read_bytes()[:5]` check at `pdf2epub/web.py:573` with a
bounded read of the first five bytes, for example by opening the file in binary
mode and calling `read(5)`. Keep the identical failure behavior: remove the job
directory with `shutil.rmtree(store.root)` and raise
`HTTPException(400, "The uploaded file is not a PDF")`.

A zero-byte or truncated upload must still be rejected by this check — `read(5)`
on an empty file returns `b""`, which is correctly unequal to `b"%PDF-"`.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py` → exit 0, and the
existing non-PDF rejection test still passes.

### Step 2: Hash the source PDF in chunks on resume

Add a module-level helper in `pdf2epub/web.py`, next to `_safe_pdf_name`:

```python
def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
```

Use the same 1 MiB chunk size as the upload loop so both paths agree. Replace
**both** occurrences of `hashlib.sha256(source.read_bytes()).hexdigest()` — the
resume path around `pdf2epub/web.py:717` and the restart path around
`pdf2epub/web.py:844` — with a call to it, keeping each surrounding condition
and its ordering unchanged so `is_file()` and `is_symlink()` are still checked
first at both sites.

Locate them before editing:

```
grep -n "sha256(source.read_bytes())" pdf2epub/web.py
```

Expect exactly two matches. Fewer means the file drifted; more means a fourth
call site appeared and is also in scope.

**Verify**:
- `.venv/bin/python -m pytest -q tests/test_web.py -k "resume or restart"` →
  exit 0, all resume and restart tests pass, including both source-mismatch
  tests.
- `grep -c "sha256(source.read_bytes())" pdf2epub/web.py` → `0`.

### Step 3: Prove the streaming helper agrees with the upload digest

Add `test_file_sha256_matches_chunked_upload_digest` to `tests/test_web.py`:
write a file larger than one chunk (at least 2 MiB, content need not be a real
PDF), and assert `web._file_sha256(path)` equals
`hashlib.sha256(path.read_bytes()).hexdigest()`. This pins the property that the
refactor cannot silently change a hash and start rejecting every resume.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k "sha256"` →
exit 0, one new test passes.

### Step 4: Full suite and lint

**Verify**:
- `.venv/bin/python -m pytest -q` → exit 0, 86 or more tests pass.
- `.venv/bin/ruff check .` → exit 0, `All checks passed!`

## Test plan

- `test_file_sha256_matches_chunked_upload_digest` in `tests/test_web.py` —
  multi-chunk equivalence, the regression guard for Step 2.
- Existing coverage to keep green without modification: the non-PDF upload
  rejection test and the resume source-mismatch test in `tests/test_web.py`.
  Locate them with
  `grep -n "not a PDF\|source_mismatch" tests/test_web.py` before you start, and
  re-run them after each step.
- No new test is needed for Step 1 beyond the existing rejection test; the
  behavior is unchanged by design.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m pytest -q` exits 0 with at least 86 tests passing.
- [ ] `.venv/bin/ruff check .` exits 0 with `All checks passed!`.
- [ ] `grep -c "read_bytes()" pdf2epub/web.py` returns `0` — the upload
      validation path and **both** hashing paths (resume and restart) are gone.
- [ ] The restart endpoint's source-mismatch test still passes
      (`test_restart_refuses_a_changed_source_file` in `tests/test_web.py`).
- [ ] The `is_file()` and `is_symlink()` guards are still present at **both**
      call sites and still evaluated before the file is opened.
- [ ] `git status` shows no modified file outside the In scope list.
- [ ] `plans/README.md` status row for plan 07 updated.

## STOP conditions

Stop and report back instead of improvising if:

- The excerpts in "Current state" do not match the live code.
- The resume source-mismatch test starts failing and the cause is not obviously
  in your new hashing helper — a hash mismatch blocks resume for every existing
  job on disk, so do not paper over it.
- You find another caller that depends on `source.read_bytes()` returning the
  whole file.

## Maintenance notes

- The 1 MiB chunk size now appears in two places (`create_job`'s upload loop and
  `_file_sha256`). If one changes, the other should too; they do not have to
  match for correctness, only for consistency of memory behavior.
- A reviewer should check that Step 1 did not turn a rejected upload into an
  orphaned job directory: the `shutil.rmtree(store.root)` call must still run on
  every rejection path, and the file handle must be closed before it does.
- Deliberately deferred: bounding memory in `pdf2epub/checkpoints.py` and in the
  quality-report read at `pdf2epub/web.py:781`. The latter is a separate,
  larger finding about report size.
