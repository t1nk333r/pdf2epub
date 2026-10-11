import base64
import io
import json
import urllib.error

import pytest
from PIL import Image

from pdf2epub.ocr import (
    OCR_PROMPT,
    OLLAMA_BOOK_MODELS,
    OLLAMA_FAST_MODEL,
    OLLAMA_QUALITY_MODEL,
    MistralDocumentOcrProvider,
    OcrConfigurationError,
    OcrError,
    OcrSettings,
    OllamaVisionOcrProvider,
    OpenAiCompatibleVisionOcrProvider,
    create_ocr_provider,
    ollama_allowed_models,
    provider_names,
    provider_status,
)


def image_bytes(size=(2000, 1000)):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format="PNG")
    return output.getvalue()


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]


def test_ollama_sends_native_vision_request(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "message": {"content": "<think>hidden</think>النص العربي"},
                "prompt_eval_count": 1350,
                "eval_count": 12,
                "total_duration": 2_500_000_000,
            }
        )

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OllamaVisionOcrProvider("http://localhost:11434", OLLAMA_QUALITY_MODEL)
    result = provider.recognize(image_bytes(), "ara+eng")
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["body"]["stream"] is False
    assert captured["body"]["think"] is False
    assert captured["body"]["options"] == {
        "temperature": 0,
        "num_ctx": 8192,
        "num_predict": 3000,
        "repeat_penalty": 1.05,
    }
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": OCR_PROMPT,
    }
    user = captured["body"]["messages"][1]
    assert user["role"] == "user"
    assert user["content"] == "استخرج نص هذه الصفحة."
    assert len(user["images"]) == 1
    with Image.open(io.BytesIO(base64.b64decode(user["images"][0]))) as sent:
        assert sent.format == "JPEG"
        assert sent.size == (1280, 640)
    assert captured["timeout"] >= 300
    assert result.text == "النص العربي"
    assert result.provider == "ollama"
    assert result.prompt_tokens == 1350
    assert result.output_tokens == 12
    assert result.duration_seconds == 2.5


def test_ollama_prompt_override_is_sent(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data)
        return FakeResponse({"message": {"content": "نص"}})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OllamaVisionOcrProvider(
        "http://localhost:11434", OLLAMA_QUALITY_MODEL, prompt="PROBE PROMPT"
    )
    provider.recognize(image_bytes((100, 100)), "ara")
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": "PROBE PROMPT",
    }


def test_ollama_retries_5xx_then_succeeds(monkeypatch):
    calls = 0
    sleeps = []

    def fake_open(request, timeout):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
        return FakeResponse({"message": {"content": "نص"}})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    monkeypatch.setattr("pdf2epub.ocr.time.sleep", sleeps.append)
    result = OllamaVisionOcrProvider(
        "http://localhost:11434", OLLAMA_QUALITY_MODEL
    ).recognize(image_bytes((100, 100)), "ara")
    assert calls == 3
    assert sleeps == [10, 10]
    assert result.retries == 2


def test_non_quality_model_falls_back_to_32b(monkeypatch):
    models = []

    def fake_open(request, timeout):
        body = json.loads(request.data)
        models.append(body["model"])
        content = "Latin output" if len(models) == 1 else "النص العربي الصحيح"
        return FakeResponse(
            {
                "message": {"content": content},
                "prompt_eval_count": 100,
                "eval_count": 10,
                "total_duration": 1_000_000_000,
            }
        )

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OllamaVisionOcrProvider(
        "http://localhost:11434",
        OLLAMA_FAST_MODEL,
        fallback_model=OLLAMA_QUALITY_MODEL,
    )
    result = provider.recognize(image_bytes((100, 100)), "ara")
    assert models == [OLLAMA_FAST_MODEL, OLLAMA_QUALITY_MODEL]
    assert result.model == OLLAMA_QUALITY_MODEL
    assert result.fallback_reason == "output contains Latin letters"
    assert len(result.attempts) == 2


@pytest.mark.parametrize(
    ("text", "metrics", "expected"),
    [
        (
            "قصير",
            {"eval_count": 2},
            "output shorter than 15% of the median page length",
        ),
        (
            "[غير مقروء] " * 4,
            {"eval_count": 20},
            "output contains more than three unreadable markers",
        ),
        (
            "نص عربي سليم " * 10,
            {"eval_count": 3000},
            "output exhausted the 3000-token generation budget",
        ),
    ],
)
def test_fallback_quality_rules(text, metrics, expected):
    provider = OllamaVisionOcrProvider(
        "http://localhost:11434", OLLAMA_FAST_MODEL, page_lengths=[100]
    )
    assert provider._fallback_reason(text, metrics) == expected


