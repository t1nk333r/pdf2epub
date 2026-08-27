import json
import zipfile

import pymupdf
import pytest
from PIL import Image

from pdf2epub.models import ConversionOptions
from pdf2epub.ocr import OcrError, OcrResult
from pdf2epub.pipeline import ConversionIntegrityError, convert


class FakeOcrProvider:
    name = "test_provider"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        assert image_png.startswith(b"\x89PNG")
        assert language == "ara+eng"
        return OcrResult("الفصل الأول\nهذا نص من محرك خارجي.", 0.97, self.name)


class FailingOcrProvider:
    name = "failing"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        raise OcrError("provider unavailable")


def test_custom_provider_runs_through_complete_pipeline(tmp_path):
    source = tmp_path / "scan.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(source)
    pdf.close()

    outputs = convert(
        source,
        tmp_path / "output",
        ConversionOptions(
            mode="force_ocr", preserve_images=False, require_epubcheck=False
        ),
        ocr_provider=FakeOcrProvider(),
    )

    report = json.loads(outputs["report"].read_text())
    assert report["schema_version"] == 2
    assert report["sources"] == {"ocr:test_provider": 1}
    assert report["ocr_average_confidence"] == 0.97
    assert report["integrity"]["status"] == "passed"
    assert report["page_details"][0]["selection"] == "ocr"
    assert (
        report["page_details"][0]["candidates"]["ocr:test_provider"]["confidence"]
        == 0.97
    )
    with zipfile.ZipFile(outputs["epub"]) as epub:
        chapter = epub.read("EPUB/chapters/chapter-001.xhtml").decode()
        assert "هذا نص من محرك خارجي" in chapter


def test_missing_scan_ocr_blocks_normal_epub_publication(tmp_path):
    source = tmp_path / "scan.pdf"
    image_path = tmp_path / "page.png"
    Image.new("RGB", (100, 100), "white").save(image_path)
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_image(page.rect, filename=image_path)
    pdf.save(source)
    pdf.close()

    output = tmp_path / "output"
    with pytest.raises(ConversionIntegrityError) as raised:
        convert(
            source,
            output,
            ConversionOptions(preserve_images=False, require_epubcheck=False),
            ocr_provider=FailingOcrProvider(),
        )

    assert raised.value.pages == [1]
    assert raised.value.report_path.exists()
    assert not (output / "scan.epub").exists()


def test_review_pages_require_explicit_partial_output(tmp_path):
    source = tmp_path / "questionable.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "short embedded text")
    pdf.save(source)
    pdf.close()
    output = tmp_path / "output"
    options = ConversionOptions(preserve_images=False, require_epubcheck=False)

    with pytest.raises(ConversionIntegrityError, match="requires review"):
        convert(source, output, options, ocr_provider=FailingOcrProvider())

    options.allow_partial = True
    outputs = convert(source, output, options, ocr_provider=FailingOcrProvider())
    report = json.loads(outputs["report"].read_text())
    assert report["integrity"]["status"] == "review"
    assert "Partial output was explicitly allowed" in report["warnings"][0]
