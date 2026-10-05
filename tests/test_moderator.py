from types import SimpleNamespace

import synthetic_user.simulation.moderator as moderator
from synthetic_user.core.models import AdapterResponse, Outcome, Persona, TurnDecision


def decision(**overrides) -> TurnDecision:
    values = {
        "satisfied": False,
        "giving_up": False,
        "next_question": None,
        "doubt_expressed": False,
        "reasoning": "The response needs another check.",
    }
    values.update(overrides)
    return TurnDecision(**values)


def state(**overrides) -> moderator.ConversationState:
    values = {
        "turn": 1,
        "max_turns": 8,
        "questions_asked": ["What is my balance?"],
        "consecutive_errors": 0,
        "tokens_used": 100,
        "token_budget": 500,
    }
    values.update(overrides)
    return moderator.ConversationState(**values)


def test_check_evaluates_completion_before_other_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        moderator,
        "get_settings",
        lambda: SimpleNamespace(similarity_threshold=0.92),
    )

    check = moderator.check(
        state(turn=8, consecutive_errors=3, tokens_used=501),
        decision(satisfied=True, next_question="What is my balance?"),
    )
    assert check is Outcome.COMPLETED


def test_check_returns_each_remaining_branch(monkeypatch) -> None:
    monkeypatch.setattr(
        moderator,
        "get_settings",
        lambda: SimpleNamespace(similarity_threshold=0.92),
    )

    assert moderator.check(state(), decision(giving_up=True)) is Outcome.ABANDONED
    assert moderator.check(state(turn=8), decision()) is Outcome.MAX_TURNS
    assert (
        moderator.check(state(), decision(next_question="  WHAT   is my   BALANCE?  "))
        is Outcome.LOOPED
    )
    assert moderator.check(state(consecutive_errors=3), decision(next_question="New question")) is Outcome.ADAPTER_ERROR
    assert moderator.check(state(tokens_used=501), decision(next_question="New question")) is Outcome.BUDGET
    assert moderator.check(state(), decision(next_question="A genuinely different question")) is None


def test_record_turn_tracks_questions_and_consecutive_errors() -> None:
    questions = []
    conversation_state = state(questions_asked=questions)

    conversation_state.record_turn("First question", AdapterResponse(reply="error", error="timeout", latency_ms=1))
    conversation_state.record_turn("Second question", AdapterResponse(reply="ok", latency_ms=1))

    assert conversation_state.turn == 3
    assert questions == ["First question", "Second question"]
    assert conversation_state.consecutive_errors == 0


def test_max_turns_for_patience_one_is_two() -> None:
    persona = Persona(
        id="persona-1",
        version=1,
        name="Careful Analyst",
        functional_role="Operations analyst",
        domain_expertise="intermediate",
        technical_proficiency="medium",
        vocabulary_style="Plain language with careful qualifiers",
        primary_motivation="Complete the task accurately",
        success_criteria="Receive a usable and verifiable answer",
        patience=1,
        ambiguity_response="re_ask",
        forbidden_assumptions=["Do not infer missing account details"],
        verification_literacy="high",
    )
    settings = SimpleNamespace(hard_max_turns=8)

    assert moderator.max_turns_for(persona, settings) == 2
    assert moderator.check(state(turn=2, max_turns=2), decision()) is Outcome.MAX_TURNS


def test_hard_max_turns_can_reduce_persona_limit() -> None:
    persona = Persona(
        id="persona-1",
        version=1,
        name="Careful Analyst",
        functional_role="Operations analyst",
        domain_expertise="intermediate",
        technical_proficiency="medium",
        vocabulary_style="Plain language with careful qualifiers",
        primary_motivation="Complete the task accurately",
        success_criteria="Receive a usable and verifiable answer",
        patience=10,
        ambiguity_response="re_ask",
        forbidden_assumptions=["Do not infer missing account details"],
        verification_literacy="high",
    )

    assert moderator.max_turns_for(persona, SimpleNamespace(hard_max_turns=4)) == 4
