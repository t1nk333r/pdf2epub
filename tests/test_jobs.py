import json
import os

import pytest

from pdf2epub import checkpoints
from pdf2epub.checkpoints import (
    CheckpointError,
    CheckpointStore,
    JobManifest,
    page_from_dict,
    page_to_dict,
    processing_contract,
)
from pdf2epub.models import (
    Block,
    BlockType,
    ConversionOptions,
    JobState,
    PageResult,
    PageSelection,
    PageType,
    TextQuality,
    can_transition,
)


def manifest(job_id="a" * 32):
    options = ConversionOptions(require_epubcheck=False)
    return JobManifest(
        id=job_id,
        filename="كتاب.pdf",
        source_sha256="1" * 64,
        source_size=10,
        page_count=3,
        state=JobState.PROCESSING,
        stage="Analyzing",
        options={name: getattr(options, name) for name in options.__slots__},
        contract=processing_contract("1" * 64, 3, options, "tesseract", None),
        provider_id="tesseract",
    )


def page(number=1):
    return PageResult(
        number,
        PageType.HYBRID,
        "embedded_text",
        612.5,
        792.0,
        blocks=[Block(BlockType.HEADING, "عنوان", (1, 2, 3, 4), bold=True)],
        quality=TextQuality(score=0.9, reasons=["ok"]),
        selection=PageSelection.EMBEDDED,
        candidate_scores={"embedded_text": 0.9},
        candidates={"embedded_text": {"text": "عنوان"}},
        ocr_metrics={"attempts": []},
    )


def test_state_machine_is_closed():
    assert can_transition(JobState.QUEUED, JobState.PROCESSING)
    assert can_transition(JobState.PROCESSING, JobState.PAUSE_REQUESTED)
    assert can_transition(JobState.PAUSE_REQUESTED, JobState.PAUSED)
    assert can_transition(JobState.PAUSED, JobState.PROCESSING)
    assert not can_transition(JobState.COMPLETE, JobState.PROCESSING)
    assert not can_transition(JobState.PAUSED, JobState.COMPLETE)


def test_page_schema_roundtrip_preserves_typed_values():
    restored = page_from_dict(page_to_dict(page()))
    assert restored == page()
    assert restored.page_type is PageType.HYBRID
    assert restored.blocks[0].type is BlockType.HEADING
    assert restored.blocks[0].bbox == (1.0, 2.0, 3.0, 4.0)


def test_fingerprint_changes_and_excludes_endpoint_secrets(monkeypatch):
    options = ConversionOptions()
    first = processing_contract(
        "1" * 64,
        3,
        options,
        "ollama",
        "model",
        "http://user:secret@host:11434/api?key=no",
    )
    second = processing_contract(
        "1" * 64, 3, options, "ollama", "model", "http://host:11434/api"
    )
    assert first == second
    options.render_dpi += 1
    assert first != processing_contract(
        "1" * 64, 3, options, "ollama", "model", "http://host:11434/api"
    )
    assert first != processing_contract(
        "2" * 64,
        3,
        ConversionOptions(),
        "ollama",
        "model",
        "http://host:11434/api",
    )
    assert first != processing_contract(
        "1" * 64,
        3,
        ConversionOptions(),
        "ollama",
        "other",
        "http://host:11434/api",
    )
    monkeypatch.setattr(checkpoints, "OCR_CONTRACT_VERSION", "arabic-ocr-v2")
    assert first != processing_contract(
        "1" * 64,
        3,
        ConversionOptions(),
        "ollama",
        "model",
        "http://host:11434/api",
    )


def test_atomic_checkpoint_repairs_lagging_manifest(tmp_path):
    store = CheckpointStore(tmp_path, "a" * 32)
    store.initialize()
    item = manifest()
    store.write_manifest(item)
    store.write_page(page(), item)
    assert item.safe_pages == 0
    assert [saved.page for saved in store.contiguous_pages(item)] == [1]
    assert item.safe_pages == 1
    assert store.load_manifest().safe_pages == 1


def test_corrupt_next_checkpoint_preserves_contiguous_prefix(tmp_path):
    store = CheckpointStore(tmp_path, "a" * 32)
    store.initialize()
    item = manifest()
    store.write_manifest(item)
    store.write_page(page(), item)
    item.safe_pages = 1
    store.write_manifest(item)
    store.checkpoint_path(2).write_text("{")
    assert [saved.page for saved in store.contiguous_pages(item)] == [1]
    assert not store.checkpoint_path(2).exists()
    assert len(list(store.checkpoints.glob(".corrupt-page-000002-*.json"))) == 1


def test_corrupt_safe_checkpoint_blocks_resume(tmp_path):
    store = CheckpointStore(tmp_path, "a" * 32)
    store.initialize()
    item = manifest()
    store.write_page(page(), item)
    item.safe_pages = 1
    store.write_manifest(item)
    store.checkpoint_path(1).write_text("{")
    with pytest.raises(CheckpointError, match="Cannot read"):
        store.contiguous_pages(item)


def test_traversing_image_path_is_rejected():
    value = page_to_dict(page())
    value["blocks"][0]["image_path"] = "../secret"
    with pytest.raises(CheckpointError, match="unsafe path"):
        page_from_dict(value)


def test_manifest_rejects_absolute_output_path():
    value = manifest().to_dict()
    value["outputs"] = {"epub": "/etc/passwd"}
    with pytest.raises(CheckpointError, match="unsafe path"):
        JobManifest.from_dict(json.loads(json.dumps(value)))


def test_failure_before_checkpoint_replace_publishes_no_page(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path, "a" * 32)
    store.initialize()
    item = manifest()

    def fail(path, value):
        raise OSError("injected write failure")

    monkeypatch.setattr(store, "_atomic_json", fail)
    with pytest.raises(OSError, match="injected"):
        store.write_page(page(), item)
    assert not store.checkpoint_path(1).exists()


def test_orphan_temporary_file_does_not_extend_safe_prefix(tmp_path):
    store = CheckpointStore(tmp_path, "a" * 32)
    store.initialize()
    item = manifest()
    store.write_manifest(item)
    (store.checkpoints / ".page-000001.json.crash.tmp").write_text("complete?")
    assert store.contiguous_pages(item) == []


def test_failure_after_temporary_fsync_before_replace_is_recoverable(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path, "a" * 32)
    store.initialize()
    item = manifest()
    original = os.replace

    def fail_replace(source, target):
        raise OSError("injected rename interruption")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected rename"):
        store.write_page(page(), item)
    assert not store.checkpoint_path(1).exists()
    monkeypatch.setattr(os, "replace", original)
    store.write_page(page(), item)
    assert store.load_page(1, item).page == 1


def test_symlinked_job_directory_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ("a" * 32)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(CheckpointError, match="symlink"):
        CheckpointStore(tmp_path, "a" * 32)
