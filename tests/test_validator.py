import json
from types import SimpleNamespace

from synthetic_user.core.llm import StubLLMClient
from synthetic_user.core.models import (
    AdapterResponse,
    CapabilitySpec,
    Conversation,
    Outcome,
    ProductProfile,
    Turn,
)
from synthetic_user.validation.validator import validate


class RecordingStubLLMClient(StubLLMClient):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str, role: str, seed: int | None = None, **kwargs) -> str:
        self.prompts.append(prompt)
        return super().complete(prompt, role, seed, **kwargs)


def product() -> ProductProfile:
    return ProductProfile(
        product_name="Insight Billing Assistant",
        version="1.0",
        description="Answers billing questions.",
        what_it_can_do=["Answer billing questions"],
        what_it_cannot_do=["Change billing records"],
    )


def spec() -> CapabilitySpec:
    return CapabilitySpec(
        product_version=1,
        spec_version=1,
        supported=["single-table aggregation"],
        not_supported_yet=["currency conversion"],
        known_gaps=["follow-up filters"],
    )


def conversation(turns: list[Turn]) -> Conversation:
    return Conversation(
        id="conversation-1",
        question_id="question-1",
        repeat_index=0,
        seed=42,
        persona_id="persona-1",
        persona_version=1,
        turns=turns,
        outcome=Outcome.COMPLETED,
        doubt_anywhere=False,
        closing_summary=None,
    )


def turn(index: int, message: str, reply: str, error: str | None = None) -> Turn:
    return Turn(
        turn_index=index,
        message=message,
        response=AdapterResponse(reply=reply, error=error, latency_ms=1),
        decision=None,
    )


def settings() -> SimpleNamespace:
    return SimpleNamespace(similarity_threshold=0.92)


def test_missing_capability_spec_skips_llm_and_flags_result() -> None:
    llm = StubLLMClient([])
    result = validate(
        conversation([turn(0, "How much did we bill?", "Twenty dollars.")]),
        "find total billing",
        product(),
        None,
        llm,
        settings(),
    )

    assert result.classification is None
    assert "capability_spec_missing" in result.deterministic_flags
    assert result.refused_clearly is None


def test_deterministic_flags_cover_simulation_failures() -> None:
    result = validate(
        conversation(
            [
                turn(0, "", "", error="timeout"),
                turn(1, "How much did we bill?", "no", error="timeout"),
                turn(2, "What's the weather today?", "ok"),
            ]
        ),
        "find total billing",
        product(),
        None,
        StubLLMClient([]),
        settings(),
    )

    assert "adapter_error_majority" in result.deterministic_flags
    assert "empty_or_too_short_replies" in result.deterministic_flags
    assert "final_question_off_intent" in result.deterministic_flags

    repeated = validate(
        conversation(
            [
                turn(0, "How much did we bill?", "Here is an answer."),
                turn(1, "  HOW   MUCH did we bill? ", "Here is another answer."),
            ]
        ),
        "find total billing",
        product(),
        None,
        StubLLMClient([]),
        settings(),
    )
    assert "repeated_question" in repeated.deterministic_flags

    zero = validate(conversation([]), "find total billing", product(), None, StubLLMClient([]), settings())
    assert "zero_substantive_turns" in zero.deterministic_flags


def test_capability_classification_makes_exactly_one_llm_call() -> None:
    llm = RecordingStubLLMClient(
        [
            json.dumps(
                {
                    "classification": "not_supported_yet",
                    "refused_clearly": True,
                    "cleanup_suspected": False,
                    "reason": "Currency conversion is outside the current scope.",
                }
            )
        ]
    )
    result = validate(
        conversation([turn(0, "Convert this total to EUR.", "I cannot do that.")]),
        "convert billing total to EUR",
        product(),
        spec(),
        llm,
        settings(),
    )

    assert result.classification == "not_supported_yet"
    assert result.refused_clearly is True
    assert len(llm.prompts) == 1
    assert "currency conversion" in llm.prompts[0]
