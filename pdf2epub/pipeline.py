from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .analyzer import analyze_pdf
from .epub import build_epub
from .models import ConversionOptions
from .ocr import OcrProvider


def convert(
    input_pdf: Path,
    output_dir: Path,
    options: ConversionOptions | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    ocr_provider: OcrProvider | None = None,
) -> dict[str, Path]:
    options = options or ConversionOptions()
    output_dir.mkdir(parents=True, exist_ok=True)
    document = analyze_pdf(input_pdf, output_dir, options, progress, ocr_provider)
    if progress:
        progress("Building EPUB", 0, 1)
    epub_path = output_dir / f"{input_pdf.stem}.epub"
    document.warnings.extend(build_epub(document, epub_path))
    report_path = output_dir / "quality-report.json"
    report_path.write_text(
        json.dumps(document.report(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if progress:
        progress("Complete", 1, 1)
    return {"epub": epub_path, "report": report_path}
