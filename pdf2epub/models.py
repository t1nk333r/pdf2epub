from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class PageType(StrEnum):
    TEXT = "TEXT"
    SCAN = "SCAN"
    HYBRID = "HYBRID"
    BROKEN_TEXT = "BROKEN_TEXT"
    IMAGE_HEAVY = "IMAGE_HEAVY"


class PageSelection(StrEnum):
    EMBEDDED = "embedded"
    OCR = "ocr"
    REVIEW = "review"
    UNAVAILABLE = "unavailable"
    INTENTIONAL_BLANK = "intentional_blank"


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    IMAGE = "image"
    FOOTNOTE = "footnote"
    POETRY = "poetry"
    LIST = "list"


@dataclass(slots=True)
class TextQuality:
    score: float = 0.0
    arabic_ratio: float = 0.0
    replacement_ratio: float = 0.0
    single_char_ratio: float = 0.0
    suspicious_spacing: bool = False
    reasons: list[str] = field(default_factory=list)
    suspicious_glyph_ratio: float = 0.0


@dataclass(slots=True)
class Block:
    type: BlockType
    text: str = ""
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    confidence: float = 1.0
    font_size: float = 12.0
    bold: bool = False
    image_path: str | None = None
    source: str = "embedded_text"
    heading_level: int = 1
    footnote_id: str | None = None


@dataclass(slots=True)
class PageResult:
    page: int
    page_type: PageType
    source: str
    width: float
    height: float
    rotation: int = 0
    blocks: list[Block] = field(default_factory=list)
    quality: TextQuality = field(default_factory=TextQuality)
    ocr_confidence: float | None = None
    warnings: list[str] = field(default_factory=list)
    selection: PageSelection = PageSelection.EMBEDDED
    selection_reason: str = ""
    candidate_scores: dict[str, float] = field(default_factory=dict)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    ocr_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DocumentResult:
    title: str
    language: str = "ar"
    pages: list[PageResult] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fix_bidi: bool = True

    def report(self) -> dict[str, Any]:
        types = {kind.value: 0 for kind in PageType}
        sources: dict[str, int] = {}
        broken: list[int] = []
        confidences: list[float] = []
        headings = images = footnotes = 0
        for page in self.pages:
            types[page.page_type.value] += 1
            sources[page.source] = sources.get(page.source, 0) + 1
            if page.page_type == PageType.BROKEN_TEXT or page.warnings:
                broken.append(page.page)
            if page.ocr_confidence is not None:
                confidences.append(page.ocr_confidence)
            for block in page.blocks:
                headings += block.type == BlockType.HEADING
                images += block.type == BlockType.IMAGE
                footnotes += block.type == BlockType.FOOTNOTE
        unavailable = [
            page.page
            for page in self.pages
            if page.selection == PageSelection.UNAVAILABLE
        ]
        review = [
            page.page for page in self.pages if page.selection == PageSelection.REVIEW
        ]
        return {
            "schema_version": 2,
            "pages": len(self.pages),
            "page_types": types,
            "sources": sources,
            "ocr_average_confidence": round(sum(confidences) / len(confidences), 3)
            if confidences
            else None,
            "review_pages": sorted(set(broken)),
            "chapters_detected": headings,
            "images_preserved": images,
            "footnotes_detected": footnotes,
            "warnings": self.warnings,
            "integrity": {
                "status": "failed"
                if unavailable
                else ("review" if review else "passed"),
                "unavailable_pages": unavailable,
                "review_pages": review,
            },
            "page_details": [asdict(p) for p in self.pages],
        }


@dataclass(slots=True)
class ConversionOptions:
    mode: str = "automatic"
    preserve_images: bool = True
    detect_chapters: bool = True
    detect_footnotes: bool = True
    fix_bidi: bool = True
    ocr_bad_pages: bool = True
    ocr_language: str = "ara+eng"
    ocr_provider: str = "tesseract"
    ollama_model: str | None = None
    render_dpi: int = 300
    good_threshold: float = 0.68
    acceptable_threshold: float = 0.48
    allow_partial: bool = False
    require_epubcheck: bool = True
