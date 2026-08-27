from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .models import ConversionOptions
from .ocr import provider_descriptor, provider_names, provider_status
from .pipeline import ConversionIntegrityError, ConversionPublicationError, convert
from .settings import clear_runtime_settings, public_settings, update_runtime_settings

app = FastAPI(title="Arabic PDF → EPUB", version="0.3.0")
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
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class SettingsUpdate(BaseModel):
    values: dict[str, str]


def _quality_summary(report: dict) -> dict:
    summary = dict(report)
    page_details = summary.pop("page_details", [])
    summary["page_details_count"] = len(page_details)
    summary["full_page_details"] = "Available in the downloaded quality report"
    return summary


def _recover_completed_job(job_id: str) -> Job | None:
    if not JOB_ID_RE.fullmatch(job_id):
        return None
    output_dir = JOB_ROOT / job_id / "output"
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
    )
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
        report = json.loads(outputs["report"].read_text(encoding="utf-8"))
        stage = (
            "Complete — review required"
            if report.get("integrity", {}).get("status") == "review"
            else "Complete"
        )
        with lock:
            job = jobs[job_id]
            job.outputs = {kind: str(path) for kind, path in outputs.items()}
            job.state, job.stage, job.done, job.total = "complete", stage, 1, 1
    except (ConversionIntegrityError, ConversionPublicationError) as exc:
        with lock:
            jobs[job_id].state = "failed"
            jobs[job_id].stage = "Content integrity failed"
            jobs[job_id].error = str(exc)
            jobs[job_id].outputs = {"report": str(exc.report_path)}
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


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "epubcheck_available": shutil.which("epubcheck") is not None,
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
    remote_acknowledged: bool = Form(False),
) -> dict:
    if mode not in {"automatic", "prefer_text", "force_ocr"}:
        raise HTTPException(400, "Invalid mode")
    if ocr_provider not in provider_names():
        raise HTTPException(400, "Invalid OCR provider")
    descriptor = provider_descriptor(ocr_provider)
    if descriptor and descriptor.remote and not remote_acknowledged:
        raise HTTPException(
            400, "Remote OCR requires explicit acknowledgement of page-image transfer"
        )
    job_id = uuid4().hex
    job_dir = JOB_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    pdf = job_dir / _safe_pdf_name(file.filename)
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
    if not path or not path.is_file():
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
:root{font-family:Tajawal,system-ui,sans-serif;color:#17201b;background:#eef3ef}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:2rem}.card{width:min(760px,100%);background:#fff;padding:2rem;border-radius:22px;box-shadow:0 20px 60px #173b2520}.navbar{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}.title-wrap{min-width:0}h1,h2,.lead{font-family:Amiri,serif;font-weight:400}h1{margin:0 0 .35rem;color:#174f32;font-size:2.35rem;line-height:1.35}.lead{font-size:1.2rem;line-height:1.9;margin-top:0}.cog{width:46px;height:46px;padding:0;border-radius:50%;font-size:1.35rem;flex:0 0 auto}.drop{border:2px dashed #83a892;border-radius:14px;padding:2rem;text-align:center;background:#f8fbf9}fieldset{border:0;padding:1rem 0;display:grid;gap:.7rem}select,input[type=file],.settings-input{width:100%;padding:.7rem;border:1px solid #b8c8bd;border-radius:8px;background:#fff;font-family:inherit}button,.download{border:0;border-radius:10px;padding:.8rem 1.2rem;background:#176b42;color:#fff;font-family:inherit;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}.secondary{background:#65736a}.danger{background:#9c3b35}.bar{height:12px;background:#dce7df;border-radius:9px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:#27a867;transition:width .3s}.hidden{display:none}.error{color:#a31818}.success{color:#176b42}pre{direction:ltr;text-align:left;white-space:pre-wrap;background:#f4f6f4;padding:1rem;border-radius:10px;font-size:.8rem}dialog{width:min(720px,calc(100% - 2rem));max-height:88vh;border:0;border-radius:18px;padding:0;box-shadow:0 25px 90px #0005}dialog::backdrop{background:#07150e99}.dialog-body{padding:1.5rem;overflow:auto;max-height:88vh}.dialog-head{display:flex;justify-content:space-between;align-items:center;gap:1rem}.dialog-head h2{margin:0;color:#174f32;font-size:1.8rem}.close{background:transparent;color:#26372d;font-size:1.5rem;padding:.2rem .6rem}.settings-group{border-top:1px solid #dbe5de;margin-top:1rem;padding-top:1rem}.settings-group h3{margin:.2rem 0 .8rem;color:#174f32}.setting{display:grid;gap:.3rem;margin-bottom:.8rem}.setting small{color:#65736a}.setting-row{display:flex;align-items:center;gap:.5rem}.setting-row .settings-input{flex:1}.dialog-actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1rem}.settings-note{background:#f2f7f3;border-radius:10px;padding:.8rem;line-height:1.7}@media(max-width:560px){body{padding:.75rem}.card{padding:1.2rem}h1{font-size:1.9rem}.lead{font-size:1rem}.drop{padding:1.2rem}}
</style>
</head>
<body>
<main class="card">
<nav class="navbar" aria-label="شريط الأدوات"><div class="title-wrap"><h1>تحويل PDF عربي إلى EPUB</h1><p class="lead">يفحص كل صفحة ويستخدم النص المضمّن متى كان موثوقاً، ثم يلجأ إلى OCR للصفحات الرديئة أو المصوّرة.</p></div><button id="settings-button" type="button" class="cog" title="إعدادات OCR" aria-label="فتح إعدادات OCR">⚙</button></nav>
<form id="form"><div class="drop"><input required name="file" type="file" accept="application/pdf,.pdf"></div><fieldset><label>طريقة المعالجة <select name="mode"><option value="automatic">تلقائي</option><option value="prefer_text">تفضيل النص المضمّن</option><option value="force_ocr">فرض OCR</option></select></label><label>محرك OCR <select name="ocr_provider" id="ocr-provider"><option value="">جارٍ تحميل المحركات…</option></select></label><small id="provider-note">يُرسل محتوى الصفحات إلى الخدمة المحددة فقط عندما تحتاج الصفحة إلى OCR.</small>
<label><input type="checkbox" name="preserve_images" checked> حفظ الرسوم والصور الداخلية فقط — لا تُحفظ صور الصفحات الكاملة</label><label><input type="checkbox" name="detect_chapters" checked> اكتشاف الفصول</label><label><input type="checkbox" name="detect_footnotes" checked> اكتشاف الحواشي</label><label><input type="checkbox" name="fix_bidi" checked> ضبط اتجاه العربية والإنجليزية</label><label><input type="checkbox" name="ocr_bad_pages" checked> OCR للصفحات الرديئة</label></fieldset><button>تحويل</button></form>
<section id="status" class="hidden"><h2 id="stage">جارٍ التحليل</h2><div class="bar"><i id="progress"></i></div><p id="percent"></p><p id="error" class="error"></p><div id="links"></div><pre id="report" class="hidden"></pre><button id="restart" type="button" class="secondary hidden">ابدأ من جديد</button></section>
</main>
<dialog id="settings-dialog"><div class="dialog-body"><div class="dialog-head"><h2>إعدادات OCR</h2><button id="settings-close" type="button" class="close" aria-label="إغلاق">×</button></div><p class="settings-note">قيم هذه الصفحة تتغلب على قيم <code>.env</code> حتى إعادة تشغيل الخادم. المفاتيح السرية لا تُعاد إلى المتصفح أبداً؛ اترك الحقل فارغاً للاحتفاظ بالقيمة الحالية.</p><form id="settings-form"><div id="settings-fields"></div><p id="settings-message"></p><div class="dialog-actions"><button type="submit">حفظ وتحديث المحركات</button><button id="settings-reset" type="button" class="secondary">العودة إلى قيم .env</button></div></form></div></dialog>
<script>
const form=document.querySelector('#form'),statusBox=document.querySelector('#status'),restart=document.querySelector('#restart'),providerSelect=document.querySelector('#ocr-provider'),dialog=document.querySelector('#settings-dialog'),settingsForm=document.querySelector('#settings-form'),settingsFields=document.querySelector('#settings-fields'),settingsMessage=document.querySelector('#settings-message');
let run=0,providers=[];
const sourceLabels={runtime:'من صفحة الإعدادات',environment:'من ملف .env',default:'قيمة افتراضية'};
async function jsonRequest(url,options={}){const response=await fetch(url,options);const data=await response.json();if(!response.ok)throw new Error(data.detail||'تعذر إكمال الطلب');return data}
async function loadProviders(){try{const data=await jsonRequest('/api/ocr/providers');providers=data.providers;providerSelect.replaceChildren();for(const provider of providers){const option=document.createElement('option');option.value=provider.id;option.disabled=!provider.ready;option.textContent=provider.label+(provider.remote?' — خارجي':' — محلي')+(provider.ready?'':' — غير جاهز');providerSelect.append(option)}const first=providers.find(provider=>provider.ready);if(first){providerSelect.value=first.id;document.querySelector('#provider-note').textContent=first.detail}else{providerSelect.value='';document.querySelector('#provider-note').textContent='لا يوجد محرك OCR جاهز. افتح زر الإعدادات ⚙ لإعداده.'}}catch(error){document.querySelector('#provider-note').textContent=error.message}}
function settingField(field){const wrapper=document.createElement('label');wrapper.className='setting';const title=document.createElement('span');title.textContent=field.label;const row=document.createElement('span');row.className='setting-row';const input=document.createElement('input');input.className='settings-input';input.name=field.key;input.type=field.secret?'password':(field.key==='PDF2EPUB_OCR_TIMEOUT'?'number':'text');input.placeholder=field.placeholder||'';input.value=field.value||'';input.autocomplete=field.secret?'new-password':'off';row.append(input);if(field.secret){const clearLabel=document.createElement('label');const clear=document.createElement('input');clear.type='checkbox';clear.dataset.clearKey=field.key;clearLabel.append(clear,' مسح');row.append(clearLabel)}const note=document.createElement('small');note.textContent=(sourceLabels[field.source]||field.source)+(field.secret&&field.configured?' — توجد قيمة محفوظة':'');wrapper.append(title,row,note);return wrapper}
async function loadSettings(){settingsMessage.textContent='جارٍ تحميل الإعدادات…';settingsFields.replaceChildren();try{const data=await jsonRequest('/api/settings');const groups=new Map();for(const field of data.fields){if(!groups.has(field.section)){const group=document.createElement('section');group.className='settings-group';const heading=document.createElement('h3');heading.textContent=field.section;group.append(heading);groups.set(field.section,group);settingsFields.append(group)}groups.get(field.section).append(settingField(field))}settingsMessage.textContent=''}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}}
document.querySelector('#settings-button').onclick=async()=>{dialog.showModal();await loadSettings()};document.querySelector('#settings-close').onclick=()=>dialog.close();
settingsForm.onsubmit=async event=>{event.preventDefault();const values={};for(const input of settingsForm.querySelectorAll('.settings-input')){if(input.type!=='password'||input.value)values[input.name]=input.value}for(const clear of settingsForm.querySelectorAll('[data-clear-key]:checked'))values[clear.dataset.clearKey]='';settingsMessage.className='';settingsMessage.textContent='جارٍ الحفظ…';try{await jsonRequest('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});settingsMessage.className='success';settingsMessage.textContent='تم الحفظ في ذاكرة الخادم.';await loadProviders();await loadSettings()}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}};
document.querySelector('#settings-reset').onclick=async()=>{if(!confirm('حذف كل قيم الصفحة والعودة إلى .env؟'))return;try{await jsonRequest('/api/settings',{method:'DELETE'});await loadProviders();await loadSettings();settingsMessage.className='success';settingsMessage.textContent='تمت العودة إلى قيم .env.'}catch(error){settingsMessage.className='error';settingsMessage.textContent=error.message}};
form.onsubmit=async event=>{event.preventDefault();const selected=providers.find(provider=>provider.id===providerSelect.value);if(!selected||!selected.ready){alert('اختر محرك OCR جاهزاً من الإعدادات.');return}if(selected.remote&&!confirm('سيتم إرسال صور الصفحات التي تحتاج إلى OCR إلى خدمة خارجية. هل تريد المتابعة؟'))return;const current=++run;statusBox.classList.remove('hidden');form.classList.add('hidden');restart.classList.add('hidden');const body=new FormData(form);body.set('remote_acknowledged',selected.remote?'true':'false');for(const name of ['preserve_images','detect_chapters','detect_footnotes','fix_bidi','ocr_bad_pages'])body.set(name,form.elements[name].checked?'true':'false');try{const data=await jsonRequest('/api/jobs',{method:'POST',body});poll(data.job_id,current)}catch(error){showError(error.message)}};
async function poll(id,current){if(current!==run)return;try{const data=await jsonRequest('/api/jobs/'+id);document.querySelector('#stage').textContent=data.stage;document.querySelector('#progress').style.width=data.percent+'%';document.querySelector('#percent').textContent=data.percent+'%';if(data.state==='failed'){if(data.downloads&&data.downloads.report)document.querySelector('#links').innerHTML='<a class="download" href="'+data.downloads.report+'">تنزيل تقرير المشكلة</a>';showError(data.error);return}if(data.state==='complete'){document.querySelector('#links').innerHTML='<a class="download" href="'+data.downloads.epub+'">تنزيل EPUB</a> <a class="download" href="'+data.downloads.report+'">تقرير الجودة</a>';const report=document.querySelector('#report');report.classList.remove('hidden');report.textContent=JSON.stringify(data.quality,null,2);restart.classList.remove('hidden');return}setTimeout(()=>poll(id,current),800)}catch(error){showError(error.message)}}
function showError(message){document.querySelector('#error').textContent=message;restart.classList.remove('hidden')}
restart.onclick=()=>{run++;form.reset();form.classList.remove('hidden');statusBox.classList.add('hidden');document.querySelector('#stage').textContent='جارٍ التحليل';document.querySelector('#progress').style.width='0%';document.querySelector('#percent').textContent='';document.querySelector('#error').textContent='';document.querySelector('#links').innerHTML='';document.querySelector('#report').textContent='';document.querySelector('#report').classList.add('hidden');restart.classList.add('hidden');loadProviders()};
loadProviders();
</script>
</body></html>"""
