# Plan 11: Stop chapter merging from deleting a chapter's navigation entry

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` unless a reviewer says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 5f56abb..HEAD -- pdf2epub/epub.py tests/test_epub.py`
>
> **Important**: this plan was written against a working tree with **818
> uncommitted insertions** on top of `5f56abb`. The excerpts in "Current state"
> come from that working tree. Run `git status --porcelain` first:
> - **dirty** → compare the excerpts against the working tree directly.
> - **clean, and `grep -n "_merge_stub_chapters" pdf2epub/epub.py` finds
>   nothing** → this plan's subject does not exist. STOP and report.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: correctness
- **Planned at**: commit `5f56abb` (+ uncommitted work), 2026-08-31

## Why this matters

A book's front matter — cover, half-title, colophon — puts one large line of
type on an otherwise empty page. Each such line reads as a chapter heading, so
the builder used to emit a separate XHTML file per page, fragmenting the front
matter into 600-byte stubs. `_merge_stub_chapters` fixes that by folding any
chapter with under 240 visible characters into the one before it.

The fold is applied to the `chapters` list, and that same list drives **both**
the spine files **and** the table of contents. So a folded chapter's title is
not merely moved — it disappears from the navigation entirely. Its text is still
in the book; the reader simply has no way to jump to it.

For cover pages that is the intended outcome. For a genuinely short chapter — a
poem, a one-page preface, a part-divider carrying only "الباب الأول" — it
silently deletes a real navigation entry. The book looks complete, the TOC is
quietly wrong, and nothing in the quality report mentions it.

The fix is to keep the entry and point it at the right place in the merged file,
rather than choosing between "fragment the book" and "lose the entry".

## Current state

### The merge (`pdf2epub/epub.py:165-189`)

```python
# A cover, a half-title and a colophon each carry a large line of type that
# reads as a heading. Emitting one file per such line fragments the front
# matter into stubs, so anything this short is folded into its neighbour.
MIN_CHAPTER_TEXT = 240


def _merge_stub_chapters(chapters: list[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for title, body in chapters:
        visible = len(re.sub(r"<[^>]+>", "", body).strip())
        if merged and visible < MIN_CHAPTER_TEXT:
            previous_title, previous_body = merged[-1]
            merged[-1] = (previous_title, previous_body + body)
        else:
            merged.append((title, body))
    # A cover and a half-title carry the same words; keep one file, not two.
    if len(merged) > 1 and merged[0][0].strip() == merged[1][0].strip():
        merged[0] = (merged[0][0], merged[0][1] + merged[1][1])
        del merged[1]
    return merged
```

Note that the merged title is discarded — only `previous_title` survives.

### Where the loss becomes visible (`pdf2epub/epub.py:244-260`)

```python
        chapters = _merge_stub_chapters(chapters)
        manifest: list[str] = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="css" href="css/book.css" media-type="text/css"/>',
        ]
        spine: list[str] = []
        nav_items: list[str] = []
        for i, (title, body) in enumerate(chapters, 1):
            name = f"chapter-{i:03d}.xhtml"
            (epub / "chapters" / name).write_text(_xhtml(title, body), encoding="utf-8")
            manifest.append(
                f'<item id="c{i}" href="chapters/{name}" media-type="application/xhtml+xml"/>'
            )
            spine.append(f'<itemref idref="c{i}"/>')
            nav_items.append(
                f'<li><a href="chapters/{name}">{html.escape(title)}</a></li>'
            )
```

One loop builds files, spine and `nav_items` from the same list. Merging two
chapters therefore removes one nav entry as a side effect — there is no separate
navigation model that could have kept it.

### How chapters are split (`pdf2epub/epub.py:232-243`)

```python
        for page in document.pages:
            for block in page.blocks:
                if block.type == BlockType.HEADING and block.heading_level == 1:
                    if current:
                        chapters.append((current_title, "".join(current)))
                        current = []
                    current_title = block.text.strip()[:160]
                current.append(_block_html(block, document.fix_bidi, page.page))
```

The heading block that opens a chapter is also rendered *into* that chapter's
body by `_block_html`. So after a merge, the merged chapter's `<h1>` is still
present in the combined body — the heading text is in the file, just unreachable
from the TOC. That is what makes a fragment link the natural fix.

