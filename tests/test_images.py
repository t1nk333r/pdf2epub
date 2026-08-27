import io
from dataclasses import dataclass

from PIL import Image

from pdf2epub.analyzer import _is_safe_figure_image, _save_epub_image


class FakePdf:
    def __init__(self, data: bytes):
        self.data = data

    def extract_image(self, xref):
        return {"ext": "tiff", "image": self.data}


@dataclass
class Rect:
    width: float
    height: float


class FakePage:
    def __init__(self, placements):
        self.rect = Rect(600, 800)
        self.placements = placements

    def get_image_rects(self, xref):
        return self.placements[xref]


def test_foreign_pdf_image_is_transcoded_to_png(tmp_path):
    source = io.BytesIO()
    Image.new("CMYK", (20, 20), color=(0, 20, 40, 0)).save(source, format="TIFF")
    target = _save_epub_image(FakePdf(source.getvalue()), 1, tmp_path, "page-1")
    assert target.suffix == ".png"
    with Image.open(target) as image:
        assert image.mode == "RGB"


def test_full_page_pdf_raster_is_never_preserved():
    page = FakePage({1: [Rect(590, 790)]})
    assert not _is_safe_figure_image(page, 1, 590 * 790, 600 * 800)


def test_small_internal_figure_is_preserved():
    page = FakePage({1: [Rect(240, 180)]})
    assert _is_safe_figure_image(page, 1, 240 * 180, 600 * 800)


def test_tiled_page_rasters_are_all_rejected():
    page = FakePage({1: [Rect(300, 400)], 2: [Rect(300, 400)]})
    total_area = 600 * 800
    assert not _is_safe_figure_image(page, 1, total_area, 600 * 800)
    assert not _is_safe_figure_image(page, 2, total_area, 600 * 800)


def test_image_without_reliable_placement_is_rejected():
    page = FakePage({1: []})
    assert not _is_safe_figure_image(page, 1, 100, 600 * 800)
