from types import SimpleNamespace

from synthetic_user.core.llm import StubLLMClient
from synthetic_user.core.models import (
    CapabilitySpec,
    GeneratedQuestion,
    Persona,
    ProductProfile,
    TurnDecision,
)
from synthetic_user.simulation.persona_agent import decide


class RecordingStubLLMClient(StubLLMClient):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str, role: str, seed: int | None = None, **kwargs) -> str:
        self.prompts.append(prompt)
        return super().complete(prompt, role, seed, **kwargs)


def persona() -> Persona:
    return Persona(
        id="persona-1",
        version=1,
        name="Careful Analyst",
        functional_role="Operations analyst",
        domain_expertise="intermediate",
        technical_proficiency="medium",
        vocabulary_style="Plain language with careful qualifiers",
        primary_motivation="Complete the task accurately",
        success_criteria="Receive a usable and verifiable answer",
        patience=3,
        ambiguity_response="re_ask",
        forbidden_assumptions=["Do not infer missing account details"],
        verification_literacy="high",
    )


def product_profile() -> ProductProfile:
    return ProductProfile(
        product_name="Balance Tool",
        version="1.0",
        description="A tool that answers balance questions.",
        what_it_can_do=["Show balances"],
        what_it_cannot_do=["Change account settings"],
    )


def test_decide_does_not_expose_internal_capability_spec() -> None:
    """Unit-level half of the leak guard. The run-level half, which also covers
    schema, few-shots and reference internals, is below."""
    internal_spec = CapabilitySpec(
        product_version=1,
        spec_version=7,
        supported=["INTERNAL_SUPPORTED_SENTINEL"],
        not_supported_yet=["INTERNAL_NOT_SUPPORTED_SENTINEL"],
        known_gaps=["INTERNAL_GAP_SENTINEL"],
    )
    llm = RecordingStubLLMClient(
        ['{"satisfied": true, "giving_up": false, "next_question": null, "doubt_expressed": false, "reasoning": "I got the answer."}']
    )
    state = SimpleNamespace(
        turn=1,
        max_turns=4,
        questions_asked=["What is my balance?"],
        transcript=[
            {"message": "What is my balance?", "response": {"reply": "Your balance is $20."}}
        ],
    )

    result = decide(persona(), product_profile(), "Find my current balance", state, llm, seed=11)
    rendered_prompt = llm.prompts[0]

    assert result == TurnDecision(
        satisfied=True,
        giving_up=False,
        next_question=None,
        doubt_expressed=False,
        reasoning="I got the answer.",
    )
    assert internal_spec.supported[0] not in rendered_prompt
    assert internal_spec.not_supported_yet[0] not in rendered_prompt
    assert internal_spec.known_gaps[0] not in rendered_prompt
    assert "expected_response_type" not in rendered_prompt
    assert "What is my balance?" in rendered_prompt
    assert "Your balance is $20." in rendered_prompt
    assert "Find my current balance" in rendered_prompt
    assert "FOLLOW-UPS REMAINING:\n3" in rendered_prompt


def test_decide_retries_once_then_falls_back() -> None:
    llm = RecordingStubLLMClient(["not json", "still not json"])
    state = SimpleNamespace(turn=0, max_turns=2, questions_asked=[], transcript=[])

    result = decide(persona(), product_profile(), "Find my current balance", state, llm, seed=3)

    assert result.giving_up is True
    assert result.reasoning == "parse failure"
    assert len(llm.prompts) == 2
    assert "STRICT REMINDER" in llm.prompts[1]


def test_turn_prompt_carries_real_replies_from_a_real_conversation_state() -> None:
    """Guards the integration seam between the moderator and the persona agent.

    A hand-built state object cannot catch this: the bug it protects against was
    ConversationState not retaining responses at all, so the persona decided every
    turn against a transcript whose replies were blank.
    """
    from synthetic_user.core.models import AdapterResponse
    from synthetic_user.simulation.moderator import ConversationState

    state = ConversationState(max_turns=4, token_budget=1000)
    state.record_turn(
        "What is my balance?",
        AdapterResponse(reply="Your balance is $20.", latency_ms=5),
    )

    llm = RecordingStubLLMClient(
        ['{"satisfied": true, "giving_up": false, "next_question": null, "doubt_expressed": false, "reasoning": "Got it."}']
    )
    decide(persona(), product_profile(), "Find my current balance", state, llm, seed=5)

    assert "Your balance is $20." in llm.prompts[0]


def test_closing_summary_prompt_states_how_the_conversation_ended() -> None:
    """The summary must not be able to claim success on an abandoned conversation."""
    from synthetic_user.core.models import AdapterResponse, Conversation, Outcome, Turn
    from synthetic_user.simulation.persona_agent import closing_summary

    conversation = Conversation(
        id="c1",
        question_id="q1",
        repeat_index=0,
        seed=1,
        persona_id="persona-1",
        persona_version=1,
        turns=[
            Turn(
                turn_index=0,
                message="What is my balance?",
                response=AdapterResponse(reply="Found 16 result(s).", latency_ms=5),
                decision=TurnDecision(
                    satisfied=False,
                    giving_up=True,
                    next_question=None,
                    doubt_expressed=True,
                    reasoning="I still do not have a clear answer, so I give up.",
                ),
            )
        ],
        outcome=Outcome.ABANDONED,
        doubt_anywhere=True,
        closing_summary=None,
    )

    llm = RecordingStubLLMClient(["It went badly. I never got the number."])
    closing_summary(persona(), conversation, llm)
    rendered_prompt = llm.prompts[0]

    assert "gave up without getting what you needed" in rendered_prompt
    assert "I still do not have a clear answer, so I give up." in rendered_prompt
    assert "<<HOW_IT_ENDED>>" not in rendered_prompt


