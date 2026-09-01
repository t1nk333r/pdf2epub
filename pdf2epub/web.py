from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .checkpoints import (
    MAX_CHECKPOINT_BYTES,
    MAX_JOB_LOGS,
    CheckpointError,
    CheckpointStore,
    ConversionControl,
    JobManifest,
    PauseRequested,
    processing_contract,
)
from .models import ConversionOptions, JobState, require_transition
from .ocr import (
    OLLAMA_QUALITY_MODEL,
    OcrSettings,
    ollama_allowed_models,
    provider_descriptor,
    provider_names,
    provider_status,
)
from .pipeline import (
    ConversionIntegrityError,
    ConversionPublicationError,
    _quality_summary,
    _summary_path,
    convert,
)
from .settings import clear_runtime_settings, public_settings, update_runtime_settings

# Sidecar summaries are the full report minus page_details, so 1 MiB comfortably
# covers real books; this only bounds the legacy fallback that re-derives the
# summary from a full quality-report.json for jobs converted before the sidecar
# existed.
MAX_REPORT_SUMMARY_BYTES = 1 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _recover_jobs()
    yield


app = FastAPI(title="Arabic PDF → EPUB", version="0.3.0", lifespan=lifespan)
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


def _job_quality(report_path: Path) -> dict | None:
    """Load the quality summary for a finished job, bounded and cheap.

    Prefers the small sidecar written alongside the report (`pipeline.py`
    writes it once, at conversion time). Falls back to parsing the full
    report — guarded by the same size limit `CheckpointStore` uses — only for
    jobs converted before the sidecar existed. Either guard tripping degrades
    to `None` rather than raising, so an oversized artifact never turns a
    status view into a 500.
    """
    summary_path = _summary_path(report_path)
    if summary_path.is_file():
        if summary_path.stat().st_size > MAX_REPORT_SUMMARY_BYTES:
            return None
        return json.loads(summary_path.read_text(encoding="utf-8"))
    if not report_path.is_file() or report_path.stat().st_size > MAX_CHECKPOINT_BYTES:
        return None
    return _quality_summary(json.loads(report_path.read_text(encoding="utf-8")))


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _update(job_id: str, stage: str, done: int, total: int) -> None:
    with lock:
        job = jobs[job_id]
        if stage == "Building EPUB":
            if job.state == JobState.PAUSE_REQUESTED:
                job.pause_event.clear()
                _append_log(
                    job,
                    "Pause requested too late to take effect; the last page "
                    "was already saved, so the EPUB build will finish",
                )
            _transition_job(job, JobState.BUILDING)
            job.current_page = None
        changed = (job.stage, job.done, job.total) != (stage, done, total)
        job.stage, job.done, job.total = stage, done, total
        if changed:
            _append_log(job, f"{stage} — {done}/{total} ({job.percent}%)")
        # pdf2epub/analyzer.py is the producer of this message; it formats one
        # `total_duration=<seconds>s` group per OCR attempt on the page.
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
        report_path = outputs["report"]
        if report_path.stat().st_size > MAX_CHECKPOINT_BYTES:
            raise ValueError(
                f"Quality report exceeds the {MAX_CHECKPOINT_BYTES}-byte read limit"
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
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
            elif (
                manifest.state == JobState.FAILED
                and manifest.page_count > 0
                and manifest.safe_pages == manifest.page_count
            ):
                manifest.state = require_transition(
                    manifest.state, JobState.INTERRUPTED
                )
                manifest.stage = "Publication interrupted"
                manifest.current_page = None
                manifest.lease = None
                manifest.blocked_code = "publication_failed"
                manifest.blocked_reason = (
                    "اكتمل OCR، لكن تعذر بناء أو التحقق من EPUB. "
                    "أصلح EPUBCheck ثم أعد المتابعة."
                )
                store.write_manifest(manifest)
            jobs[child.name] = _job_from_manifest(store, manifest)
        except CheckpointError:
            continue


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
    allow_partial: bool = Form(False),
    ocr_provider: str = Form("tesseract"),
    ollama_model: str = Form(OLLAMA_QUALITY_MODEL),
    remote_acknowledged: bool = Form(False),
) -> dict:
    if mode not in {"automatic", "prefer_text", "force_ocr"}:
        raise HTTPException(400, "Invalid mode")
    if ocr_provider not in provider_names():
        raise HTTPException(400, "Invalid OCR provider")
    if ocr_provider == "ollama" and ollama_model not in ollama_allowed_models(
        OcrSettings.from_env()
    ):
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
    with pdf.open("rb") as handle:
        header = handle.read(5)
    if header != b"%PDF-":
        shutil.rmtree(store.root)
        raise HTTPException(400, "The uploaded file is not a PDF")
    options = ConversionOptions(
        mode=mode,
        preserve_images=preserve_images,
        detect_chapters=detect_chapters,
        detect_footnotes=detect_footnotes,
        fix_bidi=fix_bidi,
        ocr_bad_pages=ocr_bad_pages,
        allow_partial=allow_partial,
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
            or _file_sha256(source) != job.manifest.source_sha256
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


_LIVE_STATES = {JobState.PROCESSING, JobState.PAUSE_REQUESTED, JobState.BUILDING}


def _delete_job_storage(job: Job) -> bool:
    """Remove one job's directory. False when it sits outside the job root."""
    root = job.store.root.resolve()
    if root.parent != JOB_ROOT.resolve() or root.is_symlink():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.state in _LIVE_STATES or (job.thread and job.thread.is_alive()):
            raise HTTPException(
                409,
                {
                    "code": "job_running",
                    "message": "أوقف التحويل مؤقتاً قبل حذفه",
                },
            )
        if not _delete_job_storage(job):
            raise HTTPException(
                400, "Refusing to delete storage outside the job root"
            )
        jobs.pop(job_id, None)
        return {"deleted": [job_id]}


@app.delete("/api/jobs")
def clear_jobs(confirm: bool = False) -> dict:
    """Delete every job that is not currently running."""
    if not confirm:
        raise HTTPException(
            400,
            {
                "code": "confirmation_required",
                "message": "أضف confirm=true لحذف كل التحويلات",
            },
        )
    with lock:
        removable = [
            job
            for job in jobs.values()
            if job.state not in _LIVE_STATES
            and not (job.thread and job.thread.is_alive())
        ]
        deleted, skipped = [], []
        for job in removable:
            # One job stored outside the root must not abort the whole sweep.
            if _delete_job_storage(job):
                jobs.pop(job.id, None)
                deleted.append(job.id)
            else:
                skipped.append(job.id)
        return {"deleted": deleted, "skipped": skipped, "kept": len(jobs)}


@app.post("/api/jobs/{job_id}/restart")
def restart_job(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.thread and job.thread.is_alive():
            return job.public()
        if job.state != JobState.FAILED:
            raise HTTPException(
                409,
                {
                    "code": "cannot_restart",
                    "message": "لا يمكن إعادة تشغيل هذه المهمة",
                },
            )
        source = job.store.root / "source.pdf"
        if (
            not source.is_file()
            or source.is_symlink()
            or _file_sha256(source) != job.manifest.source_sha256
        ):
            job.blocked_code = "source_mismatch"
            job.blocked_reason = (
                "ملف PDF المصدر مفقود أو تغير؛ لا يمكن إعادة التشغيل من مصدر مختلف."
            )
            _persist(job)
            raise HTTPException(
                422, {"code": job.blocked_code, "message": job.blocked_reason}
            )
        # A restart is a fresh run of the same source under the same contract,
        # so every saved page is dropped rather than reused.
        job.store.discard_checkpoints()
        job.manifest.safe_pages = 0
        job.manifest.current_page = None
        job.manifest.page_durations = []
        job.manifest.outputs = {}
        job.safe_pages = job.done = 0
        job.total = 1
        job.current_page = None
        job.error = None
        job.outputs = {}
        job.paused_at = job.completed_at = None
        job.blocked_code = job.blocked_reason = None
        job.recoverable = True
        job.pause_event.clear()
        _transition_job(job, JobState.QUEUED)
        job.stage = "Restarting"
        _append_log(job, "Conversion restarted from the first page")
        _start_worker(job, source, ConversionOptions(**job.manifest.options))
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
            data["quality"] = _job_quality(report)
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
:root{--bg:#eef3ef;--surface:#fff;--surface-2:#f8fbf9;--text:#17201b;--muted:#65736a;--accent:#176b42;--accent-strong:#174f32;--accent-bright:#27a867;--border:#cbd9cf;--border-strong:#83a892;--badge-bg:#dce7df;--danger:#9c3b35;--error:#a31818;--focus:#e4a62a;--on-accent:#fff;--warn-bg:#fff8df;--warn-text:#654900;--warn-border:#d7a531;--error-bg:#fff0ef;--fail-bg:#ffe2df;--fail-text:#851313;--pause-bg:#fff0c7;--ok-bg:#d9f0e2;--ok-text:#12512f;--shadow:#173b2520;--shadow-strong:#0005;--backdrop:#07150e99;--log-rule:#ffffff0d}@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f1512;--surface:#171f1b;--surface-2:#1c2621;--text:#dde8e1;--muted:#93a89b;--accent:#2f9d63;--accent-strong:#8ad9ab;--accent-bright:#3fc27c;--border:#2d3b33;--border-strong:#3d5146;--badge-bg:#24312a;--danger:#b8564f;--error:#ff9d96;--focus:#e4a62a;--on-accent:#08110d;--warn-bg:#2e2717;--warn-text:#e8c86a;--warn-border:#8a6d21;--error-bg:#2c1917;--fail-bg:#331d1b;--fail-text:#ff9d96;--pause-bg:#2e2717;--ok-bg:#17301f;--ok-text:#7fd3a2;--shadow:#0006;--shadow-strong:#0009;--backdrop:#000000bb;--log-rule:#ffffff12}}:root[data-theme=dark]{--bg:#0f1512;--surface:#171f1b;--surface-2:#1c2621;--text:#dde8e1;--muted:#93a89b;--accent:#2f9d63;--accent-strong:#8ad9ab;--accent-bright:#3fc27c;--border:#2d3b33;--border-strong:#3d5146;--badge-bg:#24312a;--danger:#b8564f;--error:#ff9d96;--focus:#e4a62a;--on-accent:#08110d;--warn-bg:#2e2717;--warn-text:#e8c86a;--warn-border:#8a6d21;--error-bg:#2c1917;--fail-bg:#331d1b;--fail-text:#ff9d96;--pause-bg:#2e2717;--ok-bg:#17301f;--ok-text:#7fd3a2;--shadow:#0006;--shadow-strong:#0009;--backdrop:#000000bb;--log-rule:#ffffff12}:root{font-family:Tajawal,system-ui,sans-serif;color:var(--text);background:var(--bg);color-scheme:light dark}html[data-theme=dark]{color-scheme:dark}html[data-theme=light]{color-scheme:light}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:2rem}.card{width:min(1180px,100%);background:var(--surface);padding:2rem;border-radius:22px;box-shadow:0 20px 60px var(--shadow)}.navbar{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}.title-wrap{min-width:0}h1,h2,.lead{font-family:Amiri,serif;font-weight:400}h1{margin:0 0 .35rem;color:var(--accent-strong);font-size:2.35rem;line-height:1.35}.lead{font-size:1.2rem;line-height:1.9;margin-top:0}.cog{width:46px;height:46px;padding:0;border-radius:50%;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;line-height:1}.cog svg{display:block}#lang-toggle{font-size:.78rem;font-weight:700;letter-spacing:.02em}.nav-actions{display:flex;gap:.5rem;flex:0 0 auto}.capability-warnings{display:grid;gap:.65rem;margin:0 0 1rem}.capability-warnings.hidden{display:none}.capability-warning{margin:0;padding:.8rem 1rem;border:1px solid var(--warn-border);border-radius:10px;background:var(--warn-bg);color:var(--warn-text);line-height:1.7}.capability-warning.error-level{border:2px solid var(--error);background:var(--error-bg);color:var(--fail-text);font-weight:700}.drop{border:2px dashed var(--border-strong);border-radius:14px;padding:2rem;text-align:center;background:var(--surface-2)}fieldset{border:0;padding:1rem 0;display:grid;gap:.7rem}select,input[type=file],.settings-input{width:100%;padding:.7rem;border:1px solid var(--border);border-radius:8px;background:var(--surface);font-family:inherit}button,.download{border:0;border-radius:10px;padding:.8rem 1.2rem;background:var(--accent);color:var(--on-accent);font-family:inherit;font-weight:700;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:.4rem;text-align:center;line-height:1.2}.secondary{background:var(--muted)}.danger{background:var(--danger)}.bar{height:12px;background:var(--badge-bg);border-radius:9px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:var(--accent-bright);transition:width .3s}.hidden{display:none}.error{color:var(--error)}.success{color:var(--accent)}pre{direction:ltr;text-align:left;white-space:pre-wrap;background:var(--surface-2);padding:1rem;border-radius:10px;font-size:.8rem}dialog{width:min(720px,calc(100% - 2rem));max-height:88vh;border:0;border-radius:18px;padding:0;box-shadow:0 25px 90px var(--shadow-strong)}dialog::backdrop{background:var(--backdrop)}.dialog-body{padding:1.5rem;overflow:auto;max-height:88vh}.dialog-head{display:flex;justify-content:space-between;align-items:center;gap:1rem}.dialog-head h2{margin:0;color:var(--accent-strong);font-size:1.8rem}.close{background:transparent;color:var(--text);font-size:1.5rem;padding:0;width:2.2rem;height:2.2rem;display:inline-flex;align-items:center;justify-content:center;line-height:1}.settings-group{border-top:1px solid var(--border);margin-top:1rem;padding-top:1rem}.settings-group h3{margin:.2rem 0 .8rem;color:var(--accent-strong)}.setting{display:grid;gap:.3rem;margin-bottom:.8rem}.setting small{color:var(--muted)}.setting-row{display:flex;align-items:center;gap:.5rem}.setting-row .settings-input{flex:1}.dialog-actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1rem}.settings-note{background:var(--surface-2);border-radius:10px;padding:.8rem;line-height:1.7}.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,23rem);gap:1.6rem;align-items:start;margin-top:1rem}.col-main,.col-side{min-width:0}.col-side{position:sticky;top:1rem}@media(max-width:900px){.layout{grid-template-columns:minmax(0,1fr);gap:1.1rem}.col-side{position:static;order:-1}}@media(max-width:560px){body{padding:.75rem}.card{padding:1.2rem}h1{font-size:1.9rem}.lead{font-size:1rem}.drop{padding:1.2rem}}
.log-wrap{margin:1rem 0}.log-wrap h3{margin:0 0 .45rem;color:var(--accent-strong)}.log-stream{height:190px;overflow:auto;background:#101813;color:#d8eadf;border-radius:10px;padding:.75rem;font:500 .8rem/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;scrollbar-color:#52705e #101813}.log-line{display:grid;grid-template-columns:6rem 1fr;gap:.65rem;padding:.15rem 0;border-bottom:1px solid var(--log-rule);direction:ltr}.log-time{color:#8caf98}.log-message{direction:auto;unicode-bidi:plaintext;text-align:start;overflow-wrap:anywhere}.log-line.success .log-message{color:#75d99c}.log-line.error .log-message{color:#ff9d96}.jobs{margin:0;border:1px solid var(--border);border-radius:14px;padding:.9rem;background:var(--surface-2)}.jobs-head{display:flex;align-items:center;justify-content:space-between;gap:.5rem;margin-bottom:.6rem}.jobs h2{margin:0;font-size:1.35rem;color:var(--accent-strong)}.jobs-head button{padding:.3rem .65rem;font-size:.78rem;border-radius:7px}.job-delete{background:transparent;color:var(--danger);padding:.35rem;width:2.4rem;height:2.4rem;border-radius:8px;display:inline-flex;align-items:center;justify-content:center;margin-inline-start:auto}.job-delete:hover{background:var(--fail-bg)}.job-delete svg{display:block}.job-list{display:grid;gap:.4rem;max-height:min(62vh,32rem);overflow:auto;padding-inline-end:.2rem}.job-card{border:1px solid var(--border);border-radius:9px;padding:.5rem .6rem;background:var(--surface);font-size:.88rem}.job-card.active{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}.job-head{display:flex;justify-content:space-between;align-items:center;gap:.4rem}.job-head strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}.job-meta{display:flex;flex-wrap:wrap;gap:.15rem .6rem;margin:.2rem 0 0;color:var(--muted);font-size:.78rem;line-height:1.5}.job-actions button{padding:.32rem .7rem;font-size:.8rem;border-radius:7px}.badge{border-radius:99px;padding:.12rem .5rem;background:var(--badge-bg);font-size:.75rem;flex:0 0 auto}.badge.processing{background:var(--ok-bg);color:var(--ok-text)}.badge.paused,.badge.interrupted{background:var(--pause-bg);color:var(--warn-text)}.badge.failed{background:var(--fail-bg);color:var(--fail-text)}.job-actions{display:flex;gap:.35rem;flex-wrap:wrap;margin-top:.4rem}.state-banner{padding:.8rem;border-radius:10px;background:var(--surface-2);line-height:1.7}.state-banner.interrupted{background:var(--warn-bg)}.control-row{display:flex;gap:.6rem;flex-wrap:wrap;margin:.8rem 0}button:focus-visible,.download:focus-visible{outline:3px solid var(--focus);outline-offset:3px}button:disabled{opacity:.55;cursor:not-allowed}@media(max-width:560px){.log-line{grid-template-columns:5.2rem 1fr}}
</style>
</head>
<body>
<main class="card">
<nav class="navbar" data-i18n-aria="nav.aria" aria-label="شريط الأدوات"><div class="title-wrap"><h1 data-i18n="app.title">تحويل PDF عربي إلى EPUB</h1><p class="lead" data-i18n="app.lead">يفحص كل صفحة ويستخدم النص المضمّن متى كان موثوقاً، ثم يلجأ إلى OCR للصفحات الرديئة أو المصوّرة.</p></div><div class="nav-actions"><button id="lang-toggle" type="button" class="cog" data-i18n-title="lang.toggle" title="English / عربي" data-i18n-aria="lang.toggle" aria-label="English / عربي">EN</button><button id="theme-toggle" type="button" class="cog" data-i18n-title="theme.toggle" title="تبديل الوضع الليلي" data-i18n-aria="theme.toggle" aria-label="تبديل الوضع الليلي"></button><button id="settings-button" type="button" class="cog" data-i18n-title="settings.open" title="إعدادات OCR" data-i18n-aria="settings.open" aria-label="فتح إعدادات OCR"><svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9" cy="7" r="2.2" fill="currentColor" stroke="none"/><circle cx="15" cy="12" r="2.2" fill="currentColor" stroke="none"/><circle cx="9" cy="17" r="2.2" fill="currentColor" stroke="none"/></svg></button></div></nav>
<div class="layout">
<div class="col-main">
<section id="capability-warnings" class="capability-warnings hidden" role="alert" aria-live="polite"></section>
<form id="form"><div class="drop"><input required name="file" type="file" accept="application/pdf,.pdf"></div><fieldset><label><span data-i18n="form.mode">طريقة المعالجة</span> <select name="mode"><option value="automatic" data-i18n="mode.auto">تلقائي</option><option value="prefer_text" data-i18n="mode.text">تفضيل النص المضمّن</option><option value="force_ocr" data-i18n="mode.ocr">فرض OCR</option></select></label><label><span data-i18n="form.provider">محرك OCR</span> <select name="ocr_provider" id="ocr-provider"><option value="" data-i18n="providers.loading">جارٍ تحميل المحركات…</option></select></label><small id="provider-note" data-i18n="form.providerNote">يُرسل محتوى الصفحات إلى الخدمة المحددة فقط عندما تحتاج الصفحة إلى OCR.</small>
<label id="ollama-model-row"><span data-i18n="form.ollama">نموذج Ollama لهذا الكتاب</span> <select name="ollama_model" id="ollama-model"><option value="qwen3-vl:32b-instruct-gpu">جودة — Qwen3 VL 32B Instruct</option><option value="qwen3-vl:30b-instruct-gpu">سريع — Qwen3 VL 30B Instruct</option><option value="qwen3.8:27b-q4_K_M">متوازن — Qwen3.8 27B Q4</option></select></label>
<label><input type="checkbox" name="preserve_images" checked> <span data-i18n="chk.images">حفظ الرسوم والصور الداخلية فقط — لا تُحفظ صور الصفحات الكاملة</span></label><label><input type="checkbox" name="detect_chapters" checked> <span data-i18n="chk.chapters">اكتشاف الفصول</span></label><label><input type="checkbox" name="detect_footnotes" checked> <span data-i18n="chk.footnotes">اكتشاف الحواشي</span></label><label><input type="checkbox" name="fix_bidi" checked> <span data-i18n="chk.bidi">ضبط اتجاه العربية والإنجليزية</span></label><label><input type="checkbox" name="ocr_bad_pages" checked> <span data-i18n="chk.ocrbad">OCR للصفحات الرديئة</span></label><label><input type="checkbox" name="allow_partial"> <span data-i18n="chk.partial">إخراج EPUB حتى لو احتاجت صفحات إلى مراجعة — ملف تشخيصي، راجع تقرير الجودة</span></label></fieldset><button data-i18n="btn.convert">تحويل</button></form>
<section id="status" class="hidden" aria-busy="false"><h2 id="stage" data-i18n="status.analyzing">جارٍ التحليل</h2><div id="state-message" class="state-banner" aria-live="polite"></div><p id="safe-progress"></p><p id="current-activity"></p><p id="eta"></p><div class="bar"><i id="progress"></i></div><p id="percent"></p><div class="control-row"><button id="pause" type="button" class="secondary hidden" data-i18n-aria="btn.pause" aria-label="إيقاف التحويل مؤقتاً" data-i18n="btn.pause">إيقاف مؤقت</button><button id="resume" type="button" class="hidden" data-i18n-aria="btn.resume" aria-label="متابعة التحويل" data-i18n="btn.resume">متابعة التحويل</button></div><section class="log-wrap" aria-labelledby="log-heading"><h3 id="log-heading" data-i18n="log.heading">سجل التحويل المباشر</h3><div id="log-stream" class="log-stream" role="log" aria-live="polite" aria-relevant="additions"></div></section><p id="error" class="error"></p><div id="links"></div><pre id="report" class="hidden"></pre><div class="control-row"><button id="retry" type="button" class="hidden" data-i18n="btn.retry">إعادة تشغيل التحويل من الصفحة الأولى</button><button id="restart" type="button" class="secondary hidden" data-i18n="btn.fresh">ابدأ من جديد</button></div></section>
</div>
<aside class="col-side"><section class="jobs" aria-labelledby="jobs-heading"><div class="jobs-head"><h2 id="jobs-heading" data-i18n="jobs.heading">التحويلات</h2><button id="clear-jobs" type="button" class="danger" data-i18n="jobs.clear">مسح الكل</button></div><div id="job-list" class="job-list"><p>جارٍ تحميل التحويلات…</p></div></section></aside>
</div>
</main>
<dialog id="settings-dialog"><div class="dialog-body"><div class="dialog-head"><h2 data-i18n="dlg.title">إعدادات OCR</h2><button id="settings-close" type="button" class="close" aria-label="إغلاق">×</button></div><p class="settings-note" data-i18n="settings.note">قيم هذه الصفحة تتغلب على قيم <code>.env</code> حتى إعادة تشغيل الخادم. المفاتيح السرية لا تُعاد إلى المتصفح أبداً؛ اترك الحقل فارغاً للاحتفاظ بالقيمة الحالية.</p><form id="settings-form"><div id="settings-fields"></div><p id="settings-message"></p><div class="dialog-actions"><button type="submit" data-i18n="dlg.save">حفظ وتحديث المحركات</button><button id="settings-reset" type="button" class="secondary" data-i18n="dlg.reset">العودة إلى قيم .env</button></div></form></div></dialog>
<script>
const STRINGS={ar:{},en:{
'nav.aria':'Toolbar','app.title':'Arabic PDF to EPUB','app.lead':'Every page is inspected: embedded text is used when it is trustworthy, and OCR takes over for damaged or scanned pages.',
'lang.toggle':'English / عربي','theme.toggle':'Toggle dark mode','settings.open':'Open OCR settings',
'form.mode':'Processing mode','mode.auto':'Automatic','mode.text':'Prefer embedded text','mode.ocr':'Force OCR',
'form.provider':'OCR engine','form.providerNote':'Page content is sent to the selected service only when a page needs OCR.','form.ollama':'Ollama model for this book',
'chk.images':'Keep inline figures only — full-page scans are never saved','chk.chapters':'Detect chapters','chk.footnotes':'Detect footnotes',
'chk.bidi':'Fix Arabic and English text direction','chk.ocrbad':'OCR poor-quality pages','chk.partial':'Publish an EPUB even if pages need review — diagnostic output, check the quality report',
'btn.convert':'Convert','status.analyzing':'Analyzing','log.heading':'Live conversion log','jobs.heading':'Conversions','jobs.clear':'Clear all',
'dlg.title':'OCR settings','dlg.save':'Save and refresh engines','dlg.reset':'Restore .env values',
'btn.pause':'Pause','btn.resume':'Resume','btn.retry':'Restart from the first page','btn.fresh':'Start over','btn.view':'View','btn.download':'Download','btn.delete':'Delete conversion',
'card.pause':'Pause','card.resume':'Resume','card.restart':'Restart',
'providers.loading':'Loading engines…','provider.local':'local','provider.remote':'remote','provider.notReady':'not ready','provider.none':'No OCR engine is ready. Open settings to configure one.',
'jobs.empty':'No saved conversions yet.','jobs.loadFail':'Could not load conversions: ','job.pages':'{0}/{1} pages','job.page':'page {0}',
'confirm.delete':'Permanently delete this conversion and all its saved files?','confirm.clear':'Permanently delete every conversion that is not running?','confirm.reset':'Discard all page values and restore .env?',
'confirm.remote':'Page images that need OCR will be sent to an external service. Continue?','alert.provider':'Choose a ready OCR engine in settings.',
'state.queued':'Waiting in the queue to start processing.','state.processing':'You can request a pause; the current page finishes and is saved first.','state.pause_requested':'Pausing after the current page is saved.',
'state.paused':'You can close the browser or reboot; saved progress is kept.','state.interrupted':'The previous run stopped. The last complete checkpoint is safe; resuming needs your confirmation.',
'state.building':'OCR is complete; the EPUB is being assembled and validated.','state.failed':'Conversion failed.','state.complete':'Conversion completed successfully.',
'state.label.queued':'Queued','state.label.processing':'Processing','state.label.pause_requested':'Pause requested','state.label.paused':'Paused',
'state.label.interrupted':'Ready to resume','state.label.building':'Building EPUB','state.label.complete':'Complete','state.label.failed':'Failed',
'eta':'Estimated time remaining: {0} min','safe':'{0} of {1} pages saved safely','activity':'Processing page {0}',
'links.epub':'Download EPUB','links.report':'Quality report','links.problem':'Download problem report',
'settings.loading':'Loading settings…','settings.saving':'Saving…','settings.saved':'Saved in server memory.','settings.restored':'Restored .env values.',
'settings.note':'Values on this page override .env until the server restarts. Secret keys are never returned to the browser; leave a field empty to keep its current value.',
'settings.clear':'clear','settings.stored':' — a value is stored','notfound':'Could not find the requested conversion: '}};
let LANG='ar';try{LANG=localStorage.getItem('pdf2epub-lang')||'ar'}catch(error){LANG='ar'}
function t(key,...args){const table=STRINGS[LANG]||{};let out=table[key];if(out===undefined)return null;
return out.replace(/\\{(\\d+)\\}/g,(m,i)=>args[Number(i)]===undefined?m:args[Number(i)])}
function applyLang(){const root=document.documentElement;root.lang=LANG==='en'?'en':'ar';root.dir=LANG==='en'?'ltr':'rtl';
for(const el of document.querySelectorAll('[data-i18n]')){if(el.dataset.i18nAr===undefined)el.dataset.i18nAr=el.textContent;
const v=t(el.dataset.i18n);el.textContent=v===null?el.dataset.i18nAr:v}
for(const el of document.querySelectorAll('[data-i18n-title]')){if(el.dataset.i18nTitleAr===undefined)el.dataset.i18nTitleAr=el.title;
const v=t(el.dataset.i18nTitle);el.title=v===null?el.dataset.i18nTitleAr:v}
for(const el of document.querySelectorAll('[data-i18n-aria]')){if(el.dataset.i18nAriaAr===undefined)el.dataset.i18nAriaAr=el.getAttribute('aria-label');
const v=t(el.dataset.i18nAria);el.setAttribute('aria-label',v===null?el.dataset.i18nAriaAr:v)}
const b=document.querySelector('#lang-toggle');if(b)b.textContent=LANG==='en'?'ع':'EN'}
const ICON_MOON='<svg viewBox="0 0 24 24" width="19" height="19" fill="currentColor" aria-hidden="true" focusable="false"><path d="M20.5 14.6A8.5 8.5 0 0 1 9.4 3.5a8.5 8.5 0 1 0 11.1 11.1z"/></svg>',ICON_SUN='<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/></svg>',ICON_TRASH='<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M4 7h16M10 4h4M9 7l.8 12h8.4L19 7M10 11v5M14 11v5"/></svg>';
const themeToggle=document.querySelector('#theme-toggle');
function applyTheme(theme){if(theme){document.documentElement.dataset.theme=theme}else{delete document.documentElement.dataset.theme}const dark=theme==='dark'||(!theme&&matchMedia('(prefers-color-scheme:dark)').matches);themeToggle.innerHTML=dark?ICON_SUN:ICON_MOON}
let storedTheme=null;try{storedTheme=localStorage.getItem('pdf2epub-theme')}catch(error){storedTheme=null}
applyTheme(storedTheme);
themeToggle.onclick=()=>{const dark=document.documentElement.dataset.theme==='dark'||(!document.documentElement.dataset.theme&&matchMedia('(prefers-color-scheme:dark)').matches);const next=dark?'light':'dark';applyTheme(next);try{localStorage.setItem('pdf2epub-theme',next)}catch(error){}};
matchMedia('(prefers-color-scheme:dark)').addEventListener('change',()=>{if(!document.documentElement.dataset.theme)applyTheme(null)});
applyLang();
document.querySelector('#lang-toggle').onclick=()=>{LANG=LANG==='en'?'ar':'en';try{localStorage.setItem('pdf2epub-lang',LANG)}catch(error){}applyLang();loadProviders();loadJobs();loadHealth()};
const form=document.querySelector('#form'),statusBox=document.querySelector('#status'),restart=document.querySelector('#restart'),providerSelect=document.querySelector('#ocr-provider'),dialog=document.querySelector('#settings-dialog'),settingsForm=document.querySelector('#settings-form'),settingsFields=document.querySelector('#settings-fields'),settingsMessage=document.querySelector('#settings-message'),logStream=document.querySelector('#log-stream'),capabilityWarnings=document.querySelector('#capability-warnings'),pauseButton=document.querySelector('#pause'),resumeButton=document.querySelector('#resume'),retryButton=document.querySelector('#retry'),jobList=document.querySelector('#job-list');
let run=0,providers=[],lastLogId=0,activeJob=null;
const stateLabelsAr={queued:'في الانتظار',processing:'قيد المعالجة',pause_requested:'جارٍ طلب الإيقاف',paused:'متوقف مؤقتاً',interrupted:'جاهز للاستعادة',building:'تجميع EPUB',complete:'مكتمل',failed:'فشل'};
function stateLabel(state){return t('state.label.'+state)||stateLabelsAr[state]}
const sourceLabels={runtime:'من صفحة الإعدادات',environment:'من ملف .env',default:'قيمة افتراضية'};
async function jsonRequest(url,options={}){const response=await fetch(url,options);const data=await response.json();if(!response.ok){const detail=data.detail;throw new Error(typeof detail==='object'&&detail?detail.message:(detail||'تعذر إكمال الطلب'))}return data}
function renderWarnings(warnings){capabilityWarnings.replaceChildren();for(const warning of warnings){const message=document.createElement('p');message.className='capability-warning'+(warning.severity==='error'?' error-level':'');message.textContent=warning.message;capabilityWarnings.append(message)}capabilityWarnings.classList.toggle('hidden',warnings.length===0)}
async function loadHealth(){try{const data=await jsonRequest('/api/health');renderWarnings(data.warnings||[])}catch(error){renderWarnings([{severity:'warning',message:'تعذر التحقق من جاهزية أدوات النشر وOCR: '+error.message}])}}
function updateModelVisibility(){document.querySelector('#ollama-model-row').classList.toggle('hidden',providerSelect.value!=='ollama')}
async function loadProviders(){try{const data=await jsonRequest('/api/ocr/providers');providers=data.providers;providerSelect.replaceChildren();for(const provider of providers){const option=document.createElement('option');option.value=provider.id;option.disabled=!provider.ready;const kind=t(provider.remote?'provider.remote':'provider.local')||(provider.remote?'خارجي':'محلي');const nr=provider.ready?'':' — '+(t('provider.notReady')||'غير جاهز');option.textContent=LANG==='en'?provider.label+' — '+kind+nr:kind+nr+' — \\u2068'+provider.label+'\\u2069';providerSelect.append(option)}const ollama=providers.find(provider=>provider.id==='ollama');if(ollama&&ollama.book_models?.includes(ollama.model))document.querySelector('#ollama-model').value=ollama.model;const first=providers.find(provider=>provider.ready);if(first){providerSelect.value=first.id;document.querySelector('#provider-note').textContent=first.detail}else{providerSelect.value='';document.querySelector('#provider-note').textContent=t('provider.none')||'لا يوجد محرك OCR جاهز. افتح زر الإعدادات لإعداده.'}updateModelVisibility()}catch(error){document.querySelector('#provider-note').textContent=error.message}}
providerSelect.onchange=()=>{const selected=providers.find(provider=>provider.id===providerSelect.value);if(selected)document.querySelector('#provider-note').textContent=selected.detail;updateModelVisibility()};
function settingField(field){const wrapper=document.createElement('label');wrapper.className='setting';const title=document.createElement('span');title.textContent=field.label;const row=document.createElement('span');row.className='setting-row';const input=document.createElement('input');input.className='settings-input';input.name=field.key;input.type=field.secret?'password':(['PDF2EPUB_OCR_TIMEOUT','PDF2EPUB_OLLAMA_NUM_CTX'].includes(field.key)?'number':'text');input.placeholder=field.placeholder||'';input.value=field.value||'';input.autocomplete=field.secret?'new-password':'off';row.append(input);if(field.secret){const clearLabel=document.createElement('label');const clear=document.createElement('input');clear.type='checkbox';clear.dataset.clearKey=field.key;clearLabel.append(clear,' '+(t('settings.clear')||'مسح'));row.append(clearLabel)}const note=document.createElement('small');note.textContent=(sourceLabels[field.source]||field.source)+(field.secret&&field.configured?(t('settings.stored')||' — توجد قيمة محفوظة'):'');wrapper.append(title,row,note);return wrapper}
async function loadSettings(){settingsMessage.textContent=t('settings.loading')||'جارٍ تحميل الإعدادات…';settingsFields.replaceChildren();try{const data=await jsonRequest('/api/settings');const groups=new Map();for(const field of data.fields){if(!groups.has(field.section)){const group=document.createElement('section');group.className='settings-group';const heading=document.createElement('h3');heading.textContent=field.section;group.append(heading);groups.set(field.section,group);settingsFields.append(group)}groups.get(field.section).append(settingField(field))}settingsMessage.textContent=''}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}}
document.querySelector('#settings-button').onclick=async()=>{dialog.showModal();await loadSettings()};document.querySelector('#settings-close').onclick=()=>dialog.close();
settingsForm.onsubmit=async event=>{event.preventDefault();const values={};for(const input of settingsForm.querySelectorAll('.settings-input')){if(input.type!=='password'||input.value)values[input.name]=input.value}for(const clear of settingsForm.querySelectorAll('[data-clear-key]:checked'))values[clear.dataset.clearKey]='';settingsMessage.className='';settingsMessage.textContent=t('settings.saving')||'جارٍ الحفظ…';try{await jsonRequest('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});settingsMessage.className='success';settingsMessage.textContent=t('settings.saved')||'تم الحفظ في ذاكرة الخادم.';await Promise.all([loadProviders(),loadHealth()]);await loadSettings()}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}};
document.querySelector('#settings-reset').onclick=async()=>{if(!confirm(t('confirm.reset')||'حذف كل قيم الصفحة والعودة إلى .env؟'))return;try{await jsonRequest('/api/settings',{method:'DELETE'});await Promise.all([loadProviders(),loadHealth()]);await loadSettings();settingsMessage.className='success';settingsMessage.textContent=t('settings.restored')||'تمت العودة إلى قيم .env.'}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}};
function renderLogs(logs=[]){let appended=false;for(const entry of logs){if(entry.id<=lastLogId)continue;const line=document.createElement('div');line.className='log-line '+entry.level;const time=document.createElement('span');time.className='log-time';const parsed=new Date(entry.timestamp);time.textContent=Number.isNaN(parsed.getTime())?entry.timestamp:parsed.toLocaleTimeString('ar-SA');const message=document.createElement('span');message.className='log-message';message.textContent=entry.message;line.append(time,message);logStream.append(line);lastLogId=entry.id;appended=true}if(appended)logStream.scrollTop=logStream.scrollHeight}
function durationLabel(seconds){if(seconds==null)return'';const minutes=Math.max(1,Math.round(seconds/60));return t('eta',minutes)||('الوقت المتبقي التقريبي: '+minutes+' دقيقة')}
function renderJobState(data){activeJob=data.id;document.querySelector('#stage').textContent=stateLabel(data.state)||data.stage;document.querySelector('#progress').style.width=data.percent+'%';document.querySelector('#percent').textContent=data.percent+'%';document.querySelector('#safe-progress').textContent=t('safe',data.safe_pages,data.page_count)||('تم حفظ '+data.safe_pages+' من '+data.page_count+' صفحة بأمان');document.querySelector('#current-activity').textContent=data.current_page?(t('activity',data.current_page)||'تتم معالجة الصفحة '+data.current_page):'';document.querySelector('#eta').textContent=durationLabel(data.eta_seconds);const message=document.querySelector('#state-message');message.className='state-banner '+data.state;const messages={queued:t('state.queued')||'بانتظار بدء المعالجة.',processing:t('state.processing')||'يمكن طلب الإيقاف المؤقت؛ ستكتمل الصفحة الحالية وتُحفظ أولاً.',pause_requested:t('state.pause_requested')||'سيتم الإيقاف بعد حفظ الصفحة الحالية.',paused:t('state.paused')||'يمكن إغلاق المتصفح أو إعادة تشغيل الجهاز؛ سيبقى التقدم المحفوظ.',interrupted:t('state.interrupted')||'توقف التشغيل السابق. آخر نقطة حفظ مكتملة آمنة، والمتابعة تحتاج إلى تأكيدك.',building:t('state.building')||'اكتمل OCR ويجري الآن تجميع EPUB والتحقق منه. يمكن إعادة البناء من الصفحات المحفوظة بعد إعادة التشغيل.',failed:data.blocked_reason||data.error||t('state.failed')||'فشل التحويل.',complete:t('state.complete')||'اكتمل التحويل بنجاح.'};message.textContent=messages[data.state]||data.stage;pauseButton.classList.toggle('hidden',!['processing','pause_requested'].includes(data.state));pauseButton.disabled=data.state!=='processing';resumeButton.classList.toggle('hidden',!['paused','interrupted'].includes(data.state));resumeButton.disabled=!data.can_resume;retryButton.classList.toggle('hidden',data.state!=='failed');retryButton.disabled=false;statusBox.setAttribute('aria-busy',data.state==='pause_requested'?'true':'false');renderLogs(data.logs)}
async function dashboardAction(job,action,button){openJob(job.id);if(button)button.disabled=true;try{renderJobState(await jsonRequest('/api/jobs/'+job.id+'/'+action,{method:'POST'}));poll(job.id,run);loadJobs()}catch(error){showError(error.message);if(button)button.disabled=false}}
async function loadJobs(){try{const data=await jsonRequest('/api/jobs');jobList.replaceChildren();if(!data.jobs.length){jobList.innerHTML='<p>'+(t('jobs.empty')||'لا توجد تحويلات محفوظة بعد.')+'</p>';return}for(const job of data.jobs){const card=document.createElement('article');card.className='job-card'+(job.id===activeJob?' active':'');const head=document.createElement('div');head.className='job-head';const name=document.createElement('strong');name.textContent=job.filename;name.title=job.filename;const badge=document.createElement('span');badge.className='badge '+job.state;badge.textContent=stateLabel(job.state)||job.state;head.append(name,badge);const meta=document.createElement('p');meta.className='job-meta';const pages=document.createElement('span');pages.textContent=t('job.pages',job.safe_pages,job.page_count)||(job.safe_pages+'/'+job.page_count+' صفحة');const when=document.createElement('span');when.textContent=job.current_page?(t('job.page',job.current_page)||'صفحة '+job.current_page):new Date(job.updated_at||job.created_at).toLocaleString('ar-SA',{dateStyle:'short',timeStyle:'short'});meta.append(pages,when);const actions=document.createElement('div');actions.className='job-actions';const act=(label,action,className)=>{const button=document.createElement('button');button.type='button';button.textContent=label;if(className)button.className=className;button.onclick=()=>dashboardAction(job,action,button);actions.append(button)};if(job.state==='processing')act(t('card.pause')||'إيقاف مؤقت','pause');else if(['paused','interrupted'].includes(job.state)&&job.can_resume)act(t('card.resume')||'متابعة','resume');else if(job.state==='failed')act(t('card.restart')||'إعادة تشغيل','restart');const view=document.createElement('button');view.type='button';view.className='secondary';view.textContent=job.state==='complete'?(t('btn.download')||'تنزيل'):(t('btn.view')||'عرض');view.onclick=()=>openJob(job.id);actions.append(view);const remove=document.createElement('button');remove.type='button';remove.className='job-delete';remove.innerHTML=ICON_TRASH;remove.title=t('btn.delete')||'حذف التحويل';remove.setAttribute('aria-label','حذف التحويل '+job.filename);remove.onclick=async()=>{if(!confirm(t('confirm.delete')||'حذف هذا التحويل نهائياً مع كل ملفاته المحفوظة؟'))return;remove.disabled=true;try{await jsonRequest('/api/jobs/'+job.id,{method:'DELETE'});if(activeJob===job.id)restart.onclick();loadJobs()}catch(error){showError(error.message);remove.disabled=false}};actions.append(remove);card.append(head,meta,actions);jobList.append(card)}}catch(error){jobList.textContent=(t('jobs.loadFail')||'تعذر تحميل التحويلات: ')+error.message}}
document.querySelector('#clear-jobs').onclick=async()=>{if(!confirm(t('confirm.clear')||'حذف كل التحويلات غير الجارية نهائياً؟'))return;try{const data=await jsonRequest('/api/jobs?confirm=true',{method:'DELETE'});if(activeJob&&data.deleted.includes(activeJob))restart.onclick();loadJobs()}catch(error){showError(error.message)}};
function openJob(id){const current=++run;activeJob=id;lastLogId=0;logStream.replaceChildren();history.replaceState(null,'','?job='+encodeURIComponent(id));localStorage.setItem('pdf2epub-active-job',id);statusBox.classList.remove('hidden');form.classList.add('hidden');poll(id,current)}
form.onsubmit=async event=>{event.preventDefault();const selected=providers.find(provider=>provider.id===providerSelect.value);if(!selected||!selected.ready){alert(t('alert.provider')||'اختر محرك OCR جاهزاً من الإعدادات.');return}if(selected.remote&&!confirm(t('confirm.remote')||'سيتم إرسال صور الصفحات التي تحتاج إلى OCR إلى خدمة خارجية. هل تريد المتابعة؟'))return;const body=new FormData(form);body.set('remote_acknowledged',selected.remote?'true':'false');for(const name of ['preserve_images','detect_chapters','detect_footnotes','fix_bidi','ocr_bad_pages','allow_partial'])body.set(name,form.elements[name].checked?'true':'false');try{const data=await jsonRequest('/api/jobs',{method:'POST',body});openJob(data.job_id);loadJobs()}catch(error){showError(error.message)}};
async function poll(id,current){if(current!==run)return;try{const data=await jsonRequest('/api/jobs/'+id);renderJobState(data);if(data.state==='failed'){if(data.downloads&&data.downloads.report)document.querySelector('#links').innerHTML='<a class="download" href="'+data.downloads.report+'">'+(t('links.problem')||'تنزيل تقرير المشكلة')+'</a>';showError(data.error);return}if(data.state==='complete'){document.querySelector('#links').innerHTML='<a class="download" href="'+data.downloads.epub+'">'+(t('links.epub')||'تنزيل EPUB')+'</a> <a class="download" href="'+data.downloads.report+'">'+(t('links.report')||'تقرير الجودة')+'</a>';const report=document.querySelector('#report');report.classList.remove('hidden');report.textContent=JSON.stringify(data.quality,null,2);restart.classList.remove('hidden');loadJobs();return}setTimeout(()=>poll(id,current),800)}catch(error){history.replaceState(null,'',location.pathname);localStorage.removeItem('pdf2epub-active-job');showError((t('notfound')||'تعذر العثور على التحويل المطلوب: ')+error.message);form.classList.remove('hidden');loadJobs()}}
pauseButton.onclick=async()=>{if(!activeJob)return;pauseButton.disabled=true;statusBox.setAttribute('aria-busy','true');try{renderJobState(await jsonRequest('/api/jobs/'+activeJob+'/pause',{method:'POST'}))}catch(error){showError(error.message);pauseButton.disabled=false}};
resumeButton.onclick=async()=>{if(!activeJob)return;resumeButton.disabled=true;statusBox.setAttribute('aria-busy','true');try{const data=await jsonRequest('/api/jobs/'+activeJob+'/resume',{method:'POST'});renderJobState(data);poll(activeJob,run)}catch(error){showError(error.message);resumeButton.disabled=false}};
retryButton.onclick=async()=>{if(!activeJob)return;retryButton.disabled=true;document.querySelector('#error').textContent='';document.querySelector('#links').innerHTML='';try{const data=await jsonRequest('/api/jobs/'+activeJob+'/restart',{method:'POST'});renderJobState(data);poll(activeJob,run);loadJobs()}catch(error){showError(error.message);retryButton.disabled=false}};
function showError(message){document.querySelector('#error').textContent=message;restart.classList.remove('hidden')}
restart.onclick=()=>{run++;activeJob=null;history.replaceState(null,'',location.pathname);localStorage.removeItem('pdf2epub-active-job');lastLogId=0;logStream.replaceChildren();form.reset();form.classList.remove('hidden');statusBox.classList.add('hidden');document.querySelector('#stage').textContent='جارٍ التحليل';document.querySelector('#progress').style.width='0%';document.querySelector('#percent').textContent='';document.querySelector('#error').textContent='';document.querySelector('#links').innerHTML='';document.querySelector('#report').textContent='';document.querySelector('#report').classList.add('hidden');restart.classList.add('hidden');retryButton.classList.add('hidden');loadProviders();loadJobs()};
Promise.all([loadProviders(),loadHealth(),loadJobs()]).then(()=>{const requested=new URLSearchParams(location.search).get('job')||localStorage.getItem('pdf2epub-active-job');if(requested&&/^[0-9a-f]{32}$/.test(requested))openJob(requested)});
</script>
</body></html>"""
