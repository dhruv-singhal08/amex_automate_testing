import json

import pytest

from synthetic_user.adapters.base import evaluable_from
from synthetic_user.adapters.mock import MockAdapter
from synthetic_user.core.models import Conversation, Outcome, QuestionReference, Turn
from synthetic_user.evaluation.judge import judge
from synthetic_user.evaluation.sql_profile import SqlReferenceProfile, parse_sql

REFERENCE_SQL_COLUMNS = [
    "credit_card_billing.department",
    "credit_card_billing.transaction_amount_usd",
]


def _reference(**overrides) -> QuestionReference:
    base = dict(
        id="ref-1",
        question_id="question-1",
        expected_behaviour="answer",
        expectation="Total card spend for Marketing over the last full month.",
        tables=["credit_card_billing"],
        columns=list(REFERENCE_SQL_COLUMNS),
        filters=["department = Marketing", "last full month"],
        aggregation="sum",
        grain="one row per department",
        time_window="last full calendar month",
        analysis_type="aggregate",
        expected_output_columns=["total spend"],
        must_match=["tables", "aggregation", "filters"],
        acceptable_alternatives=["grouping by billing_cycle_month instead of transaction_date"],
        few_shot_ids=["fs-001"],
        few_shot_distance="near",
        schema_version="abc123",
        prompt_version="reference_generate_v1",
        model="stub",
        validation_status="valid",
    )
    base.update(overrides)
    return QuestionReference(**base)


def _conversation_from_variant(variant: str) -> Conversation:
    """Drive a real MockAdapter so the evaluable output is produced the real way."""
    adapter = MockAdapter(variant=variant)
    session = adapter.open_session()
    response = adapter.send(session, "how much did marketing spend last month")
    adapter.close_session(session)
    return Conversation(
        id=f"conv-{variant}",
        question_id="question-1",
        repeat_index=0,
        seed=7,
        persona_id="junior_analyst",
        persona_version=2,
        turns=[
            Turn(
                turn_index=0,
                message="how much did marketing spend last month",
                response=response,
                decision=None,
                evaluable=evaluable_from(adapter, response),
            )
        ],
        outcome=Outcome.COMPLETED,
        doubt_anywhere=False,
        closing_summary=None,
    )


