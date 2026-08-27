from pdf2epub.analyzer import (
    _has_severe_text_issue,
    _merge_paragraph_lines,
    _visual_text_lines,
)
from pdf2epub.models import Block, BlockType


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
