from __future__ import annotations

import base64
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .settings import setting_value
from .text import normalize_arabic

OCR_PROMPT = """Extract all visible text from this document page.
Preserve Arabic exactly as printed, including line and paragraph breaks.
Preserve English, numbers, punctuation, headings, lists, and footnotes.
Follow the page's natural Arabic right-to-left reading order.
Do not translate, summarize, correct, explain, or add Markdown.
Return only the extracted text."""
MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024


class OcrError(RuntimeError):
    """A provider could not complete OCR."""


class OcrConfigurationError(OcrError):
    """A selected provider is not configured."""


@dataclass(slots=True)
class OcrRegion:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None = None


@dataclass(slots=True)
class OcrResult:
    text: str
    confidence: float | None = None
    provider: str = "unknown"
    regions: list[OcrRegion] = field(default_factory=list)


class OcrProvider(Protocol):
    name: str

    def recognize(self, image_png: bytes, language: str) -> OcrResult: ...


@dataclass(slots=True)
class OcrSettings:
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:27b"
    ollama_api_key: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = None
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_model: str = "mistral-ocr-latest"
    mistral_api_key: str | None = None
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> OcrSettings:
        raw_timeout = setting_value("PDF2EPUB_OCR_TIMEOUT", "120") or "120"
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 120.0
        return cls(
            ollama_base_url=(
                setting_value("PDF2EPUB_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
                or ""
            ).rstrip("/"),
            ollama_model=setting_value("PDF2EPUB_OLLAMA_MODEL", "qwen3.5:27b")
            or "qwen3.5:27b",
            ollama_api_key=setting_value("PDF2EPUB_OLLAMA_API_KEY") or None,
            vision_base_url=(setting_value("PDF2EPUB_VISION_BASE_URL") or "").rstrip(
                "/"
            )
            or None,
            vision_model=setting_value("PDF2EPUB_VISION_MODEL") or None,
            vision_api_key=setting_value("PDF2EPUB_VISION_API_KEY") or None,
            mistral_base_url=(
                setting_value("PDF2EPUB_MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
                or ""
            ).rstrip("/"),
            mistral_model=setting_value("PDF2EPUB_MISTRAL_MODEL", "mistral-ocr-latest")
            or "mistral-ocr-latest",
            mistral_api_key=setting_value("PDF2EPUB_MISTRAL_API_KEY") or None,
            timeout_seconds=max(1.0, min(timeout, 900.0)),
        )


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    id: str
    label: str
    remote: bool
    factory: Callable[[OcrSettings], OcrProvider]
    readiness: Callable[[OcrSettings], tuple[bool, bool, str]]


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
            body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                raise OcrError("OCR provider response exceeded the 8 MB limit")
            data = json.loads(body)
    except urllib.error.HTTPError as exc:
        raise OcrError(f"OCR provider returned HTTP {exc.code}") from exc
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
        regions: list[OcrRegion] = []
        confidences: list[float] = []
        for indices in groups.values():
            line_text = " ".join(data["text"][i].strip() for i in indices)
            lines.append(line_text)
            line_confidences = [
                float(data["conf"][i]) for i in indices if float(data["conf"][i]) >= 0
            ]
            confidences.extend(line_confidences)
            x0 = min(float(data["left"][i]) for i in indices)
            y0 = min(float(data["top"][i]) for i in indices)
            x1 = max(float(data["left"][i]) + float(data["width"][i]) for i in indices)
            y1 = max(float(data["top"][i]) + float(data["height"][i]) for i in indices)
            regions.append(
                OcrRegion(
                    line_text,
                    (x0, y0, x1, y1),
                    sum(line_confidences) / len(line_confidences) / 100
                    if line_confidences
                    else None,
                )
            )
        confidence = sum(confidences) / len(confidences) / 100 if confidences else None
        return OcrResult(
            normalize_arabic("\n".join(lines)),
            confidence,
            provider=self.name,
            regions=regions,
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
                "think": False,
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


@dataclass(slots=True)
class MistralDocumentOcrProvider:
    """Adapter for Mistral's dedicated Document AI OCR endpoint."""

    base_url: str
    model: str
    api_key: str
    timeout: float = 120.0
    name: str = "mistral"

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        del language
        encoded = base64.b64encode(image_png).decode("ascii")
        data = _post_json(
            f"{self.base_url.rstrip('/')}/ocr",
            {
                "model": self.model,
                "document": {
                    "type": "image_url",
                    "image_url": f"data:image/png;base64,{encoded}",
                },
                "include_blocks": True,
                "include_image_base64": False,
                "confidence_scores_granularity": "block",
            },
            {"Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        try:
            pages = data["pages"]
            text = "\n\n".join(
                str(page.get("markdown", ""))
                for page in pages
                if isinstance(page, dict)
            )
        except (KeyError, TypeError) as exc:
            raise OcrError("Mistral OCR response did not contain pages") from exc
        if not text.strip():
            raise OcrError("Mistral OCR returned an empty result")
        confidence_values: list[float] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            confidence = page.get("confidence_scores")
            if not isinstance(confidence, dict):
                continue
            page_confidence = confidence.get("page") or confidence.get(
                "page_confidence"
            )
            if isinstance(page_confidence, int | float):
                confidence_values.append(float(page_confidence))
        average = (
            sum(confidence_values) / len(confidence_values)
            if confidence_values
            else None
        )
        return OcrResult(_clean_model_text(text), average, provider=self.name)


def _tesseract_factory(settings: OcrSettings) -> OcrProvider:
    del settings
    if not shutil.which("tesseract"):
        raise OcrConfigurationError("Tesseract executable was not found")
    return TesseractOcrProvider()


def _tesseract_readiness(settings: OcrSettings) -> tuple[bool, bool, str]:
    del settings
    executable = shutil.which("tesseract")
    if not executable:
        return False, False, "Tesseract executable was not found"
    try:
        result = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True, False, "Tesseract language availability could not be checked"
    languages = set((result.stdout + result.stderr).splitlines())
    if "ara" not in languages:
        return True, False, "Tesseract is installed but Arabic language data is missing"
    return True, True, "Tesseract and Arabic language data are available"


def _ollama_factory(settings: OcrSettings) -> OcrProvider:
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


def _ollama_readiness(settings: OcrSettings) -> tuple[bool, bool, str]:
    configured = bool(settings.ollama_base_url and settings.ollama_model)
    if not configured:
        return False, False, "Set the Ollama base URL and model, or install Ollama"
    headers = {"Accept": "application/json"}
    if settings.ollama_api_key:
        headers["Authorization"] = f"Bearer {settings.ollama_api_key}"
    request = urllib.request.Request(
        f"{settings.ollama_base_url.rstrip('/')}/api/tags", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                return True, False, "Ollama model listing exceeded the response limit"
            data = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - readiness returns sanitized diagnostics
        return (
            True,
            False,
            f"Ollama is configured but not reachable: {type(exc).__name__}",
        )
    models = {
        str(model.get("name", ""))
        for model in data.get("models", [])
        if isinstance(model, dict)
    }
    requested = settings.ollama_model
    available = requested in models or any(
        name.split(":", 1)[0] == requested.split(":", 1)[0] for name in models
    )
    if not available:
        return (
            True,
            False,
            f"Ollama is reachable but model {requested} is not installed",
        )
    return True, True, f"Ollama model {requested} is ready"


def _openai_factory(settings: OcrSettings) -> OcrProvider:
    if not (
        settings.vision_base_url and settings.vision_model and settings.vision_api_key
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


def _openai_readiness(settings: OcrSettings) -> tuple[bool, bool, str]:
    configured = bool(
        settings.vision_base_url and settings.vision_model and settings.vision_api_key
    )
    if not configured:
        return False, False, "Online vision endpoint, model, and API key are required"
    return (
        True,
        True,
        "Configuration is present; the endpoint is checked on first OCR call",
    )


def _mistral_factory(settings: OcrSettings) -> OcrProvider:
    if not (
        settings.mistral_base_url
        and settings.mistral_model
        and settings.mistral_api_key
    ):
        raise OcrConfigurationError(
            "Mistral OCR requires PDF2EPUB_MISTRAL_API_KEY and a model"
        )
    return MistralDocumentOcrProvider(
        settings.mistral_base_url,
        settings.mistral_model,
        settings.mistral_api_key,
        settings.timeout_seconds,
    )


def _mistral_readiness(settings: OcrSettings) -> tuple[bool, bool, str]:
    configured = bool(
        settings.mistral_base_url
        and settings.mistral_model
        and settings.mistral_api_key
    )
    if not configured:
        return False, False, "Set PDF2EPUB_MISTRAL_API_KEY to enable Mistral OCR"
    return (
        True,
        True,
        "Configuration is present; the endpoint is checked on first OCR call",
    )


PROVIDER_REGISTRY: dict[str, ProviderDescriptor] = {}


def register_provider(descriptor: ProviderDescriptor) -> None:
    if descriptor.id in PROVIDER_REGISTRY:
        raise ValueError(f"OCR provider is already registered: {descriptor.id}")
    PROVIDER_REGISTRY[descriptor.id] = descriptor


register_provider(
    ProviderDescriptor(
        "tesseract",
        "Tesseract (local)",
        False,
        _tesseract_factory,
        _tesseract_readiness,
    )
)
register_provider(
    ProviderDescriptor("ollama", "Ollama", False, _ollama_factory, _ollama_readiness)
)
register_provider(
    ProviderDescriptor(
        "openai_compatible",
        "Online vision model",
        True,
        _openai_factory,
        _openai_readiness,
    )
)
register_provider(
    ProviderDescriptor(
        "mistral",
        "Mistral Document OCR",
        True,
        _mistral_factory,
        _mistral_readiness,
    )
)


def provider_names() -> tuple[str, ...]:
    return tuple(PROVIDER_REGISTRY)


def provider_descriptor(name: str) -> ProviderDescriptor | None:
    return PROVIDER_REGISTRY.get(name)


def create_ocr_provider(name: str, settings: OcrSettings | None = None) -> OcrProvider:
    settings = settings or OcrSettings.from_env()
    descriptor = PROVIDER_REGISTRY.get(name)
    if descriptor is None:
        raise OcrConfigurationError(f"Unknown OCR provider: {name}")
    return descriptor.factory(settings)


def provider_status(settings: OcrSettings | None = None) -> list[dict]:
    settings = settings or OcrSettings.from_env()
    statuses: list[dict] = []
    for descriptor in PROVIDER_REGISTRY.values():
        configured, ready, detail = descriptor.readiness(settings)
        label = descriptor.label
        if descriptor.id == "ollama":
            label = f"Ollama — {settings.ollama_model}"
        statuses.append(
            {
                "id": descriptor.id,
                "label": label,
                "configured": configured,
                "ready": ready,
                "remote": descriptor.remote,
                "detail": detail,
            }
        )
    return statuses
