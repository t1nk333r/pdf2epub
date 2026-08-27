import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from pdf2epub.epub import EpubValidationError, build_epub
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
        assert (
            'epub:type="bodymatter"'
            in archive.read("EPUB/chapters/chapter-001.xhtml").decode()
        )
        assert 'epub:type="landmarks"' in archive.read("EPUB/nav.xhtml").decode()
        assert (
            '<meta property="rendition:layout">reflowable</meta>'
            in archive.read("EPUB/package.opf").decode()
        )
        assert "direction:" not in archive.read("EPUB/css/book.css").decode()


def test_internal_figure_is_not_implicitly_marked_as_cover(tmp_path):
    image = tmp_path / "figure.png"
    Image.new("RGB", (20, 20), "white").save(image)
    document = DocumentResult("كتاب", images=[image])
    output = tmp_path / "figure.epub"
    build_epub(document, output, validate=False)
    with zipfile.ZipFile(output) as archive:
        package = archive.read("EPUB/package.opf").decode()
        assert 'href="images/figure.png" media-type="image/png"' in package
        assert "cover-image" not in package


def test_invalid_image_prevents_epub_publication(tmp_path):
    image = tmp_path / "broken.png"
    image.write_bytes(b"not-an-image")
    output = tmp_path / "broken.epub"
    with pytest.raises(EpubValidationError, match="invalid image resource"):
        build_epub(DocumentResult("كتاب", images=[image]), output, validate=False)
    assert not output.exists()


def test_missing_epubcheck_prevents_normal_publication(tmp_path, monkeypatch):
    monkeypatch.setattr("pdf2epub.epub.shutil.which", lambda command: None)
    output = tmp_path / "book.epub"
    with pytest.raises(EpubValidationError, match="required for publication"):
        build_epub(DocumentResult("كتاب"), output)
    assert not output.exists()


def test_epubcheck_receives_an_epub_suffixed_temporary_file(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["path"] = args[1]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pdf2epub.epub.shutil.which", lambda command: "/epubcheck")
    monkeypatch.setattr("pdf2epub.epub.subprocess.run", fake_run)
    output = tmp_path / "book.epub"
    build_epub(DocumentResult("كتاب"), output)
    assert captured["path"].endswith(".epub")
    assert output.exists()
