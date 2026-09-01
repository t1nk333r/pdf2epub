"""Characterization tests for the candidate-selection branches in
`analyze_pdf` (pdf2epub/analyzer.py) that plans/17 found uncovered:
intentional-blank pages, image-heavy page typing, the two retained-embedded
REVIEW outcomes, and OCR-returned-nothing UNAVAILABLE.

These pin down what the code does today, ahead of the planned extraction of
the selection policy into a pure function. Model/harness reused from
tests/test_pipeline_ocr.py: build a small pymupdf doc, run
convert(source, out, ConversionOptions(...), ocr_provider=...), assert on
json.loads(outputs["report"].read_text())["page_details"]. Latin filler text
is used throughout (pymupdf.insert_text is not Arabic-shaping-safe, per the
plan-13 constraint); the selection logic under test is script-agnostic.
"""

import json

import pymupdf
import pytest
from PIL import Image

from pdf2epub.models import ConversionOptions
from pdf2epub.ocr import OcrResult
from pdf2epub.pipeline import ConversionIntegrityError, convert


class FakeOcrProvider:
    """A minimal call-counting OCR provider for pages that must never call OCR."""

    name = "test_provider"

    def __init__(self):
        self.calls = 0

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        self.calls += 1
        return OcrResult(
            "some ocr text that should never be needed", 0.9, self.name
        )


# Scratch probe (via _raw_blocks + evaluate_text on this exact text): visible
# length 96, score 0.797, reasons []. Comfortably above good_threshold (0.68)
# and acceptable_threshold (0.48) with no defects, so neighbouring pages stay
# plain "embedded" and never call OCR — isolating the blank page in the middle.
_NEIGHBOUR_TEXT_LINES = [
    "Lorem ipsum dolor sit amet consectetur",
    "adipiscing elit sed do eiusmod tempor",
    "incididunt ut labore et dolore magna",
]


def test_truly_blank_page_is_intentional_blank(tmp_path):
    # Page 2 has no embedded text, no images, and no vector drawings: the only
    # page in the document with nothing at all on it.
    source = tmp_path / "blank.pdf"
    pdf = pymupdf.open()
    first = pdf.new_page()
    first.insert_text((72, 72), _NEIGHBOUR_TEXT_LINES, fontsize=9)
    pdf.new_page()
    third = pdf.new_page()
    third.insert_text((72, 72), _NEIGHBOUR_TEXT_LINES, fontsize=9)
    pdf.save(source)
    pdf.close()

    provider = FakeOcrProvider()
    outputs = convert(
        source,
        tmp_path / "output",
        ConversionOptions(preserve_images=False, require_epubcheck=False),
        ocr_provider=provider,
    )

    report = json.loads(outputs["report"].read_text())
    blank_detail = report["page_details"][1]
    assert blank_detail["selection"] == "intentional_blank"
    assert blank_detail["page_type"] == "TEXT"
    assert blank_detail["source"] == "intentional_blank"
    assert (
        blank_detail["selection_reason"]
        == "No text, images, or vector drawings were present"
    )
    # Neither the blank page nor its high-quality neighbours ever called OCR.
    assert provider.calls == 0
    assert report["integrity"]["status"] == "passed"


class RealTextOcrProvider:
    """Returns a plausible transcription, used where OCR must succeed."""

    name = "scan_reader"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        return OcrResult(
            "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
            "eiusmod tempor incididunt ut labore",
            0.9,
            self.name,
            model="scan-model",
        )


def test_image_heavy_page_types(tmp_path):
    # A raster covering ~42% of a Letter page (450x450 = 202500 out of the
    # ~484704 page area, both well past the 35% image_heavy threshold).
    image_path = tmp_path / "big.png"
    Image.new("RGB", (400, 400), "red").save(image_path)
    big_image_rect = pymupdf.Rect(50, 50, 500, 500)

    # Case 1: image-heavy page that ALSO carries good embedded text. Quality
    # is high enough (score 0.797, see probe above) that OCR is never
    # triggered, so the elif image_heavy branch is reached and embedded text
    # is truthy -> HYBRID.
    hybrid_source = tmp_path / "hybrid.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_image(big_image_rect, filename=image_path)
    page.insert_text((72, 72), _NEIGHBOUR_TEXT_LINES, fontsize=9)
    pdf.save(hybrid_source)
    pdf.close()

    hybrid_outputs = convert(
        hybrid_source,
        tmp_path / "hybrid-output",
        ConversionOptions(preserve_images=False, require_epubcheck=False),
        ocr_provider=FakeOcrProvider(),
    )
    hybrid_report = json.loads(hybrid_outputs["report"].read_text())
    hybrid_detail = hybrid_report["page_details"][0]
    assert hybrid_detail["page_type"] == "HYBRID"
    assert hybrid_detail["selection"] == "embedded"

    # Case 2: the same image-heavy page but with NO embedded text at all.
    # image_heavy alone can never reach `kind = ... else PageType.IMAGE_HEAVY`
    # here: `not embedded` unconditionally forces `use_ocr = True` (it is one
    # of the top-level `or` terms), unless `intentional_blank` is True — and
    # intentional_blank requires `not images`, which directly contradicts an
    # image-heavy page. So whenever embedded is falsy, the analyzer always
    # takes the `elif use_ocr:` branch first and PageType.IMAGE_HEAVY is
    # unreachable through analyze_pdf's normal control flow. Characterizing
    # what actually happens instead: OCR runs, succeeds, and wins the page.
    no_text_source = tmp_path / "no_text.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_image(big_image_rect, filename=image_path)
    pdf.save(no_text_source)
    pdf.close()

    provider = RealTextOcrProvider()
    no_text_outputs = convert(
        no_text_source,
        tmp_path / "no-text-output",
        ConversionOptions(preserve_images=False, require_epubcheck=False),
        ocr_provider=provider,
    )
    no_text_report = json.loads(no_text_outputs["report"].read_text())
    no_text_detail = no_text_report["page_details"][0]
    assert no_text_detail["selection"] == "ocr"
    assert no_text_detail["page_type"] == "SCAN"
    assert no_text_detail["source"] == "ocr:scan_reader"
    assert no_text_detail["selection_reason"] == "OCR was the only usable candidate"


