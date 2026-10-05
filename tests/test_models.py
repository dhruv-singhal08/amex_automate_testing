from datetime import datetime

import pytest
from pydantic import ValidationError

from synthetic_user.core.models import (
    AdapterResponse,
    CapabilitySpec,
    Conversation,
    GeneratedQuestion,
    Outcome,
    Persona,
    ProductProfile,
    RunRecord,
    Turn,
    TurnDecision,
    ValidationResult,
)


def persona_data() -> dict:
    return {
        "id": "persona-1",
        "version": 1,
        "name": "Careful Analyst",
        "functional_role": "Operations analyst",
        "domain_expertise": "intermediate",
        "technical_proficiency": "medium",
        "vocabulary_style": "Plain language with careful qualifiers",
        "primary_motivation": "Complete the task accurately",
        "success_criteria": "Receive a usable and verifiable answer",
        "patience": 5,
        "ambiguity_response": "re_ask",
        "forbidden_assumptions": ["Do not infer missing account details"],
        "verification_literacy": "high",
    }


def test_persona_defaults_and_constraints() -> None:
    persona = Persona.model_validate(persona_data())

    assert persona.tail_flag is False
    assert persona.provenance == "ungrounded"
    assert persona.approval_state == "draft"

    with pytest.raises(ValidationError):
        Persona.model_validate({**persona_data(), "vocabulary_style": "too short"})
    with pytest.raises(ValidationError):
        Persona.model_validate({**persona_data(), "patience": 0})
    with pytest.raises(ValidationError):
        Persona.model_validate({**persona_data(), "patience": 11})


def test_persona_rejects_empty_forbidden_assumptions_with_explanation() -> None:
    with pytest.raises(ValidationError, match="silently repairing the product's mistakes"):
        Persona.model_validate({**persona_data(), "forbidden_assumptions": []})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("domain_expertise", "advanced"),
        ("technical_proficiency", "advanced"),
        ("ambiguity_response", "ignore"),
        ("verification_literacy", "advanced"),
        ("approval_state", "rejected"),
    ],
)
def test_persona_literal_constraints(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Persona.model_validate({**persona_data(), field: value})


def test_product_and_capability_models() -> None:
    profile = ProductProfile(
        product_name="Example Product",
        version="1.2",
        description="A product description",
        what_it_can_do=["Answer questions"],
        what_it_cannot_do=["Access private records"],
    )
    spec = CapabilitySpec(
        product_version=2,
        spec_version=1,
        supported=["Answer questions"],
        not_supported_yet=["Export reports"],
        known_gaps=["Ambiguous dates"],
    )

    assert profile.product_name == "Example Product"
    assert spec.spec_version == 1


def test_generated_question_constraints() -> None:
    question = GeneratedQuestion(
        id="question-1",
        question="What is my current balance?",
        intent="retrieve balance",
        complexity="simple",
        persona_trait="verification literacy",
        expected_response_type="data",
    )
    assert question.complexity == "simple"

    with pytest.raises(ValidationError):
        GeneratedQuestion(
            id="question-1",
            question="Question",
            intent="intent",
            complexity="extreme",
            persona_trait="trait",
            expected_response_type="data",
        )
    with pytest.raises(ValidationError):
        GeneratedQuestion(
            id="question-1",
            question="Question",
            intent="intent",
            complexity="simple",
            persona_trait="trait",
            expected_response_type="summary",
        )


def test_adapter_response_defaults_are_isolated() -> None:
    first = AdapterResponse(reply="OK", latency_ms=12)
    second = AdapterResponse(reply="Still OK", latency_ms=13)

    first.artifacts["id"] = "artifact-1"
    assert second.artifacts == {}
    assert first.raw == {}
    assert first.error is None


def test_turn_conversation_validation_and_outcomes() -> None:
    response = AdapterResponse(reply="Your balance is $20", latency_ms=10)
    decision = TurnDecision(
        satisfied=True,
        giving_up=False,
        next_question=None,
        doubt_expressed=False,
        reasoning="The answer is specific enough to proceed.",
    )
    turn = Turn(turn_index=0, message="What is my balance?", response=response, decision=decision)
    conversation = Conversation(
        id="conversation-1",
        question_id="question-1",
        repeat_index=0,
        seed=42,
        persona_id="persona-1",
        persona_version=1,
        turns=[turn],
        outcome=Outcome.COMPLETED,
        doubt_anywhere=False,
        closing_summary=None,
    )

    assert conversation.turns[0].decision is not None
    assert set(Outcome) == {
        Outcome.COMPLETED,
        Outcome.ABANDONED,
        Outcome.MAX_TURNS,
        Outcome.LOOPED,
        Outcome.ADAPTER_ERROR,
        Outcome.BUDGET,
    }


def test_validation_result_and_run_record() -> None:
    result = ValidationResult(
        conversation_id="conversation-1",
        classification=None,
        refused_clearly=None,
        cleanup_suspected=False,
        deterministic_flags=[],
        reason="No deterministic issue found.",
    )
    record = RunRecord(
        id="run-1",
        started_at=datetime(2026, 9, 14, 12, 0),
        finished_at=datetime(2026, 9, 14, 12, 1),
        persona_versions={"persona-1": 1},
        prompt_versions={"generation": 1},
        model_versions={"persona": "gemini-2.0-flash"},
        product_version="1.0",
        spec_version=None,
        seed_base=42,
        conversation_ids=["conversation-1"],
    )

    assert result.cleanup_suspected is False
    assert record.spec_version is None


def test_character_sheet_excludes_harness_bookkeeping() -> None:
    """Prompts receive character only; identity and approval state are harness concerns."""
    from synthetic_user.assets.registry import load_named_persona

    persona = load_named_persona("junior_analyst")
    sheet = persona.character_sheet()

    for bookkeeping in ("id", "version", "tail_flag", "provenance", "approval_state"):
        assert bookkeeping not in sheet

    assert sheet["vocabulary_style"] == persona.vocabulary_style
    assert sheet["example_utterances"] == persona.example_utterances
    assert sheet["typical_tasks"] == persona.typical_tasks


def test_character_sheet_omits_empty_optional_fields() -> None:
    """A persona that defines no examples should not send empty keys to the model."""
    from synthetic_user.assets.registry import load_named_persona

    sheet = load_named_persona("idle_junior_analyst").character_sheet()

    assert "example_utterances" not in sheet
    assert "constraints" not in sheet
    assert sheet["functional_role"]
