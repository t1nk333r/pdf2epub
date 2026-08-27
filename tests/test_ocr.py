import base64
import json

import pytest

from pdf2epub.ocr import (
    MistralDocumentOcrProvider,
    OcrConfigurationError,
    OcrSettings,
    OllamaVisionOcrProvider,
    OpenAiCompatibleVisionOcrProvider,
    create_ocr_provider,
    provider_names,
    provider_status,
)


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
        return FakeResponse({"message": {"content": "النص العربي"}})

    monkeypatch.setattr("pdf2epub.ocr.urllib.request.urlopen", fake_open)
    provider = OllamaVisionOcrProvider("http://localhost:11434", "gemma3")
    result = provider.recognize(b"png-data", "ara+eng")
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["body"]["stream"] is False
    assert captured["body"]["think"] is False
    assert captured["body"]["options"]["num_ctx"] == 4096
    assert captured["body"]["messages"][0]["images"] == [
        base64.b64encode(b"png-data").decode()
    ]
    assert result.text == "النص العربي"
    assert result.provider == "ollama"


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
    result = provider.recognize(b"image", "ara")
    image_url = captured["body"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert captured["auth"] == "Bearer secret"
    assert result.text == "نص"


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
