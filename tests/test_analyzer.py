import hashlib
from dataclasses import asdict

import pymupdf
import pytest

from pdf2epub.analyzer import (
    _has_severe_text_issue,
    _merge_paragraph_lines,
    _visual_text_lines,
)
from pdf2epub.checkpoints import (
    CheckpointStore,
    ConversionControl,
    JobManifest,
    PauseRequested,
    processing_contract,
)
from pdf2epub.models import Block, BlockType, ConversionOptions, JobState


def _line(text, bbox):
    return {"bbox": bbox, "spans": [{"text": text}]}


def test_word_fragments_on_same_baseline_become_one_line():
    lines = [
        _line("من", (400, 100, 420, 120)),
        _line("الانهماك", (340, 100, 395, 120)),
        _line("في", (320, 100, 335, 120)),
    ]
    assert _visual_text_lines(lines) == ["من الانهماك في"]


def test_separate_visual_rows_remain_separate():
    lines = [
        _line("السطر", (380, 100, 420, 120)),
        _line("الأول", (330, 100, 375, 120)),
        _line("السطر الثاني", (320, 130, 420, 150)),
    ]
    assert _visual_text_lines(lines) == ["السطر الأول", "السطر الثاني"]


def test_full_width_pdf_lines_merge_but_short_paragraph_end_stops_merge():
    blocks = [
        Block(BlockType.PARAGRAPH, "بداية الفقرة الطويلة", (40, 100, 420, 120)),
        Block(BlockType.PARAGRAPH, "تكملة الفقرة", (40, 125, 420, 145)),
        Block(BlockType.PARAGRAPH, "نهايتها.", (250, 150, 420, 170)),
        Block(BlockType.PARAGRAPH, "فقرة جديدة", (100, 175, 420, 195)),
    ]
    _merge_paragraph_lines(blocks)
    assert [block.text for block in blocks] == [
        "بداية الفقرة الطويلة تكملة الفقرة نهايتها.",
        "فقرة جديدة",
    ]


def test_one_corrupt_block_routes_the_page_to_candidate_comparison():
    blocks = [
        Block(BlockType.HEADING, "ائوضؤع اتمضؤة"),
        Block(BlockType.PARAGRAPH, "هذا نص عربي سليم ومفهوم للقارئ."),
    ]
    assert _has_severe_text_issue(blocks)


