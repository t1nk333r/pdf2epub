import json
import zipfile

import pymupdf

from pdf2epub.models import ConversionOptions
from pdf2epub.ocr import OcrResult
from pdf2epub.pipeline import convert


class FakeOcrProvider:
    name = "test_provider"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        assert image_png.startswith(b"\x89PNG")
        assert language == "ara+eng"
        return OcrResult("الفصل الأول\nهذا نص من محرك خارجي.", 0.97, self.name)


def test_custom_provider_runs_through_complete_pipeline(tmp_path):
    source = tmp_path / "scan.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(source)
    pdf.close()

    outputs = convert(
        source,
        tmp_path / "output",
        ConversionOptions(mode="force_ocr", preserve_images=False),
        ocr_provider=FakeOcrProvider(),
    )

    report = json.loads(outputs["report"].read_text())
    assert report["sources"] == {"ocr:test_provider": 1}
    assert report["ocr_average_confidence"] == 0.97
    with zipfile.ZipFile(outputs["epub"]) as epub:
        chapter = epub.read("EPUB/chapters/chapter-001.xhtml").decode()
        assert "هذا نص من محرك خارجي" in chapter
