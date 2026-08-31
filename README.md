# Arabic PDF → EPUB

A page-aware Arabic PDF converter that prefers trustworthy embedded Unicode text and falls back to Arabic/English OCR only when extraction is missing or poor. It produces a reflowable EPUB 3 with RTL metadata/CSS and a page-level JSON quality report.

## What the MVP handles

- Per-page `TEXT`, `SCAN`, `HYBRID`, `BROKEN_TEXT`, and `IMAGE_HEAVY` classification
- Arabic extraction-quality signals, candidate comparison, and a fail-closed missing-content gate
- Registry-based Tesseract, Ollama, Mistral Document OCR, or OpenAI-compatible vision OCR
- Approximate RTL two-column order, heading/footnote/poetry heuristics, original embedded-image preservation
- Arabic/English LTR isolation, RTL EPUB 3 navigation, mandatory internal validation, and EPUBCheck integration
- Web upload/status/download interface and a command-line interface

This is an MVP: complex region-level hybrid OCR, semantic AI correction, high-fidelity tables, and OCR review PDFs need specialist layout/OCR models and are not presented as solved.

## Install

Python 3.11–3.14 is supported. Install Poppler only if you want its separate command-line diagnostics; PyMuPDF powers the current pipeline.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

For scanned Arabic pages, install Tesseract plus Arabic language data:

```bash
# macOS
brew install tesseract tesseract-lang

# Debian/Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng
```

Install `epubcheck` and put it on `PATH`; it is required for normal publication.
Internal ZIP, XML, manifest, spine, navigation, and image validation also always
runs. A missing validator or any conformance error is fatal and the EPUB is not
published.

The web page reports missing local capabilities before upload. A red EPUBCheck
warning means analysis can run, but a normal EPUB cannot be published until
EPUBCheck is installed and available on `PATH`. A Tesseract warning means local
OCR is disabled because the executable or its `ara` language data is missing;
you may still use another OCR provider that is shown as ready. The Docker image
includes EPUBCheck and Arabic/English Tesseract, while bare-metal installations
must provide them separately using the commands above.

## Durable jobs, pause, and recovery

The web dashboard stores every completed page as an atomic checkpoint. Use
**إيقاف مؤقت** to request a cooperative pause: an in-flight OCR page finishes,
is saved, and only then does the job become paused. Use **متابعة التحويل** to
continue from the first missing page. After a sudden power failure, at most the
currently in-flight page repeats once the preceding checkpoint is durable.

Set `PDF2EPUB_DATA_DIR` to a persistent, backed-up filesystem. The directory is
sensitive: it retains uploaded PDFs, extracted text, images, checkpoints, and
final output. Startup marks unfinished work `interrupted` and never resumes GPU
work automatically. Resume verifies the source, page count, options, provider,
model, prompt contract, and checkpoint schema. Restore a missing Ollama model or
OCR provider through the settings/environment, then explicitly resume.

There is no automatic cleanup, quota, or retention policy yet. Monitor disk
usage and back up the entire data root while the service is stopped. Run exactly
one application process and one Uvicorn worker against a data root; the local
lease is not a distributed lock.

## Run

```bash
uvicorn pdf2epub.web:app --reload
```

Open <http://127.0.0.1:8000>, or use the CLI:

```bash
pdf2epub book.pdf --output output
```

The cog button in the navigation bar opens the OCR settings panel. Values saved
there override `.env` values until the server restarts; they are kept only in
server memory. The effective order is **web page → environment / `.env` → built-in
default**. Secret fields are never returned to the browser after saving. Use the
reset button to discard all web-page overrides and return to `.env` values.

Keep the application bound to localhost unless you add authentication and TLS.
Anyone who can reach the settings page can replace the in-memory OCR endpoints
and credentials.

## Docker

The image includes Arabic/English Tesseract, Java, and EPUBCheck. To run it:

```bash
cp .env.example .env
docker compose up --build
```

Open <http://127.0.0.1:8000>. The Compose file binds only to localhost and stores
durable jobs and checkpoints under `/data` in the `pdf2epub_data` volume. Set `PDF2EPUB_PORT` in `.env`
if port 8000 is already used; this Compose-only value cannot be changed from the
web settings panel because it controls the listener before the app starts.

Inside Docker, the default Ollama URL is `http://host.docker.internal:11434`.
On Linux/Omarchy, Ollama must listen on an address reachable by Docker rather
than only `127.0.0.1`. API keys entered through the cog are intentionally lost
when the container restarts; put persistent secrets in the local `.env` file,
which is excluded from the image and Git.

### Jenkins

