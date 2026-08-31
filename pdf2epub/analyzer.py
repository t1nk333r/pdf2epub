from __future__ import annotations

import io
import re
import statistics
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .checkpoints import ConversionControl
from .models import (
    Block,
    BlockType,
    ConversionOptions,
    DocumentResult,
    PageResult,
    PageSelection,
    PageType,
)
from .ocr import OcrError, OcrProvider, create_ocr_provider
from .text import (
    BROKEN_EXTRACTION_REASONS,
    HEADING_RE,
    SEVERE_TEXT_REASONS,
    evaluate_text,
    normalize_arabic,
    ocr_covers_embedded,
)

Progress = Callable[[str, int, int], None]
EPUB_IMAGE_EXTENSIONS = {"gif", "jpeg", "jpg", "png", "svg"}


class MissingDependency(RuntimeError):
    pass


def _fitz():
    try:
        import pymupdf as fitz

        return fitz
    except ImportError as exc:
        raise MissingDependency(
            "PyMuPDF is required; install the project dependencies"
        ) from exc


def _visual_text_lines(pdf_lines: list[dict]) -> list[str]:
    """Merge PDF word fragments that occupy the same visual baseline."""
    rows: list[dict[str, object]] = []
    for line in pdf_lines:
        text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
        if not text:
            continue
        x0, y0, _x1, y1 = line.get("bbox", (0, 0, 0, 0))
        height = max(float(y1) - float(y0), 1.0)
        center = (float(y0) + float(y1)) / 2
        row = next(
            (
                candidate
                for candidate in rows
                if abs(center - float(candidate["center"]))
                <= max(height, float(candidate["height"])) * 0.35
            ),
            None,
        )
        if row is None:
            rows.append(
                {
                    "center": center,
                    "height": height,
                    "pieces": [(float(x0), text)],
                }
            )
        else:
            pieces = row["pieces"]
            assert isinstance(pieces, list)
            pieces.append((float(x0), text))
            count = len(pieces)
            row["center"] = (float(row["center"]) * (count - 1) + center) / count
            row["height"] = max(float(row["height"]), height)

    output: list[str] = []
    for row in sorted(rows, key=lambda item: float(item["center"])):
        pieces = row["pieces"]
        assert isinstance(pieces, list)
        # PyMuPDF preserves the PDF's content order, which is the safest order for
        # Arabic text containing occasional Latin fragments.
        output.append(" ".join(text for _x0, text in pieces))
    return output


def _raw_blocks(page) -> list[Block]:
    blocks: list[Block] = []
    data = page.get_text("dict", sort=False)
    for item in data.get("blocks", []):
        if item.get("type") != 0:
            continue
        lines = _visual_text_lines(item.get("lines", []))
        sizes: list[float] = []
        bold = False
        for line in item.get("lines", []):
            spans = line.get("spans", [])
            sizes.extend(
                float(span.get("size", 12))
                for span in spans
                if span.get("text", "").strip()
            )
            bold |= any("bold" in span.get("font", "").lower() for span in spans)
        text = normalize_arabic("\n".join(lines))
        if text:
            blocks.append(
                Block(
                    BlockType.PARAGRAPH,
                    text,
                    tuple(item.get("bbox", (0, 0, 0, 0))),
                    1.0,
                    statistics.median(sizes) if sizes else 12,
                    bold,
                )
            )
    return blocks


def _reading_order(blocks: list[Block], width: float) -> list[Block]:
    """Approximate RTL column order, then top-to-bottom within each column."""
    if len(blocks) < 3:
        return sorted(blocks, key=lambda b: (b.bbox[1], -b.bbox[0]))
    left = [b for b in blocks if (b.bbox[0] + b.bbox[2]) / 2 < width / 2]
    right = [b for b in blocks if b not in left]
    wide = [b for b in blocks if b.bbox[2] - b.bbox[0] > width * 0.65]
    if len(left) >= 2 and len(right) >= 2 and len(wide) < len(blocks) / 2:
        return sorted(right, key=lambda b: b.bbox[1]) + sorted(
            left, key=lambda b: b.bbox[1]
        )
    return sorted(blocks, key=lambda b: (b.bbox[1], -b.bbox[0]))