### The heading rendered into the body (`pdf2epub/epub.py:190-193`)

```python
    if block.type == BlockType.HEADING:
        level = 2 if block.heading_level == 2 else 1
        return f"<h{level}>{content}</h{level}>"
```

Headings carry no `id` attribute today. Step 1 adds one.

### Conventions to match

- 4-space indent, type hints on every signature, Ruff default rules clean.
- All XHTML is built by string concatenation with `html.escape` on any text that
  came from the document — see `pdf2epub/epub.py:259`. Match that; do not
  introduce a templating library.
- Ids elsewhere in this module are slugified with a explicit regex and given a
  stable prefix — see `_footnote_target` at `pdf2epub/epub.py:141-147`:

  ```python
  def _footnote_target(identifier: str, page: int) -> str:
      safe = re.sub(r"[^\w.-]+", "-", identifier, flags=re.UNICODE).strip("-")
      return f"fn-{page}-{safe or 'note'}"
  ```

  Follow the same shape for chapter anchors.
- Tests live in `tests/test_epub.py`, plain `def test_*` functions.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest -q` | exit 0, all pass |
| Focused tests | `.venv/bin/python -m pytest -q tests/test_epub.py` | exit 0 |
| Lint | `.venv/bin/ruff check .` | exit 0, `All checks passed!` |

Note: the console scripts in `.venv/bin/` carry a stale shebang and fail with
"No such file or directory". Always invoke pytest as
`.venv/bin/python -m pytest`. The `ruff` binary works directly.

## Scope

**In scope** (the only files to modify):

- `pdf2epub/epub.py`
- `tests/test_epub.py`
- `plans/README.md` (status row only)

**Out of scope**:

- **`MIN_CHAPTER_TEXT` and the merge policy itself.** Do not tune the threshold
  or change which chapters merge. This plan changes what a merge *costs*, not
  when it happens. Retuning the threshold without fixing the navigation loss
  just moves the bug.
- `pdf2epub/analyzer.py` and how `heading_level` is assigned. A separate concern.
- The nav document's structure beyond the `<li>` list — do not introduce nested
  `<ol>` hierarchy in this plan, even though level-2 headings would allow it.
  That is a larger design change; note it as a follow-up instead.
- The landmarks nav and the spine ordering.

## Git workflow

- Branch: `advisor/11-keep-nav-entries-for-merged-chapters`
- Commit subjects are imperative and unprefixed; recent examples from
  `git log --oneline`: `Guard job state transitions and recover corrupt checkpoints`,
  `Run startup recovery from a lifespan handler`. Match that style.
- Do NOT push or open a pull request unless the operator instructs it.

## Steps

### Step 1: Give every level-1 heading a stable anchor

In `_block_html` (`pdf2epub/epub.py:190-193`), emit an `id` on level-1 headings
so a nav entry can target the heading inside a merged file. Derive it from the
page number and a slug of the heading text, following `_footnote_target`'s
shape — for example `ch-<page>-<slug>`. Escape it for use in an attribute.

Level-2 headings do not need anchors in this plan; they never open a chapter.

The id must be stable for the same input: two runs over the same PDF must
produce byte-identical files. Do not use a counter that depends on iteration
order across pages, and do not use randomness or a timestamp.

**Verify**: build any small EPUB twice and compare — the chapter XHTML files
must be byte-identical between runs. `.venv/bin/python -m pytest -q tests/test_epub.py`
→ exit 0.

### Step 2: Carry merged titles through the merge

Change `_merge_stub_chapters` so a chapter is a title, a body, **and** the list
of titles that were folded into it along with the anchor each one points at.
A small dataclass or a 3-tuple is fine; match the surrounding style, which uses
plain tuples.

When folding, append the folded chapter's `(title, anchor)` to the survivor's
list instead of discarding it. Preserve the existing behaviour in every other
respect: the same chapters merge, in the same order, with the same bodies.

The duplicate-title case (cover and half-title carrying the same words) must
**not** produce two identical nav entries — when the folded title equals the
survivor's title, drop it rather than recording it.

**Verify**: `.venv/bin/python -m pytest -q tests/test_epub.py -k "stub"` →
existing merge tests still pass, with assertions updated only where the return
shape changed.

### Step 3: Emit the recovered nav entries

In the loop at `pdf2epub/epub.py:251-260`, after appending the chapter's own
`<li>`, append one `<li>` per carried title, each linking to
`chapters/<name>#<anchor>`. Escape both the title and the anchor.

