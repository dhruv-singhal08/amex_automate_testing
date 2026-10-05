"""Tracing must be invisible to the rest of the system.

These tests exercise the real LLMClient.complete() path (not StubLLMClient), because
that is where the tracing wrapper lives. They assert the wrapper preserves the
contract whether tracing is on or off, and that a broken tracer never breaks a call.
"""

from types import SimpleNamespace

import pytest

from synthetic_user.core import tracing
from synthetic_user.core.config import Settings
from synthetic_user.core.llm import LLMClient


class _FakeGenAIClient:
    """Stands in for google-genai so no network call is made."""

    def __init__(self, text: str = "hello", tokens: int = 7) -> None:
        self.models = SimpleNamespace(generate_content=self._generate)
        self._text = text
        self._tokens = tokens
        self.calls = 0

    def _generate(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            text=self._text,
            usage_metadata=SimpleNamespace(total_token_count=self._tokens),
        )


def _settings() -> Settings:
    return Settings(
        LLM_API_KEY="test-key",
        LLM_MODEL_GENERATION="model-a",
        LLM_MODEL_PERSONA="model-b",
        LLM_MODEL_VALIDATOR="model-c",
        LLM_MODEL_PRODUCT="model-d",
    )


@pytest.fixture
def tracing_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "_resolved", True)
    monkeypatch.setattr(tracing, "_client", None)


def test_complete_returns_text_and_counts_tokens_with_tracing_disabled(tracing_disabled) -> None:
    fake = _FakeGenAIClient()
    client = LLMClient(settings=_settings(), client=fake)

    assert client.complete(prompt="hi", role="persona") == "hello"
    assert client.tokens_used == 7
    assert fake.calls == 1


def test_complete_is_unchanged_when_the_tracer_raises(monkeypatch) -> None:
    """A monitoring failure must never break a run."""

    class _ExplodingClient:
        def start_as_current_observation(self, **_kwargs):
            raise RuntimeError("langfuse is down")

    monkeypatch.setattr(tracing, "_resolved", True)
    monkeypatch.setattr(tracing, "_client", _ExplodingClient())

    fake = _FakeGenAIClient()
    client = LLMClient(settings=_settings(), client=fake)

    assert client.complete(prompt="hi", role="validator") == "hello"
    assert client.tokens_used == 7


def test_provider_errors_still_propagate_through_the_tracing_wrapper(tracing_disabled) -> None:
    """Tracing wraps the call; it must not swallow real failures."""

    class _FailingClient:
        def __init__(self) -> None:
            self.models = SimpleNamespace(generate_content=self._generate)

        def _generate(self, **_kwargs):
            raise ValueError("provider exploded")

    client = LLMClient(settings=_settings(), client=_FailingClient())

    with pytest.raises(ValueError, match="provider exploded"):
        client.complete(prompt="hi", role="generation")


def test_span_helpers_are_inert_when_disabled(tracing_disabled) -> None:
    assert tracing.enabled() is False

    with tracing.session("run-1"), tracing.trace("question"), tracing.span("turn 0"):
        pass

    with tracing.llm_call(name="llm.persona", model="m", prompt="p") as call:
        call.finish(output="out", usage={"total": 1})

    tracing.flush()
