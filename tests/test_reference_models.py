import pytest
from pydantic import ValidationError

from synthetic_user.assets.registry import (
    load_named_fewshots,
    load_named_glossary,
    load_named_schema,
)
from synthetic_user.core.models import (
    FewShotExample,
    GeneratedQuestion,
    JudgeResult,
    QuestionReference,
    SchemaSpec,
    SchemaTable,
)


def _reference(**overrides) -> QuestionReference:
    base = dict(
        id="ref-1",
        question_id="question-1",
        expected_behaviour="answer",
        expectation="Total card spend for Marketing over the last full month.",
        tables=["credit_card_billing"],
        columns=["credit_card_billing.transaction_amount_usd"],
        filters=["department = Marketing", "billing month = last full month"],
        aggregation="sum",
        grain="one row overall",
        time_window="last full calendar month",
        analysis_type="aggregate",
        expected_output_columns=["total_spend_usd"],
        must_match=["tables", "aggregation"],
        acceptable_alternatives=["grouping by billing_cycle_month instead of transaction_date"],
        few_shot_ids=["fs-001"],
        few_shot_distance="near",
        schema_version="abc123",
        prompt_version="reference_v1",
        model="gemini-3.5-flash-lite",
    )
    base.update(overrides)
    return QuestionReference(**base)


# --- QuestionReference constraints -------------------------------------------------


def test_reference_accepts_must_match_naming_real_fields() -> None:
    reference = _reference(must_match=["tables", "columns", "aggregation", "time_window"])
    assert reference.validation_status == "pending"


def test_reference_rejects_must_match_typo() -> None:
    """A typo here would silently disable a mandatory check, so it must fail loudly."""
    with pytest.raises(ValidationError, match="must_match names fields that do not exist"):
        _reference(must_match=["table"])  # singular - not a field


def test_reference_rejects_unknown_behaviour_and_analysis_type() -> None:
    with pytest.raises(ValidationError):
        _reference(expected_behaviour="escalate")
    with pytest.raises(ValidationError):
        _reference(analysis_type="forecast")


def test_reference_rejects_unknown_few_shot_distance() -> None:
    with pytest.raises(ValidationError):
        _reference(few_shot_distance="far")


def test_reference_validation_status_is_constrained() -> None:
    assert _reference(validation_status="valid").validation_status == "valid"
    with pytest.raises(ValidationError):
        _reference(validation_status="probably")


# --- SchemaSpec content hash -------------------------------------------------------


def _spec(description: str = "Card transactions.") -> SchemaSpec:
    return SchemaSpec(
        product_version="1.0",
        tables=[
            SchemaTable(
                name="credit_card_billing",
                description=description,
                columns=[{"name": "transaction_id", "type": "VARCHAR", "description": "Id."}],
            )
        ],
    )


def test_schema_version_is_derived_and_stable() -> None:
    assert _spec().schema_version == _spec().schema_version
    assert _spec().schema_version != ""


def test_schema_version_changes_when_content_changes() -> None:
    """A reference pinned to a schema_version must not survive a schema edit."""
    assert _spec().schema_version != _spec("Now described differently.").schema_version


def test_explicit_schema_version_is_not_overwritten() -> None:
    spec = SchemaSpec(product_version="1.0", tables=[], schema_version="pinned")
    assert spec.schema_version == "pinned"


# --- JudgeResult -------------------------------------------------------------------


def test_judge_result_requires_valid_overall_and_verdicts() -> None:
    result = JudgeResult(
        conversation_id="c1",
        question_id="question-1",
        parse_ok=True,
        overall="partially_correct",
        field_verdicts={"tables": "match", "aggregation": "mismatch"},
        reason="Right table, wrong aggregation.",
        judge_model="gemini-3.5-flash-lite",
        prompt_version="judge_v1",
    )
    assert result.evaluated_turn is None
    assert result.failure_tags == []

    with pytest.raises(ValidationError):
        JudgeResult(
            conversation_id="c1",
            question_id="question-1",
            parse_ok=True,
            overall="mostly_fine",
            reason="",
            judge_model="m",
            prompt_version="judge_v1",
        )


def test_judge_result_rejects_unknown_field_verdict() -> None:
    with pytest.raises(ValidationError):
        JudgeResult(
            conversation_id="c1",
            question_id="question-1",
            parse_ok=True,
            overall="correct",
            field_verdicts={"tables": "sort_of"},
            reason="",
            judge_model="m",
            prompt_version="judge_v1",
        )


# --- GeneratedQuestion is additive -------------------------------------------------


def test_generated_question_reference_id_defaults_to_none() -> None:
    """The existing generate -> run -> validate flow must keep working untouched."""
    question = GeneratedQuestion(
        id="question-1",
        question="how much did marketing spend",
        intent="Find marketing spend.",
        complexity="simple",
        persona_trait="example_utterances",
        expected_response_type="data",
    )
    assert question.reference_id is None


# --- Asset loading -----------------------------------------------------------------


def test_schema_asset_loads_and_matches_mock_product() -> None:
    spec = load_named_schema("insight_schema")
    assert [table.name for table in spec.tables] == ["credit_card_billing"]

    columns = {column.name for column in spec.tables[0].columns}
    assert {"transaction_amount_usd", "department", "merchant_category", "payment_status"} <= columns
    assert len(columns) == 15
    assert spec.schema_version
    assert all(column.description for column in spec.tables[0].columns)


def test_fewshot_asset_loads_and_is_flagged_placeholder() -> None:
    examples = load_named_fewshots("insight_fewshots")
    assert len(examples) == 10
    assert all(isinstance(example, FewShotExample) for example in examples)
    assert {example.source for example in examples} == {"placeholder"}
    assert all(example.tables == ["credit_card_billing"] for example in examples)
    assert len({example.id for example in examples}) == 10


def test_glossary_asset_loads() -> None:
    glossary = load_named_glossary("insight_glossary")
    assert "spend" in glossary
    assert glossary["spend"]


def test_missing_glossary_is_not_an_error() -> None:
    assert load_named_glossary("no_such_glossary_file") == {}