# A contents entry is a title followed by the page it points at, usually padded
# with dot leaders. OCR often drops the leaders, so they cannot be required.
CONTENTS_ENTRY_RE = re.compile(r"(?:\.{3,}\s*)?[\u0660-\u06690-9]{1,4}\s*$")
DOT_LEADER_RE = re.compile(r"\.{4,}")


def _contents_entries(lines: list[str]) -> int:
    return sum(
        bool(CONTENTS_ENTRY_RE.search(line)) and len(line.split()) >= 2
        for line in lines
    )


def _looks_like_contents(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    return _contents_entries(lines) >= max(2, len(lines) // 2)


def _is_contents_page(blocks: list[Block]) -> bool:
    """A contents page lists destinations, so nothing on it opens a chapter."""
    text = "\n".join(block.text for block in blocks)
    # Dot leaders are the strongest signal and survive OCR reflowing entries
    # onto shared lines, which defeats any per-line rule.
    if len(DOT_LEADER_RE.findall(text)) >= 3:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 4:
        return False
    return _contents_entries(lines) >= 0.6 * len(lines)


def _classify_structure(
    blocks: list[Block], height: float, options: ConversionOptions
) -> None:
    expanded: list[Block] = []
    for block in blocks:
        lines = block.text.splitlines()
        if (
            options.detect_chapters
            and len(lines) > 1
            and HEADING_RE.match(lines[0].strip())
        ):
            block.text = lines[0].strip()
            block.type = BlockType.HEADING
            expanded.append(block)
            expanded.append(
                Block(
                    BlockType.PARAGRAPH,
                    "\n".join(lines[1:]),
                    block.bbox,
                    block.confidence,
                    block.font_size,
                    block.bold,
                    source=block.source,
                )
            )
        else:
            expanded.append(block)
    blocks[:] = expanded
    sizes = [b.font_size for b in blocks if b.text]
    body_size = statistics.median(sizes) if sizes else 12
    contents_page = _is_contents_page(blocks)
    for block in blocks:
        compact = block.text.replace("\n", " ").strip()
        chapter_opener = bool(HEADING_RE.match(compact))
        if (
            options.detect_chapters
            and not contents_page
            and not _looks_like_contents(block.text)
            and (
                chapter_opener
                or (block.font_size >= body_size * 1.28 and len(compact) < 140)
            )
        ):
            block.type = BlockType.HEADING
            # Only a recognised chapter opener, or markedly larger type, starts
            # a new EPUB file. Anything else is a subheading inside the current
            # one, so a title page cannot fragment the book into stubs.
            block.heading_level = (
                1
                if chapter_opener or block.font_size >= body_size * 1.6
                else 2
            )
        elif (
            options.detect_footnotes
            and block.bbox[1] > height * 0.78
            and block.font_size < body_size * 0.9
        ):
            block.type = BlockType.FOOTNOTE
        elif (
            block.text.count("\n") >= 2
            and statistics.mean(len(line) for line in block.text.splitlines() if line)
            < 55
            and not re.search(r"[.!؟؛:]\s*$", block.text)
        ):
            block.type = BlockType.POETRY
        elif block.type == BlockType.PARAGRAPH:
            # A PDF line ending is usually layout, not a semantic paragraph break.
            block.text = re.sub(
                r"(?<![.!؟؛:])\n(?!\n|[-•٠-٩0-9]+[.)])", " ", block.text
            )


def _merge_paragraph_lines(blocks: list[Block]) -> None:
    """Turn consecutive PDF line boxes into reflowable prose paragraphs."""
    paragraphs = [block for block in blocks if block.type == BlockType.PARAGRAPH]
    if len(paragraphs) < 2:
        return
    widths = [block.bbox[2] - block.bbox[0] for block in paragraphs]
    heights = [block.bbox[3] - block.bbox[1] for block in paragraphs]
    typical_width = statistics.median(widths)
    typical_height = max(statistics.median(heights), 1.0)
    merged: list[Block] = []
    last_line: Block | None = None
    for block in blocks:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and last_line is not None
            and all(
                (
                    previous.type == BlockType.PARAGRAPH,
                    block.type == BlockType.PARAGRAPH,
                    last_line.type == BlockType.PARAGRAPH,
                    last_line.bbox[2] - last_line.bbox[0] >= typical_width * 0.72,
                    -typical_height * 0.2
                    <= block.bbox[1] - last_line.bbox[3]
                    <= typical_height * 0.75,
                )
            )
        ):
            previous.text = f"{previous.text.rstrip()} {block.text.lstrip()}"
            previous.bbox = (
                min(previous.bbox[0], block.bbox[0]),
                min(previous.bbox[1], block.bbox[1]),
                max(previous.bbox[2], block.bbox[2]),
                max(previous.bbox[3], block.bbox[3]),
            )
        else:
            merged.append(block)
        last_line = block
    blocks[:] = merged


def _remove_repeated_margins(document: DocumentResult) -> None:
    if len(document.pages) < 4:
        return
    candidates: Counter[str] = Counter()
    for page in document.pages:
        seen: set[str] = set()
        for block in page.blocks:
            normalized = re.sub(r"\d+", "#", block.text.strip())
            if (
                normalized
                and len(normalized) < 160
                and (
                    block.bbox[1] < page.height * 0.1
                    or block.bbox[3] > page.height * 0.9
                )
            ):
                seen.add(normalized)
        candidates.update(seen)
    repeated = {
        text
        for text, count in candidates.items()
        if count >= max(3, round(len(document.pages) * 0.4))
    }
    if not repeated:
        return
    for page in document.pages:
        page.blocks = [
            b
            for b in page.blocks
            if not (
                (b.bbox[1] < page.height * 0.1 or b.bbox[3] > page.height * 0.9)
                and re.sub(r"\d+", "#", b.text.strip()) in repeated
            )
        ]


def _ocr_page(
    page, dpi: int, language: str, provider: OcrProvider
) -> tuple[list[Block], float | None, str, dict]:
    scale = dpi / 72
    pix = page.get_pixmap(
        matrix=_fitz().Matrix(scale, scale), colorspace=_fitz().csRGB, alpha=False
    )
    result = provider.recognize(pix.tobytes("png"), language)
    text = normalize_arabic(result.text)
    block_confidence = (
        result.confidence
        if result.confidence is not None
        else evaluate_text(text).score
    )
    blocks = [
        Block(
            BlockType.PARAGRAPH,
            normalize_arabic(region.text),
            tuple(coordinate / scale for coordinate in region.bbox),
            region.confidence if region.confidence is not None else block_confidence,
            source=f"ocr:{result.provider}",
        )
        for region in result.regions
        if region.text.strip()
    ]
    if not blocks and text:
        blocks = _structured_ocr_blocks(
            text,
            (0, 0, page.rect.width, page.rect.height),
            block_confidence,
            f"ocr:{result.provider}",
        )
    metrics = {
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "output_tokens": result.output_tokens,
        "duration_seconds": result.duration_seconds,
        "retries": result.retries,
        "attempts": result.attempts,
        "fallback_reason": result.fallback_reason,
        "fallback_used": len(result.attempts) > 1,
    }
    return (
        blocks,
        result.confidence,
        result.provider,
        metrics,
    )


def _structured_ocr_blocks(
    text: str,
    bbox: tuple[float, float, float, float],
    confidence: float,
    source: str,
) -> list[Block]:
    """Convert conservative OCR Markdown conventions into semantic blocks."""
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(
                Block(
                    BlockType.PARAGRAPH,
                    "\n".join(paragraph),
                    bbox,
                    confidence,
                    source=source,
                )
            )
            paragraph.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            continue
        heading = re.match(r"^(#{1,2})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            blocks.append(
                Block(
                    BlockType.HEADING,
                    heading.group(2).strip(),
                    bbox,
                    confidence,
                    source=source,
                    heading_level=len(heading.group(1)),
                )
            )
            continue
        footnote = re.match(r"^\[\^([^\]]+)\]\s*:?[ \t]*(.+)$", stripped)
        if footnote:
            flush_paragraph()
            blocks.append(
                Block(
                    BlockType.FOOTNOTE,
                    footnote.group(2).strip(),
                    bbox,
                    confidence,
                    source=source,
                    footnote_id=footnote.group(1),
                )
            )
            continue
        paragraph.append(stripped)
    flush_paragraph()
    return blocks


def _has_severe_text_issue(blocks: list[Block]) -> bool:
    return any(
        SEVERE_TEXT_REASONS.intersection(evaluate_text(block.text).reasons)
        for block in blocks
        if block.text.strip()
    )


def _save_epub_image(doc, xref: int, image_dir: Path, basename: str) -> Path:
    extracted = doc.extract_image(xref)
    extension = extracted["ext"].lower()
    data = extracted["image"]
    if extension in EPUB_IMAGE_EXTENSIONS:
        target = image_dir / f"{basename}.{extension}"
        target.write_bytes(data)
        return target
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            target = image_dir / f"{basename}.png"
            image.save(target, format="PNG", optimize=True)
            return target
    except Exception:  # noqa: BLE001 - PyMuPDF is the fallback decoder
        fitz = _fitz()
        pixmap = fitz.Pixmap(doc, xref)
        if pixmap.colorspace and pixmap.colorspace.n > 3:
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
        target = image_dir / f"{basename}.png"
        target.write_bytes(pixmap.tobytes("png"))
        return target


def _is_safe_figure_image(
    page, xref: int, total_image_area: float, page_area: float
) -> bool:
    """Allow genuine figures while rejecting page scans and tiled page rasters."""
    if total_image_area >= page_area * 0.55:
        return False
    try:
        rects = page.get_image_rects(xref)
    except Exception:  # noqa: BLE001 - uncertain placement must not leak a PDF page
        return False
    if not rects:
        return False
    own_area = sum(max(rect.width, 0) * max(rect.height, 0) for rect in rects)
    if own_area >= page_area * 0.28:
        return False
    page_width = max(page.rect.width, 1)
    page_height = max(page.rect.height, 1)
    return not any(
        rect.width >= page_width * 0.72 or rect.height >= page_height * 0.52
        for rect in rects
    )


def analyze_pdf(
    path: Path,
    work_dir: Path,
    options: ConversionOptions,
    progress: Progress | None = None,
    ocr_provider: OcrProvider | None = None,
    control: ConversionControl | None = None,
) -> DocumentResult:
    fitz = _fitz()
    doc = fitz.open(path)
    try:
        result = (
            control.store.reconstruct(control.manifest)
            if control is not None
            else DocumentResult(title=path.stem, fix_bidi=options.fix_bidi)
        )
    except Exception:
        doc.close()
        raise
    image_dir = control.store.images if control is not None else work_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    try:
        for index, page in enumerate(doc):
            if index < len(result.pages):
                continue
            if control is not None:
                control.manifest.current_page = index + 1
                control.store.write_manifest(control.manifest)
            if progress:
                progress("Analyzing pages", index, len(doc))
            blocks = _raw_blocks(page)
            embedded = "\n".join(b.text for b in blocks)
            quality = evaluate_text(embedded)
            embedded_blocks = blocks
            embedded_has_severe_issue = _has_severe_text_issue(embedded_blocks)
            images = page.get_images(full=True)
            page_area = max(page.rect.width * page.rect.height, 1)
            image_area = 0.0
            for info in images:
                try:
                    image_area += sum(
                        rect.width * rect.height for rect in page.get_image_rects(info[0])
                    )
                except Exception:  # noqa: BLE001, S110 - malformed PDF image metadata varies
                    pass
            image_heavy = bool(images and image_area > page_area * 0.35)
            source = "embedded_text"
            kind = PageType.TEXT
            selection = PageSelection.EMBEDDED
            selection_reason = "Embedded text passed the quality policy"
            candidate_scores = {"embedded_text": quality.score}
            candidates = {
                "embedded_text": {
                    "text": embedded,
                    "score": quality.score,
                    "reasons": quality.reasons,
                }
            }
            selected_quality = quality
            try:
                intentional_blank = (
                    options.mode != "force_ocr"
                    and not embedded
                    and not images
                    and not page.get_drawings()
                )
            except Exception:  # noqa: BLE001 - malformed drawing streams are not blank proof
                intentional_blank = False
            use_ocr = not intentional_blank and (
                options.mode == "force_ocr"
                or not embedded
                or (
                    options.mode == "automatic"
                    and options.ocr_bad_pages
                    and (
                        quality.score < options.acceptable_threshold
                        or embedded_has_severe_issue
                        or bool(
                            BROKEN_EXTRACTION_REASONS.intersection(quality.reasons)
                        )
                    )
                )
            )
            warnings: list[str] = []
            ocr_confidence = None
            ocr_metrics: dict = {}
            if intentional_blank:
                blocks = []
                source = "intentional_blank"
                kind = PageType.TEXT
                selection = PageSelection.INTENTIONAL_BLANK
                selection_reason = "No text, images, or vector drawings were present"
            elif use_ocr:
                try:
                    if ocr_provider is None:
                        ocr_provider = create_ocr_provider(
                            options.ocr_provider, ollama_model=options.ollama_model
                        )
                    if progress:
                        # Tesseract has no model to name; every other provider
                        # does, and which one ran is the first thing you want
                        # to know when a page comes back wrong.
                        model_name = getattr(ocr_provider, "model", None)
                        via = f"OCR page {index + 1} via {ocr_provider.name}"
                        progress(
                            f"{via} — {model_name}" if model_name else via,
                            index,
                            len(doc),
                        )
                    ocr_blocks, ocr_confidence, provider_name, ocr_metrics = _ocr_page(
                        page, options.render_dpi, options.ocr_language, ocr_provider
                    )
                    if progress:
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
                    ocr_text = "\n".join(block.text for block in ocr_blocks)
                    ocr_quality = evaluate_text(ocr_text)
                    candidate_scores[f"ocr:{provider_name}"] = ocr_quality.score
                    candidates[f"ocr:{provider_name}"] = {
                        "text": ocr_text,
                        "score": ocr_quality.score,
                        "reasons": ocr_quality.reasons,
                        "confidence": ocr_confidence,
                        "metrics": ocr_metrics,
                    }
                    ocr_has_severe_issue = _has_severe_text_issue(ocr_blocks)
                    # A quality score cannot tell a faithful read from an
                    # invented one, so OCR must also account for the text the
                    # page already had before it is allowed to replace it.
                    ocr_covers_page = ocr_covers_embedded(embedded, ocr_text)
                    choose_ocr = bool(ocr_blocks) and ocr_covers_page and (
                        options.mode == "force_ocr"
                        or (
                            not embedded
                            and not ocr_has_severe_issue
                            and ocr_quality.score >= options.acceptable_threshold
                        )
                        or (
                            embedded_has_severe_issue
                            and not ocr_has_severe_issue
                            and ocr_quality.score >= options.acceptable_threshold
                        )
                        or ocr_quality.score >= quality.score + 0.08
                        or (
                            bool(quality.reasons)
                            and not ocr_has_severe_issue
                            and ocr_quality.score >= quality.score
                            and ocr_quality.score >= options.acceptable_threshold
                        )
                    )
                    if choose_ocr:
                        blocks = ocr_blocks
                        selected_quality = ocr_quality
                        source = f"ocr:{provider_name}"
                        kind = PageType.SCAN if not embedded else PageType.BROKEN_TEXT
                        selection = PageSelection.OCR
                        selection_reason = (
                            "OCR was the only usable candidate"
                            if not embedded
                            else "OCR passed quality checks that embedded text failed"
                        )
                    elif embedded:
                        blocks = embedded_blocks
                        source = "embedded_text"
                        kind = PageType.BROKEN_TEXT
                        selection = PageSelection.REVIEW
                        if not ocr_covers_page:
                            selection_reason = (
                                "OCR did not account for the text already on "
                                "the page"
                            )
                            warnings.append(
                                "OCR dropped or replaced page content; "
                                "retained embedded text"
                            )
                        else:
                            selection_reason = (
                                "OCR did not provide a clearly safer candidate"
                            )
                            warnings.append(
                                "Embedded and OCR candidates require review; "
                                "retained embedded text"
                            )
                    else:
                        blocks = []
                        source = "unavailable"
                        kind = PageType.SCAN
                        selection = PageSelection.UNAVAILABLE
                        selection_reason = "OCR returned no usable text"
                        warnings.append("OCR returned no usable text")
                except (MissingDependency, OcrError) as exc:
                    if progress:
                        progress(
                            f"OCR page {index + 1} failed — {exc}", index, len(doc)
                        )
                    if embedded:
                        blocks = embedded_blocks
                        warnings.append(
                            f"OCR unavailable; retained low-quality embedded text: {exc}"
                        )
                        kind = PageType.BROKEN_TEXT
                        selection = PageSelection.REVIEW
                        selection_reason = (
                            "OCR was unavailable for questionable embedded text"
                        )
                    else:
                        warnings.append(str(exc))
                        blocks = []
                        source = "unavailable"
                        kind = PageType.SCAN
                        selection = PageSelection.UNAVAILABLE
                        selection_reason = "No embedded text and OCR was unavailable"
            elif quality.score < options.good_threshold:
                if quality.reasons:
                    kind = PageType.BROKEN_TEXT
                    selection = PageSelection.REVIEW
                    selection_reason = (
                        "Embedded text is below the good-quality threshold"
                    )
                    warnings.append("Embedded text is only moderately reliable")
                else:
                    # The score is held down by page length alone. A short page
                    # with no detected defect is not a page that needs review.
                    selection_reason = (
                        "Embedded text is short but shows no quality defects"
                    )
            elif image_heavy:
                kind = PageType.HYBRID if embedded else PageType.IMAGE_HEAVY
            blocks = _reading_order(blocks, page.rect.width)
            _classify_structure(blocks, page.rect.height, options)
            _merge_paragraph_lines(blocks)
            if options.preserve_images and selection in {
                PageSelection.EMBEDDED,
                PageSelection.REVIEW,
            }:
                for image_index, info in enumerate(images):
                    try:
                        if not _is_safe_figure_image(page, info[0], image_area, page_area):
                            continue
                        extracted = doc.extract_image(info[0])
                        if len(extracted["image"]) < 4096:
                            continue
                        target = _save_epub_image(
                            doc,
                            info[0],
                            image_dir,
                            f"page-{index + 1:04d}-{image_index + 1}",
                        )
                        result.images.append(target)
                        blocks.append(
                            Block(
                                BlockType.IMAGE,
                                image_path=target.name,
                                source="embedded_image",
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - one corrupt image must not abort a book
                        warnings.append(
                            f"Could not preserve image {image_index + 1}: {exc}"
                        )
            page_result = PageResult(
                    index + 1,
                    kind,
                    source,
                    page.rect.width,
                    page.rect.height,
                    page.rotation,
                    blocks,
                    selected_quality,
                    ocr_confidence,
                    warnings,
                    selection,
                    selection_reason,
                    candidate_scores,
                    candidates,
                    ocr_metrics,
                )
            result.pages.append(page_result)
            if control is not None:
                control.page_committed(page_result)
        _remove_repeated_margins(result)
        if progress:
            progress("Analyzing pages", len(doc), len(doc))
        return result
    finally:
        doc.close()