class JudgeStub:
    """Stands in for the judge model. `payload` is what the model would return."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def complete(self, prompt: str, role: str, seed=None, temperature: float = 0.7, system=None) -> str:
        assert role == "judge"
        self.prompts.append(prompt)
        return json.dumps(self.payload)


def _payload(verdicts: dict[str, str], **overrides) -> dict:
    base = {
        "field_verdicts": verdicts,
        "behaviour_match": True,
        "failure_tags": [],
        "reference_suspect": False,
        "reason": "stubbed",
    }
    base.update(overrides)
    return base


ALL_MATCH = {field: "match" for field in
             ("tables", "columns", "filters", "aggregation", "grain", "analysis_type",
              "time_window", "expected_output_columns")}


# --- MockAdapter variants -> expected overall and tag --------------------------------


def test_correct_variant_scores_correct() -> None:
    llm = JudgeStub(_payload(ALL_MATCH))
    result = judge(_conversation_from_variant("correct"), _reference(), llm)

    assert result.overall == "correct"
    assert result.failure_tags == []
    assert result.parse_ok is True
    assert result.table_recall == 1.0
    assert result.column_recall == 1.0


def test_wrong_table_variant_scores_incorrect_with_wrong_table_tag() -> None:
    verdicts = {**ALL_MATCH, "tables": "mismatch", "columns": "mismatch"}
    llm = JudgeStub(_payload(verdicts))
    result = judge(_conversation_from_variant("wrong_table"), _reference(), llm)

    assert result.overall == "incorrect"  # tables is in must_match
    assert "wrong_table" in result.failure_tags
    assert result.table_recall == 0.0


def test_missing_filter_variant_scores_incorrect_with_missing_filter_tag() -> None:
    verdicts = {**ALL_MATCH, "filters": "mismatch"}
    llm = JudgeStub(_payload(verdicts))
    result = judge(_conversation_from_variant("missing_filter"), _reference(), llm)

    assert result.overall == "incorrect"  # filters is in must_match
    assert "missing_filter" in result.failure_tags


def test_wrong_grain_variant_scores_partially_correct() -> None:
    """grain is NOT in must_match, so a grain-only miss is partial, not incorrect."""
    verdicts = {**ALL_MATCH, "grain": "mismatch"}
    llm = JudgeStub(_payload(verdicts))
    result = judge(_conversation_from_variant("wrong_grain"), _reference(), llm)

    assert result.overall == "partially_correct"
    assert "wrong_grain" in result.failure_tags


def test_wrong_aggregation_variant_scores_incorrect() -> None:
    verdicts = {**ALL_MATCH, "aggregation": "mismatch"}
    llm = JudgeStub(_payload(verdicts))
    result = judge(_conversation_from_variant("wrong_aggregation"), _reference(), llm)

    assert result.overall == "incorrect"  # aggregation is in must_match
    assert "wrong_aggregation" in result.failure_tags


def test_refusal_variant_against_answer_reference_is_incorrect() -> None:
    llm = JudgeStub(_payload({}, behaviour_match=False))
    result = judge(_conversation_from_variant("refusal"), _reference(), llm)

    assert result.overall == "incorrect"
    assert result.behaviour_match is False


def test_refusal_variant_against_refuse_reference_is_correct() -> None:
    """The product was right to refuse, and produced no query to judge."""
    llm = JudgeStub(_payload({}, behaviour_match=True))
    reference = _reference(
        expected_behaviour="refuse", tables=[], columns=[], must_match=["expected_behaviour"]
    )
    result = judge(_conversation_from_variant("refusal"), reference, llm)

    assert result.overall == "correct"
    assert result.parse_ok is True  # nothing to parse, so not a parse failure
    assert result.failure_tags == []


def test_clarifying_variant_against_clarify_reference_is_correct() -> None:
    llm = JudgeStub(_payload({}, behaviour_match=True))
    reference = _reference(
        expected_behaviour="clarify", tables=[], columns=[], must_match=["expected_behaviour"]
    )
    result = judge(_conversation_from_variant("clarifying_question"), reference, llm)

    assert result.overall == "correct"
    assert result.behaviour_match is True


def test_answering_when_it_should_have_refused_is_tagged() -> None:
    """Producing a query at all is itself the evidence, whatever the model says."""
    llm = JudgeStub(_payload(ALL_MATCH, behaviour_match=True))
    reference = _reference(expected_behaviour="refuse", tables=[], columns=[])
    result = judge(_conversation_from_variant("correct"), reference, llm)

    assert "should_have_refused" in result.failure_tags


# --- equivalence ---------------------------------------------------------------------


def test_equivalent_rewrite_still_scores_correct() -> None:
    """Different aliases and clause order must not change the verdict."""
    original = _conversation_from_variant("correct")
    original_sql = original.turns[0].evaluable["query"]

    rewritten = (
        "SELECT c.department, SUM(c.transaction_amount_usd) AS total_spend_usd "
        "FROM credit_card_billing AS c "
        "WHERE c.transaction_date >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
        "AND c.transaction_date < date_trunc('month', CURRENT_DATE) "
        "AND c.department = 'Marketing' "
        "GROUP BY c.department"
    )
    profile = SqlReferenceProfile()
    reference = _reference()

    first = profile.deterministic_checks(reference, {"kind": "sql", "query": original_sql})
    second = profile.deterministic_checks(reference, {"kind": "sql", "query": rewritten})

    assert first["structure"]["tables"] == second["structure"]["tables"]
    assert first["structure"]["columns"] == second["structure"]["columns"]
    assert first["structure"]["aggregates"] == second["structure"]["aggregates"]
    assert first["structure"]["group_by"] == second["structure"]["group_by"]
    assert set(first["structure"]["where_predicates"]) == set(second["structure"]["where_predicates"])
    assert (first["table_recall"], first["column_recall"]) == (
        second["table_recall"],
        second["column_recall"],
    )

    rewritten_turn = original.turns[0].model_copy(
        update={"evaluable": {"kind": "sql", "query": rewritten}}
    )
    conversation = original.model_copy(update={"turns": [rewritten_turn]})
    result = judge(conversation, reference, JudgeStub(_payload(ALL_MATCH)))
    assert result.overall == "correct"


# --- reference_suspect overrides ------------------------------------------------------


def test_reference_suspect_overrides_field_verdicts() -> None:
    """A bad reference must never be reported as a product failure."""
    verdicts = {**ALL_MATCH, "tables": "mismatch", "aggregation": "mismatch"}
    llm = JudgeStub(
        _payload(verdicts, reference_suspect=True, behaviour_match=False,
                 reason="The reference demands a column the question never implied.")
    )
    result = judge(_conversation_from_variant("wrong_table"), _reference(), llm)

    assert result.overall == "reference_suspect"
    assert "reference" in result.reason.lower()


# --- robustness -----------------------------------------------------------------------


def test_unparseable_sql_still_reaches_the_judge() -> None:
    conversation = _conversation_from_variant("correct")
    broken = conversation.turns[0].model_copy(
        update={"evaluable": {"kind": "sql", "query": "SELECT FROM WHERE (("}}
    )
    llm = JudgeStub(_payload({**ALL_MATCH, "tables": "mismatch"}))
    result = judge(conversation.model_copy(update={"turns": [broken]}), _reference(), llm)

    assert result.parse_ok is False
    assert result.table_precision is None
    assert result.overall == "incorrect"  # judged by the LLM despite the parse failure


def test_parse_failure_and_judge_failure_is_not_evaluable() -> None:
    class BrokenJudge:
        def complete(self, prompt, role, seed=None, temperature=0.7, system=None):
            raise RuntimeError("judge offline")

    conversation = _conversation_from_variant("correct")
    broken = conversation.turns[0].model_copy(
        update={"evaluable": {"kind": "sql", "query": "SELECT FROM WHERE (("}}
    )
    result = judge(conversation.model_copy(update={"turns": [broken]}), _reference(), BrokenJudge())

    assert result.overall == "not_evaluable"
    assert result.field_verdicts == {}


def test_unparseable_judge_response_does_not_raise() -> None:
    class Garbage:
        def complete(self, prompt, role, seed=None, temperature=0.7, system=None):
            return "not json"

    result = judge(_conversation_from_variant("correct"), _reference(), Garbage())
    assert result.overall in {"correct", "not_evaluable"}
    assert result.field_verdicts == {}


def test_unknown_failure_tags_and_verdicts_are_discarded() -> None:
    llm = JudgeStub(
        _payload({**ALL_MATCH, "made_up_field": "match", "grain": "vibes"},
                 failure_tags=["wrong_grain", "not_a_real_tag"])
    )
    result = judge(_conversation_from_variant("correct"), _reference(), llm)

    assert "made_up_field" not in result.field_verdicts
    # An invalid verdict value drops the whole field rather than storing junk.
    assert result.field_verdicts.get("grain") != "vibes"
    assert "not_a_real_tag" not in result.failure_tags
    assert "wrong_grain" in result.failure_tags


def test_judge_prompt_excludes_few_shots() -> None:
    """The judge must decide from the reference, not from examples it has seen."""
    from synthetic_user.assets.registry import load_named_fewshots

    llm = JudgeStub(_payload(ALL_MATCH))
    judge(_conversation_from_variant("correct"), _reference(), llm,
          glossary={"spend": "The sum of transaction_amount_usd."})
    prompt = llm.prompts[0]

    # Compare on markers unique to the few-shot bank. An SQL prefix would give a
    # false positive: the product's own query legitimately starts the same way.
    for example in load_named_fewshots("insight_fewshots"):
        assert example.id not in prompt
        assert example.notes not in prompt
    assert "date_trunc('year', CURRENT_DATE)" not in prompt  # only fs-006/007 use this
    assert "spend" in prompt  # the glossary IS passed
    assert "Total card spend for Marketing" in prompt  # and the reference


def test_nothing_in_evaluation_executes_sql(monkeypatch) -> None:
    """Evaluation must never touch a database - the real product forbids it."""
    import duckdb

    def explode(*args, **kwargs):
        raise AssertionError("evaluation attempted to connect to a database")

    monkeypatch.setattr(duckdb, "connect", explode)
    result = judge(_conversation_from_variant("correct"), _reference(), JudgeStub(_payload(ALL_MATCH)))
    assert result.overall == "correct"
    assert parse_sql("SELECT 1 FROM credit_card_billing")["parse_ok"] is True
