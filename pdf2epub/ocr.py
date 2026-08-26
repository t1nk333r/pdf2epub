from __future__ import annotations

import base64
import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .text import normalize_arabic

OCR_PROMPT = """Extract all visible text from this document page.
Preserve Arabic exactly as printed, including line and paragraph breaks.
Preserve English, numbers, punctuation, headings, lists, and footnotes.
Follow the page's natural Arabic right-to-left reading order.
Do not translate, summarize, correct, explain, or add Markdown.
Return only the extracted text."""


class OcrError(RuntimeError):
    """A provider could not complete OCR."""


class OcrConfigurationError(OcrError):
    """A selected provider is not configured."""


@dataclass(slots=True)
class OcrResult:
    text: str
    confidence: float | None = None
    provider: str = "unknown"


class OcrProvider(Protocol):
    name: str

    def recognize(self, image_png: bytes, language: str) -> OcrResult: ...


@dataclass(slots=True)
class OcrSettings:
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gemma3"
    ollama_api_key: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = None
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> OcrSettings:
        return cls(
            ollama_base_url=os.getenv(
                "PDF2EPUB_OLLAMA_BASE_URL", "http://127.0.0.1:11434"
            ).rstrip("/"),
            ollama_model=os.getenv("PDF2EPUB_OLLAMA_MODEL", "gemma3"),
            ollama_api_key=os.getenv("PDF2EPUB_OLLAMA_API_KEY") or None,
            vision_base_url=(os.getenv("PDF2EPUB_VISION_BASE_URL") or "").rstrip("/")
            or None,
            vision_model=os.getenv("PDF2EPUB_VISION_MODEL") or None,
            vision_api_key=os.getenv("PDF2EPUB_VISION_API_KEY") or None,
            timeout_seconds=float(os.getenv("PDF2EPUB_OCR_TIMEOUT", "120")),
        )


def _clean_model_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    return normalize_arabic(text)


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item["text"])
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    return ""


def _post_json(
    url: str, payload: dict, headers: dict[str, str], timeout: float
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise OcrError(f"OCR provider returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OcrError(f"Could not reach OCR provider at {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OcrError("OCR provider returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise OcrError("OCR provider returned an unexpected response")
    return data


class TesseractOcrProvider:
    name = "tesseract"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        try:
            import io

            import pytesseract
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise OcrConfigurationError(
                "Pillow and pytesseract are required for Tesseract OCR"
            ) from exc
        image = ImageOps.autocontrast(Image.open(io.BytesIO(image_png)).convert("L"))
        try:
            data = pytesseract.image_to_data(
                image,
                lang=language,
                config="--psm 3",
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.TesseractNotFoundError as exc:
            raise OcrConfigurationError("Tesseract executable was not found") from exc
        except pytesseract.TesseractError as exc:
            raise OcrError(f"Tesseract failed: {exc}") from exc
        groups: dict[tuple[int, int, int], list[int]] = {}
        for index, raw in enumerate(data["text"]):
            if raw.strip():
                key = (
                    data["block_num"][index],
                    data["par_num"][index],
                    data["line_num"][index],
                )
                groups.setdefault(key, []).append(index)
        lines: list[str] = []
        confidences: list[float] = []
        for indices in groups.values():
            lines.append(" ".join(data["text"][i].strip() for i in indices))
            confidences.extend(
                float(data["conf"][i]) for i in indices if float(data["conf"][i]) >= 0
            )
        confidence = sum(confidences) / len(confidences) / 100 if confidences else None
        return OcrResult(
            normalize_arabic("\n".join(lines)), confidence, provider=self.name
        )


@dataclass(slots=True)
class OllamaVisionOcrProvider:
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = 120.0
    name: str = "ollama"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        encoded = base64.b64encode(image_png).decode("ascii")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = _post_json(
            f"{self.base_url.rstrip('/')}/api/chat",
            {
                "model": self.model,
                "stream": False,
                "messages": [
                    {"role": "user", "content": OCR_PROMPT, "images": [encoded]}
                ],
                "options": {"temperature": 0},
            },
            headers,
            self.timeout,
        )
        try:
            text = _message_text(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise OcrError("Ollama response did not contain message.content") from exc
        if not text.strip():
            raise OcrError("Ollama returned an empty OCR result")
        return OcrResult(_clean_model_text(str(text)), provider=self.name)


@dataclass(slots=True)
class OpenAiCompatibleVisionOcrProvider:
    """Adapter for hosted services exposing an OpenAI-compatible vision API."""

    base_url: str
    model: str
    api_key: str
    timeout: float = 120.0
    name: str = "openai_compatible"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        encoded = base64.b64encode(image_png).decode("ascii")
        data = _post_json(
            f"{self.base_url.rstrip('/')}/chat/completions",
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": OCR_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{encoded}"
                                },
                            },
                        ],
                    }
                ],
            },
            {"Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        try:
            text = _message_text(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise OcrError(
                "Vision API response did not contain choices[0].message.content"
            ) from exc
        if not text.strip():
            raise OcrError("Vision API returned an empty OCR result")
        return OcrResult(_clean_model_text(str(text)), provider=self.name)


def create_ocr_provider(name: str, settings: OcrSettings | None = None) -> OcrProvider:
    settings = settings or OcrSettings.from_env()
    if name == "tesseract":
        return TesseractOcrProvider()
    if name == "ollama":
        if not settings.ollama_base_url or not settings.ollama_model:
            raise OcrConfigurationError(
                "Ollama requires PDF2EPUB_OLLAMA_BASE_URL and PDF2EPUB_OLLAMA_MODEL"
            )
        return OllamaVisionOcrProvider(
            settings.ollama_base_url,
            settings.ollama_model,
            settings.ollama_api_key,
            settings.timeout_seconds,
        )
    if name == "openai_compatible":
        if not (
            settings.vision_base_url
            and settings.vision_model
            and settings.vision_api_key
        ):
            raise OcrConfigurationError(
                "Online vision OCR requires PDF2EPUB_VISION_BASE_URL, "
                "PDF2EPUB_VISION_MODEL, and PDF2EPUB_VISION_API_KEY"
            )
        return OpenAiCompatibleVisionOcrProvider(
            settings.vision_base_url,
            settings.vision_model,
            settings.vision_api_key,
            settings.timeout_seconds,
        )
    raise OcrConfigurationError(f"Unknown OCR provider: {name}")


def provider_status(settings: OcrSettings | None = None) -> list[dict]:
    settings = settings or OcrSettings.from_env()
    return [
        {
            "id": "tesseract",
            "label": "Tesseract (local)",
            "configured": shutil.which("tesseract") is not None,
        },
        {
            "id": "ollama",
            "label": f"Ollama — {settings.ollama_model}",
            "configured": bool(
                settings.ollama_base_url
                and settings.ollama_model
                and (os.getenv("PDF2EPUB_OLLAMA_BASE_URL") or shutil.which("ollama"))
            ),
        },
        {
            "id": "openai_compatible",
            "label": "Online vision model",
            "configured": bool(
                settings.vision_base_url
                and settings.vision_model
                and settings.vision_api_key
            ),
        },
    ]
