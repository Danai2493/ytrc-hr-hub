"""Pluggable AI providers. Every provider does one thing: read an image and
return JSON that matches a schema. Pick one with AI_PROVIDER:

  none       AI switched off (HR types the form in by hand)
  mock       built-in demo answers, no data leaves the server
  anthropic  Claude (Anthropic API)
  openai     OpenAI, or any OpenAI-compatible server via AI_BASE_URL
             (for example a model hosted inside the company network)
  gemini     Google Gemini (fits a Google Workspace company)

PDPA: a photo of a form contains personal data. Before switching on a cloud
provider, the company needs a data processing agreement with that provider,
or must use a model hosted on its own network."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx

from ...config import settings


class AIError(Exception):
    pass


@dataclass
class AIResult:
    data: dict
    provider: str
    model: str
    raw: Optional[dict] = None


class Provider(Protocol):
    name: str

    def extract(self, image: bytes, mime: str, schema: dict, instructions: str, hint: Optional[dict] = None) -> AIResult: ...


class NoProvider:
    name = "none"

    def extract(self, image, mime, schema, instructions, hint=None) -> AIResult:
        raise AIError("AI is switched off. Type the form in by hand.")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        try:
            import anthropic
        except ImportError as e:  # optional dependency
            raise AIError("Install the 'anthropic' package to use AI_PROVIDER=anthropic") from e
        kwargs: dict[str, Any] = {}
        if settings.ai_api_key:
            kwargs["api_key"] = settings.ai_api_key
        if settings.ai_base_url:
            kwargs["base_url"] = settings.ai_base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.model = settings.ai_model or "claude-opus-5-5"

    def extract(self, image, mime, schema, instructions, hint=None) -> AIResult:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(image).decode()},
                        },
                        {"type": "text", "text": instructions},
                    ],
                }
            ],
        )
        if resp.stop_reason == "refusal":
            raise AIError("The AI provider declined to read this image")
        if resp.stop_reason == "max_tokens":
            raise AIError("The AI answer was cut off; try a smaller photo")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise AIError("The AI provider returned no answer")
        return AIResult(json.loads(text), self.name, resp.model, {"usage": resp.usage.model_dump() if resp.usage else None})


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(self) -> None:
        self.base = (settings.ai_base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = settings.ai_model or "gpt-4.1"
        self.key = settings.ai_api_key

    def extract(self, image, mime, schema, instructions, hint=None) -> AIResult:
        uri = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instructions},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                }
            ],
            "response_format": {"type": "json_schema", "json_schema": {"name": "form", "schema": schema, "strict": True}},
        }
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        r = httpx.post(f"{self.base}/chat/completions", json=body, headers=headers, timeout=180)
        if r.status_code >= 400:
            raise AIError(f"AI provider error {r.status_code}: {r.text[:300]}")
        msg = r.json()["choices"][0]["message"]
        if msg.get("refusal"):
            raise AIError("The AI provider declined to read this image")
        return AIResult(json.loads(msg["content"]), self.name, self.model)


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        self.model = settings.ai_model or "gemini-2.5-pro"
        self.key = settings.ai_api_key
        self.base = (settings.ai_base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    def extract(self, image, mime, schema, instructions, hint=None) -> AIResult:
        body = {
            "contents": [
                {
                    "parts": [
                        {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}},
                        {"text": instructions + "\n\nReturn JSON matching this schema:\n" + json.dumps(schema)},
                    ]
                }
            ],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        r = httpx.post(
            f"{self.base}/models/{self.model}:generateContent",
            params={"key": self.key},
            json=body,
            timeout=180,
        )
        if r.status_code >= 400:
            raise AIError(f"AI provider error {r.status_code}: {r.text[:300]}")
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise AIError("The AI provider returned no answer") from e
        return AIResult(json.loads(text), self.name, self.model)


class MockProvider:
    """Deterministic demo answers. Used in the demo and in tests."""

    name = "mock"
    model = "mock-1"

    def extract(self, image, mime, schema, instructions, hint=None) -> AIResult:
        hint = hint or {}
        if "fixture" in hint:
            return AIResult(hint["fixture"], self.name, self.model)
        from .demo_fixtures import demo_answer

        return AIResult(demo_answer(hint.get("form_type", "OT_FORM"), hint), self.name, self.model)


def get_provider(name: Optional[str] = None) -> Provider:
    name = (name or settings.ai_provider or "none").lower()
    if name == "anthropic":
        return AnthropicProvider()
    if name in ("openai", "openai-compatible", "local"):
        return OpenAICompatibleProvider()
    if name == "gemini":
        return GeminiProvider()
    if name == "mock":
        return MockProvider()
    return NoProvider()