_SPLIT_LETTERS = " ".join(("abcdefghijklmnopqrstuvwxyz" * 3)[:70])

# Scratch probe (via _raw_blocks + evaluate_text): two lines of a 260-char
# single-character-joined string produce embedded text of length 259 (>= the
# 200-char MIN_COMPARABLE_TEXT floor that activates the OCR coverage gate),
# score 0.547, reasons ['excessive one-character tokens'] (a
# BROKEN_EXTRACTION_REASONS member, so it triggers OCR under automatic mode
# even though the score itself is above acceptable_threshold).
_COVERAGE_GATE_SPLIT_TEXT = " ".join(("abcdefghijklmnopqrstuvwxyz" * 12)[:260])


class TinyFractionOcrProvider:
    """Returns far too little text to plausibly cover a long embedded page."""

    name = "tiny"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        return OcrResult("Short answer.", 0.9, self.name, model="tiny-model")


def test_ocr_that_fails_coverage_leaves_embedded_for_review(tmp_path):
    # The embedded layer is long enough (259 chars) that ocr_covers_embedded
    # actually compares lengths/word-overlap instead of short-circuiting, and
    # a 14-character OCR reply fails that comparison outright.
    source = tmp_path / "coverage.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text(
        (72, 72),
        [_COVERAGE_GATE_SPLIT_TEXT[:130], _COVERAGE_GATE_SPLIT_TEXT[130:260]],
        fontsize=8,
    )
    pdf.save(source)
    pdf.close()
    options = ConversionOptions(preserve_images=False, require_epubcheck=False)
    options.allow_partial = True

    outputs = convert(
        source, tmp_path / "output", options, ocr_provider=TinyFractionOcrProvider()
    )

    report = json.loads(outputs["report"].read_text())
    detail = report["page_details"][0]
    assert detail["selection"] == "review"
    assert detail["source"] == "embedded_text"
    assert (
        detail["selection_reason"]
        == "OCR did not account for the text already on the page"
    )
    assert report["integrity"]["status"] == "review"
    assert report["integrity"]["review_pages"] == [1]


class WeakerOcrProvider:
    """Returns a short, plausible-enough transcription that still scores worse
    than the (short, defect-flagged) embedded text — never crossing the
    +0.08 margin or matching-or-better bar that choose_ocr requires."""

    name = "weaker"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        return OcrResult("a", 0.5, self.name, model="weak-model")


def test_ocr_no_better_than_embedded_leaves_review(tmp_path):
    # Scratch probe (via _raw_blocks + evaluate_text on _SPLIT_LETTERS[:70]):
    # embedded length 69 (< 200, so ocr_covers_embedded short-circuits True
    # regardless of what OCR returns), score 0.3107,
    # reasons ['excessive one-character tokens']. WeakerOcrProvider's "a"
    # scores 0.3823 (length_score = 1/80, score = 0.42*0.0125 + 0.58*0.65).
    # 0.3823 is below quality.score + 0.08 (0.3907) and below
    # acceptable_threshold (0.48), so every choose_ocr disjunct is False.
    source = tmp_path / "no_better.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), [_SPLIT_LETTERS[:70]], fontsize=9)
    pdf.save(source)
    pdf.close()
    options = ConversionOptions(preserve_images=False, require_epubcheck=False)
    options.allow_partial = True

    outputs = convert(
        source, tmp_path / "output", options, ocr_provider=WeakerOcrProvider()
    )

    report = json.loads(outputs["report"].read_text())
    detail = report["page_details"][0]
    assert detail["selection"] == "review"
    assert detail["source"] == "embedded_text"
    assert detail["selection_reason"] == "OCR did not provide a clearly safer candidate"
    assert report["integrity"]["status"] == "review"
    assert report["integrity"]["review_pages"] == [1]


class WhitespaceOnlyOcrProvider:
    """OCR ran successfully but read nothing usable off the page."""

    name = "empty"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        return OcrResult("   \n  ", 0.9, self.name, model="empty-model")


def test_empty_ocr_with_no_embedded_text_is_unavailable(tmp_path):
    # force_ocr with a page with no embedded text and no images: OCR is the
    # only candidate, and it comes back with only whitespace, so no blocks
    # are produced and the page has no embedded fallback either.
    source = tmp_path / "empty.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(source)
    pdf.close()
    output = tmp_path / "output"
    options = ConversionOptions(
        mode="force_ocr", preserve_images=False, require_epubcheck=False
    )

    with pytest.raises(ConversionIntegrityError, match="No usable text") as raised:
        convert(source, output, options, ocr_provider=WhitespaceOnlyOcrProvider())

    assert raised.value.pages == [1]
    report = json.loads(raised.value.report_path.read_text())
    detail = report["page_details"][0]
    assert detail["selection"] == "unavailable"
    assert "OCR returned no usable text" in detail["warnings"]
