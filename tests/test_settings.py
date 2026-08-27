import pytest

from pdf2epub.ocr import OcrSettings
from pdf2epub.settings import (
    clear_runtime_settings,
    public_settings,
    update_runtime_settings,
)


@pytest.fixture(autouse=True)
def clean_runtime_settings():
    clear_runtime_settings()
    yield
    clear_runtime_settings()


def _field(key: str) -> dict:
    return next(field for field in public_settings() if field["key"] == key)


def test_runtime_value_overrides_environment(monkeypatch):
    monkeypatch.setenv("PDF2EPUB_OLLAMA_MODEL", "from-environment")
    update_runtime_settings({"PDF2EPUB_OLLAMA_MODEL": "from-web-page"})

    assert OcrSettings.from_env().ollama_model == "from-web-page"
    assert _field("PDF2EPUB_OLLAMA_MODEL")["source"] == "runtime"


def test_reset_returns_to_environment_value(monkeypatch):
    monkeypatch.setenv("PDF2EPUB_OLLAMA_MODEL", "from-environment")
    update_runtime_settings({"PDF2EPUB_OLLAMA_MODEL": "from-web-page"})
    clear_runtime_settings()

    assert OcrSettings.from_env().ollama_model == "from-environment"
    assert _field("PDF2EPUB_OLLAMA_MODEL")["source"] == "environment"


def test_secrets_are_never_returned(monkeypatch):
    monkeypatch.setenv("PDF2EPUB_MISTRAL_API_KEY", "environment-secret")
    field = _field("PDF2EPUB_MISTRAL_API_KEY")
    assert field["configured"] is True
    assert field["value"] == ""

    update_runtime_settings({"PDF2EPUB_MISTRAL_API_KEY": "runtime-secret"})
    field = _field("PDF2EPUB_MISTRAL_API_KEY")
    assert field["configured"] is True
    assert field["source"] == "runtime"
    assert field["value"] == ""


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PDF2EPUB_OLLAMA_BASE_URL", "file:///tmp/socket"),
        ("PDF2EPUB_OCR_TIMEOUT", "0"),
        ("PDF2EPUB_OCR_TIMEOUT", "901"),
        ("PDF2EPUB_OLLAMA_NUM_CTX", "511"),
        ("PDF2EPUB_OLLAMA_NUM_CTX", "not-a-number"),
    ],
)
def test_invalid_settings_are_rejected(key, value):
    with pytest.raises(ValueError):
        update_runtime_settings({key: value})
