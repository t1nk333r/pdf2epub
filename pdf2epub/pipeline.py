from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from .analyzer import analyze_pdf
from .checkpoints import ConversionControl
from .epub import EpubValidationError, build_epub
from .models import ConversionOptions, JobState, require_transition
from .ocr import OcrProvider


class ConversionIntegrityError(RuntimeError):
    def __init__(self, pages: list[int], report_path: Path, reason: str):
        self.pages = pages
        self.report_path = report_path
        rendered = ", ".join(str(page) for page in pages[:20])
        if len(pages) > 20:
            rendered += f" and {len(pages) - 20} more"
        super().__init__(f"{reason}: {rendered}")


class ConversionPublicationError(RuntimeError):
    def __init__(self, message: str, report_path: Path):
        self.report_path = report_path
        super().__init__(message)


def _write_report(document, report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=report_path.parent,
        prefix=f".{report_path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                document.report(), handle, ensure_ascii=False, indent=2, default=str
            )
            handle.write("\n")
        os.replace(temporary, report_path)
    finally:
        temporary.unlink(missing_ok=True)


def convert(
    input_pdf: Path,
    output_dir: Path,
    options: ConversionOptions | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    ocr_provider: OcrProvider | None = None,
    control: ConversionControl | None = None,
    output_stem: str | None = None,
) -> dict[str, Path]:
    options = options or ConversionOptions()
    output_dir.mkdir(parents=True, exist_ok=True)
    document = analyze_pdf(
        input_pdf, output_dir, options, progress, ocr_provider, control=control
    )
    report_path = output_dir / "quality-report.json"
    _write_report(document, report_path)
    integrity = document.report()["integrity"]
    if not options.allow_partial:
        unavailable = integrity["unavailable_pages"]
        if unavailable:
            raise ConversionIntegrityError(
                unavailable,
                report_path,
                "No usable text could be recovered from pages",
            )
        review = integrity["review_pages"]
        if review:
            raise ConversionIntegrityError(
                review,
                report_path,
                "Conversion requires review for pages",
            )
    elif integrity["status"] != "passed":
        document.warnings.append(
            "Partial output was explicitly allowed; review the integrity section"
        )
    if progress:
        progress("Building EPUB", 0, 1)
    if control is not None:
        control.manifest.state = require_transition(
            control.manifest.state, JobState.BUILDING
        )
        control.manifest.stage = "Building EPUB"
        control.manifest.current_page = None
        control.store.write_manifest(control.manifest)
    epub_path = output_dir / f"{output_stem or input_pdf.stem}.epub"
    try:
        document.warnings.extend(
            build_epub(document, epub_path, validate=options.require_epubcheck)
        )
    except EpubValidationError as exc:
        document.warnings.append(str(exc))
        _write_report(document, report_path)
        raise ConversionPublicationError(str(exc), report_path) from exc
    _write_report(document, report_path)
    if progress:
        progress("Complete", 1, 1)
    return {"epub": epub_path, "report": report_path}
