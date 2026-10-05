import json

from synthetic_user.adapters.mock import MockAdapter
from synthetic_user.assets.registry import load_named_persona, load_named_schema
from synthetic_user.core.models import QuestionReference, SchemaColumn, SchemaSpec, SchemaTable
from synthetic_user.evaluation.metamorphic import (
    RELATIONS,
    VariantResult,
    check_question,
    compare_structures,
    consistency_rates,
    date_columns,
    is_date_predicate,
    split_predicates,
)
from synthetic_user.evaluation.sql_profile import parse_sql

SCHEMA = load_named_schema("insight_schema")

ORIGINAL_SQL = (
    "SELECT department, SUM(transaction_amount_usd) AS total_spend_usd "
    "FROM credit_card_billing "
    "WHERE department = 'Marketing' "
    "AND transaction_date >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
    "GROUP BY department"
)


class VariantStub:
    """Returns the three rewrites the variant generator would have produced."""

    def __init__(self, variants: dict[str, str] | None = None, raw: str | None = None) -> None:
        self._variants = variants if variants is not None else {
            relation: f"{relation} of the original question" for relation in RELATIONS
        }
        self._raw = raw
        self.prompts: list[str] = []

    def complete(self, prompt: str, role: str, seed=None, temperature: float = 0.7, system=None) -> str:
        assert role == "reference_generation"
        self.prompts.append(prompt)
        return self._raw if self._raw is not None else json.dumps(self._variants)


# --- date predicate identification ---------------------------------------------------


def test_date_columns_come_from_schema_types_not_names() -> None:
    columns = date_columns(SCHEMA)
    assert "credit_card_billing.transaction_date" in columns
    # Typed VARCHAR(6), so not a date column by type - see the caveat in the summary.
    assert "credit_card_billing.billing_cycle_month" not in columns
    assert "credit_card_billing.department" not in columns


def test_date_predicates_detected_by_column_type_and_by_date_arithmetic() -> None:
    known = date_columns(SCHEMA)
    assert is_date_predicate("credit_card_billing.transaction_date >= '2026-01-01'", known)
    assert is_date_predicate("x >= date_trunc('month', CURRENT_DATE)", known)
    assert not is_date_predicate("credit_card_billing.department = 'Marketing'", known)


def test_split_predicates_separates_date_from_the_rest() -> None:
    structure = parse_sql(ORIGINAL_SQL)
    other, dates = split_predicates(structure["where_predicates"], date_columns(SCHEMA))
    assert any("department" in predicate for predicate in other)
    assert any("transaction_date" in predicate for predicate in dates)


def test_custom_schema_date_type_is_honoured() -> None:
    schema = SchemaSpec(
        product_version="1",
        tables=[
            SchemaTable(name="t", description="d", columns=[
                SchemaColumn(name="month_key", type="TIMESTAMP", description="d"),
                SchemaColumn(name="label", type="VARCHAR", description="d"),
            ])
        ],
    )
    assert date_columns(schema) == {"t.month_key"}


# --- comparison ------------------------------------------------------------------------


def _compare(variant_sql: str, relation: str = "paraphrase"):
    return compare_structures(parse_sql(ORIGINAL_SQL), parse_sql(variant_sql), SCHEMA, relation)


def test_identical_sql_is_consistent() -> None:
    status, differences = _compare(ORIGINAL_SQL)
    assert status == "consistent"
    assert differences == {}


def test_equivalent_rewrite_is_consistent() -> None:
    """Aliases and clause order must not register as an inconsistency."""
    rewritten = (
        "SELECT c.department, SUM(c.transaction_amount_usd) AS total_spend_usd "
        "FROM credit_card_billing AS c "
        "WHERE c.transaction_date >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
        "AND c.department = 'Marketing' "
        "GROUP BY c.department"
    )
    assert _compare(rewritten)[0] == "consistent"


def test_changed_table_is_inconsistent() -> None:
    status, differences = _compare(
        "SELECT department, SUM(amount) FROM invoices WHERE department = 'Marketing' GROUP BY department"
    )
    assert status == "inconsistent"
    assert "tables" in differences


def test_dropped_filter_is_inconsistent() -> None:
    status, differences = _compare(
        "SELECT department, SUM(transaction_amount_usd) FROM credit_card_billing "
        "WHERE transaction_date >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
        "GROUP BY department"
    )
    assert status == "inconsistent"
    assert "non_date_predicates" in differences


def test_changed_aggregation_is_inconsistent() -> None:
    status, differences = _compare(
        "SELECT department, COUNT(*) FROM credit_card_billing "
        "WHERE department = 'Marketing' "
        "AND transaction_date >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
        "GROUP BY department"
    )
    assert status == "inconsistent"
    assert "aggregates" in differences


def test_time_shift_changing_only_the_date_filter_is_consistent() -> None:
    shifted = (
        "SELECT department, SUM(transaction_amount_usd) AS total_spend_usd "
        "FROM credit_card_billing "
        "WHERE department = 'Marketing' "
        "AND transaction_date >= date_trunc('quarter', CURRENT_DATE - INTERVAL 1 QUARTER) "
        "GROUP BY department"
    )
    status, differences = compare_structures(
        parse_sql(ORIGINAL_SQL), parse_sql(shifted), SCHEMA, "time_shift"
    )
    assert status == "consistent"
    assert differences == {}


