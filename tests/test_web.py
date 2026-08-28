import hashlib
import json
import threading
from dataclasses import asdict
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdf2epub import pipeline, web
from pdf2epub.checkpoints import (
    CheckpointStore,
    ConversionControl,
    JobManifest,
    processing_contract,
)
from pdf2epub.epub import EpubValidationError
from pdf2epub.models import (
    ConversionOptions,
    JobState,
    PageResult,
    PageType,
)
from pdf2epub.ocr import OcrResult
from pdf2epub.settings import clear_runtime_settings
from pdf2epub.web import Job, _append_log, _quality_summary, _safe_pdf_name


@pytest.fixture
def client():
    with TestClient(web.app) as test_client:
        yield test_client


def test_quality_summary_omits_large_page_details():
    report = {
        "pages": 341,
        "sources": {"embedded_text": 271},
        "page_details": [{"page": i, "blocks": ["large text"]} for i in range(341)],
    }
    summary = _quality_summary(report)
    assert "page_details" not in summary
    assert summary["page_details_count"] == 341
    assert report["page_details"]


def test_completed_job_is_recovered_after_restart(tmp_path, monkeypatch):
    job_id = "a" * 32
    output = tmp_path / job_id / "output"
    output.mkdir(parents=True)
    (output / "book.epub").write_bytes(b"epub")
    (output / "quality-report.json").write_text("{}")
    monkeypatch.setattr(web, "JOB_ROOT", tmp_path)
    web.jobs.pop(job_id, None)

    recovered = web._get_job(job_id)
    assert recovered is not None
    assert recovered.state == "complete"
    assert recovered.outputs["epub"].endswith("book.epub")


def test_recovery_rejects_invalid_job_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "JOB_ROOT", tmp_path)
    assert web._recover_completed_job("../not-a-job") is None


def test_uploaded_arabic_filename_is_preserved_safely():
    assert _safe_pdf_name("زخرف القول.pdf") == "زخرف القول.pdf"
    assert _safe_pdf_name("../../bad/name.pdf") == "name.pdf"


def test_remote_ocr_requires_explicit_transfer_acknowledgement(client):
    response = client.post(
        "/api/jobs",
        files={"file": ("book.pdf", b"%PDF-minimal", "application/pdf")},
        data={"ocr_provider": "mistral"},
    )
    assert response.status_code == 400
    assert "explicit acknowledgement" in response.json()["detail"]


def _provider_status(*, tesseract_configured=True, tesseract_ready=True):
    return [
        {
            "id": "tesseract",
            "configured": tesseract_configured,
            "ready": tesseract_ready,
        },
        {"id": "mistral", "configured": False, "ready": False},
    ]


def test_health_endpoint_reports_no_warnings_when_local_tools_ready(
    client, monkeypatch
):
    monkeypatch.setattr(web.shutil, "which", lambda name: "/usr/bin/epubcheck")
    monkeypatch.setattr(web, "provider_status", _provider_status)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["epubcheck_available"] is True
    assert response.json()["warnings"] == []


def test_health_warns_when_epubcheck_is_missing(client, monkeypatch):
    monkeypatch.setattr(web.shutil, "which", lambda name: None)
    monkeypatch.setattr(web, "provider_status", _provider_status)
    response = client.get("/api/health")
    warning = response.json()["warnings"][0]
    assert warning["code"] == "epubcheck_missing"
    assert warning["severity"] == "error"
    assert "لا يمكن نشر" in warning["message"]


def test_health_warns_when_tesseract_is_missing(client, monkeypatch):
    monkeypatch.setattr(web.shutil, "which", lambda name: "/usr/bin/epubcheck")
    monkeypatch.setattr(
        web,
        "provider_status",
        lambda: _provider_status(tesseract_configured=False, tesseract_ready=False),
    )
    response = client.get("/api/health")
    warning = response.json()["warnings"][0]
    assert warning["code"] == "tesseract_missing"
    assert warning["severity"] == "warning"
    assert "غير متاح" in warning["message"]


def test_health_warns_when_tesseract_arabic_data_is_missing(client, monkeypatch):
    monkeypatch.setattr(web.shutil, "which", lambda name: "/usr/bin/epubcheck")
    monkeypatch.setattr(
        web,
        "provider_status",
        lambda: _provider_status(tesseract_ready=False),
    )
    response = client.get("/api/health")
    warning = response.json()["warnings"][0]
    assert warning["code"] == "tesseract_arabic_missing"
    assert warning["severity"] == "warning"
    assert "اللغة العربية" in warning["message"]