# --- run-level leak guard ----------------------------------------------------------
#
# Extends the capability-spec guard above to every privileged asset. Schema,
# few-shots and all reference fields except `expectation` must stay out of both
# the persona-agent prompt and anything sent to the adapter, across a whole run.


class _RecordingAdapter:
    """MockAdapter that remembers every message the harness sent it."""

    def __init__(self) -> None:
        from synthetic_user.adapters.mock import MockAdapter

        self._inner = MockAdapter(variant="correct")
        self.messages: list[str] = []

    def open_session(self):
        return self._inner.open_session()

    def send(self, session, message):
        self.messages.append(message)
        return self._inner.send(session, message)

    def extract_evaluable(self, response):
        return self._inner.extract_evaluable(response)

    def close_session(self, session) -> None:
        self._inner.close_session(session)


def test_no_privileged_asset_reaches_the_persona_or_the_adapter() -> None:
    from synthetic_user.assets.registry import (
        load_named_fewshots,
        load_named_persona,
        load_named_product,
        load_named_schema,
    )
    from synthetic_user.core.models import QuestionReference
    from synthetic_user.simulation.runner import run_conversation

    schema = load_named_schema("insight_schema")
    fewshots = load_named_fewshots("insight_fewshots")

    reference = QuestionReference(
        id="ref-1",
        question_id="question-1",
        expected_behaviour="answer",
        expectation="How much Marketing spent on cards last month.",
        tables=["credit_card_billing"],
        columns=["credit_card_billing.transaction_amount_usd"],
        filters=["department = LEAK_FILTER_SENTINEL"],
        aggregation="LEAK_AGGREGATION_SENTINEL",
        grain="LEAK_GRAIN_SENTINEL",
        time_window="LEAK_WINDOW_SENTINEL",
        analysis_type="aggregate",
        expected_output_columns=["LEAK_OUTPUT_SENTINEL"],
        must_match=["tables"],
        acceptable_alternatives=["LEAK_ALTERNATIVE_SENTINEL"],
        few_shot_ids=["fs-001"],
        few_shot_distance="near",
        schema_version=schema.schema_version,
        prompt_version="reference_generate_v1",
        model="stub",
        validation_status="valid",
    )

    question = GeneratedQuestion(
        id="question-1",
        question="how much did marketing spend last month",
        intent="LEAK_RAW_INTENT_SENTINEL",
        complexity="simple",
        persona_trait="example_utterances",
        expected_response_type="data",
        reference_id="ref-1",
    )

    decisions = [
        '{"satisfied": false, "giving_up": false, "next_question": "what about just travel",'
        ' "doubt_expressed": true, "reasoning": "not sure that is only marketing"}',
        '{"satisfied": true, "giving_up": false, "next_question": null,'
        ' "doubt_expressed": false, "reasoning": "got it"}',
        "Two sentences about how it went.",
    ]
    llm = RecordingStubLLMClient(decisions * 4)
    adapter = _RecordingAdapter()

    conversation = run_conversation(
        question,
        load_named_persona("junior_analyst"),
        load_named_product("insight_profile"),
        adapter,
        llm,
        seed=5,
        settings=SimpleNamespace(
            hard_max_turns=4, token_budget_per_run=10_000, similarity_threshold=0.92
        ),
        reference=reference,
    )

    surfaces = llm.prompts + adapter.messages

    forbidden = [
        # schema identifiers
        "credit_card_billing",
        "transaction_amount_usd",
        "merchant_category",
        "billing_cycle_month",
        # few-shot SQL
        "SELECT",
        "GROUP BY",
        fewshots[0].sql.strip()[:40],
        # every reference field except expectation
        "LEAK_FILTER_SENTINEL",
        "LEAK_AGGREGATION_SENTINEL",
        "LEAK_GRAIN_SENTINEL",
        "LEAK_WINDOW_SENTINEL",
        "LEAK_OUTPUT_SENTINEL",
        "LEAK_ALTERNATIVE_SENTINEL",
        "fs-001",
        "reference_generate_v1",
    ]

    for surface in surfaces:
        for secret in forbidden:
            assert secret not in surface, f"leaked {secret!r} into: {surface[:200]}"

    # The one reference field that IS allowed through, as the persona's intent.
    assert any("How much Marketing spent on cards last month." in p for p in llm.prompts)
    # ...and it replaces the raw generated intent rather than sitting alongside it.
    assert not any("LEAK_RAW_INTENT_SENTINEL" in p for p in llm.prompts)
    assert conversation.turns


def test_adapter_evaluable_output_is_stored_per_turn() -> None:
    from synthetic_user.assets.registry import load_named_persona, load_named_product
    from synthetic_user.simulation.runner import run_conversation

    llm = RecordingStubLLMClient(
        [
            '{"satisfied": true, "giving_up": false, "next_question": null,'
            ' "doubt_expressed": false, "reasoning": "got it"}',
            "Fine, I got what I needed.",
        ]
    )
    question = GeneratedQuestion(
        id="question-1",
        question="how much did marketing spend",
        intent="Find marketing spend.",
        complexity="simple",
        persona_trait="example_utterances",
        expected_response_type="data",
    )
    conversation = run_conversation(
        question,
        load_named_persona("junior_analyst"),
        load_named_product("insight_profile"),
        _RecordingAdapter(),
        llm,
        seed=1,
        settings=SimpleNamespace(
            hard_max_turns=3, token_budget_per_run=10_000, similarity_threshold=0.92
        ),
    )

    evaluable = conversation.turns[0].evaluable
    assert evaluable is not None
    assert evaluable["kind"] == "sql"
    assert "credit_card_billing" in evaluable["query"]
    assert evaluable["rows"]
