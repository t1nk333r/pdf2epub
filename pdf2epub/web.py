from __future__ import annotations

import json
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from .models import ConversionOptions
from .ocr import provider_status
from .pipeline import convert

app = FastAPI(title="Arabic PDF → EPUB", version="0.2.0")
JOB_ROOT = Path(gettempdir()) / "arabic-pdf2epub-jobs"
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

    @property
    def percent(self) -> int:
        return round(self.done / max(self.total, 1) * 100)

    def public(self) -> dict:
        data = asdict(self)
        data["percent"] = self.percent
        return data


jobs: dict[str, Job] = {}
lock = threading.Lock()


def _update(job_id: str, stage: str, done: int, total: int) -> None:
    with lock:
        job = jobs[job_id]
        job.state = "processing"
        job.stage, job.done, job.total = stage, done, total


def _run(job_id: str, pdf: Path, options: ConversionOptions) -> None:
    try:
        outputs = convert(
            pdf,
            pdf.parent / "output",
            options,
            lambda s, d, t: _update(job_id, s, d, t),
        )
        with lock:
            job = jobs[job_id]
            job.outputs = {kind: str(path) for kind, path in outputs.items()}
            job.state, job.stage, job.done, job.total = "complete", "Complete", 1, 1
    except Exception as exc:  # noqa: BLE001 - background job boundary records all failures
        with lock:
            jobs[job_id].state = "failed"
            jobs[job_id].stage = "Failed"
            jobs[job_id].error = str(exc)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX_HTML


@app.get("/api/ocr/providers")
def ocr_providers() -> dict:
    return {"providers": provider_status()}


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
) -> dict:
    if mode not in {"automatic", "prefer_text", "force_ocr"}:
        raise HTTPException(400, "Invalid mode")
    if ocr_provider not in {"tesseract", "ollama", "openai_compatible"}:
        raise HTTPException(400, "Invalid OCR provider")
    job_id = uuid4().hex
    job_dir = JOB_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    pdf = job_dir / "input.pdf"
    size = 0
    with pdf.open("wb") as target:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                target.close()
                shutil.rmtree(job_dir)
                raise HTTPException(413, "PDF exceeds the 500 MB upload limit")
            target.write(chunk)
    if pdf.read_bytes()[:5] != b"%PDF-":
        shutil.rmtree(job_dir)
        raise HTTPException(400, "The uploaded file is not a PDF")
    options = ConversionOptions(
        mode=mode,
        preserve_images=preserve_images,
        detect_chapters=detect_chapters,
        detect_footnotes=detect_footnotes,
        fix_bidi=fix_bidi,
        ocr_bad_pages=ocr_bad_pages,
        ocr_provider=ocr_provider,
    )
    with lock:
        jobs[job_id] = Job(job_id)
    threading.Thread(target=_run, args=(job_id, pdf, options), daemon=True).start()
    return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        data = job.public()
    if data["state"] == "complete":
        data["downloads"] = {
            kind: f"/api/jobs/{job_id}/download/{kind}" for kind in data["outputs"]
        }
        report = Path(data["outputs"]["report"])
        if report.exists():
            data["quality"] = json.loads(report.read_text(encoding="utf-8"))
    data.pop("outputs", None)
    return data


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    with lock:
        job = jobs.get(job_id)
        path = Path(job.outputs.get(kind, "")) if job else None
    if not path or not path.is_file():
        raise HTTPException(404, "Output not found")
    media = "application/epub+zip" if kind == "epub" else "application/json"
    return FileResponse(path, media_type=media, filename=path.name)


