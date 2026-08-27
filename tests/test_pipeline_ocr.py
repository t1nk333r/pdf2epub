import hashlib
import json
import zipfile
from dataclasses import asdict

import pymupdf
import pytest
from PIL import Image

from pdf2epub import pipeline
from pdf2epub.checkpoints import (
    CheckpointStore,
    ConversionControl,
    JobManifest,
    PauseRequested,
    processing_contract,
)
from pdf2epub.models import ConversionOptions, JobState
from pdf2epub.ocr import OcrError, OcrResult
from pdf2epub.pipeline import ConversionIntegrityError, convert


class FakeOcrProvider:
    name = "test_provider"

    def __init__(self):
        self.calls = 0

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        self.calls += 1
        assert image_png.startswith(b"\x89PNG")
        assert language == "ara+eng"
        return OcrResult(
            "# الفصل الأول\n\nهذا نص من محرك خارجي [^1].\n\n[^1]: نص الحاشية.",
            0.97,
            self.name,
            model="test-model",
            prompt_tokens=1200,
            output_tokens=80,
            duration_seconds=3.5,
            attempts=[
                {
                    "model": "test-model",
                    "prompt_eval_count": 1200,
                    "eval_count": 80,
                    "total_duration": 3_500_000_000,
                    "duration_seconds": 3.5,
                    "retries": 0,
                }
            ],
        )


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
    metrics = report["page_details"][0]["ocr_metrics"]
    assert metrics["model"] == "test-model"
    assert metrics["attempts"][0]["prompt_eval_count"] == 1200
    assert metrics["fallback_used"] is False
    with zipfile.ZipFile(outputs["epub"]) as epub:
        chapter = epub.read("EPUB/chapters/chapter-001.xhtml").decode()
        assert "هذا نص من محرك خارجي" in chapter
        assert "<h1>الفصل الأول</h1>" in chapter
        assert 'epub:type="noteref" href="#fn-1"' in chapter
        assert 'epub:type="footnote" class="footnote" id="fn-1"' in chapter


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


def test_pipeline_pause_publishes_nothing_and_resume_reuses_checkpoint(tmp_path):
    source = tmp_path / "scan.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(source)
    pdf.close()
    options = ConversionOptions(
        mode="force_ocr", preserve_images=False, require_epubcheck=False
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    store = CheckpointStore(tmp_path / "jobs", "a" * 32)
    store.initialize()
    manifest = JobManifest(
        id="a" * 32,
        filename="scan.pdf",
        source_sha256=source_hash,
        source_size=source.stat().st_size,
        page_count=1,
        state=JobState.PROCESSING,
        stage="Analyzing",
        options=asdict(options),
        contract=processing_contract(source_hash, 1, options, "test_provider", None),
        provider_id="test_provider",
    )
    store.write_manifest(manifest)
    provider = FakeOcrProvider()
    with pytest.raises(PauseRequested):
        convert(
            source,
            store.output,
            options,
            ocr_provider=provider,
            control=ConversionControl(store, manifest, pause_requested=lambda: True),
        )
    assert not list(store.output.glob("*.epub"))
    outputs = convert(
        source,
        store.output,
        options,
        ocr_provider=provider,
        control=ConversionControl(store, manifest),
    )
    assert outputs["epub"].is_file()
    assert manifest.safe_pages == 1
    assert provider.calls == 1


def test_report_interruption_resumes_without_repeating_ocr(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(source)
    pdf.close()
    options = ConversionOptions(
        mode="force_ocr", preserve_images=False, require_epubcheck=False
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    store = CheckpointStore(tmp_path / "jobs", "a" * 32)
    store.initialize()
    manifest = JobManifest(
        id="a" * 32,
        filename="scan.pdf",
        source_sha256=source_hash,
        source_size=source.stat().st_size,
        page_count=1,
        state=JobState.PROCESSING,
        stage="Analyzing",
        options=asdict(options),
        contract=processing_contract(source_hash, 1, options, "test_provider", None),
        provider_id="test_provider",
    )
    store.write_manifest(manifest)
    provider = FakeOcrProvider()
    original = pipeline._write_report
    failures = 0

    def fail_once(document, report_path):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("injected report interruption")
        return original(document, report_path)

    monkeypatch.setattr(pipeline, "_write_report", fail_once)
    with pytest.raises(OSError, match="injected report"):
        convert(
            source,
            store.output,
            options,
            ocr_provider=provider,
            control=ConversionControl(store, manifest),
        )
    assert not list(store.output.glob("*.epub"))
    outputs = convert(
        source,
        store.output,
        options,
        ocr_provider=provider,
        control=ConversionControl(store, manifest),
    )
    assert outputs["epub"].is_file()
    assert provider.calls == 1


def test_epub_build_interruption_resumes_without_repeating_ocr(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(source)
    pdf.close()
    options = ConversionOptions(
        mode="force_ocr", preserve_images=False, require_epubcheck=False
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    store = CheckpointStore(tmp_path / "jobs", "a" * 32)
    store.initialize()
    manifest = JobManifest(
        id="a" * 32,
        filename="scan.pdf",
        source_sha256=source_hash,
        source_size=source.stat().st_size,
        page_count=1,
        state=JobState.PROCESSING,
        stage="Analyzing",
        options=asdict(options),
        contract=processing_contract(source_hash, 1, options, "test_provider", None),
        provider_id="test_provider",
    )
    store.write_manifest(manifest)
    provider = FakeOcrProvider()
    original = pipeline.build_epub
    failures = 0

    def fail_once(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("injected build interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "build_epub", fail_once)
    with pytest.raises(OSError, match="injected build"):
        convert(
            source,
            store.output,
            options,
            ocr_provider=provider,
            control=ConversionControl(store, manifest),
        )
    assert not list(store.output.glob("*.epub"))
    outputs = convert(
        source,
        store.output,
        options,
        ocr_provider=provider,
        control=ConversionControl(store, manifest),
    )
    assert outputs["epub"].is_file()
    assert provider.calls == 1