def test_time_shift_that_also_drops_a_filter_is_inconsistent() -> None:
    shifted = (
        "SELECT department, SUM(transaction_amount_usd) FROM credit_card_billing "
        "WHERE transaction_date >= date_trunc('year', CURRENT_DATE) GROUP BY department"
    )
    status, differences = compare_structures(
        parse_sql(ORIGINAL_SQL), parse_sql(shifted), SCHEMA, "time_shift"
    )
    assert status == "inconsistent"
    assert "non_date_predicates" in differences


def test_time_shift_that_did_not_move_the_period_is_flagged_not_failed() -> None:
    """A limp variant is a variant problem, not a product failure."""
    status, differences = compare_structures(
        parse_sql(ORIGINAL_SQL), parse_sql(ORIGINAL_SQL), SCHEMA, "time_shift"
    )
    assert status == "consistent"
    assert differences["unchanged_time_window"] is True


def test_unparseable_variant_is_unparseable_not_inconsistent() -> None:
    status, differences = _compare("SELECT FROM WHERE ((")
    assert status == "unparseable"
    assert "reason" in differences


# --- end to end with MockAdapter --------------------------------------------------------


def _check(adapter, llm=None):
    return check_question(
        persona=load_named_persona("junior_analyst"),
        question_id="question-1",
        question_text="how much did marketing spend last month",
        original_sql=MockAdapter(variant="correct").send({}, "x").artifacts["sql"],
        adapter=adapter,
        llm=llm or VariantStub(),
        schema=SCHEMA,
        seed=1,
    )


def test_stable_product_is_consistent_across_all_relations() -> None:
    """MockAdapter pinned to one variant answers every rewrite identically."""
    results = _check(MockAdapter(variant="correct"))

    assert [r.relation for r in results] == list(RELATIONS)
    assert {r.status for r in results} == {"consistent"}
    assert all(r.variant_question for r in results)


def test_product_that_answers_differently_per_wording_is_inconsistent() -> None:
    """Default MockAdapter derives its behaviour from the message, so rewording
    changes the query - exactly the failure these probes exist to catch."""
    results = _check(MockAdapter())
    assert any(r.status == "inconsistent" for r in results)


def test_refusing_product_reports_no_sql_rather_than_inconsistent() -> None:
    results = _check(MockAdapter(variant="refusal"))
    assert {r.status for r in results} == {"no_sql"}


def test_variant_generation_failure_is_recorded_not_raised() -> None:
    results = _check(MockAdapter(variant="correct"), llm=VariantStub(raw="not json"))
    assert len(results) == 3
    assert {r.status for r in results} == {"no_sql"}
    assert all("could not be generated" in r.differences["reason"] for r in results)


def test_variant_prompt_carries_the_persona_but_no_schema() -> None:
    llm = VariantStub()
    _check(MockAdapter(variant="correct"), llm=llm)
    prompt = llm.prompts[0]

    assert "junior" in prompt.lower() or "analyst" in prompt.lower()
    assert "credit_card_billing" not in prompt
    assert "transaction_amount_usd" not in prompt


# --- consistency_rate ---------------------------------------------------------------------


def _reference(question_id: str, distance: str) -> QuestionReference:
    return QuestionReference(
        id=f"ref-{question_id}", question_id=question_id, expected_behaviour="answer",
        expectation="e", analysis_type="aggregate", few_shot_distance=distance,
        schema_version="v", prompt_version="p", model="m", validation_status="valid",
    )


def test_consistency_rate_is_segmented_by_few_shot_distance() -> None:
    results = [
        VariantResult("q1", "paraphrase", "o", "v", "consistent"),
        VariantResult("q2", "paraphrase", "o", "v", "inconsistent"),
        VariantResult("q3", "paraphrase", "o", "v", "consistent"),
        VariantResult("q1", "time_shift", "o", "v", "consistent"),
    ]
    references = {
        "q1": _reference("q1", "near"),
        "q2": _reference("q2", "schema_only"),
        "q3": _reference("q3", "near"),
    }
    report = consistency_rates(results, references)

    assert report["paraphrase"]["consistency_rate"] == 66.67
    assert report["paraphrase"]["by_few_shot_distance"]["near"]["consistency_rate"] == 100.0
    assert report["paraphrase"]["by_few_shot_distance"]["schema_only"]["consistency_rate"] == 0.0
    assert report["time_shift"]["consistency_rate"] == 100.0


def test_unparseable_and_no_sql_are_excluded_from_the_denominator() -> None:
    """"Could not compare" must not be counted as "the product was inconsistent"."""
    results = [
        VariantResult("q1", "paraphrase", "o", "v", "consistent"),
        VariantResult("q2", "paraphrase", "o", "v", "unparseable"),
        VariantResult("q3", "paraphrase", "o", "v", "no_sql"),
    ]
    report = consistency_rates(results, {})

    assert report["paraphrase"]["consistency_rate"] == 100.0
    assert report["paraphrase"]["comparable"] == 1
    assert report["paraphrase"]["unparseable"] == 1
    assert report["paraphrase"]["no_sql"] == 1


def test_no_comparable_probes_reports_none_not_zero() -> None:
    results = [VariantResult("q1", "paraphrase", "o", "v", "no_sql")]
    assert consistency_rates(results, {})["paraphrase"]["consistency_rate"] is None
