from fastapi.testclient import TestClient

from pdf2epub import web
from pdf2epub.settings import clear_runtime_settings
from pdf2epub.web import Job, _append_log, _quality_summary, _safe_pdf_name


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


def test_remote_ocr_requires_explicit_transfer_acknowledgement():
    response = TestClient(web.app).post(
        "/api/jobs",
        files={"file": ("book.pdf", b"%PDF-minimal", "application/pdf")},
        data={"ocr_provider": "mistral"},
    )
    assert response.status_code == 400
    assert "explicit acknowledgement" in response.json()["detail"]


def test_health_endpoint_reports_epubcheck_state():
    response = TestClient(web.app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert isinstance(response.json()["epubcheck_available"], bool)


def test_settings_api_updates_values_without_returning_secrets():
    client = TestClient(web.app)
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


def test_settings_api_rejects_unknown_fields():
    response = TestClient(web.app).put(
        "/api/settings", json={"values": {"NOT_A_SETTING": "value"}}
    )
    assert response.status_code == 400
    assert "Unknown settings" in response.json()["detail"]


def test_home_contains_settings_dialog():
    response = TestClient(web.app).get("/")
    assert response.status_code == 200
    assert 'id="settings-button"' in response.text
    assert 'id="settings-dialog"' in response.text
    assert 'id="log-stream"' in response.text


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