def test_settings_api_updates_values_without_returning_secrets(client):
    clear_runtime_settings()
    try:
        response = client.put(
            "/api/settings",
            json={
                "values": {
                    "PDF2EPUB_OLLAMA_MODEL": "qwen-test",
                    "PDF2EPUB_OLLAMA_API_KEY": "do-not-return",
                }
            },
        )
        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["PDF2EPUB_OLLAMA_MODEL"]["value"] == "qwen-test"
        assert fields["PDF2EPUB_OLLAMA_MODEL"]["source"] == "runtime"
        assert fields["PDF2EPUB_OLLAMA_API_KEY"]["value"] == ""
        assert fields["PDF2EPUB_OLLAMA_API_KEY"]["configured"] is True
    finally:
        clear_runtime_settings()


def test_settings_api_rejects_unknown_fields(client):
    response = client.put("/api/settings", json={"values": {"NOT_A_SETTING": "value"}})
    assert response.status_code == 400
    assert "Unknown settings" in response.json()["detail"]


def test_home_contains_settings_dialog(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="settings-button"' in response.text
    assert 'id="settings-dialog"' in response.text
    assert 'id="capability-warnings"' in response.text
    assert 'role="alert"' in response.text
    assert 'aria-live="polite"' in response.text
    assert "loadHealth()" in response.text
    assert "message.textContent=warning.message" in response.text
    assert 'id="log-stream"' in response.text
    assert 'id="ollama-model"' in response.text
    assert "qwen3-vl:32b-instruct-gpu" in response.text
    assert "qwen3-vl:30b-instruct-gpu" in response.text
    assert "qwen3.8:27b-q4_K_M" in response.text
    assert 'id="job-list"' in response.text
    assert 'id="pause"' in response.text
    assert 'id="resume"' in response.text
    assert "إيقاف مؤقت" in response.text
    assert "متابعة التحويل" in response.text
    assert "aria-busy" in response.text
    assert "new URLSearchParams(location.search)" in response.text
    assert "localStorage.getItem('pdf2epub-active-job')" in response.text
    assert "typeof detail==='object'&&detail?detail.message" in response.text
    assert "dashboardAction(job,'pause')" in response.text
    assert "dashboardAction(job,'resume')" in response.text


def test_job_logs_are_structured_bounded_and_public(monkeypatch):
    monkeypatch.setattr(web, "MAX_JOB_LOGS", 2)
    job = Job("b" * 32)
    _append_log(job, "Queued")
    _append_log(job, "Analyzing page 1")
    _append_log(job, "Finished", "success")

    public = job.public()
    assert "log_sequence" not in public
    assert [entry["id"] for entry in public["logs"]] == [2, 3]
    assert public["logs"][-1]["level"] == "success"
    assert public["logs"][-1]["message"] == "Finished"


def test_progress_updates_append_log_event():
    job_id = "c" * 32
    web.jobs[job_id] = Job(job_id)
    try:
        web._update(job_id, "Analyzing pages", 2, 10)
        public = web.jobs[job_id].public()
        assert public["percent"] == 20
        assert public["logs"][-1]["message"] == "Analyzing pages — 2/10 (20%)"
    finally:
        web.jobs.pop(job_id, None)


def _persistent_job(tmp_path, state=JobState.PAUSED):
    job_id = "d" * 32
    options = ConversionOptions(require_epubcheck=False)
    store = CheckpointStore(tmp_path, job_id)
    store.initialize()
    source = store.root / "source.pdf"
    document = pymupdf.open()
    for _ in range(10):
        document.new_page()
    document.save(source)
    document.close()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = JobManifest(
        id=job_id,
        filename="كتاب.pdf",
        source_sha256=source_hash,
        source_size=source.stat().st_size,
        page_count=10,
        state=state,
        stage=state.value,
        options=asdict(options),
        contract=processing_contract(source_hash, 10, options, "tesseract", None),
        provider_id="tesseract",
    )
    store.write_manifest(manifest)
    job = web._job_from_manifest(store, manifest)
    web.jobs[job_id] = job
    return job


def test_jobs_list_is_summary_without_full_logs(client, tmp_path):
    job = _persistent_job(tmp_path)
    _append_log(job, "latest")
    try:
        response = client.get("/api/jobs")
        summary = next(item for item in response.json()["jobs"] if item["id"] == job.id)
        assert "logs" not in summary
        assert summary["last_event"]["message"] == "latest"
        assert summary["safe_pages"] == 0
    finally:
        web.jobs.pop(job.id, None)


def test_pause_is_idempotent_and_sets_page_boundary_event(client, tmp_path):
    job = _persistent_job(tmp_path, JobState.PROCESSING)
    try:
        first = client.post(f"/api/jobs/{job.id}/pause")
        second = client.post(f"/api/jobs/{job.id}/pause")
        assert first.status_code == second.status_code == 200
        assert second.json()["state"] == "pause_requested"
        assert job.pause_event.is_set()
    finally:
        web.jobs.pop(job.id, None)


def test_resume_is_idempotent_and_starts_one_worker(tmp_path, monkeypatch):
    job = _persistent_job(tmp_path)
    starts = []
    monkeypatch.setattr(
        web,
        "provider_status",
        lambda: [{"id": "tesseract", "ready": True}],
    )

    def fake_start(runtime, pdf, options):
        starts.append(runtime.id)
        runtime.thread = type("Thread", (), {"is_alive": lambda self: True})()

    monkeypatch.setattr(web, "_start_worker", fake_start)
    barrier = threading.Barrier(3)
    results = []

    def resume():
        barrier.wait()
        results.append(web.resume_job(job.id))

    workers = [threading.Thread(target=resume) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)
        assert len(results) == 2
        assert len(starts) == 1
    finally:
        web.jobs.pop(job.id, None)


def test_startup_recovery_marks_active_job_interrupted(tmp_path, monkeypatch):
    job = _persistent_job(tmp_path, JobState.PROCESSING)
    job.manifest.lease = {"process": "stale", "token": "old-worker"}
    job.store.write_manifest(job.manifest)
    web.jobs.pop(job.id)
    monkeypatch.setattr(web, "JOB_ROOT", tmp_path)
    web._recover_jobs()
    try:
        assert web.jobs[job.id].state == "interrupted"
        assert web.jobs[job.id].thread is None
        assert web.jobs[job.id].manifest.lease is None
    finally:
        web.jobs.pop(job.id, None)


def test_resume_unavailable_provider_returns_stable_422(client, tmp_path, monkeypatch):
    job = _persistent_job(tmp_path)
    monkeypatch.setattr(
        web,
        "provider_status",
        lambda: [{"id": "tesseract", "ready": False}],
    )
    try:
        response = client.post(f"/api/jobs/{job.id}/resume")
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "provider_unavailable"
        assert job.state == "paused"
    finally:
        web.jobs.pop(job.id, None)


def test_create_job_persists_source_contract_before_worker(
    client, tmp_path, monkeypatch
):
    source = tmp_path / "upload.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(source)
    document.close()
    jobs_root = tmp_path / "jobs"
    monkeypatch.setattr(web, "JOB_ROOT", jobs_root)
    monkeypatch.setattr(web, "provider_names", lambda: {"tesseract"})
    monkeypatch.setattr(web, "_start_worker", lambda *args: None)
    response = client.post(
        "/api/jobs",
        files={"file": ("كتاب.pdf", source.read_bytes(), "application/pdf")},
        data={"ocr_provider": "tesseract"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    try:
        store = CheckpointStore(jobs_root, job_id)
        item = store.load_manifest()
        assert item.filename == "كتاب.pdf"
        assert (
            item.source_sha256
            == hashlib.sha256((store.root / "source.pdf").read_bytes()).hexdigest()
        )
        assert item.page_count == 1
        assert "API_KEY" not in json.dumps(item.to_dict())
    finally:
        web.jobs.pop(job_id, None)


def test_stale_pause_persistence_cannot_lower_committed_safe_pages(tmp_path):
    job = _persistent_job(tmp_path, JobState.PROCESSING)
    try:
        page = PageResult(1, PageType.TEXT, "embedded_text", 100, 100)
        ConversionControl(job.store, job.manifest).page_committed(page)
        job.safe_pages = 0
        web._persist(job)
        assert job.safe_pages == 1
        assert job.manifest.safe_pages == 1
        assert job.store.load_manifest().safe_pages == 1
    finally:
        web.jobs.pop(job.id, None)


def test_illegal_production_state_transition_is_rejected():
    job = Job("e" * 32, state=JobState.COMPLETE)
    with pytest.raises(ValueError, match="Illegal job state transition"):
        web._transition_job(job, JobState.PROCESSING)


def test_resume_checks_exact_manifest_ollama_model_and_can_retry(
    client, tmp_path, monkeypatch
):
    job = _persistent_job(tmp_path)
    alternate = "qwen3-vl:30b-instruct-gpu"
    options = ConversionOptions(
        ocr_provider="ollama", ollama_model=alternate, require_epubcheck=False
    )
    job.manifest.provider_id = "ollama"
    job.manifest.model = alternate
    job.manifest.options = asdict(options)
    job.manifest.contract = processing_contract(
        job.manifest.source_sha256,
        job.manifest.page_count,
        options,
        "ollama",
        alternate,
    )
    job.store.write_manifest(job.manifest)
    available = False
    checked_models = []

    def statuses(settings=None):
        model = settings.ollama_model if settings else web.OLLAMA_QUALITY_MODEL
        checked_models.append(model)
        return [{"id": "ollama", "ready": available and model == alternate}]

    monkeypatch.setattr(web, "provider_status", statuses)

    def fake_start(runtime, pdf, selected_options):
        runtime.thread = type("Thread", (), {"is_alive": lambda self: True})()

    monkeypatch.setattr(web, "_start_worker", fake_start)
    try:
        blocked = client.post(f"/api/jobs/{job.id}/resume")
        assert blocked.status_code == 422
        assert blocked.json()["detail"]["code"] == "provider_unavailable"
        assert checked_models[-1] == alternate
        assert job.public()["can_resume"] is True

        available = True
        resumed = client.post(f"/api/jobs/{job.id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "processing"
        assert resumed.json()["blocked_reason"] is None
        assert checked_models[-1] == alternate
    finally:
        web.jobs.pop(job.id, None)


class CountingWebProvider:
    name = "web_test"

    def __init__(self):
        self.calls = 0

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        self.calls += 1
        return OcrResult("هذا نص عربي صالح من الصفحة.", 0.99, self.name)


def test_publication_failure_is_resumable_without_ocr_and_keeps_filename(
    tmp_path, monkeypatch
):
    job_id = "f" * 32
    store = CheckpointStore(tmp_path, job_id)
    store.initialize()
    source = store.root / "source.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(source)
    document.close()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    options = ConversionOptions(
        mode="force_ocr", preserve_images=False, require_epubcheck=True
    )
    manifest = JobManifest(
        id=job_id,
        filename="كتاب.pdf",
        source_sha256=source_hash,
        source_size=source.stat().st_size,
        page_count=1,
        state=JobState.QUEUED,
        stage="Waiting",
        options=asdict(options),
        contract=processing_contract(source_hash, 1, options, "web_test", None),
        provider_id="web_test",
    )
    store.write_manifest(manifest)
    job = web._job_from_manifest(store, manifest)
    web.jobs[job_id] = job
    provider = CountingWebProvider()
    original_convert = pipeline.convert

    def convert_with_provider(*args, **kwargs):
        return original_convert(*args, ocr_provider=provider, **kwargs)

    monkeypatch.setattr(web, "convert", convert_with_provider)
    original_build = pipeline.build_epub
    builds = 0

    def fail_once(*args, **kwargs):
        nonlocal builds
        builds += 1
        if builds == 1:
            raise EpubValidationError("EPUBCheck unavailable")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(pipeline, "build_epub", fail_once)
    try:
        web._run(job_id, source, options)
        assert job.state == "interrupted"
        assert job.blocked_code == "publication_failed"
        assert job.safe_pages == 1
        assert provider.calls == 1
        assert set(job.outputs) == {"report"}

        response = web.resume_job(job_id)
        assert response["state"] == "processing"
        job.thread.join(timeout=5)
        assert not job.thread.is_alive()
        assert job.state == "complete"
        assert provider.calls == 1
        assert Path(job.outputs["epub"]).name == "كتاب.epub"
        assert not (store.output / "source.epub").exists()
    finally:
        web.jobs.pop(job_id, None)
