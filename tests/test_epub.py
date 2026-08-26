import zipfile

from pdf2epub.epub import build_epub
from pdf2epub.models import Block, BlockType, DocumentResult, PageResult, PageType


def test_epub_has_required_structure(tmp_path):
    document = DocumentResult(
        "كتاب",
        pages=[
            PageResult(
                1,
                PageType.TEXT,
                "embedded_text",
                600,
                800,
                blocks=[
                    Block(BlockType.HEADING, "الفصل الأول"),
                    Block(BlockType.PARAGRAPH, "نص عربي مع docker compose up"),
                ],
            )
        ],
    )
    output = tmp_path / "book.epub"
    build_epub(document, output, validate=False)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist()[0] == "mimetype"
        assert archive.read("mimetype") == b"application/epub+zip"
        assert (
            'page-progression-direction="rtl"'
            in archive.read("EPUB/package.opf").decode()
        )
        assert (
            "docker compose up"
            in archive.read("EPUB/chapters/chapter-001.xhtml").decode()
        )
        assert "direction:" not in archive.read("EPUB/css/book.css").decode()
