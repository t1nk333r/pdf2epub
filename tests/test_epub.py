import re
import zipfile
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from PIL import Image

from pdf2epub.epub import EpubValidationError, build_epub
from pdf2epub.models import Block, BlockType, DocumentResult, PageResult, PageType


def _document_with_a_short_chapter_between_two_others() -> DocumentResult:
    # "غلاف" opens the book and survives as-is. "قصيدة" is a genuinely short
    # chapter (a one-line poem) that falls under MIN_CHAPTER_TEXT and gets
    # folded into "غلاف" — its nav entry must survive as a fragment link.
    # "الباب الأول" is long enough to stand on its own.
    return DocumentResult(
        "كتاب",
        pages=[
            PageResult(
                1,
                PageType.TEXT,
                "embedded_text",
                600,
                800,
                blocks=[
                    Block(BlockType.HEADING, "غلاف", heading_level=1),
                    Block(BlockType.HEADING, "قصيدة", heading_level=1),
                    Block(BlockType.PARAGRAPH, "بيت شعر واحد"),
                    Block(BlockType.HEADING, "الباب الأول", heading_level=1),
                    Block(BlockType.PARAGRAPH, "نص طويل جداً " * 40),
                ],
            )
        ],
    )


def _document_with_a_repeated_title() -> DocumentResult:
    # A cover and a half-title, both titled "العنوان", on different pages.
    # The second is long enough to dodge the length-based fold, so only the
    # duplicate-title special case merges them.
    return DocumentResult(
        "كتاب",
        pages=[
            PageResult(
                1,
                PageType.TEXT,
                "embedded_text",
                600,
                800,
                blocks=[
                    Block(BlockType.HEADING, "العنوان", heading_level=1),
                    Block(BlockType.PARAGRAPH, "نص قصير"),
                ],
            ),
            PageResult(
                2,
                PageType.TEXT,
                "embedded_text",
                600,
                800,
                blocks=[
                    Block(BlockType.HEADING, "العنوان", heading_level=1),
                    Block(BlockType.PARAGRAPH, "نص طويل جداً " * 40),
                ],
            ),
        ],
    )


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


def test_semantic_headings_footnotes_and_rtl_body(tmp_path):
    document = DocumentResult(
        "كتاب",
        pages=[
            PageResult(
                1,
                PageType.SCAN,
                "ocr:ollama",
                600,
                800,
                blocks=[
                    Block(BlockType.HEADING, "الفصل", heading_level=1),
                    Block(BlockType.HEADING, "المبحث", heading_level=2),
                    Block(BlockType.PARAGRAPH, "متن الحاشية [^1]"),
                    Block(BlockType.FOOTNOTE, "نص الحاشية", footnote_id="1"),
                ],
            )
        ],
    )
    output = tmp_path / "semantic.epub"
    build_epub(document, output, validate=False)
    with zipfile.ZipFile(output) as archive:
        chapters = "\n".join(
            archive.read(name).decode()
            for name in archive.namelist()
            if name.startswith("EPUB/chapters/")
        )
        assert '<body epub:type="bodymatter" dir="rtl" lang="ar">' in chapters
        assert '<h1 id="ch-1-الفصل">الفصل</h1>' in chapters
        assert "<h2>المبحث</h2>" in chapters
        assert '<a epub:type="noteref" href="#fn-1-1">1</a>' in chapters
        assert 'epub:type="footnote" class="footnote" id="fn-1-1"' in chapters


def test_unnumbered_footnotes_preserve_text_without_duplicate_targets(tmp_path):
    document = DocumentResult(
        "كتاب",
        pages=[
            PageResult(
                number,
                PageType.TEXT,
                "embedded_text",
                600,
                800,
                blocks=[
                    Block(BlockType.PARAGRAPH, "متن الحاشية [^note]"),
                    Block(BlockType.FOOTNOTE, f"حاشية أولى في الصفحة {number}"),
                    Block(BlockType.FOOTNOTE, f"حاشية ثانية في الصفحة {number}"),
                    Block(BlockType.FOOTNOTE, "حاشية مرقمة", footnote_id="note"),
                ],
            )
            for number in (1, 2)
        ],
    )
    output = tmp_path / "footnotes.epub"
    build_epub(document, output, validate=False)
    namespace = {"xhtml": "http://www.w3.org/1999/xhtml"}
    with zipfile.ZipFile(output) as archive:
        chapter = ElementTree.fromstring(
            archive.read("EPUB/chapters/chapter-001.xhtml")
        )
    ids = [element.get("id") for element in chapter.iter() if element.get("id")]
    assert len(ids) == len(set(ids))
    notes = chapter.findall(".//xhtml:aside", namespace)
    assert len(notes) == 6
    for number in (1, 2):
        assert any(note.text == f"حاشية أولى في الصفحة {number}" for note in notes)
        assert any(note.text == f"حاشية ثانية في الصفحة {number}" for note in notes)
    for reference in chapter.findall(".//xhtml:a", namespace):
        assert reference.get("href").removeprefix("#") in ids


