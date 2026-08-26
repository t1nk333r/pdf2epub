import base64
import json

import pytest

from pdf2epub.ocr import (
    OcrConfigurationError,
    OcrSettings,
    OllamaVisionOcrProvider,
    OpenAiCompatibleVisionOcrProvider,
    create_ocr_provider,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


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