Keep them as siblings in the flat `<ol>` — nesting is out of scope.

**Verify**: a test asserts that a merged short chapter's title appears in
`nav.xhtml` with a fragment link, and that the total `<li>` count equals the
number of distinct chapter titles before merging.

### Step 4: Confirm the anchors actually resolve

Every fragment link must point at an `id` that exists in the file it names.
Add a test that parses the generated `nav.xhtml`, extracts every
`href="chapters/X#Y"`, opens `X`, and asserts `id="Y"` is present.

This is the check that would have caught the original defect class, and it also
guards the footnote anchors added elsewhere in this module.

**Verify**: `.venv/bin/python -m pytest -q tests/test_epub.py -k "anchor"` →
exit 0.

### Step 5: Full suite and lint

**Verify**:
- `.venv/bin/python -m pytest -q` → exit 0, at least 119 tests pass.
- `.venv/bin/ruff check .` → exit 0, `All checks passed!`
- If EPUBCheck is available, build a real book and confirm it still validates.
  Dangling fragment links are a validation error, so this is a meaningful gate:
  `.venv/bin/python -m pdf2epub.cli <some.pdf> -o /tmp/plan11 --allow-partial`
  → exit 0.

## Test plan

In `tests/test_epub.py`:

- `test_merged_chapter_keeps_its_navigation_entry` — a short chapter folded into
  its predecessor still has an `<li>` in `nav.xhtml`, pointing at a fragment.
- `test_duplicate_title_merge_does_not_duplicate_the_nav_entry` — cover and
  half-title with identical titles yield exactly one entry.
- `test_every_nav_fragment_resolves_to_an_existing_id` — the Step 4 check.
- `test_chapter_ids_are_stable_across_builds` — two builds, byte-identical
  chapter files.
- Existing tests that must stay green, with assertions updated only for the
  changed return shape: `test_stub_front_matter_is_folded_into_one_chapter`,
  `test_a_single_short_chapter_is_left_alone`.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m pytest -q` exits 0 with at least 119 tests passing.
- [ ] `.venv/bin/ruff check .` exits 0 with `All checks passed!`.
- [ ] For any document, the number of `<li>` entries in `nav.xhtml` equals the
      number of distinct chapter titles **before** merging — proven by a test,
      not by inspection.
- [ ] Every `href="chapters/*#*"` in `nav.xhtml` resolves to an `id` that exists
      in the referenced file — proven by a test.
- [ ] `MIN_CHAPTER_TEXT` is unchanged at 240.
- [ ] Building the same PDF twice produces byte-identical chapter files.
- [ ] `git status` shows no modified file outside the In scope list.
- [ ] `plans/README.md` status row for plan 11 updated.

## STOP conditions

Stop and report back instead of improvising if:

- The excerpts in "Current state" do not match the working tree (see the drift
  note at the top).
- EPUBCheck reports a validation error after your change that it did not report
  before. Dangling fragment links are exactly the failure mode this plan risks
  introducing, so do not work around it — report it.
- You find that fixing the navigation requires changing which chapters merge.
  That is out of scope; report the conflict.
- The stable-id requirement cannot be met without a global counter. That
  suggests headings are not uniquely identifiable by page and text, which is a
  design question worth raising rather than papering over.

## Maintenance notes

- The root cause is that one list serves three purposes — files, spine, and
  navigation. This plan works within that structure. If a future change needs
  genuine TOC hierarchy (level-2 headings as nested entries), the right move is
  to build a navigation model separately from the file list, and this plan's
  carried-titles list is the seam where that would start.
- A reviewer should check that the anchor slug cannot collide within one merged
  file — two folded chapters with the same title on the same page would produce
  duplicate ids, which EPUBCheck rejects. The page number in the prefix makes
  that unlikely but not impossible.
- Deliberately deferred: nested navigation for level-2 headings, and surfacing
  merge decisions in the quality report so a reader can see that chapters were
  folded. The latter is cheap and would make this class of change auditable —
  worth doing if merging behaviour is ever tuned again.
