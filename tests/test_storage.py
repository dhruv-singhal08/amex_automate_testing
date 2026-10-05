from synthetic_user.core.models import (
    AdapterResponse,
    Conversation,
    Outcome,
    Turn,
    TurnDecision,
)
from synthetic_user.core.storage import get_conversations, init_db, save_conversation


def test_conversation_with_nested_turns_round_trips(tmp_path) -> None:
    init_db(f"sqlite:///{tmp_path / 'storage.db'}")
    conversation = Conversation(
        id="conversation-1",
        question_id="question-1",
        repeat_index=2,
        seed=1234,
        persona_id="persona-1",
        persona_version=3,
        turns=[
            Turn(
                turn_index=0,
                message="What is my balance?",
                response=AdapterResponse(
                    reply="Your balance is $20.",
                    artifacts={"currency": "USD", "amount": 20},
                    latency_ms=12,
                    raw={"request_id": "req-1"},
                ),
                decision=TurnDecision(
                    satisfied=False,
                    giving_up=False,
                    next_question="Can you show the date?",
                    doubt_expressed=True,
                    reasoning="The answer lacks a date.",
                ),
            ),
            Turn(
                turn_index=1,
                message="Can you show the date?",
                response=AdapterResponse(
                    reply="The balance is from September 14.",
                    artifacts={"date": "2026-09-14"},
                    latency_ms=15,
                ),
                decision=TurnDecision(
                    satisfied=True,
                    giving_up=False,
                    next_question=None,
                    doubt_expressed=False,
                    reasoning="The date answers the remaining question.",
                ),
            ),
            Turn(
                turn_index=2,
                message="Thanks.",
                response=AdapterResponse(
                    reply="You are welcome.",
                    error=None,
                    latency_ms=7,
                ),
                decision=None,
            ),
        ],
        outcome=Outcome.COMPLETED,
        doubt_anywhere=True,
        closing_summary="The user received the balance and its date.",
    )

    save_conversation(conversation, run_id="run-1")

    loaded = get_conversations("run-1")
    assert loaded == [conversation]
    assert loaded[0].turns == conversation.turns


def test_conversation_with_date_valued_rows_survives_saving(tmp_path) -> None:
    """A DATE column in adapter rows must not destroy the whole conversation.

    evaluable was previously written raw, so datetime.date objects hit the JSON
    column and the entire conversation was dropped - and only for queries that
    touched dates, which biased the sample toward the behaviour least under test.
    """
    import datetime

    from synthetic_user.core.models import AdapterResponse, Conversation, Outcome, Turn
    from synthetic_user.core.storage import get_conversations, init_db, save_conversation

    init_db(f"sqlite:///{tmp_path / 'dates.db'}")

    rows = [{"transaction_date": datetime.date(2026, 5, 18), "total": 1506.92}]
    conversation = Conversation(
        id="conv-dates",
        question_id="question-1",
        repeat_index=0,
        seed=1,
        persona_id="junior_analyst",
        persona_version=2,
        turns=[
            Turn(
                turn_index=0,
                message="how much did we spend last month",
                response=AdapterResponse(
                    reply="You spent $1,506.92.",
                    artifacts={"sql": "SELECT transaction_date FROM credit_card_billing", "rows": rows},
                    latency_ms=3,
                ),
                decision=None,
                evaluable={"kind": "sql", "query": "SELECT transaction_date FROM credit_card_billing", "rows": rows},
            )
        ],
        outcome=Outcome.COMPLETED,
        doubt_anywhere=False,
        closing_summary=None,
    )

    save_conversation(conversation, run_id="run-dates")

    stored = get_conversations("run-dates")
    assert len(stored) == 1, "the conversation was silently dropped"
    evaluable = stored[0].turns[0].evaluable
    assert evaluable["rows"][0]["transaction_date"] == "2026-05-18"
    assert evaluable["query"].startswith("SELECT")
