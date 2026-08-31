# Plan 08: Pin the progress-string contract the ETA silently depends on

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 5f56abb..HEAD -- pdf2epub/analyzer.py pdf2epub/web.py tests/test_web.py`
> If an in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding. A material
> mismatch is a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `5f56abb`, 2026-08-28

## Why this matters

The web UI's "time remaining" estimate is built by regex-scraping a
human-readable progress message. `pdf2epub/analyzer.py` formats a log line
containing `total_duration=<seconds>s`, and `pdf2epub/web.py` parses that
substring back out to accumulate per-page durations. Nothing declares this
coupling and no test covers it. Rewording the OCR progress message — changing
`total_duration` to `duration`, dropping the trailing `s`, or switching to
scientific notation for a fast page — silently produces an empty
`page_durations` list, and the ETA disappears with no error anywhere. This plan
does not redesign the mechanism; it makes the contract explicit and tested so
the next person to touch that log line finds out immediately.

## Current state

### The producer (`pdf2epub/analyzer.py:540-552`)

```python
                        attempts = ocr_metrics.get("attempts") or [ocr_metrics]
                        attempt_log = "; ".join(
                            f"model={attempt.get('model') or provider_name} "
                            f"prompt_eval_count={attempt.get('prompt_eval_count') or 0} "
                            f"eval_count={attempt.get('eval_count') or 0} "
                            f"total_duration={attempt.get('duration_seconds') or 0}s "
                            f"retries={attempt.get('retries') or 0}"
                            for attempt in attempts
                        )
                        fallback = (
                            f"; fallback_used=yes; reason={ocr_metrics['fallback_reason']}"
                            if ocr_metrics.get("fallback_reason")
                            else "; fallback_used=no"
                        )
                        progress(
                            f"OCR page {index + 1} completed — {attempt_log}{fallback}",
                            index,
                            len(doc),
                        )
```

### The consumer (`pdf2epub/web.py:287-294`)

```python
        if stage.startswith("OCR page") and " completed " in stage and job.manifest:
            durations = [
                float(value)
                for value in re.findall(r"total_duration=([0-9.]+)s", stage)
            ]
            if durations:
                job.manifest.page_durations.append(sum(durations))
                job.manifest.page_durations = job.manifest.page_durations[-100:]
