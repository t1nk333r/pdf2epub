# Arabic PDF → EPUB

A page-aware Arabic PDF converter that prefers trustworthy embedded Unicode text and falls back to Arabic/English OCR only when extraction is missing or poor. It produces a reflowable EPUB 3 with RTL metadata/CSS and a page-level JSON quality report.

## What the MVP handles

- Per-page `TEXT`, `SCAN`, `HYBRID`, `BROKEN_TEXT`, and `IMAGE_HEAVY` classification
- Arabic extraction-quality scoring (replacement glyphs, disconnected letters, one-letter-token abuse, script ratio)
- Pluggable Tesseract, Ollama vision, or hosted vision-model OCR and conservative Unicode cleanup
- Approximate RTL two-column order, heading/footnote/poetry heuristics, original embedded-image preservation
- Arabic/English LTR isolation, RTL EPUB 3 navigation, and optional EPUBCheck validation
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

Optional: install `epubcheck` and put it on `PATH` for standards validation.

## Run

```bash
uvicorn pdf2epub.web:app --reload
```

Open <http://127.0.0.1:8000>, or use the CLI:

```bash
pdf2epub book.pdf --output output
```

## OCR providers

OCR is selected page by page only when embedded PDF text is missing or unreliable. The web UI lists the providers configured on the server. API keys are never accepted from the browser or written to quality reports.

### Local Tesseract

This remains the default:

```bash
pdf2epub book.pdf --ocr-provider tesseract
```

### Local or cloud Ollama

Use any Ollama vision model capable of accurately reading Arabic pages. For a local Ollama instance:

```bash
export PDF2EPUB_OLLAMA_BASE_URL=http://127.0.0.1:11434
export PDF2EPUB_OLLAMA_MODEL=gemma3
uvicorn pdf2epub.web:app
```

For Ollama cloud, set its API base URL and token:

```bash
export PDF2EPUB_OLLAMA_BASE_URL=https://ollama.com
export PDF2EPUB_OLLAMA_MODEL=your-vision-model
export PDF2EPUB_OLLAMA_API_KEY=your-token
```

The adapter calls Ollama's native `/api/chat` endpoint with a base64 PNG and non-streaming output.

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

Selecting a remote provider sends rendered images of OCR-routed pages to that service. Review the provider's privacy and retention policy before processing sensitive documents.

### Add another OCR service

Implement the small `OcrProvider` protocol in `pdf2epub/ocr.py`: give the adapter a `name` and a `recognize(image_png, language) -> OcrResult` method, then register it in `create_ocr_provider`. PDF analysis, reconstruction, reporting, and EPUB generation remain provider-independent.

## Tests

```bash
pytest
```

## Production notes

The in-process thread queue and `/tmp` storage are suitable for a single-node MVP. For production, replace them with Celery/Redis, durable object storage, persisted job metadata, upload malware scanning, authentication/rate limits, and scheduled job cleanup. OCR workloads should run in isolated workers with CPU/memory/time limits.