def test_openai_compatible_sends_data_url(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.headers["Authorization"]
        return FakeResponse({"choices": [{"message": {"content": "```text\nنص\n```"}}]})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OpenAiCompatibleVisionOcrProvider(
        "https://vision.example/v1", "vision-model", "secret"
    )
    result = provider.recognize(image_bytes((100, 100)), "ara")
    assert captured["body"]["messages"][0]["role"] == "system"
    image_url = captured["body"]["messages"][1]["content"][0]["image_url"]["url"]
    assert image_url.startswith("data:image/jpeg;base64,")
    assert captured["body"]["max_tokens"] == 8192
    assert "chat_template_kwargs" not in captured["body"]
    assert "temperature" not in captured["body"]
    assert captured["auth"] == "Bearer secret"
    assert result.text == "نص"


def test_openai_compatible_reports_real_inference_usage(monkeypatch):
    def fake_open(request, timeout):
        return FakeResponse(
            {
                "model": "claude-sonnet-5-5",
                "choices": [
                    {"message": {"content": "نص الصفحة"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1748, "completion_tokens": 459},
            }
        )

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OpenAiCompatibleVisionOcrProvider(
        "https://proxy.example/v1", "api-supplementary/claude-sonnet-5-5", "secret"
    )
    result = provider.recognize(image_bytes((100, 100)), "ara")
    assert result.model == "api-supplementary/claude-sonnet-5-5"
    assert result.prompt_tokens == 1748
    assert result.output_tokens == 459
    assert result.duration_seconds >= 0
    assert result.attempts[0]["eval_count"] == 459


def test_openai_compatible_refuses_a_truncated_completion(monkeypatch):
    monkeypatch.setattr(
        "pdf2epub.ocr.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(
            {
                "choices": [
                    {"message": {"content": "نص ناقص"}, "finish_reason": "length"}
                ]
            }
        ),
    )
    provider = OpenAiCompatibleVisionOcrProvider(
        "https://proxy.example/v1", "api-supplementary/claude-sonnet-5-5", "secret"
    )
    with pytest.raises(OcrError, match="truncated"):
        provider.recognize(image_bytes((100, 100)), "ara")


def test_openai_prompt_override_is_sent(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data)
        return FakeResponse({"choices": [{"message": {"content": "نص"}}]})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OpenAiCompatibleVisionOcrProvider(
        "https://vision.example/v1", "vision-model", "secret", prompt="PROBE PROMPT"
    )
    provider.recognize(image_bytes((100, 100)), "ara")
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": "PROBE PROMPT",
    }


def test_authentication_error_explains_key_and_proxy_checks_without_leaking_secrets(
    monkeypatch,
):
    def unauthorized(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "rejected secret",
            {},
            io.BytesIO(b'{"error":"rejected secret"}'),
        )

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", unauthorized)
    provider = OpenAiCompatibleVisionOcrProvider(
        "https://proxy.example/v1", "vision-model", "secret"
    )
    with pytest.raises(OcrError, match="HTTP 401") as caught:
        provider.recognize(image_bytes((100, 100)), "ara")
    message = str(caught.value)
    assert "API key" in message
    assert "upstream credentials" in message
    assert "secret" not in message


def test_hosted_provider_requires_server_configuration():
    settings = OcrSettings(vision_base_url=None, vision_model=None, vision_api_key=None)
    with pytest.raises(OcrConfigurationError):
        create_ocr_provider("openai_compatible", settings)


def test_mistral_uses_dedicated_ocr_endpoint(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["auth"] = request.headers["Authorization"]
        return FakeResponse(
            {
                "pages": [
                    {
                        "markdown": "النص العربي",
                        "confidence_scores": {"page": 0.98},
                    }
                ]
            }
        )

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = MistralDocumentOcrProvider(
        "https://api.mistral.ai/v1", "mistral-ocr-latest", "secret"
    )
    result = provider.recognize(b"png-data", "ara+eng")

    assert captured["url"] == "https://api.mistral.ai/v1/ocr"
    assert captured["body"]["document"]["type"] == "image_url"
    assert captured["body"]["include_blocks"] is True
    assert captured["auth"] == "Bearer secret"
    assert result.text == "النص العربي"
    assert result.confidence == 0.98


def test_provider_registry_drives_names_and_remote_status():
    settings = OcrSettings(mistral_api_key="secret")
    assert "mistral" in provider_names()
    statuses = {item["id"]: item for item in provider_status(settings)}
    assert statuses["mistral"]["ready"] is True
    assert statuses["mistral"]["remote"] is True


# --- Ollama allowlist extension (PDF2EPUB_OLLAMA_EXTRA_MODELS) ---


def test_ollama_allowed_models_unset_is_exactly_the_builtin_three():
    assert ollama_allowed_models(OcrSettings()) == OLLAMA_BOOK_MODELS


def test_ollama_allowed_models_appends_extras_order_preserving_and_deduped():
    settings = OcrSettings(
        ollama_extra_models=("qari-ocr:v0.3", OLLAMA_QUALITY_MODEL, "qari-ocr:v0.3")
    )
    assert ollama_allowed_models(settings) == (*OLLAMA_BOOK_MODELS, "qari-ocr:v0.3")


def test_ollama_extra_model_accepted_by_factory_via_settings():
    settings = OcrSettings(
        ollama_base_url="http://localhost:11434",
        ollama_model="qari-ocr:v0.3",
        ollama_extra_models=("qari-ocr:v0.3",),
    )
    provider = create_ocr_provider("ollama", settings)
    assert provider.model == "qari-ocr:v0.3"


def test_ollama_extra_model_accepted_via_kwarg_override():
    settings = OcrSettings(
        ollama_base_url="http://localhost:11434",
        ollama_extra_models=("qari-ocr:v0.3",),
    )
    provider = create_ocr_provider("ollama", settings, ollama_model="qari-ocr:v0.3")
    assert provider.model == "qari-ocr:v0.3"


def test_ollama_model_outside_allowlist_and_extras_is_rejected():
    settings = OcrSettings(
        ollama_base_url="http://localhost:11434",
        ollama_model="some-thinking-model",
    )
    with pytest.raises(OcrConfigurationError):
        create_ocr_provider("ollama", settings)


def test_provider_status_unchanged_with_no_extra_config(monkeypatch):
    """Pins provider_status() with no new env vars set — the default path."""
    monkeypatch.setattr("pdf2epub.ocr.shutil.which", lambda name: None)
    settings = OcrSettings(
        ollama_base_url="",
        ollama_model=OLLAMA_QUALITY_MODEL,
        vision_base_url=None,
        vision_model=None,
        vision_api_key=None,
        mistral_base_url="https://api.mistral.ai/v1",
        mistral_model="mistral-ocr-latest",
        mistral_api_key=None,
    )
    assert provider_status(settings) == [
        {
            "id": "tesseract",
            "label": "Tesseract (local)",
            "configured": False,
            "ready": False,
            "remote": False,
            "detail": "Tesseract executable was not found",
        },
        {
            "id": "ollama",
            "label": f"Ollama — {OLLAMA_QUALITY_MODEL}",
            "configured": False,
            "ready": False,
            "remote": False,
            "detail": "Set the Ollama base URL and model, or install Ollama",
            "model": OLLAMA_QUALITY_MODEL,
            "book_models": list(OLLAMA_BOOK_MODELS),
        },
        {
            "id": "openai_compatible",
            "label": "Online vision model",
            "configured": False,
            "ready": False,
            "remote": True,
            "detail": (
                "Online vision endpoint and model are required "
                "(API key only if the endpoint needs one)"
            ),
        },
        {
            "id": "mistral",
            "label": "Mistral Document OCR",
            "configured": False,
            "ready": False,
            "remote": True,
            "detail": "Set PDF2EPUB_MISTRAL_API_KEY to enable Mistral OCR",
        },
    ]


# --- OpenAI-compatible provider: optional API key ---


def test_openai_compatible_ready_without_api_key():
    settings = OcrSettings(
        vision_base_url="https://vision.example/v1",
        vision_model="vision-model",
        vision_api_key=None,
    )
    statuses = {item["id"]: item for item in provider_status(settings)}
    assert statuses["openai_compatible"]["configured"] is True
    assert statuses["openai_compatible"]["ready"] is True
    assert statuses["openai_compatible"]["remote"] is True


def test_openai_factory_accepts_keyless_configuration():
    settings = OcrSettings(
        vision_base_url="https://vision.example/v1",
        vision_model="vision-model",
        vision_api_key=None,
    )
    provider = create_ocr_provider("openai_compatible", settings)
    assert provider.api_key is None


@pytest.mark.parametrize(
    ("api_key", "expect_header"),
    [(None, False), ("secret", True)],
)
def test_openai_compatible_authorization_header_matches_key_presence(
    monkeypatch, api_key, expect_header
):
    captured = {}

    def fake_open(request, timeout):
        captured["has_auth"] = "Authorization" in request.headers
        captured["auth"] = request.headers.get("Authorization")
        return FakeResponse({"choices": [{"message": {"content": "نص"}}]})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OpenAiCompatibleVisionOcrProvider(
        "https://vision.example/v1", "vision-model", api_key
    )
    provider.recognize(image_bytes((100, 100)), "ara")
    assert captured["has_auth"] is expect_header
    if expect_header:
        assert captured["auth"] == "Bearer secret"
