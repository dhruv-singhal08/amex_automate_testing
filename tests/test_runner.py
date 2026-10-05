import json
from types import SimpleNamespace

from synthetic_user.adapters.mock import MockAdapter
from synthetic_user.assets.registry import load_named_persona, load_named_product
from synthetic_user.core.llm import StubLLMClient
from synthetic_user.core.models import GeneratedQuestion
from synthetic_user.core.storage import get_conversations, init_db
from synthetic_user.simulation.runner import run_grid


DECISION = json.dumps(
    {
        "satisfied": True,
        "giving_up": False,
        "next_question": None,
        "doubt_expressed": False,
        "reasoning": "The answer is enough for me.",
    }
)


def settings(database_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        repeats_per_question=1,
        hard_max_turns=4,
        token_budget_per_run=500_000,
        model_generation="stub-generation",
        model_persona="stub-persona",
        model_validator="stub-validator",
        model_product="stub-product",
        seed_base=9001,
        spec_version=1,
        database_url=database_url,
    )


def make_llm() -> StubLLMClient:
    return StubLLMClient([DECISION, "I got a clear answer.", DECISION, "I got a clear answer."])


def test_two_persona_grid_reproduces_conversations_for_same_seed_base(tmp_path) -> None:
    init_db(f"sqlite:///{tmp_path / 'runner.db'}")
    junior = load_named_persona("junior_analyst")
    controller = load_named_persona("finance_controller")
    product = load_named_product("insight_profile")
    questions = {
        junior.id: [
            GeneratedQuestion(
                id="balance",
                question="How much did we bill?",
                intent="find total billing",
                complexity="simple",
                persona_trait="casual wording",
                expected_response_type="data",
            )
        ],
        controller.id: [
            GeneratedQuestion(
                id="period-total",
                question="Provide total billing for the stated period.",
                intent="find period total",
                complexity="medium",
                persona_trait="precise wording",
                expected_response_type="data",
            )
        ],
    }
    configured = settings(f"sqlite:///{tmp_path / 'runner.db'}")

    first_run = run_grid(
        [junior, controller],
        questions,
        product,
        MockAdapter(),
        make_llm(),
        configured,
    )
    second_run = run_grid(
        [junior, controller],
        questions,
        product,
        MockAdapter(),
        make_llm(),
        configured,
    )

    first = get_conversations(first_run.id)
    second = get_conversations(second_run.id)

    assert first_run.seed_base == second_run.seed_base == 9001
    assert len(first) == len(second) == 2
    assert [conversation.seed for conversation in first] == [conversation.seed for conversation in second]
    assert [
        conversation.model_dump(exclude={"id"}) for conversation in first
    ] == [conversation.model_dump(exclude={"id"}) for conversation in second]
    assert first_run.persona_versions == second_run.persona_versions
    assert first_run.prompt_versions == second_run.prompt_versions
    assert first_run.model_versions == second_run.model_versions