The included `Jenkinsfile` validates the Compose configuration, runs the tests
and Ruff under Python 3.14 with a bounded stage timeout, builds a tagged Docker
image, verifies EPUBCheck and the Arabic/English Tesseract languages, and runs
the container health check. The Jenkins agent needs Docker with Compose v2 and
permission to access the Docker daemon.

## OCR providers

OCR is selected page by page only when embedded PDF text is missing or unreliable. Questionable embedded and OCR candidates are compared and recorded in the quality report. A conversion fails instead of publishing a normal EPUB when required pages have no usable candidate. The web UI lists provider readiness, distinguishes local and remote processing, and requires confirmation before transmitting page images to a remote provider. API keys are never written to quality reports or returned by the settings API.

### Local Tesseract

This remains the default:

```bash
pdf2epub book.pdf --ocr-provider tesseract
```

### Ollama Arabic vision OCR

The Ollama adapter is restricted to the tested non-thinking models. For the local server:

```bash
export PDF2EPUB_OLLAMA_BASE_URL=http://192.168.100.19:11435
export PDF2EPUB_OLLAMA_MODEL=qwen3-vl:32b-instruct-gpu
export PDF2EPUB_OLLAMA_NUM_CTX=8192
export PDF2EPUB_OCR_TIMEOUT=300
uvicorn pdf2epub.web:app
```

The per-book selector offers:

- `qwen3-vl:32b-instruct-gpu` — quality default
- `qwen3-vl:30b-instruct-gpu` — fast
- `qwen3.8:27b-q4_K_M` — lower-VRAM alternative

Each page is resized to a grayscale 1280 px JPEG and sent in its own stateless
`/api/chat` request. The adapter uses an 8K context (up to 16K only for Qwen3.8),
disables thinking, serializes requests, and retries HTTP 5xx/connection resets.
Suspicious output from the fast or alternative model is re-run with the 32B
instruct model. Per-attempt token counts, duration, retries, and fallback reason
are written to the live log and quality report.

### Hosted OpenAI-compatible vision APIs

This adapter works with online model services exposing the `POST /chat/completions` vision schema:

```bash
export PDF2EPUB_VISION_BASE_URL=https://provider.example/v1
export PDF2EPUB_VISION_MODEL=arabic-vision-model
export PDF2EPUB_VISION_API_KEY=your-api-key
export PDF2EPUB_OCR_TIMEOUT=300
uvicorn pdf2epub.web:app
```

You can also copy `.env.example` to `.env` and start Uvicorn with `--env-file .env` instead of exporting variables in the shell.

Then choose **Online vision model** in the UI or use:

```bash
pdf2epub book.pdf --ocr-provider openai_compatible
```

### Mistral Document OCR

Mistral uses its dedicated Document AI `/v1/ocr` endpoint rather than the
chat-completions compatibility adapter:

```bash
export PDF2EPUB_MISTRAL_API_KEY=your-api-key
export PDF2EPUB_MISTRAL_MODEL=mistral-ocr-latest
uvicorn pdf2epub.web:app
```

Then choose **Mistral Document OCR** in the UI or use:

```bash
pdf2epub book.pdf --ocr-provider mistral
```

Selecting a remote provider sends rendered images of OCR-routed pages to that service. Review the provider's privacy and retention policy before processing sensitive documents.

### Add another OCR service

Implement the small `OcrProvider` protocol in `pdf2epub/ocr.py`, add a `ProviderDescriptor`, and register it once in `PROVIDER_REGISTRY`. CLI choices, API validation, readiness metadata, and the web dropdown are derived from that registry.

## Integrity and validation

The quality report has a versioned `integrity` section and records page source
selection plus embedded/OCR candidate evidence. Pages with no recoverable text
or pages still needing judgment block normal publication. Pass `--allow-partial`
on the CLI, tick the matching box in the upload form, or set
`allow_partial=True` programmatically to publish a diagnostic EPUB anyway; that
output remains marked for review and is never the default.

A page is held for review only when the scorer names an actual defect. A page
that scores low purely because it is short — a running head, a contents row —
carries no defect and publishes normally. When the embedded text layer does
trip a defect check, OCR is asked for a second opinion, and a tie is resolved
in favour of OCR rather than silently keeping the flagged text.

Generated EPUBs are built under a temporary name, validated, and atomically
published. Failed validation leaves a downloadable diagnostic report but no new
EPUB presented as successful. Embedded images are accepted only from pages using
embedded text and must pass conservative placement limits; full-page scans,
tiles, uncertain placements, and images from OCR-selected pages are excluded.

## Tests

```bash
pytest
```

## Production notes

Durable checkpoints support one local application process. For multi-process or
multi-host production, replace the local lease with a database-backed queue and
distributed ownership before adding workers. Also add upload malware scanning,
authentication/rate limits, scheduled retention, and CPU/memory/time isolation.