def test_pause_checkpoints_page_and_resume_starts_at_next_page(tmp_path):
    source = tmp_path / "book.pdf"
    pdf = pymupdf.open()
    for number in range(2):
        page = pdf.new_page()
        page.insert_text((72, 72), f"page {number + 1} embedded text")
    pdf.save(source)
    pdf.close()
    options = ConversionOptions(
        mode="prefer_text", preserve_images=False, require_epubcheck=False
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    contract = processing_contract(source_hash, 2, options, "tesseract", None)
    store = CheckpointStore(tmp_path / "jobs", "a" * 32)
    store.initialize()
    manifest = JobManifest(
        id="a" * 32,
        filename="book.pdf",
        source_sha256=source_hash,
        source_size=source.stat().st_size,
        page_count=2,
        state=JobState.PROCESSING,
        stage="Analyzing",
        options=asdict(options),
        contract=contract,
        provider_id="tesseract",
    )
    store.write_manifest(manifest)
    paused = ConversionControl(store, manifest, pause_requested=lambda: True)
    from pdf2epub.analyzer import analyze_pdf

    with pytest.raises(PauseRequested):
        analyze_pdf(source, store.output, options, control=paused)
    assert manifest.safe_pages == 1
    assert store.checkpoint_path(1).is_file()
    assert not store.checkpoint_path(2).exists()

    resumed = analyze_pdf(
        source,
        store.output,
        options,
        control=ConversionControl(store, manifest),
    )
    assert [page.page for page in resumed.pages] == [1, 2]
    assert manifest.safe_pages == 2


def test_cli_exposes_allow_partial(monkeypatch, tmp_path):
    from pdf2epub import cli

    captured = {}
    monkeypatch.setattr(
        cli, "convert", lambda *args, **kwargs: captured.update(options=args[2]) or {}
    )
    monkeypatch.setattr(
        "sys.argv", ["pdf2epub", str(tmp_path / "book.pdf"), "--allow-partial"]
    )
    cli.main()
    assert captured["options"].allow_partial is True

    captured.clear()
    monkeypatch.setattr("sys.argv", ["pdf2epub", str(tmp_path / "book.pdf")])
    cli.main()
    assert captured["options"].allow_partial is False


def test_contents_lines_do_not_become_chapter_headings():
    from pdf2epub.analyzer import _classify_structure
    from pdf2epub.models import ConversionOptions

    blocks = [
        Block(
            BlockType.PARAGRAPH,
            "مقدمة.................... 18\nالتطور الصغير............ 42",
            (0.0, 10.0, 300.0, 40.0),
            font_size=20.0,
        )
    ]
    _classify_structure(blocks, 800.0, ConversionOptions())
    assert blocks[0].type != BlockType.HEADING


def test_large_type_is_a_subheading_and_a_chapter_opener_is_level_one():
    from pdf2epub.analyzer import _classify_structure
    from pdf2epub.models import ConversionOptions

    blocks = [
        Block(BlockType.PARAGRAPH, "عنوان الكتاب", (0.0, 10.0, 300.0, 40.0), font_size=16.0),
        Block(BlockType.PARAGRAPH, "الباب الأول", (0.0, 50.0, 300.0, 80.0), font_size=12.0),
        Block(BlockType.PARAGRAPH, "نص عادي هنا", (0.0, 90.0, 300.0, 120.0), font_size=12.0),
    ]
    _classify_structure(blocks, 800.0, ConversionOptions())
    assert blocks[0].type == BlockType.HEADING
    assert blocks[0].heading_level == 2
    assert blocks[1].type == BlockType.HEADING
    assert blocks[1].heading_level == 1


def test_a_contents_page_opens_no_chapter():
    from pdf2epub.analyzer import _classify_structure
    from pdf2epub.models import ConversionOptions

    # A bold part name sitting on a contents page is a destination, not a
    # chapter opener, so it must not start a new EPUB file.
    blocks = [
        Block(BlockType.PARAGRAPH, "الباب الأول", (0.0, 10.0, 300.0, 30.0), font_size=14.0),
        Block(BlockType.PARAGRAPH, "مقدمة ١٨", (0.0, 40.0, 300.0, 60.0), font_size=11.0),
        Block(BlockType.PARAGRAPH, "التطور الصغير ٢٨", (0.0, 70.0, 300.0, 90.0), font_size=11.0),
        Block(BlockType.PARAGRAPH, "التطور الكبير ٣٨", (0.0, 100.0, 300.0, 120.0), font_size=11.0),
        Block(BlockType.PARAGRAPH, "النوع ٥٣", (0.0, 130.0, 300.0, 150.0), font_size=11.0),
    ]
    _classify_structure(blocks, 800.0, ConversionOptions())
    assert all(block.type != BlockType.HEADING for block in blocks)


def test_a_prose_page_still_opens_a_chapter():
    from pdf2epub.analyzer import _classify_structure
    from pdf2epub.models import ConversionOptions

    blocks = [
        Block(BlockType.PARAGRAPH, "الباب الأول", (0.0, 10.0, 300.0, 30.0), font_size=14.0),
        Block(BlockType.PARAGRAPH, "هذه فقرة عادية من نص الكتاب.", (0.0, 40.0, 300.0, 60.0), font_size=11.0),
        Block(BlockType.PARAGRAPH, "وهذه فقرة أخرى تكمل الفكرة.", (0.0, 70.0, 300.0, 90.0), font_size=11.0),
        Block(BlockType.PARAGRAPH, "وهذه ثالثة تختم المبحث.", (0.0, 100.0, 300.0, 120.0), font_size=11.0),
    ]
    _classify_structure(blocks, 800.0, ConversionOptions())
    assert blocks[0].type == BlockType.HEADING
    assert blocks[0].heading_level == 1
