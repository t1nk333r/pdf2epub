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

Python 3.11–3.13 is recommended. Install Poppler only if you want its separate command-line diagnostics; PyMuPDF powers the current pipeline.

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
temporary job data in the `pdf2epub_data` volume. Set `PDF2EPUB_PORT` in `.env`
if port 8000 is already used; this Compose-only value cannot be changed from the
web settings panel because it controls the listener before the app starts.

Inside Docker, the default Ollama URL is `http://host.docker.internal:11434`.
On Linux/Omarchy, Ollama must listen on an address reachable by Docker rather
than only `127.0.0.1`. API keys entered through the cog are intentionally lost
when the container restarts; put persistent secrets in the local `.env` file,
which is excluded from the image and Git.

### Jenkins

The included `Jenkinsfile` validates the Compose configuration, builds a tagged
Docker image, verifies EPUBCheck and the Arabic/English Tesseract languages, and
runs the container health check. The Jenkins agent needs Docker with Compose v2
and permission to access the Docker daemon.

## OCR providers

OCR is selected page by page only when embedded PDF text is missing or unreliable. Questionable embedded and OCR candidates are compared and recorded in the quality report. A conversion fails instead of publishing a normal EPUB when required pages have no usable candidate. The web UI lists provider readiness, distinguishes local and remote processing, and requires confirmation before transmitting page images to a remote provider. API keys are never written to quality reports or returned by the settings API.

### Local Tesseract

This remains the default:

```bash
pdf2epub book.pdf --ocr-provider tesseract
```

### Local or cloud Ollama

Use any Ollama vision model capable of accurately reading Arabic pages. For a local Ollama instance:

```bash
export PDF2EPUB_OLLAMA_BASE_URL=http://127.0.0.1:11434
export PDF2EPUB_OLLAMA_MODEL=qwen3.5:27b
uvicorn pdf2epub.web:app
```

For Ollama cloud, set its API base URL and token:

```bash
export PDF2EPUB_OLLAMA_BASE_URL=https://ollama.com
export PDF2EPUB_OLLAMA_MODEL=your-vision-model
export PDF2EPUB_OLLAMA_API_KEY=your-token
```

The adapter calls Ollama's native `/api/chat` endpoint with a base64 PNG,
non-streaming output, and thinking disabled so OCR returns only the transcription.
The `qwen3.5:27b` default is intended for a 24 GB GPU; override it for smaller
or larger hardware.

### Hosted OpenAI-compatible vision APIs

This adapter works with online model services exposing the `POST /chat/completions` vision schema:

```bash
export PDF2EPUB_VISION_BASE_URL=https://provider.example/v1
export PDF2EPUB_VISION_MODEL=arabic-vision-model
export PDF2EPUB_VISION_API_KEY=your-api-key
export PDF2EPUB_OCR_TIMEOUT=120
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
or pages still needing judgment block normal publication. Programmatic callers
may explicitly set `allow_partial=True` to create a diagnostic EPUB; that output
remains marked for review and is never the web or CLI default.

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

The in-process thread queue and `/tmp` storage are suitable for a single-node MVP. For production, replace them with Celery/Redis, durable object storage, persisted job metadata, upload malware scanning, authentication/rate limits, and scheduled job cleanup. OCR workloads should run in isolated workers with CPU/memory/time limits.