```

Note the consumer's three separate assumptions: the message starts with
`"OCR page"`, contains `" completed "` with surrounding spaces, and carries one
`total_duration=<digits or dot>s` group per attempt. A two-attempt page (the
32B fallback path) is expected to yield two matches which are summed.

### The reader of the accumulated durations (`pdf2epub/web.py:129-134`)

```python
    def _eta(self) -> int | None:
        if not self.manifest or len(self.manifest.page_durations) < 3:
            return None
        durations = sorted(self.manifest.page_durations[-20:])
        median = durations[len(durations) // 2]
        return round(max(self.page_count - self.safe_pages, 0) * median)
```

Fewer than three recorded durations means no ETA at all — which is exactly what
a broken contract looks like from the UI, and why the failure is silent.

### Conventions to match

- Tests live in `tests/`, are plain `def test_*` functions with no class
  wrappers, and use `monkeypatch` and `tmp_path` fixtures. `tests/test_web.py`
  is the file for web-layer behavior; `tests/test_pipeline_ocr.py` contains the
  end-to-end conversions that drive a fake OCR provider.
- `tests/test_pipeline_ocr.py` defines a `FakeOcrProvider`; read it before
  writing Step 2 and reuse it rather than writing a second fake.
- 4-space indentation, Ruff default rules clean.

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

- `tests/test_web.py`
- `pdf2epub/analyzer.py` — comment only, see Step 3
- `pdf2epub/web.py` — comment only, see Step 3
- `plans/README.md` (status row only)

**Out of scope**:

- Replacing the string contract with a structured progress callback. That is a
  larger interface change across `analyzer.py`, `pipeline.py`, `cli.py`, and
  `web.py`; this plan deliberately only pins the current behavior. Do not
  change the `Progress` callable signature.
- Changing the ETA formula in `Job._eta`, the 20-sample window, or the 100-entry
  cap.
- Changing the wording of any progress message. The tests you write must assert
  the wording that exists today.

## Git workflow

- Branch: `advisor/08-pin-the-progress-string-contract`
- Commit subjects are imperative and unprefixed; recent examples from
  `git log --oneline`: `Guard job state transitions and recover corrupt checkpoints`,
  `Run startup recovery from a lifespan handler`. Match that style.
- Do NOT push or open a pull request unless the operator instructs it.

## Steps

### Step 1: Test the consumer directly against today's message format

Add to `tests/test_web.py` a test named
`test_ocr_progress_message_feeds_page_durations`. Build a job with a manifest
(use the `_persistent_job` helper in that file), then call `web._update` with a
message in exactly the format the analyzer produces today, for example:

```
OCR page 3 completed — model=qwen3-vl:32b-instruct-gpu prompt_eval_count=1200 eval_count=800 total_duration=12.5s retries=0; fallback_used=no
```

Assert `job.manifest.page_durations == [12.5]`.

Add a second case in the same test or a sibling test for the two-attempt
fallback message — two `total_duration=` groups joined by `"; "` — and assert
the two values are summed into a single entry.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k "page_durations"`
→ exit 0, new tests pass. They should pass immediately; this step characterizes
existing behavior rather than fixing a defect.

### Step 2: Test the producer and consumer together

Add `test_analyzer_progress_message_is_parsed_by_the_web_layer` to
`tests/test_web.py`. Run a real conversion through `pdf2epub.pipeline.convert`
on a small generated PDF with the `FakeOcrProvider` from
`tests/test_pipeline_ocr.py`, passing a `progress` callback that forwards into
`web._update` for a job you set up first — mirroring how `_run` wires them at
`pdf2epub/web.py:302-312`.

Assert that after the conversion the job's `manifest.page_durations` has one
entry per OCR'd page. This is the test that actually fails if either side of the
contract is reworded, which is the whole point of the plan.

`FakeOcrProvider` already returns `duration_seconds=3.5`
(`tests/test_pipeline_ocr.py:40` and the mirrored attempt metrics at line 47),
so the durations the web layer records should be multiples of that. Assert
against it rather than against a hand-written number. Do not write a second
fake provider.

**Verify**: `.venv/bin/python -m pytest -q tests/test_web.py -k "parsed_by_the_web_layer"`
→ exit 0, one new test passes.

### Step 3: Make the contract visible at both ends

Add a one-line comment above the `progress(...)` call in
`pdf2epub/analyzer.py:548-552` stating that `pdf2epub/web.py` parses
`total_duration=<seconds>s` out of this message to build the ETA, and a matching
one-line comment above the `re.findall` in `pdf2epub/web.py:288-291` naming
`pdf2epub/analyzer.py` as the producer. Match the existing comment style in the
package: full sentences ending in a period, wrapped at the same width as the
code — see the two-line comment at `pdf2epub/analyzer.py:81-82` for the
exemplar.

Change no code in this step.

**Verify**: `git diff --stat pdf2epub/` → exactly two files changed, each with
one or two added lines and zero deletions.

### Step 4: Full suite and lint

**Verify**:
- `.venv/bin/python -m pytest -q` → exit 0, 88 or more tests pass.
- `.venv/bin/ruff check .` → exit 0, `All checks passed!`

## Test plan

- `test_ocr_progress_message_feeds_page_durations` in `tests/test_web.py` —
  single-attempt and two-attempt message formats, asserting the parsed and
  summed values.
- `test_analyzer_progress_message_is_parsed_by_the_web_layer` in
  `tests/test_web.py` — the end-to-end contract test; model it on
  `test_pipeline_pause_publishes_nothing_and_resume_reuses_checkpoint` in
  `tests/test_pipeline_ocr.py:143` for how to build a small PDF and drive
  `convert` with a fake provider.
- Structural pattern to follow for job setup: `_persistent_job` in
  `tests/test_web.py`.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m pytest -q` exits 0 with at least 88 tests passing.
- [ ] `.venv/bin/ruff check .` exits 0 with `All checks passed!`.
- [ ] Editing the `total_duration=` token in `pdf2epub/analyzer.py` to any other
      name makes at least one test fail. Verify this by hand: make the edit, run
      the suite, confirm a failure, then revert the edit and confirm the suite is
      green again.
- [ ] Comments naming the counterpart module exist at both ends of the contract.
- [ ] `git status` shows no modified file outside the In scope list.
- [ ] `plans/README.md` status row for plan 08 updated.

## STOP conditions

Stop and report back instead of improvising if:

- The excerpts in "Current state" do not match the live code.
- The end-to-end test in Step 2 cannot be written without changing the
  `Progress` callable signature or the progress message wording — both are out
  of scope, and a structured-progress redesign should be planned separately.
- The deliberate-break check in the Done criteria does not produce a failure,
  which would mean the tests are not actually pinning the contract.

## Maintenance notes

- The right long-term fix is a structured progress event (a small dataclass or
  dict carrying `page`, `duration_seconds`, `model`, `retries`) instead of a
  formatted string that gets re-parsed. This plan intentionally does not do
  that; it buys a safety net first. If that redesign is undertaken, these tests
  are what tells you the ETA still works.
- A reviewer should confirm the new tests assert against the message format
  produced by the analyzer, not against a hand-written string that has drifted
  from it — Step 2 exists precisely to prevent that.
- `Job._eta` returns `None` until three durations are recorded, so a partially
  broken contract still looks like "no estimate yet" in the UI. Any future
  debugging of a missing ETA should start with `manifest.page_durations`.
