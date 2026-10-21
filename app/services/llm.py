"""Thin Groq client (OpenAI-compatible chat completions). Kept separate so the AI layer stays optional (FR-020).
The API key is read from settings and is never logged or returned."""
import httpx

from app.config import Settings


class LLMError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.status = code, status


class GroqClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self.client = client

    @property
    def model(self) -> str:
        return self.s.groq_model

    def complete(self, messages: list[dict], max_tokens: int = 350, temperature: float = 0.1) -> str:
        if not self.s.ai_enabled:
            raise LLMError("AI_DISABLED", "AI insights are disabled.")
        if not self.s.groq_api_key:
            raise LLMError("AI_NOT_CONFIGURED", "AI insights are not configured (no GROQ_API_KEY).")
        own = self.client or httpx.Client(timeout=self.s.groq_timeout_seconds)
        try:
            r = own.post(
                f"{self.s.groq_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.s.groq_api_key}", "Content-Type": "application/json"},
                json={"model": self.s.groq_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
            )
        except httpx.TimeoutException as e:
            raise LLMError("AI_TIMEOUT", "The AI service took too long to respond.", 504) from e
        except httpx.HTTPError as e:
            raise LLMError("AI_UNREACHABLE", "The AI service could not be reached.") from e
        finally:
            if self.client is None:
                own.close()
        if r.status_code == 429:
            raise LLMError("AI_RATE_LIMITED", "The AI service is busy. Please try again shortly.", 429)
        if r.status_code >= 400:
            raise LLMError("AI_UPSTREAM_ERROR", f"The AI service returned an error (HTTP {r.status_code}).")
        try:
            text = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise LLMError("AI_BAD_RESPONSE", "The AI service returned an unreadable response.") from e
        if not isinstance(text, str) or not text.strip():
            raise LLMError("AI_BAD_RESPONSE", "The AI service returned an empty response.")
        return text.strip()


def get_llm(settings: Settings | None = None) -> GroqClient:
    from app.config import get_settings
    return GroqClient(settings or get_settings())