INDEX_HTML = """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>تحويل PDF عربي إلى EPUB</title><style>
:root{font-family:system-ui,sans-serif;color:#17201b;background:#eef3ef}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:2rem}.card{width:min(720px,100%);background:#fff;padding:2rem;border-radius:22px;box-shadow:0 20px 60px #173b2520}h1{margin-top:0;color:#174f32}.drop{border:2px dashed #83a892;border-radius:14px;padding:2rem;text-align:center;background:#f8fbf9}fieldset{border:0;padding:1rem 0;display:grid;gap:.6rem}select,input[type=file]{width:100%;padding:.7rem}button,.download{border:0;border-radius:10px;padding:.8rem 1.2rem;background:#176b42;color:#fff;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}.secondary{background:#65736a;margin-top:1rem}.bar{height:12px;background:#dce7df;border-radius:9px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:#27a867;transition:width .3s}.hidden{display:none}.error{color:#a31818}pre{direction:ltr;text-align:left;white-space:pre-wrap;background:#f4f6f4;padding:1rem;border-radius:10px;font-size:.8rem}</style></head>
<body><main class="card"><h1>تحويل PDF عربي إلى EPUB</h1><p>يفحص كل صفحة ويستخدم النص المضمّن متى كان موثوقاً، ثم يلجأ إلى OCR للصفحات الرديئة أو المصوّرة.</p>
<form id="form"><div class="drop"><input required name="file" type="file" accept="application/pdf,.pdf"></div><fieldset><label>طريقة المعالجة <select name="mode"><option value="automatic">تلقائي</option><option value="prefer_text">تفضيل النص المضمّن</option><option value="force_ocr">فرض OCR</option></select></label><label>محرك OCR <select name="ocr_provider" id="ocr-provider"><option value="tesseract">جارٍ تحميل المحركات…</option></select></label><small id="provider-note">يُرسل محتوى الصفحات إلى الخدمة المحددة فقط عندما تحتاج الصفحة إلى OCR.</small>
<label><input type="checkbox" name="preserve_images" checked> حفظ الصور</label><label><input type="checkbox" name="detect_chapters" checked> اكتشاف الفصول</label><label><input type="checkbox" name="detect_footnotes" checked> اكتشاف الحواشي</label><label><input type="checkbox" name="fix_bidi" checked> ضبط اتجاه العربية والإنجليزية</label><label><input type="checkbox" name="ocr_bad_pages" checked> OCR للصفحات الرديئة</label></fieldset><button>تحويل</button></form>
<section id="status" class="hidden"><h2 id="stage">جارٍ التحليل</h2><div class="bar"><i id="progress"></i></div><p id="percent"></p><p id="error" class="error"></p><div id="links"></div><pre id="report" class="hidden"></pre><button id="restart" type="button" class="secondary hidden">ابدأ من جديد</button></section></main>
<script>const f=document.querySelector('#form'),s=document.querySelector('#status'),restart=document.querySelector('#restart'),providerSelect=document.querySelector('#ocr-provider');let run=0;async function loadProviders(){try{const r=await fetch('/api/ocr/providers'),x=await r.json();providerSelect.innerHTML=x.providers.map(p=>'<option value="'+p.id+'" '+(p.configured?'':'disabled')+'>'+p.label+(p.configured?'':' — غير مُعدّ')+'</option>').join('');const first=x.providers.find(p=>p.configured);if(first)providerSelect.value=first.id;else document.querySelector('#provider-note').textContent='لا يوجد محرك OCR مُعدّ على الخادم.'}catch{document.querySelector('#provider-note').textContent='تعذر تحميل إعدادات محركات OCR.'}}loadProviders();f.onsubmit=async e=>{e.preventDefault();const current=++run;s.classList.remove('hidden');f.classList.add('hidden');restart.classList.add('hidden');const fd=new FormData(f);for(const n of ['preserve_images','detect_chapters','detect_footnotes','fix_bidi','ocr_bad_pages'])fd.set(n,f.elements[n].checked?'true':'false');const r=await fetch('/api/jobs',{method:'POST',body:fd});const x=await r.json();if(!r.ok){showError(x.detail||'تعذر رفع الملف');return}poll(x.job_id,current)};async function poll(id,current){if(current!==run)return;const r=await fetch('/api/jobs/'+id),x=await r.json();document.querySelector('#stage').textContent=x.stage;document.querySelector('#progress').style.width=x.percent+'%';document.querySelector('#percent').textContent=x.percent+'%';if(x.state==='failed'){showError(x.error);return}if(x.state==='complete'){document.querySelector('#links').innerHTML='<a class="download" href="'+x.downloads.epub+'">تنزيل EPUB</a> <a class="download" href="'+x.downloads.report+'">تقرير الجودة</a>';const p=document.querySelector('#report');p.classList.remove('hidden');p.textContent=JSON.stringify(x.quality,null,2);restart.classList.remove('hidden');return}setTimeout(()=>poll(id,current),800)}function showError(m){document.querySelector('#error').textContent=m;restart.classList.remove('hidden')}restart.onclick=()=>{run++;f.reset();f.classList.remove('hidden');s.classList.add('hidden');document.querySelector('#stage').textContent='جارٍ التحليل';document.querySelector('#progress').style.width='0%';document.querySelector('#percent').textContent='';document.querySelector('#error').textContent='';document.querySelector('#links').innerHTML='';document.querySelector('#report').textContent='';document.querySelector('#report').classList.add('hidden');restart.classList.add('hidden')}</script></body></html>"""