def test_duplicate_explicit_footnote_ids_prevent_publication(tmp_path):
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
                    Block(BlockType.FOOTNOTE, "حاشية أولى", footnote_id="1"),
                    Block(BlockType.FOOTNOTE, "حاشية ثانية", footnote_id="1"),
                ],
            )
        ],
    )
    output = tmp_path / "invalid.epub"
    with pytest.raises(EpubValidationError, match="duplicate ID: fn-1-1"):
        build_epub(document, output, validate=False)
    assert not output.exists()


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


def test_stub_front_matter_is_folded_into_one_chapter():
    from pdf2epub.epub import _Chapter, _merge_stub_chapters

    chapters = _merge_stub_chapters(
        [
            _Chapter("العنوان", "<h1>العنوان</h1>", "ch-1-a"),
            _Chapter(
                "العنوان مرة أخرى",
                "<h1>العنوان مرة أخرى</h1><p>المؤلف</p>",
                "ch-1-b",
            ),
            _Chapter(
                "الباب الأول",
                "<h1>الباب الأول</h1><p>" + "نص طويل جداً " * 40 + "</p>",
                "ch-2-c",
            ),
        ]
    )

    assert len(chapters) == 2
    assert chapters[0].title == "العنوان"
    assert "العنوان مرة أخرى" in chapters[0].body
    assert chapters[0].carried == [("العنوان مرة أخرى", "ch-1-b")]
    assert chapters[1].title == "الباب الأول"
    assert chapters[1].carried == []


def test_a_single_short_chapter_is_left_alone():
    from pdf2epub.epub import _Chapter, _merge_stub_chapters

    chapters = _merge_stub_chapters([_Chapter("قصير", "<p>نص</p>", "ch-1-x")])
    assert len(chapters) == 1


def test_merged_chapter_keeps_its_navigation_entry(tmp_path):
    output = tmp_path / "book.epub"
    build_epub(_document_with_a_short_chapter_between_two_others(), output, validate=False)
    with zipfile.ZipFile(output) as archive:
        nav = archive.read("EPUB/nav.xhtml").decode()
        chapter_one = archive.read("EPUB/chapters/chapter-001.xhtml").decode()
        assert '<li><a href="chapters/chapter-001.xhtml">غلاف</a></li>' in nav
        assert (
            '<li><a href="chapters/chapter-001.xhtml#ch-1-قصيدة">قصيدة</a></li>'
            in nav
        )
        assert '<li><a href="chapters/chapter-002.xhtml">الباب الأول</a></li>' in nav
        # Three distinct chapter titles existed before merging; all three
        # must still have a nav entry in the table of contents (the
        # landmarks nav below it carries its own, unrelated <li>).
        toc = re.search(r'epub:type="toc"[^>]*>.*?<ol>(.*?)</ol>', nav, re.DOTALL).group(1)
        assert toc.count("<li>") == 3
        assert 'id="ch-1-قصيدة"' in chapter_one


def test_duplicate_title_merge_does_not_duplicate_the_nav_entry(tmp_path):
    output = tmp_path / "book.epub"
    build_epub(_document_with_a_repeated_title(), output, validate=False)
    with zipfile.ZipFile(output) as archive:
        nav = archive.read("EPUB/nav.xhtml").decode()
        toc = re.search(r'epub:type="toc"[^>]*>.*?<ol>(.*?)</ol>', nav, re.DOTALL).group(1)
        assert toc.count("<li>") == 1
        assert toc.count("العنوان") == 1


def test_every_nav_fragment_resolves_to_an_existing_anchor(tmp_path):
    output = tmp_path / "book.epub"
    build_epub(_document_with_a_short_chapter_between_two_others(), output, validate=False)
    with zipfile.ZipFile(output) as archive:
        nav = archive.read("EPUB/nav.xhtml").decode()
        fragments = re.findall(r'href="chapters/([^"#]+)#([^"]+)"', nav)
        assert fragments, "expected at least one fragment link to check"
        for name, anchor in fragments:
            chapter = archive.read(f"EPUB/chapters/{name}").decode()
            assert f'id="{anchor}"' in chapter


def test_chapter_ids_are_stable_across_builds(tmp_path):
    document = _document_with_a_short_chapter_between_two_others()
    first = tmp_path / "first.epub"
    second = tmp_path / "second.epub"
    build_epub(document, first, validate=False)
    build_epub(document, second, validate=False)
    with zipfile.ZipFile(first) as archive_a, zipfile.ZipFile(second) as archive_b:
        chapter_names = sorted(
            name for name in archive_a.namelist() if name.startswith("EPUB/chapters/")
        )
        assert chapter_names
        for name in chapter_names:
            assert archive_a.read(name) == archive_b.read(name)
