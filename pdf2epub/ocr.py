from __future__ import annotations

import base64
import http.client
import io
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import median
from typing import Protocol

from .settings import setting_value
from .text import normalize_arabic

OCR_PROMPT = """You are an OCR engine for printed Arabic books. Transcribe the page image exactly as printed.
Rules:
- Output ONLY the page text. No preamble, no explanations, no translation, no commentary.
- Keep the text in Arabic, right-to-left, exactly as written. Preserve diacritics (تشكيل), punctuation, and Qur'anic/verse markers as printed.
- Preserve paragraph breaks: one blank line between paragraphs. Do not merge or split paragraphs.
- Mark headings by prefixing with "# " (chapter) or "## " (section).
- Footnotes: in the body put the marker as [^N]; list footnote texts at the end of the page as "[^N]: text".
- Poetry: put each hemistich pair on one line separated by " ... ".
- Page numbers, running headers/footers: omit.
- If part of the page is unreadable, write [غير مقروء] in its place; never guess."""
MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
OLLAMA_IMAGE_LONG_SIDE = 1280
OLLAMA_IMAGE_ABSOLUTE_LIMIT = 1472
OLLAMA_MAX_RETRIES = 3
OLLAMA_RETRY_DELAY_SECONDS = 10
_OLLAMA_REQUEST_LOCK = threading.Lock()
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
OLLAMA_QUALITY_MODEL = "qwen3-vl:32b-instruct-gpu"
OLLAMA_FAST_MODEL = "qwen3-vl:30b-instruct-gpu"
OLLAMA_ALTERNATIVE_MODEL = "qwen3.8:27b-q4_K_M"
OLLAMA_BOOK_MODELS = (
    OLLAMA_QUALITY_MODEL,
    OLLAMA_FAST_MODEL,
    OLLAMA_ALTERNATIVE_MODEL,
)


class OcrError(RuntimeError):
    """A provider could not complete OCR."""


class OcrConfigurationError(OcrError):
    """A selected provider is not configured."""


class OcrTransientError(OcrError):
    """A provider failure that can be retried safely."""


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
    model: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    duration_seconds: float | None = None
    retries: int = 0
    attempts: list[dict] = field(default_factory=list)
    fallback_reason: str | None = None


class OcrProvider(Protocol):
    name: str

    def recognize(self, image_png: bytes, language: str) -> OcrResult: ...


