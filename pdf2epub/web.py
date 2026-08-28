from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .checkpoints import (
    CheckpointError,
    CheckpointStore,
    ConversionControl,
    JobManifest,
    PauseRequested,
    processing_contract,
)
from .models import ConversionOptions, JobState, require_transition
from .ocr import (
    OLLAMA_BOOK_MODELS,
    OLLAMA_QUALITY_MODEL,
    OcrSettings,
    provider_descriptor,
    provider_names,
    provider_status,
)
from .pipeline import ConversionIntegrityError, ConversionPublicationError, convert
from .settings import clear_runtime_settings, public_settings, update_runtime_settings

app = FastAPI(title="Arabic PDF → EPUB", version="0.3.0")
JOB_ROOT = (
    Path(
        os.getenv(
            "PDF2EPUB_DATA_DIR",
            Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            / "pdf2epub",
        )
    )
    / "jobs"
)
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_JOB_LOGS = 2_000


@dataclass
class Job:
    id: str
    state: str = "queued"
    stage: str = "Waiting"
    done: int = 0
    total: int = 1
    error: str | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    logs: list[dict] = field(default_factory=list)
    log_sequence: int = 0
    filename: str = "book.pdf"
    safe_pages: int = 0
    current_page: int | None = None
    created_at: str = field(
        default_factory=lambda: (
            datetime.now().astimezone().isoformat(timespec="seconds")
        )
    )
    updated_at: str | None = None
    paused_at: str | None = None
    completed_at: str | None = None
    recoverable: bool = True
    blocked_reason: str | None = None
    blocked_code: str | None = None
    page_count: int = 0
    store: CheckpointStore | None = field(default=None, repr=False)
    manifest: JobManifest | None = field(default=None, repr=False)
    pause_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    worker_token: str | None = field(default=None, repr=False)

    @property
    def percent(self) -> int:
        return round(self.done / max(self.total, 1) * 100)

    def public(self) -> dict:
        data = {
            "id": self.id,
            "state": self.state,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "outputs": dict(self.outputs),
            "logs": list(self.logs),
            "filename": self.filename,
            "safe_pages": self.safe_pages,
            "current_page": self.current_page,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "paused_at": self.paused_at,
            "completed_at": self.completed_at,
            "recoverable": self.recoverable,
            "blocked_reason": self.blocked_reason,
            "blocked_code": self.blocked_code,
            "page_count": self.page_count,
            "can_pause": self.state == JobState.PROCESSING,
            "can_resume": self.state in {JobState.PAUSED, JobState.INTERRUPTED}
            and self.blocked_code
            in {None, "provider_unavailable", "publication_failed"},
            "eta_seconds": self._eta(),
        }
        data["percent"] = self.percent
        return data

    def _eta(self) -> int | None:
        if not self.manifest or len(self.manifest.page_durations) < 3:
            return None
        durations = sorted(self.manifest.page_durations[-20:])
        median = durations[len(durations) // 2]
        return round(max(self.page_count - self.safe_pages, 0) * median)


jobs: dict[str, Job] = {}
lock = threading.Lock()
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class SettingsUpdate(BaseModel):
    values: dict[str, str]


def _append_log(job: Job, message: str, level: str = "info") -> None:
    job.log_sequence += 1
    job.logs.append(
        {
            "id": job.log_sequence,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": level,
            "message": message,
        }
    )
    if len(job.logs) > MAX_JOB_LOGS:
        del job.logs[: len(job.logs) - MAX_JOB_LOGS]
    job.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")


def _transition_job(job: Job, target: JobState) -> None:
    job.state = require_transition(JobState(job.state), target)


def _persist(job: Job) -> None:
    if not job.manifest or not job.store:
        return
    manifest = job.manifest
    target_state = JobState(job.state)
    if manifest.state != target_state:
        manifest.state = require_transition(manifest.state, target_state)
    manifest.stage = job.stage
    job.safe_pages = max(job.safe_pages, manifest.safe_pages)
    manifest.current_page = job.current_page
    manifest.paused_at = job.paused_at
    manifest.completed_at = job.completed_at
    manifest.recoverable = job.recoverable
    manifest.blocked_reason = job.blocked_reason
    manifest.blocked_code = job.blocked_code
    manifest.outputs = {
        kind: str(Path(path).resolve().relative_to(job.store.root.resolve()))
        for kind, path in job.outputs.items()
    }
    manifest.logs = list(job.logs)
    manifest.log_sequence = job.log_sequence
    manifest.lease = (
        {"process": PROCESS_ID, "token": job.worker_token} if job.worker_token else None
    )
    job.store.write_manifest(manifest)
    job.safe_pages = manifest.safe_pages


def _job_from_manifest(store: CheckpointStore, manifest: JobManifest) -> Job:
    outputs = {
        kind: str(store.root / relative) for kind, relative in manifest.outputs.items()
    }
    return Job(
        id=manifest.id,
        state=manifest.state.value,
        stage=manifest.stage,
        outputs=outputs,
        logs=list(manifest.logs),
        log_sequence=manifest.log_sequence,
        filename=manifest.filename,
        safe_pages=manifest.safe_pages,
        current_page=manifest.current_page,
        created_at=manifest.created_at,
        updated_at=manifest.updated_at,
        paused_at=manifest.paused_at,
        completed_at=manifest.completed_at,
        recoverable=manifest.recoverable,
        blocked_reason=manifest.blocked_reason,
        blocked_code=manifest.blocked_code,
        page_count=manifest.page_count,
        done=manifest.safe_pages,
        total=max(manifest.page_count, 1),
        store=store,
        manifest=manifest,
    )


def _quality_summary(report: dict) -> dict:
    summary = dict(report)
    page_details = summary.pop("page_details", [])
    summary["page_details_count"] = len(page_details)
    summary["full_page_details"] = "Available in the downloaded quality report"
    return summary


def _recover_completed_job(job_id: str) -> Job | None:
    if not JOB_ID_RE.fullmatch(job_id):
        return None
    store = CheckpointStore(JOB_ROOT, job_id)
    manifest_path = store.root / "job.json"
    if manifest_path.is_file():
        try:
            manifest = store.load_manifest()
            store.contiguous_pages(manifest)
            job = _job_from_manifest(store, manifest)
            with lock:
                jobs[job_id] = job
            return job
        except CheckpointError:
            return None
    output_dir = store.output
    report = output_dir / "quality-report.json"
    epubs = list(output_dir.glob("*.epub")) if output_dir.is_dir() else []
    if not report.is_file() or len(epubs) != 1:
        return None
    job = Job(
        job_id,
        state="complete",
        stage="Complete",
        done=1,
        total=1,
        outputs={"epub": str(epubs[0]), "report": str(report)},
        store=store,
    )
    _append_log(job, "Recovered completed conversion after server restart", "success")
    with lock:
        jobs[job_id] = job
    return job


def _get_job(job_id: str) -> Job | None:
    with lock:
        job = jobs.get(job_id)
    return job or _recover_completed_job(job_id)


def _safe_pdf_name(filename: str | None) -> str:
    stem = Path(filename or "book.pdf").stem
    safe_stem = re.sub(r"[^\w .()\-]+", "_", stem, flags=re.UNICODE).strip(" ._")
    return f"{safe_stem[:120] or 'book'}.pdf"


def _update(job_id: str, stage: str, done: int, total: int) -> None:
    with lock:
        job = jobs[job_id]
        if stage == "Building EPUB":
            _transition_job(job, JobState.BUILDING)
            job.current_page = None
        changed = (job.stage, job.done, job.total) != (stage, done, total)
        job.stage, job.done, job.total = stage, done, total
        if changed:
            _append_log(job, f"{stage} — {done}/{total} ({job.percent}%)")
        if stage.startswith("OCR page") and " completed " in stage and job.manifest:
            durations = [
                float(value)
                for value in re.findall(r"total_duration=([0-9.]+)s", stage)
            ]
            if durations:
                job.manifest.page_durations.append(sum(durations))
                job.manifest.page_durations = job.manifest.page_durations[-100:]
        if stage.startswith(("Analyzing", "OCR page")):
            job.current_page = min(done + 1, total) if done < total else None
        job.safe_pages = job.manifest.safe_pages if job.manifest else done
        if job.state == JobState.BUILDING:
            _persist(job)


def _run(job_id: str, pdf: Path, options: ConversionOptions) -> None:
    try:
        with lock:
            job = jobs[job_id]
            _transition_job(job, JobState.PROCESSING)
            job.pause_event.clear()
            _append_log(job, "Conversion worker started")
            _persist(job)
            control = ConversionControl(
                job.store, job.manifest, pause_requested=job.pause_event.is_set
            )
        outputs = convert(
            pdf,
            pdf.parent / "output",
            options,
            lambda s, d, t: _update(job_id, s, d, t),
            control=control,
            output_stem=Path(job.filename).stem,
        )
        report = json.loads(outputs["report"].read_text(encoding="utf-8"))
        stage = (
            "Complete — review required"
            if report.get("integrity", {}).get("status") == "review"
            else "Complete"
        )
        with lock:
            job = jobs[job_id]
            job.outputs = {kind: str(path) for kind, path in outputs.items()}
            _transition_job(job, JobState.COMPLETE)
            job.stage, job.done, job.total = stage, 1, 1
            job.safe_pages = job.page_count
            job.completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            job.worker_token = None
            _append_log(job, stage, "success")
            _persist(job)
    except PauseRequested:
        with lock:
            job = jobs[job_id]
            job.safe_pages = job.manifest.safe_pages
            job.current_page = None
            _transition_job(job, JobState.PAUSED)
            job.stage = "Paused"
            job.paused_at = datetime.now().astimezone().isoformat(timespec="seconds")
            job.worker_token = None
            _append_log(job, "Paused after saving the current page", "success")
            _persist(job)
    except ConversionIntegrityError as exc:
        with lock:
            job = jobs[job_id]
            _transition_job(job, JobState.FAILED)
            job.stage = "Content integrity failed"
            job.error = str(exc)
            job.outputs = {"report": str(exc.report_path)}
            _append_log(job, f"Content integrity failed — {exc}", "error")
            job.worker_token = None
            _persist(job)
    except ConversionPublicationError as exc:
        with lock:
            job = jobs[job_id]
            job.safe_pages = job.manifest.safe_pages
            if job.safe_pages == job.page_count:
                _transition_job(job, JobState.INTERRUPTED)
                job.stage = "Publication interrupted"
                job.blocked_code = "publication_failed"
                job.blocked_reason = "اكتمل OCR، لكن تعذر بناء أو التحقق من EPUB. أصلح EPUBCheck ثم أعد المتابعة."
                job.outputs = {"report": str(exc.report_path)}
                _append_log(job, f"Publication interrupted — {exc}", "error")
            else:
                _transition_job(job, JobState.FAILED)
                job.stage = "Publication failed"
                job.error = str(exc)
                job.outputs = {"report": str(exc.report_path)}
                _append_log(job, f"Publication failed — {exc}", "error")
            job.worker_token = None
            _persist(job)
    except Exception as exc:  # noqa: BLE001 - background job boundary records all failures
        with lock:
            job = jobs[job_id]
            _transition_job(job, JobState.FAILED)
            job.stage = "Failed"
            job.error = str(exc)
            _append_log(job, f"Conversion failed — {exc}", "error")
            job.worker_token = None
            _persist(job)


PROCESS_ID = uuid4().hex


def _start_worker(job: Job, pdf: Path, options: ConversionOptions) -> None:
    if job.thread and job.thread.is_alive():
        return
    token = uuid4().hex
    job.worker_token = token
    thread = threading.Thread(
        target=_run,
        args=(job.id, pdf, options),
        daemon=True,
        name=f"pdf2epub-{job.id[:8]}",
    )
    job.thread = thread
    _persist(job)
    thread.start()


def _recover_jobs() -> None:
    if not JOB_ROOT.is_dir():
        return
    for child in JOB_ROOT.iterdir():
        if not child.is_dir() or not JOB_ID_RE.fullmatch(child.name):
            continue
        try:
            store = CheckpointStore(JOB_ROOT, child.name)
            manifest = store.load_manifest()
            try:
                store.contiguous_pages(manifest)
            except CheckpointError as exc:
                if manifest.state in {
                    JobState.PROCESSING,
                    JobState.PAUSE_REQUESTED,
                    JobState.BUILDING,
                }:
                    manifest.state = require_transition(
                        manifest.state, JobState.INTERRUPTED
                    )
                manifest.stage = "Recovery blocked"
                manifest.current_page = None
                manifest.lease = None
                manifest.blocked_code = exc.code
                manifest.blocked_reason = str(exc)
                store.write_manifest(manifest)
                jobs[child.name] = _job_from_manifest(store, manifest)
                continue
            if manifest.state in {
                JobState.PROCESSING,
                JobState.PAUSE_REQUESTED,
                JobState.BUILDING,
            }:
                manifest.state = require_transition(
                    manifest.state, JobState.INTERRUPTED
                )
                manifest.stage = "Interrupted"
                manifest.current_page = None
                manifest.lease = None
                manifest.blocked_reason = None
                store.write_manifest(manifest)
            jobs[child.name] = _job_from_manifest(store, manifest)
        except CheckpointError:
            continue


@app.on_event("startup")
def startup_recovery() -> None:
    _recover_jobs()


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX_HTML


@app.get("/api/ocr/providers")
def ocr_providers() -> dict:
    return {"providers": provider_status()}


@app.get("/api/health")
def health() -> dict:
    epubcheck_available = shutil.which("epubcheck") is not None
    warnings = []
    if not epubcheck_available:
        warnings.append(
            {
                "code": "epubcheck_missing",
                "severity": "error",
                "component": "epubcheck",
                "message": (
                    "لا يمكن نشر ملف EPUB حتى تثبيت EPUBCheck وإتاحته ضمن PATH."
                ),
            }
        )
    tesseract = next(
        (item for item in provider_status() if item["id"] == "tesseract"), None
    )
    if tesseract and not tesseract["configured"]:
        warnings.append(
            {
                "code": "tesseract_missing",
                "severity": "warning",
                "component": "tesseract",
                "message": (
                    "محرك Tesseract المحلي غير متاح؛ ثبّته لاستخدام OCR المحلي."
                ),
            }
        )
    elif tesseract and not tesseract["ready"]:
        warnings.append(
            {
                "code": "tesseract_arabic_missing",
                "severity": "warning",
                "component": "tesseract",
                "message": (
                    "بيانات اللغة العربية ara غير متاحة في Tesseract؛ ثبّتها "
                    "لتفعيل OCR العربي المحلي."
                ),
            }
        )
    return {
        "status": "ok",
        "epubcheck_available": epubcheck_available,
        "warnings": warnings,
    }


@app.get("/api/settings")
def get_settings() -> dict:
    return {"fields": public_settings()}


@app.put("/api/settings")
def save_settings(payload: SettingsUpdate) -> dict:
    try:
        update_runtime_settings(payload.values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"fields": public_settings(), "providers": provider_status()}


@app.delete("/api/settings")
def reset_settings() -> dict:
    clear_runtime_settings()
    return {"fields": public_settings(), "providers": provider_status()}


@app.post("/api/jobs", status_code=202)
def create_job(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI dependency declaration
    mode: str = Form("automatic"),
    preserve_images: bool = Form(True),
    detect_chapters: bool = Form(True),
    detect_footnotes: bool = Form(True),
    fix_bidi: bool = Form(True),
    ocr_bad_pages: bool = Form(True),
    ocr_provider: str = Form("tesseract"),
    ollama_model: str = Form(OLLAMA_QUALITY_MODEL),
    remote_acknowledged: bool = Form(False),
) -> dict:
    if mode not in {"automatic", "prefer_text", "force_ocr"}:
        raise HTTPException(400, "Invalid mode")
    if ocr_provider not in provider_names():
        raise HTTPException(400, "Invalid OCR provider")
    if ocr_provider == "ollama" and ollama_model not in OLLAMA_BOOK_MODELS:
        raise HTTPException(400, "Invalid Ollama OCR model")
    descriptor = provider_descriptor(ocr_provider)
    if descriptor and descriptor.remote and not remote_acknowledged:
        raise HTTPException(
            400, "Remote OCR requires explicit acknowledgement of page-image transfer"
        )
    job_id = uuid4().hex
    store = CheckpointStore(JOB_ROOT, job_id)
    store.initialize()
    pdf = store.root / "source.pdf"
    safe_filename = _safe_pdf_name(file.filename)
    digest = hashlib.sha256()
    size = 0
    with pdf.open("wb") as target:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                target.close()
                shutil.rmtree(store.root)
                raise HTTPException(413, "PDF exceeds the 500 MB upload limit")
            target.write(chunk)
            digest.update(chunk)
        target.flush()
        os.fsync(target.fileno())
    if pdf.read_bytes()[:5] != b"%PDF-":
        shutil.rmtree(store.root)
        raise HTTPException(400, "The uploaded file is not a PDF")
    options = ConversionOptions(
        mode=mode,
        preserve_images=preserve_images,
        detect_chapters=detect_chapters,
        detect_footnotes=detect_footnotes,
        fix_bidi=fix_bidi,
        ocr_bad_pages=ocr_bad_pages,
        ocr_provider=ocr_provider,
        ollama_model=ollama_model if ocr_provider == "ollama" else None,
    )
    try:
        document = pymupdf.open(pdf)
        page_count = len(document)
        document.close()
    except Exception as exc:
        shutil.rmtree(store.root)
        raise HTTPException(400, "The uploaded PDF cannot be opened") from exc
    options_dict = asdict(options)
    contract = processing_contract(
        digest.hexdigest(),
        page_count,
        options,
        ocr_provider,
        options.ollama_model,
    )
    manifest = JobManifest(
        id=job_id,
        filename=safe_filename,
        source_sha256=digest.hexdigest(),
        source_size=size,
        page_count=page_count,
        state=JobState.QUEUED,
        stage="Waiting",
        options=options_dict,
        contract=contract,
        provider_id=ocr_provider,
        model=options.ollama_model,
    )
    with lock:
        job = Job(
            job_id,
            filename=safe_filename,
            page_count=page_count,
            total=max(page_count, 1),
            store=store,
            manifest=manifest,
        )
        _append_log(job, f"Queued conversion for {safe_filename}")
        jobs[job_id] = job
        _persist(job)
        try:
            _start_worker(job, pdf, options)
        except Exception as exc:
            _transition_job(job, JobState.FAILED)
            job.error = str(exc)
            job.stage = "Failed"
            _persist(job)
            raise HTTPException(500, "Could not start conversion worker") from exc
    return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs")
def list_jobs() -> dict:
    with lock:
        ordered = sorted(jobs.values(), key=lambda item: item.created_at, reverse=True)
        summaries = []
        for job in ordered[:100]:
            data = job.public()
            data["last_event"] = data["logs"][-1] if data["logs"] else None
            data.pop("logs", None)
            data.pop("outputs", None)
            summaries.append(data)
    return {"jobs": summaries}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.state == JobState.PAUSE_REQUESTED:
            return job.public()
        if job.state != JobState.PROCESSING:
            raise HTTPException(
                409,
                {"code": "cannot_pause", "message": "لا يمكن إيقاف هذه المهمة مؤقتاً"},
            )
        _transition_job(job, JobState.PAUSE_REQUESTED)
        job.stage = "Pause requested"
        job.pause_event.set()
        _append_log(job, "Pause requested; finishing the current page")
        _persist(job)
        return job.public()


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.thread and job.thread.is_alive():
            return job.public()
        if job.state not in {JobState.PAUSED, JobState.INTERRUPTED}:
            raise HTTPException(
                409, {"code": "cannot_resume", "message": "لا يمكن متابعة هذه المهمة"}
            )
        if job.manifest.safe_pages < job.manifest.page_count:
            settings = None
            if job.manifest.provider_id == "ollama":
                settings = OcrSettings.from_env()
                settings.ollama_model = job.manifest.model or settings.ollama_model
            statuses = provider_status(settings) if settings else provider_status()
            descriptor = next(
                (item for item in statuses if item["id"] == job.manifest.provider_id),
                None,
            )
            if not descriptor or not descriptor.get("ready"):
                job.blocked_code = "provider_unavailable"
                job.blocked_reason = (
                    "محرك OCR أو النموذج المحدد غير جاهز؛ أعد توفيره ثم حاول مجدداً."
                )
                _persist(job)
                raise HTTPException(
                    422,
                    {"code": job.blocked_code, "message": job.blocked_reason},
                )
        try:
            job.store.contiguous_pages(job.manifest)
        except CheckpointError as exc:
            job.blocked_code = exc.code
            job.blocked_reason = str(exc)
            _persist(job)
            raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from exc
        source = job.store.root / "source.pdf"
        if (
            not source.is_file()
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest()
            != job.manifest.source_sha256
        ):
            job.blocked_code = "source_mismatch"
            job.blocked_reason = (
                "ملف PDF المصدر مفقود أو تغير؛ لا يمكن خلط نتائج من مصدر مختلف."
            )
            _persist(job)
            raise HTTPException(
                422, {"code": job.blocked_code, "message": job.blocked_reason}
            )
        try:
            document = pymupdf.open(source)
            actual_pages = len(document)
            document.close()
        except Exception as exc:
            raise HTTPException(
                422,
                {"code": "source_unreadable", "message": "تعذر فتح ملف PDF المصدر."},
            ) from exc
        if actual_pages != job.manifest.page_count:
            raise HTTPException(
                422,
                {"code": "source_mismatch", "message": "تغير عدد صفحات ملف المصدر."},
            )
        current_contract = processing_contract(
            job.manifest.source_sha256,
            job.manifest.page_count,
            ConversionOptions(**job.manifest.options),
            job.manifest.provider_id,
            job.manifest.model,
        )
        if current_contract != job.manifest.contract:
            job.blocked_code = "contract_mismatch"
            job.blocked_reason = "تغير عقد المعالجة؛ ابدأ مهمة جديدة بهذه الإعدادات."
            _persist(job)
            raise HTTPException(
                422, {"code": job.blocked_code, "message": job.blocked_reason}
            )
        job.blocked_code = job.blocked_reason = None
        _transition_job(job, JobState.PROCESSING)
        job.stage = "Resuming"
        job.paused_at = None
        _append_log(job, "Conversion resumed")
        _start_worker(
            job,
            job.store.root / "source.pdf",
            ConversionOptions(**job.manifest.options),
        )
        return job.public()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    with lock:
        data = job.public()
    if data["outputs"]:
        data["downloads"] = {
            kind: f"/api/jobs/{job_id}/download/{kind}" for kind in data["outputs"]
        }
        report = Path(data["outputs"].get("report", ""))
        if report.exists():
            data["quality"] = _quality_summary(
                json.loads(report.read_text(encoding="utf-8"))
            )
    data.pop("outputs", None)
    return data


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    job = _get_job(job_id)
    with lock:
        path = Path(job.outputs.get(kind, "")) if job else None
    if (
        not path
        or path.is_symlink()
        or not path.is_file()
        or not job.store
        or not path.resolve().is_relative_to(job.store.root.resolve())
    ):
        raise HTTPException(404, "Output not found")
    media = "application/epub+zip" if kind == "epub" else "application/json"
    return FileResponse(path, media_type=media, filename=path.name)


INDEX_HTML = """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>تحويل PDF عربي إلى EPUB</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Amiri:wght@400;700&family=Tajawal:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
:root{font-family:Tajawal,system-ui,sans-serif;color:#17201b;background:#eef3ef}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:2rem}.card{width:min(760px,100%);background:#fff;padding:2rem;border-radius:22px;box-shadow:0 20px 60px #173b2520}.navbar{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}.title-wrap{min-width:0}h1,h2,.lead{font-family:Amiri,serif;font-weight:400}h1{margin:0 0 .35rem;color:#174f32;font-size:2.35rem;line-height:1.35}.lead{font-size:1.2rem;line-height:1.9;margin-top:0}.cog{width:46px;height:46px;padding:0;border-radius:50%;font-size:1.35rem;flex:0 0 auto}.capability-warnings{display:grid;gap:.65rem;margin:0 0 1rem}.capability-warnings.hidden{display:none}.capability-warning{margin:0;padding:.8rem 1rem;border:1px solid #d7a531;border-radius:10px;background:#fff8df;color:#654900;line-height:1.7}.capability-warning.error-level{border:2px solid #a31818;background:#fff0ef;color:#851313;font-weight:700}.drop{border:2px dashed #83a892;border-radius:14px;padding:2rem;text-align:center;background:#f8fbf9}fieldset{border:0;padding:1rem 0;display:grid;gap:.7rem}select,input[type=file],.settings-input{width:100%;padding:.7rem;border:1px solid #b8c8bd;border-radius:8px;background:#fff;font-family:inherit}button,.download{border:0;border-radius:10px;padding:.8rem 1.2rem;background:#176b42;color:#fff;font-family:inherit;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}.secondary{background:#65736a}.danger{background:#9c3b35}.bar{height:12px;background:#dce7df;border-radius:9px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:#27a867;transition:width .3s}.hidden{display:none}.error{color:#a31818}.success{color:#176b42}pre{direction:ltr;text-align:left;white-space:pre-wrap;background:#f4f6f4;padding:1rem;border-radius:10px;font-size:.8rem}dialog{width:min(720px,calc(100% - 2rem));max-height:88vh;border:0;border-radius:18px;padding:0;box-shadow:0 25px 90px #0005}dialog::backdrop{background:#07150e99}.dialog-body{padding:1.5rem;overflow:auto;max-height:88vh}.dialog-head{display:flex;justify-content:space-between;align-items:center;gap:1rem}.dialog-head h2{margin:0;color:#174f32;font-size:1.8rem}.close{background:transparent;color:#26372d;font-size:1.5rem;padding:.2rem .6rem}.settings-group{border-top:1px solid #dbe5de;margin-top:1rem;padding-top:1rem}.settings-group h3{margin:.2rem 0 .8rem;color:#174f32}.setting{display:grid;gap:.3rem;margin-bottom:.8rem}.setting small{color:#65736a}.setting-row{display:flex;align-items:center;gap:.5rem}.setting-row .settings-input{flex:1}.dialog-actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1rem}.settings-note{background:#f2f7f3;border-radius:10px;padding:.8rem;line-height:1.7}@media(max-width:560px){body{padding:.75rem}.card{padding:1.2rem}h1{font-size:1.9rem}.lead{font-size:1rem}.drop{padding:1.2rem}}
.log-wrap{margin:1rem 0}.log-wrap h3{margin:0 0 .45rem;color:#174f32}.log-stream{height:190px;overflow:auto;background:#101813;color:#d8eadf;border-radius:10px;padding:.75rem;font:500 .8rem/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;scrollbar-color:#52705e #101813}.log-line{display:grid;grid-template-columns:6rem 1fr;gap:.65rem;padding:.15rem 0;border-bottom:1px solid #ffffff0d;direction:ltr}.log-time{color:#8caf98}.log-message{direction:auto;unicode-bidi:plaintext;text-align:start;overflow-wrap:anywhere}.log-line.success .log-message{color:#75d99c}.log-line.error .log-message{color:#ff9d96}.jobs{margin:1.5rem 0;border-top:1px solid #dbe5de;padding-top:1rem}.job-list{display:grid;gap:.7rem}.job-card{border:1px solid #cbd9cf;border-radius:12px;padding:.8rem;background:#f8fbf9}.job-head{display:flex;justify-content:space-between;gap:.5rem}.badge{border-radius:99px;padding:.2rem .6rem;background:#dce7df;font-size:.85rem}.badge.processing{background:#d9f0e2;color:#12512f}.badge.paused,.badge.interrupted{background:#fff0c7;color:#654900}.badge.failed{background:#ffe2df;color:#851313}.job-actions{display:flex;gap:.5rem;flex-wrap:wrap}.state-banner{padding:.8rem;border-radius:10px;background:#f2f7f3;line-height:1.7}.state-banner.interrupted{background:#fff8df}.control-row{display:flex;gap:.6rem;flex-wrap:wrap;margin:.8rem 0}button:focus-visible,.download:focus-visible{outline:3px solid #e4a62a;outline-offset:3px}button:disabled{opacity:.55;cursor:not-allowed}@media(max-width:560px){.log-line{grid-template-columns:5.2rem 1fr}}
</style>
</head>
<body>
<main class="card">
<nav class="navbar" aria-label="شريط الأدوات"><div class="title-wrap"><h1>تحويل PDF عربي إلى EPUB</h1><p class="lead">يفحص كل صفحة ويستخدم النص المضمّن متى كان موثوقاً، ثم يلجأ إلى OCR للصفحات الرديئة أو المصوّرة.</p></div><button id="settings-button" type="button" class="cog" title="إعدادات OCR" aria-label="فتح إعدادات OCR">⚙</button></nav>
<section id="capability-warnings" class="capability-warnings hidden" role="alert" aria-live="polite"></section>
<section class="jobs" aria-labelledby="jobs-heading"><h2 id="jobs-heading">التحويلات</h2><div id="job-list" class="job-list"><p>جارٍ تحميل التحويلات…</p></div></section>
<form id="form"><div class="drop"><input required name="file" type="file" accept="application/pdf,.pdf"></div><fieldset><label>طريقة المعالجة <select name="mode"><option value="automatic">تلقائي</option><option value="prefer_text">تفضيل النص المضمّن</option><option value="force_ocr">فرض OCR</option></select></label><label>محرك OCR <select name="ocr_provider" id="ocr-provider"><option value="">جارٍ تحميل المحركات…</option></select></label><small id="provider-note">يُرسل محتوى الصفحات إلى الخدمة المحددة فقط عندما تحتاج الصفحة إلى OCR.</small>
<label id="ollama-model-row">نموذج Ollama لهذا الكتاب <select name="ollama_model" id="ollama-model"><option value="qwen3-vl:32b-instruct-gpu">جودة — Qwen3 VL 32B Instruct</option><option value="qwen3-vl:30b-instruct-gpu">سريع — Qwen3 VL 30B Instruct</option><option value="qwen3.8:27b-q4_K_M">متوازن — Qwen3.8 27B Q4</option></select></label>
<label><input type="checkbox" name="preserve_images" checked> حفظ الرسوم والصور الداخلية فقط — لا تُحفظ صور الصفحات الكاملة</label><label><input type="checkbox" name="detect_chapters" checked> اكتشاف الفصول</label><label><input type="checkbox" name="detect_footnotes" checked> اكتشاف الحواشي</label><label><input type="checkbox" name="fix_bidi" checked> ضبط اتجاه العربية والإنجليزية</label><label><input type="checkbox" name="ocr_bad_pages" checked> OCR للصفحات الرديئة</label></fieldset><button>تحويل</button></form>
<section id="status" class="hidden" aria-busy="false"><h2 id="stage">جارٍ التحليل</h2><div id="state-message" class="state-banner" aria-live="polite"></div><p id="safe-progress"></p><p id="current-activity"></p><p id="eta"></p><div class="bar"><i id="progress"></i></div><p id="percent"></p><div class="control-row"><button id="pause" type="button" class="secondary hidden" aria-label="إيقاف التحويل مؤقتاً">إيقاف مؤقت</button><button id="resume" type="button" class="hidden" aria-label="متابعة التحويل">متابعة التحويل</button></div><section class="log-wrap" aria-labelledby="log-heading"><h3 id="log-heading">سجل التحويل المباشر</h3><div id="log-stream" class="log-stream" role="log" aria-live="polite" aria-relevant="additions"></div></section><p id="error" class="error"></p><div id="links"></div><pre id="report" class="hidden"></pre><button id="restart" type="button" class="secondary hidden">ابدأ من جديد</button></section>
</main>
<dialog id="settings-dialog"><div class="dialog-body"><div class="dialog-head"><h2>إعدادات OCR</h2><button id="settings-close" type="button" class="close" aria-label="إغلاق">×</button></div><p class="settings-note">قيم هذه الصفحة تتغلب على قيم <code>.env</code> حتى إعادة تشغيل الخادم. المفاتيح السرية لا تُعاد إلى المتصفح أبداً؛ اترك الحقل فارغاً للاحتفاظ بالقيمة الحالية.</p><form id="settings-form"><div id="settings-fields"></div><p id="settings-message"></p><div class="dialog-actions"><button type="submit">حفظ وتحديث المحركات</button><button id="settings-reset" type="button" class="secondary">العودة إلى قيم .env</button></div></form></div></dialog>
<script>
const form=document.querySelector('#form'),statusBox=document.querySelector('#status'),restart=document.querySelector('#restart'),providerSelect=document.querySelector('#ocr-provider'),dialog=document.querySelector('#settings-dialog'),settingsForm=document.querySelector('#settings-form'),settingsFields=document.querySelector('#settings-fields'),settingsMessage=document.querySelector('#settings-message'),logStream=document.querySelector('#log-stream'),capabilityWarnings=document.querySelector('#capability-warnings'),pauseButton=document.querySelector('#pause'),resumeButton=document.querySelector('#resume'),jobList=document.querySelector('#job-list');
let run=0,providers=[],lastLogId=0,activeJob=null;
const stateLabels={queued:'في الانتظار',processing:'قيد المعالجة',pause_requested:'جارٍ طلب الإيقاف',paused:'متوقف مؤقتاً',interrupted:'جاهز للاستعادة',building:'تجميع EPUB',complete:'مكتمل',failed:'فشل'};
const sourceLabels={runtime:'من صفحة الإعدادات',environment:'من ملف .env',default:'قيمة افتراضية'};
async function jsonRequest(url,options={}){const response=await fetch(url,options);const data=await response.json();if(!response.ok){const detail=data.detail;throw new Error(typeof detail==='object'&&detail?detail.message:(detail||'تعذر إكمال الطلب'))}return data}
function renderWarnings(warnings){capabilityWarnings.replaceChildren();for(const warning of warnings){const message=document.createElement('p');message.className='capability-warning'+(warning.severity==='error'?' error-level':'');message.textContent=warning.message;capabilityWarnings.append(message)}capabilityWarnings.classList.toggle('hidden',warnings.length===0)}
async function loadHealth(){try{const data=await jsonRequest('/api/health');renderWarnings(data.warnings||[])}catch(error){renderWarnings([{severity:'warning',message:'تعذر التحقق من جاهزية أدوات النشر وOCR: '+error.message}])}}
function updateModelVisibility(){document.querySelector('#ollama-model-row').classList.toggle('hidden',providerSelect.value!=='ollama')}
async function loadProviders(){try{const data=await jsonRequest('/api/ocr/providers');providers=data.providers;providerSelect.replaceChildren();for(const provider of providers){const option=document.createElement('option');option.value=provider.id;option.disabled=!provider.ready;option.textContent=provider.label+(provider.remote?' — خارجي':' — محلي')+(provider.ready?'':' — غير جاهز');providerSelect.append(option)}const ollama=providers.find(provider=>provider.id==='ollama');if(ollama&&ollama.book_models?.includes(ollama.model))document.querySelector('#ollama-model').value=ollama.model;const first=providers.find(provider=>provider.ready);if(first){providerSelect.value=first.id;document.querySelector('#provider-note').textContent=first.detail}else{providerSelect.value='';document.querySelector('#provider-note').textContent='لا يوجد محرك OCR جاهز. افتح زر الإعدادات ⚙ لإعداده.'}updateModelVisibility()}catch(error){document.querySelector('#provider-note').textContent=error.message}}
providerSelect.onchange=()=>{const selected=providers.find(provider=>provider.id===providerSelect.value);if(selected)document.querySelector('#provider-note').textContent=selected.detail;updateModelVisibility()};
function settingField(field){const wrapper=document.createElement('label');wrapper.className='setting';const title=document.createElement('span');title.textContent=field.label;const row=document.createElement('span');row.className='setting-row';const input=document.createElement('input');input.className='settings-input';input.name=field.key;input.type=field.secret?'password':(['PDF2EPUB_OCR_TIMEOUT','PDF2EPUB_OLLAMA_NUM_CTX'].includes(field.key)?'number':'text');input.placeholder=field.placeholder||'';input.value=field.value||'';input.autocomplete=field.secret?'new-password':'off';row.append(input);if(field.secret){const clearLabel=document.createElement('label');const clear=document.createElement('input');clear.type='checkbox';clear.dataset.clearKey=field.key;clearLabel.append(clear,' مسح');row.append(clearLabel)}const note=document.createElement('small');note.textContent=(sourceLabels[field.source]||field.source)+(field.secret&&field.configured?' — توجد قيمة محفوظة':'');wrapper.append(title,row,note);return wrapper}
async function loadSettings(){settingsMessage.textContent='جارٍ تحميل الإعدادات…';settingsFields.replaceChildren();try{const data=await jsonRequest('/api/settings');const groups=new Map();for(const field of data.fields){if(!groups.has(field.section)){const group=document.createElement('section');group.className='settings-group';const heading=document.createElement('h3');heading.textContent=field.section;group.append(heading);groups.set(field.section,group);settingsFields.append(group)}groups.get(field.section).append(settingField(field))}settingsMessage.textContent=''}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}}
document.querySelector('#settings-button').onclick=async()=>{dialog.showModal();await loadSettings()};document.querySelector('#settings-close').onclick=()=>dialog.close();
settingsForm.onsubmit=async event=>{event.preventDefault();const values={};for(const input of settingsForm.querySelectorAll('.settings-input')){if(input.type!=='password'||input.value)values[input.name]=input.value}for(const clear of settingsForm.querySelectorAll('[data-clear-key]:checked'))values[clear.dataset.clearKey]='';settingsMessage.className='';settingsMessage.textContent='جارٍ الحفظ…';try{await jsonRequest('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});settingsMessage.className='success';settingsMessage.textContent='تم الحفظ في ذاكرة الخادم.';await Promise.all([loadProviders(),loadHealth()]);await loadSettings()}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}};
document.querySelector('#settings-reset').onclick=async()=>{if(!confirm('حذف كل قيم الصفحة والعودة إلى .env؟'))return;try{await jsonRequest('/api/settings',{method:'DELETE'});await Promise.all([loadProviders(),loadHealth()]);await loadSettings();settingsMessage.className='success';settingsMessage.textContent='تمت العودة إلى قيم .env.'}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}};
function renderLogs(logs=[]){let appended=false;for(const entry of logs){if(entry.id<=lastLogId)continue;const line=document.createElement('div');line.className='log-line '+entry.level;const time=document.createElement('span');time.className='log-time';const parsed=new Date(entry.timestamp);time.textContent=Number.isNaN(parsed.getTime())?entry.timestamp:parsed.toLocaleTimeString('ar-SA');const message=document.createElement('span');message.className='log-message';message.textContent=entry.message;line.append(time,message);logStream.append(line);lastLogId=entry.id;appended=true}if(appended)logStream.scrollTop=logStream.scrollHeight}
function durationLabel(seconds){if(seconds==null)return'';const minutes=Math.max(1,Math.round(seconds/60));return'الوقت المتبقي التقريبي: '+minutes+' دقيقة'}
function renderJobState(data){activeJob=data.id;document.querySelector('#stage').textContent=stateLabels[data.state]||data.stage;document.querySelector('#progress').style.width=data.percent+'%';document.querySelector('#percent').textContent=data.percent+'%';document.querySelector('#safe-progress').textContent='تم حفظ '+data.safe_pages+' من '+data.page_count+' صفحة بأمان';document.querySelector('#current-activity').textContent=data.current_page?'تتم معالجة الصفحة '+data.current_page:'';document.querySelector('#eta').textContent=durationLabel(data.eta_seconds);const message=document.querySelector('#state-message');message.className='state-banner '+data.state;const messages={processing:'يمكن طلب الإيقاف المؤقت؛ ستكتمل الصفحة الحالية وتُحفظ أولاً.',pause_requested:'سيتم الإيقاف بعد حفظ الصفحة الحالية.',paused:'يمكن إغلاق المتصفح أو إعادة تشغيل الجهاز؛ سيبقى التقدم المحفوظ.',interrupted:'توقف التشغيل السابق. آخر نقطة حفظ مكتملة آمنة، والمتابعة تحتاج إلى تأكيدك.',building:'اكتمل OCR ويجري الآن تجميع EPUB والتحقق منه. يمكن إعادة البناء من الصفحات المحفوظة بعد إعادة التشغيل.',failed:data.blocked_reason||data.error||'فشل التحويل.',complete:'اكتمل التحويل بنجاح.'};message.textContent=messages[data.state]||data.stage;pauseButton.classList.toggle('hidden',!['processing','pause_requested'].includes(data.state));pauseButton.disabled=data.state!=='processing';resumeButton.classList.toggle('hidden',!['paused','interrupted'].includes(data.state));resumeButton.disabled=!data.can_resume;statusBox.setAttribute('aria-busy',data.state==='pause_requested'?'true':'false');renderLogs(data.logs)}
async function dashboardAction(job,action){openJob(job.id);const button=action==='pause'?pauseButton:resumeButton;button.disabled=true;try{renderJobState(await jsonRequest('/api/jobs/'+job.id+'/'+action,{method:'POST'}));poll(job.id,run);loadJobs()}catch(error){showError(error.message);button.disabled=false}}
async function loadJobs(){try{const data=await jsonRequest('/api/jobs');jobList.replaceChildren();if(!data.jobs.length){jobList.innerHTML='<p>لا توجد تحويلات محفوظة بعد.</p>';return}for(const job of data.jobs){const card=document.createElement('article');card.className='job-card';const head=document.createElement('div');head.className='job-head';const name=document.createElement('strong');name.textContent=job.filename;const badge=document.createElement('span');badge.className='badge '+job.state;badge.textContent=stateLabels[job.state]||job.state;head.append(name,badge);const saved=document.createElement('p');saved.textContent='تم حفظ '+job.safe_pages+' من '+job.page_count+' صفحة بأمان';const activity=document.createElement('p');activity.textContent=job.current_page?'تتم معالجة الصفحة '+job.current_page:'آخر تحديث: '+new Date(job.updated_at||job.created_at).toLocaleString('ar-SA');const actions=document.createElement('div');actions.className='job-actions';if(job.state==='processing'){const primary=document.createElement('button');primary.type='button';primary.textContent='إيقاف مؤقت';primary.onclick=()=>dashboardAction(job,'pause');actions.append(primary)}else if(['paused','interrupted'].includes(job.state)&&job.can_resume){const primary=document.createElement('button');primary.type='button';primary.textContent='متابعة التحويل';primary.onclick=()=>dashboardAction(job,'resume');actions.append(primary)}const view=document.createElement('button');view.type='button';view.className='secondary';view.textContent=job.state==='complete'?'تنزيل':'عرض';view.onclick=()=>openJob(job.id);actions.append(view);card.append(head,saved,activity,actions);jobList.append(card)}}catch(error){jobList.textContent='تعذر تحميل التحويلات: '+error.message}}
function openJob(id){const current=++run;activeJob=id;lastLogId=0;logStream.replaceChildren();history.replaceState(null,'','?job='+encodeURIComponent(id));localStorage.setItem('pdf2epub-active-job',id);statusBox.classList.remove('hidden');form.classList.add('hidden');poll(id,current)}
form.onsubmit=async event=>{event.preventDefault();const selected=providers.find(provider=>provider.id===providerSelect.value);if(!selected||!selected.ready){alert('اختر محرك OCR جاهزاً من الإعدادات.');return}if(selected.remote&&!confirm('سيتم إرسال صور الصفحات التي تحتاج إلى OCR إلى خدمة خارجية. هل تريد المتابعة؟'))return;const body=new FormData(form);body.set('remote_acknowledged',selected.remote?'true':'false');for(const name of ['preserve_images','detect_chapters','detect_footnotes','fix_bidi','ocr_bad_pages'])body.set(name,form.elements[name].checked?'true':'false');try{const data=await jsonRequest('/api/jobs',{method:'POST',body});openJob(data.job_id);loadJobs()}catch(error){showError(error.message)}};
async function poll(id,current){if(current!==run)return;try{const data=await jsonRequest('/api/jobs/'+id);renderJobState(data);if(data.state==='failed'){if(data.downloads&&data.downloads.report)document.querySelector('#links').innerHTML='<a class="download" href="'+data.downloads.report+'">تنزيل تقرير المشكلة</a>';showError(data.error);return}if(data.state==='complete'){document.querySelector('#links').innerHTML='<a class="download" href="'+data.downloads.epub+'">تنزيل EPUB</a> <a class="download" href="'+data.downloads.report+'">تقرير الجودة</a>';const report=document.querySelector('#report');report.classList.remove('hidden');report.textContent=JSON.stringify(data.quality,null,2);restart.classList.remove('hidden');loadJobs();return}setTimeout(()=>poll(id,current),800)}catch(error){history.replaceState(null,'',location.pathname);localStorage.removeItem('pdf2epub-active-job');showError('تعذر العثور على التحويل المطلوب: '+error.message);form.classList.remove('hidden');loadJobs()}}
pauseButton.onclick=async()=>{if(!activeJob)return;pauseButton.disabled=true;statusBox.setAttribute('aria-busy','true');try{renderJobState(await jsonRequest('/api/jobs/'+activeJob+'/pause',{method:'POST'}))}catch(error){showError(error.message);pauseButton.disabled=false}};
resumeButton.onclick=async()=>{if(!activeJob)return;resumeButton.disabled=true;statusBox.setAttribute('aria-busy','true');try{const data=await jsonRequest('/api/jobs/'+activeJob+'/resume',{method:'POST'});renderJobState(data);poll(activeJob,run)}catch(error){showError(error.message);resumeButton.disabled=false}};
function showError(message){document.querySelector('#error').textContent=message;restart.classList.remove('hidden')}
restart.onclick=()=>{run++;activeJob=null;history.replaceState(null,'',location.pathname);localStorage.removeItem('pdf2epub-active-job');lastLogId=0;logStream.replaceChildren();form.reset();form.classList.remove('hidden');statusBox.classList.add('hidden');document.querySelector('#stage').textContent='جارٍ التحليل';document.querySelector('#progress').style.width='0%';document.querySelector('#percent').textContent='';document.querySelector('#error').textContent='';document.querySelector('#links').innerHTML='';document.querySelector('#report').textContent='';document.querySelector('#report').classList.add('hidden');restart.classList.add('hidden');loadProviders();loadJobs()};
Promise.all([loadProviders(),loadHealth(),loadJobs()]).then(()=>{const requested=new URLSearchParams(location.search).get('job')||localStorage.getItem('pdf2epub-active-job');if(requested&&/^[0-9a-f]{32}$/.test(requested))openJob(requested)});
</script>
</body></html>"""
