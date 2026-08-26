from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .models import (
    Block,
    BlockType,
    ConversionOptions,
    DocumentResult,
    PageResult,
    PageType,
)
from .ocr import OcrError, OcrProvider, create_ocr_provider
from .text import HEADING_RE, evaluate_text, normalize_arabic

Progress = Callable[[str, int, int], None]


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


def _raw_blocks(page) -> list[Block]:
    blocks: list[Block] = []
    data = page.get_text("dict", sort=False)
    for item in data.get("blocks", []):
        if item.get("type") != 0:
            continue
        lines: list[str] = []
        sizes: list[float] = []
        bold = False
        for line in item.get("lines", []):
            spans = line.get("spans", [])
            line_text = "".join(span.get("text", "") for span in spans).strip()
            if line_text:
                lines.append(line_text)
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
    for block in blocks:
        compact = block.text.replace("\n", " ").strip()
        if options.detect_chapters and (
            HEADING_RE.match(compact)
            or (block.font_size >= body_size * 1.28 and len(compact) < 140)
        ):
            block.type = BlockType.HEADING
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
) -> tuple[list[Block], float | None, str]:
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
    return (
        (
            [
                Block(
                    BlockType.PARAGRAPH,
                    text,
                    (0, 0, page.rect.width, page.rect.height),
                    block_confidence,
                    source=f"ocr:{result.provider}",
                )
            ]
            if text
            else []
        ),
        result.confidence,
        result.provider,
    )


def analyze_pdf(
    path: Path,
    work_dir: Path,
    options: ConversionOptions,
    progress: Progress | None = None,
    ocr_provider: OcrProvider | None = None,
) -> DocumentResult:
    fitz = _fitz()
    doc = fitz.open(path)
    result = DocumentResult(title=path.stem, fix_bidi=options.fix_bidi)
    image_dir = work_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(doc):
        if progress:
            progress("Analyzing pages", index, len(doc))
        blocks = _raw_blocks(page)
        embedded = "\n".join(b.text for b in blocks)
        quality = evaluate_text(embedded)
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
        use_ocr = (
            options.mode == "force_ocr"
            or not embedded
            or (
                options.mode == "automatic"
                and options.ocr_bad_pages
                and quality.score < options.acceptable_threshold
            )
        )
        warnings: list[str] = []
        ocr_confidence = None
        if use_ocr:
            try:
                if ocr_provider is None:
                    ocr_provider = create_ocr_provider(options.ocr_provider)
                blocks, ocr_confidence, provider_name = _ocr_page(
                    page, options.render_dpi, options.ocr_language, ocr_provider
                )
                source = f"ocr:{provider_name}"
                kind = PageType.SCAN if not embedded else PageType.BROKEN_TEXT
            except (MissingDependency, OcrError) as exc:
                if embedded:
                    warnings.append(
                        f"OCR unavailable; retained low-quality embedded text: {exc}"
                    )
                    kind = PageType.BROKEN_TEXT
                else:
                    warnings.append(str(exc))
                    blocks = []
                    source = "unavailable"
                    kind = PageType.SCAN
        elif quality.score < options.good_threshold:
            kind = PageType.BROKEN_TEXT
            warnings.append("Embedded text is only moderately reliable")
        elif image_heavy:
            kind = PageType.HYBRID if embedded else PageType.IMAGE_HEAVY
        blocks = _reading_order(blocks, page.rect.width)
        _classify_structure(blocks, page.rect.height, options)
        if options.preserve_images:
            for image_index, info in enumerate(images):
                try:
                    extracted = doc.extract_image(info[0])
                    if len(extracted["image"]) < 4096:
                        continue
                    filename = (
                        f"page-{index + 1:04d}-{image_index + 1}.{extracted['ext']}"
                    )
                    target = image_dir / filename
                    target.write_bytes(extracted["image"])
                    result.images.append(target)
                    blocks.append(
                        Block(
                            BlockType.IMAGE,
                            image_path=filename,
                            source="embedded_image",
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one corrupt image must not abort a book
                    warnings.append(
                        f"Could not preserve image {image_index + 1}: {exc}"
                    )
        result.pages.append(
            PageResult(
                index + 1,
                kind,
                source,
                page.rect.width,
                page.rect.height,
                page.rotation,
                blocks,
                quality,
                ocr_confidence,
                warnings,
            )
        )
    _remove_repeated_margins(result)
    if progress:
        progress("Analyzing pages", len(doc), len(doc))
    doc.close()
    return result
