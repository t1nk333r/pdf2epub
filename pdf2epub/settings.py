from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    key: str
    label: str
    section: str
    secret: bool = False
    default: str = ""
    placeholder: str = ""


SETTING_DEFINITIONS = (
    SettingDefinition(
        "PDF2EPUB_OLLAMA_BASE_URL",
        "عنوان خادم Ollama",
        "Ollama — محلي أو سحابي",
        default="http://127.0.0.1:11434",
        placeholder="http://127.0.0.1:11434",
    ),
    SettingDefinition(
        "PDF2EPUB_OLLAMA_MODEL",
        "اسم نموذج Ollama",
        "Ollama — محلي أو سحابي",
        default="qwen3-vl:32b-instruct-gpu",
        placeholder="qwen3-vl:32b-instruct-gpu",
    ),
    SettingDefinition(
        "PDF2EPUB_OLLAMA_API_KEY",
        "مفتاح Ollama السحابي",
        "Ollama — محلي أو سحابي",
        secret=True,
        placeholder="اختياري للخادم المحلي",
    ),
    SettingDefinition(
        "PDF2EPUB_OLLAMA_NUM_CTX",
        "حجم سياق Ollama",
        "Ollama — محلي أو سحابي",
        default="8192",
        placeholder="8192",
    ),
    SettingDefinition(
        "PDF2EPUB_OLLAMA_EXTRA_MODELS",
        "نماذج Ollama إضافية",
        "Ollama — محلي أو سحابي",
        placeholder="model-a,model-b",
    ),
    SettingDefinition(
        "PDF2EPUB_MISTRAL_BASE_URL",
        "عنوان Mistral API",
        "Mistral Document OCR",
        default="https://api.mistral.ai/v1",
        placeholder="https://api.mistral.ai/v1",
    ),
    SettingDefinition(
        "PDF2EPUB_MISTRAL_MODEL",
        "نموذج Mistral OCR",
        "Mistral Document OCR",
        default="mistral-ocr-latest",
        placeholder="mistral-ocr-latest",
    ),
    SettingDefinition(
        "PDF2EPUB_MISTRAL_API_KEY",
        "مفتاح Mistral API",
        "Mistral Document OCR",
        secret=True,
        placeholder="يُحفظ في ذاكرة الخادم فقط",
    ),
    SettingDefinition(
        "PDF2EPUB_VISION_BASE_URL",
        "عنوان Vision API المتوافق",
        "OpenAI-compatible Vision",
        placeholder="https://provider.example/v1",
    ),
    SettingDefinition(
        "PDF2EPUB_VISION_MODEL",
        "اسم نموذج Vision",
        "OpenAI-compatible Vision",
        placeholder="arabic-vision-model",
    ),
    SettingDefinition(
        "PDF2EPUB_VISION_API_KEY",
        "مفتاح Vision API",
        "OpenAI-compatible Vision",
        secret=True,
        placeholder="يُحفظ في ذاكرة الخادم فقط",
    ),
    SettingDefinition(
        "PDF2EPUB_OCR_TIMEOUT",
        "مهلة OCR بالثواني",
        "إعدادات عامة",
        default="300",
        placeholder="300",
    ),
)

DEFINITIONS_BY_KEY = {definition.key: definition for definition in SETTING_DEFINITIONS}
_runtime_values: dict[str, str] = {}
_lock = threading.RLock()


def setting_value(key: str, default: str | None = None) -> str | None:
    definition = DEFINITIONS_BY_KEY.get(key)
    fallback = definition.default if definition and default is None else default
    with _lock:
        if key in _runtime_values:
            return _runtime_values[key]
    return os.getenv(key, fallback)


def _validate_value(definition: SettingDefinition, value: str) -> str:
    value = value.strip()
    if len(value) > (4096 if definition.secret else 512):
        raise ValueError(f"{definition.key} is too long")
    if definition.key.endswith("_BASE_URL") and value:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{definition.key} must be an HTTP or HTTPS URL")
        value = value.rstrip("/")
    if definition.key == "PDF2EPUB_OCR_TIMEOUT":
        try:
            timeout = float(value)
        except ValueError as exc:
            raise ValueError("PDF2EPUB_OCR_TIMEOUT must be a number") from exc
        if not 300 <= timeout <= 900:
            raise ValueError("PDF2EPUB_OCR_TIMEOUT must be between 300 and 900")
        value = str(timeout).removesuffix(".0")
    if definition.key == "PDF2EPUB_OLLAMA_NUM_CTX":
        try:
            num_ctx = int(value)
        except ValueError as exc:
            raise ValueError("PDF2EPUB_OLLAMA_NUM_CTX must be an integer") from exc
        if not 512 <= num_ctx <= 16384:
            raise ValueError(
                "PDF2EPUB_OLLAMA_NUM_CTX must be between 512 and 16384"
            )
        value = str(num_ctx)
    return value


def update_runtime_settings(values: dict[str, str]) -> None:
    unknown = sorted(set(values).difference(DEFINITIONS_BY_KEY))
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(unknown))
    validated = {
        key: _validate_value(DEFINITIONS_BY_KEY[key], value)
        for key, value in values.items()
    }
    with _lock:
        _runtime_values.update(validated)


def clear_runtime_settings() -> None:
    with _lock:
        _runtime_values.clear()


def public_settings() -> list[dict]:
    fields: list[dict] = []
    with _lock:
        runtime_keys = set(_runtime_values)
    for definition in SETTING_DEFINITIONS:
        effective = setting_value(definition.key, definition.default) or ""
        environment_has_value = bool(os.getenv(definition.key))
        source = (
            "runtime"
            if definition.key in runtime_keys
            else ("environment" if environment_has_value else "default")
        )
        item = asdict(definition)
        item.update(
            {
                "value": "" if definition.secret else effective,
                "configured": bool(effective),
                "source": source,
            }
        )
        fields.append(item)
    return fields
