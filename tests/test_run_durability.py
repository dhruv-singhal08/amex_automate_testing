"""An interrupted grid must not strand its conversations.

run_grid used to call save_run only after the whole loop, so killing it part-way
left conversations in the database with no run record - invisible to get_run,
metrics, the UI and every other read path. It now claims the run id up front.
"""

from types import SimpleNamespace

import pytest

from synthetic_user.adapters.mock import MockAdapter
from synthetic_user.assets.registry import load_named_persona, load_named_product
from synthetic_user.core.llm import StubLLMClient
from synthetic_user.core.models import GeneratedQuestion
from synthetic_user.core.storage import get_conversations, get_run, init_db
from synthetic_user.simulation.runner import run_grid

DECISION = (
    '{"satisfied": true, "giving_up": false, "next_question": null,'
    ' "doubt_expressed": false, "reasoning": "got it"}'
)


def _questions(count: int) -> list[GeneratedQuestion]:
    return [
        GeneratedQuestion(
            id=f"question-{index}",
            question=f"how much did marketing spend {index}",
            intent="Find marketing spend.",
            complexity="simple",
            persona_trait="example_utterances",
            expected_response_type="data",
        )
        for index in range(count)
    ]


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        repeats_per_question=1,
        hard_max_turns=3,
        token_budget_per_run=10_000,
        similarity_threshold=0.92,
        model_generation="m",
        model_persona="m",
        model_validator="m",
        model_product="m",
        seed_base=0,
    )


def test_run_record_exists_even_if_the_grid_is_interrupted(tmp_path) -> None:
    init_db(f"sqlite:///{tmp_path / 'durability.db'}")

    persona = load_named_persona("junior_analyst")
    product = load_named_product("insight_profile")

    class Interrupted(MockAdapter):
        """Models the process being killed part-way through the grid.

        An ordinary Exception would be caught and recorded as an ADAPTER_ERROR
        conversation, which is by design - so it would not test anything here.
        KeyboardInterrupt escapes that handler, exactly as a real kill does.
        """

        def __init__(self) -> None:
            super().__init__(variant="correct")
            self.sent = 0

        def send(self, session, message):
            self.sent += 1
            if self.sent > 2:
                raise KeyboardInterrupt("killed mid-grid")
            return super().send(session, message)

    with pytest.raises(KeyboardInterrupt):
        run_grid(
            [persona],
            {persona.id: _questions(5)},
            product,
            Interrupted(),
            StubLLMClient([DECISION, "summary"] * 20),
            _settings(),
        )

    # The grid never finished, but the conversations it did write must still be
    # reachable through their run record.
    from synthetic_user.core.storage import list_runs

    runs = list_runs()
    assert runs, "an interrupted grid left no run record at all"
    run_id = runs[0].id
    assert get_run(run_id) is not None
    assert get_conversations(run_id), "conversations were written but stranded"


def test_completed_grid_records_every_conversation_id(tmp_path) -> None:
    init_db(f"sqlite:///{tmp_path / 'durability_ok.db'}")

    persona = load_named_persona("junior_analyst")
    record = run_grid(
        [persona],
        {persona.id: _questions(3)},
        load_named_product("insight_profile"),
        MockAdapter(variant="correct"),
        StubLLMClient([DECISION, "summary"] * 20),
        _settings(),
    )

    assert len(record.conversation_ids) == 3
    assert get_run(record.id).conversation_ids == record.conversation_ids
    assert len(get_conversations(record.id)) == 3