@dataclass(slots=True)
class OcrSettings:
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = OLLAMA_QUALITY_MODEL
    ollama_api_key: str | None = None
    ollama_num_ctx: int = 8192
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = None
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_model: str = "mistral-ocr-latest"
    mistral_api_key: str | None = None
    timeout_seconds: float = 300.0

    @classmethod
    def from_env(cls) -> OcrSettings:
        raw_timeout = setting_value("PDF2EPUB_OCR_TIMEOUT", "300") or "300"
        raw_ollama_num_ctx = (
            setting_value("PDF2EPUB_OLLAMA_NUM_CTX", "8192") or "8192"
        )
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 300.0
        try:
            ollama_num_ctx = int(raw_ollama_num_ctx)
        except ValueError:
            ollama_num_ctx = 8192
        return cls(
            ollama_base_url=(
                setting_value("PDF2EPUB_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
                or ""
            ).rstrip("/"),
            ollama_model=setting_value(
                "PDF2EPUB_OLLAMA_MODEL", OLLAMA_QUALITY_MODEL
            )
            or OLLAMA_QUALITY_MODEL,
            ollama_api_key=setting_value("PDF2EPUB_OLLAMA_API_KEY") or None,
            ollama_num_ctx=max(512, min(ollama_num_ctx, 16384)),
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
            timeout_seconds=max(300.0, min(timeout, 900.0)),
        )


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    id: str
    label: str
    remote: bool
    factory: Callable[[OcrSettings], OcrProvider]
    readiness: Callable[[OcrSettings], tuple[bool, bool, str]]


def _clean_model_text(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text)
    text = re.sub(r"<think>.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    return normalize_arabic(text)


def _prepare_vision_image(image_bytes: bytes) -> bytes:
    """Bound vision tokens by resizing and normalizing a page before upload."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise OcrConfigurationError("Pillow is required for vision OCR") from exc
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
            target = min(OLLAMA_IMAGE_LONG_SIDE, OLLAMA_IMAGE_ABSOLUTE_LIMIT)
            image.thumbnail((target, target), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=87, optimize=True)
            return output.getvalue()
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError(f"Could not prepare page image for OCR: {exc}") from exc


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
        if 500 <= exc.code <= 599:
            raise OcrTransientError(
                f"OCR provider returned HTTP {exc.code}"
            ) from exc
        raise OcrError(f"OCR provider returned HTTP {exc.code}") from exc
    except (ConnectionResetError, http.client.RemoteDisconnected) as exc:
        raise OcrTransientError(f"OCR provider reset the connection: {exc}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        if isinstance(getattr(exc, "reason", None), ConnectionResetError) or "reset" in str(
            exc
        ).lower():
            raise OcrTransientError(
                f"OCR provider reset the connection: {exc}"
            ) from exc
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
    timeout: float = 300.0
    num_ctx: int = 8192
    fallback_model: str | None = None
    page_lengths: list[int] = field(default_factory=list)
    name: str = "ollama"
    prompt: str = OCR_PROMPT

    def _request_model(self, model: str, encoded: str) -> tuple[str, dict]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        request_num_ctx = min(
            self.num_ctx, 16384 if model == OLLAMA_ALTERNATIVE_MODEL else 8192
        )
        retries = 0
        started = time.monotonic()
        while True:
            try:
                data = _post_json(
                    f"{self.base_url.rstrip('/')}/api/chat",
                    {
                        "model": model,
                        "stream": False,
                        "think": False,
                        "messages": [
                            {"role": "system", "content": self.prompt},
                            {
                                "role": "user",
                                "content": "استخرج نص هذه الصفحة.",
                                "images": [encoded],
                            },
                        ],
                        "options": {
                            "temperature": 0,
                            "num_ctx": request_num_ctx,
                            "num_predict": 3000,
                            "repeat_penalty": 1.05,
                        },
                    },
                    headers,
                    max(self.timeout, 300.0),
                )
                break
            except OcrTransientError as exc:
                if retries >= OLLAMA_MAX_RETRIES:
                    raise OcrError(
                        f"Ollama model {model} failed after {retries} retries: {exc}"
                    ) from exc
                retries += 1
                time.sleep(OLLAMA_RETRY_DELAY_SECONDS)
        try:
            text = _message_text(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise OcrError("Ollama response did not contain message.content") from exc
        if not text.strip():
            raise OcrError("Ollama returned an empty OCR result")
        duration = data.get("total_duration")
        duration_seconds = (
            float(duration) / 1_000_000_000
            if isinstance(duration, int | float)
            else time.monotonic() - started
        )
        metrics = {
            "model": model,
            "prompt_eval_count": int(data.get("prompt_eval_count", 0)),
            "eval_count": int(data.get("eval_count", 0)),
            "total_duration": int(data.get("total_duration", 0)),
            "duration_seconds": round(duration_seconds, 3),
            "retries": retries,
        }
        return _clean_model_text(str(text)), metrics

    def _fallback_reason(self, text: str, metrics: dict) -> str | None:
        visible_length = len(re.sub(r"\s+", "", text))
        if self.page_lengths and visible_length < median(self.page_lengths) * 0.15:
            return "output shorter than 15% of the median page length"
        if re.search(r"[A-Za-z]", text):
            return "output contains Latin letters"
        if text.count("[غير مقروء]") > 3:
            return "output contains more than three unreadable markers"
        if int(metrics.get("eval_count", 0)) >= 3000:
            return "output exhausted the 3000-token generation budget"
        return None

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        del language
        prepared = _prepare_vision_image(image_png)
        encoded = base64.b64encode(prepared).decode("ascii")
        attempts: list[dict] = []
        with _OLLAMA_REQUEST_LOCK:
            text, metrics = self._request_model(self.model, encoded)
            attempts.append(metrics)
            fallback_reason = (
                self._fallback_reason(text, metrics)
                if self.fallback_model and self.model != self.fallback_model
                else None
            )
            if fallback_reason:
                text, metrics = self._request_model(
                    self.fallback_model or self.model, encoded
                )
                attempts.append(metrics)
        self.page_lengths.append(len(re.sub(r"\s+", "", text)))
        return OcrResult(
            text,
            provider=self.name,
            model=str(metrics["model"]),
            prompt_tokens=int(metrics["prompt_eval_count"]),
            output_tokens=int(metrics["eval_count"]),
            duration_seconds=sum(float(item["duration_seconds"]) for item in attempts),
            retries=sum(int(item["retries"]) for item in attempts),
            attempts=attempts,
            fallback_reason=fallback_reason,
        )


@dataclass(slots=True)
class OpenAiCompatibleVisionOcrProvider:
    """Adapter for hosted services exposing an OpenAI-compatible vision API."""

    base_url: str
    model: str
    api_key: str
    timeout: float = 300.0
    name: str = "openai_compatible"
    prompt: str = OCR_PROMPT

    def recognize(self, image_png: bytes, language: str) -> OcrResult:
        encoded = base64.b64encode(_prepare_vision_image(image_png)).decode("ascii")
        data = _post_json(
            f"{self.base_url.rstrip('/')}/chat/completions",
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": 3000,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [
                    {"role": "system", "content": self.prompt},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{encoded}"
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
    if settings.ollama_model not in OLLAMA_BOOK_MODELS:
        raise OcrConfigurationError(
            "Ollama OCR model must be an approved non-thinking model: "
            + ", ".join(OLLAMA_BOOK_MODELS)
        )
    return OllamaVisionOcrProvider(
        settings.ollama_base_url,
        settings.ollama_model,
        settings.ollama_api_key,
        settings.timeout_seconds,
        settings.ollama_num_ctx,
    )


def _ollama_readiness(settings: OcrSettings) -> tuple[bool, bool, str]:
    configured = bool(settings.ollama_base_url and settings.ollama_model)
    if not configured:
        return False, False, "Set the Ollama base URL and model, or install Ollama"
    if settings.ollama_model not in OLLAMA_BOOK_MODELS:
        return (
            True,
            False,
            f"Model {settings.ollama_model} is not an approved non-thinking OCR model",
        )
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
    available = requested in models
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


def create_ocr_provider(
    name: str,
    settings: OcrSettings | None = None,
    *,
    ollama_model: str | None = None,
) -> OcrProvider:
    settings = settings or OcrSettings.from_env()
    descriptor = PROVIDER_REGISTRY.get(name)
    if descriptor is None:
        raise OcrConfigurationError(f"Unknown OCR provider: {name}")
    provider = descriptor.factory(settings)
    if isinstance(provider, OllamaVisionOcrProvider) and ollama_model:
        if ollama_model not in OLLAMA_BOOK_MODELS:
            raise OcrConfigurationError(f"Unsupported Ollama OCR model: {ollama_model}")
        provider.model = ollama_model
        if ollama_model != OLLAMA_QUALITY_MODEL:
            provider.fallback_model = OLLAMA_QUALITY_MODEL
    return provider


def provider_status(settings: OcrSettings | None = None) -> list[dict]:
    settings = settings or OcrSettings.from_env()
    statuses: list[dict] = []
    for descriptor in PROVIDER_REGISTRY.values():
        configured, ready, detail = descriptor.readiness(settings)
        label = descriptor.label
        if descriptor.id == "ollama":
            label = f"Ollama — {settings.ollama_model}"
        status = {
            "id": descriptor.id,
            "label": label,
            "configured": configured,
            "ready": ready,
            "remote": descriptor.remote,
            "detail": detail,
        }
        if descriptor.id == "ollama":
            status["model"] = settings.ollama_model
            status["book_models"] = list(OLLAMA_BOOK_MODELS)
        statuses.append(status)
    return statuses
