import json
from types import SimpleNamespace

from synthetic_user.assets.registry import (
    load_named_fewshots,
    load_named_persona,
    load_named_product,
    load_named_schema,
)
from synthetic_user.core.models import SchemaColumn, SchemaSpec, SchemaTable
from synthetic_user.generation.reference_generator import (
    generate_with_references,
    identifier_leaks,
    plan_items,
)


def _settings(**overrides):
    base = dict(
        fewshot_mix={"near": 0.3, "pattern": 0.4, "schema_only": 0.3},
        unanswerable_ratio=0.1,
        reference_consistency_samples=1,
        model_reference_generation="stub-model",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _schema() -> SchemaSpec:
    return load_named_schema("insight_schema")


class ScriptedLLM:
    """Returns generation and validation payloads in the order they are requested."""

    def __init__(self, generation: dict | list[dict], validation: dict | None = None) -> None:
        self._generation = generation if isinstance(generation, list) else None
        self._single = None if isinstance(generation, list) else generation
        self._validation = validation or {
            "answerable_with_reference": True,
            "single_interpretation": True,
            "interpretations": [],
            "is_fewshot_paraphrase": False,
            "behaviour_consistent": True,
            "reference_sound": True,
            "notes": "sound",
        }
        self.roles: list[str] = []
        self._index = 0

    def complete(self, prompt: str, role: str, seed=None, temperature: float = 0.7, system=None) -> str:
        self.roles.append(role)
        if role == "reference_validator":
            return json.dumps(self._validation)
        if self._generation is not None:
            payload = self._generation[min(self._index, len(self._generation) - 1)]
            self._index += 1
            return json.dumps(payload)
        return json.dumps(self._single)


def _payload(question: str = "how much did marketing spend last month", **ref_overrides) -> dict:
    reference = dict(
        expected_behaviour="answer",
        expectation="Total card spend for Marketing over the last full month.",
        tables=["credit_card_billing"],
        columns=["credit_card_billing.transaction_amount_usd"],
        filters=["department = Marketing"],
        aggregation="sum",
        grain="one row overall",
        time_window="last full month",
        analysis_type="aggregate",
        expected_output_columns=["total spend"],
        must_match=["tables", "aggregation"],
        acceptable_alternatives=["grouping by billing month"],
    )
    reference.update(ref_overrides)
    return {
        "question": question,
        "intent": "Find marketing spend last month.",
        "complexity": "simple",
        "persona_trait": "example_utterances",
        "expected_response_type": "data",
        "reference": reference,
    }


# --- planning ----------------------------------------------------------------------


def test_distance_mix_is_respected() -> None:
    plans = plan_items(10, _schema(), load_named_fewshots("insight_fewshots"), _settings(), "mixed", seed=1)
    counts = {d: sum(1 for p in plans if p.few_shot_distance == d) for d in ("near", "pattern", "schema_only")}
    assert counts == {"near": 3, "pattern": 4, "schema_only": 3}


def test_fewshot_mode_none_forces_schema_only() -> None:
    plans = plan_items(8, _schema(), load_named_fewshots("insight_fewshots"), _settings(), "none", seed=1)
    assert {p.few_shot_distance for p in plans} == {"schema_only"}
    assert all(p.few_shot is None for p in plans)


def test_schema_only_items_never_carry_a_few_shot() -> None:
    plans = plan_items(10, _schema(), load_named_fewshots("insight_fewshots"), _settings(), "mixed", seed=3)
    for plan in plans:
        assert (plan.few_shot is None) == (plan.few_shot_distance == "schema_only")


def test_unanswerable_ratio_produces_refuse_and_clarify() -> None:
    plans = plan_items(10, _schema(), [], _settings(unanswerable_ratio=0.4), "none", seed=2)
    behaviours = [p.expected_behaviour for p in plans]
    assert behaviours.count("answer") == 6
    assert behaviours.count("refuse") == 2
    assert behaviours.count("clarify") == 2


def test_planning_is_reproducible_from_seed() -> None:
    args = (10, _schema(), load_named_fewshots("insight_fewshots"), _settings(), "mixed")
    first = [(p.few_shot_distance, p.expected_behaviour, p.analysis_type) for p in plan_items(*args, seed=7)]
    second = [(p.few_shot_distance, p.expected_behaviour, p.analysis_type) for p in plan_items(*args, seed=7)]
    assert first == second


def test_coverage_biases_toward_uncovered_analysis_types() -> None:
    """With one table, six planned items should fill six distinct analysis cells."""
    plans = plan_items(6, _schema(), [], _settings(unanswerable_ratio=0.0), "none", seed=5)
    assert len({p.analysis_type for p in plans}) == 6


# --- identifier leak detection ------------------------------------------------------


def test_identifier_leak_flags_table_and_underscore_columns() -> None:
    schema = _schema()
    assert identifier_leaks("show me credit_card_billing rows", schema) == ["credit_card_billing"]
    assert identifier_leaks("what is the transaction_amount_usd for sales", schema) == [
        "transaction_amount_usd"
    ]


def test_identifier_leak_allows_plain_english_column_words() -> None:
    """A real user says "amount" and "department" - those are not leaks."""
    schema = _schema()
    assert identifier_leaks("how much did the marketing department spend", schema) == []
    assert identifier_leaks("what was the biggest amount in that region", schema) == []


def test_leaked_identifier_marks_the_pair_invalid() -> None:
    llm = ScriptedLLM(_payload(question="what is the transaction_amount_usd for marketing"))
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.validation_status == "invalid"
    assert "leaks technical identifiers" in reference.validation_notes
    assert "transaction_amount_usd" in reference.validation_notes


# --- schema reference validation ----------------------------------------------------


def test_unknown_table_is_rejected() -> None:
    llm = ScriptedLLM(_payload(tables=["invoices"], columns=["invoices.total"]))
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.validation_status == "invalid"
    assert "unknown table 'invoices'" in reference.validation_notes


def test_unknown_column_is_rejected() -> None:
    llm = ScriptedLLM(_payload(columns=["credit_card_billing.invoice_total"]))
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.validation_status == "invalid"
    assert "unknown column" in reference.validation_notes


def test_valid_pair_is_marked_valid() -> None:
    llm = ScriptedLLM(_payload())
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    question, reference = pairs[0]
    assert reference.validation_status == "valid"
    assert reference.validation_notes is None
    assert question.reference_id == reference.id
    assert reference.question_id == question.id
    assert reference.schema_version == _schema().schema_version
    assert reference.prompt_version == "reference_generate_v1"


def test_bare_column_names_are_qualified() -> None:
    llm = ScriptedLLM(_payload(columns=["transaction_amount_usd"]))
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.columns == ["credit_card_billing.transaction_amount_usd"]
    assert reference.validation_status == "valid"


def test_must_match_typo_is_dropped_and_flagged_not_raised() -> None:
    llm = ScriptedLLM(_payload(must_match=["tables", "table"]))
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.must_match == ["tables"]
    assert reference.validation_status == "invalid"
    assert "must_match named unknown fields: table" in reference.validation_notes


# --- behaviour-aware validator interpretation ---------------------------------------


def test_refuse_item_is_valid_when_schema_cannot_answer_it() -> None:
    """For a refuse item, "not answerable" is the correct outcome, not a failure."""
    payload = _payload(question="did we get a refund for that cancelled flight",
                       expected_behaviour="refuse", tables=[], columns=[])
    verdict = {
        "answerable_with_reference": False, "single_interpretation": True, "interpretations": [],
        "is_fewshot_paraphrase": False, "behaviour_consistent": True, "reference_sound": True,
        "notes": "refunds are not in the schema",
    }
    llm = ScriptedLLM(payload, validation=verdict)
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=1.0),
    )
    _, reference = pairs[0]
    assert reference.expected_behaviour == "refuse"
    assert reference.validation_status == "valid"


def test_answer_item_rejected_when_validator_says_unanswerable() -> None:
    verdict = {
        "answerable_with_reference": False, "single_interpretation": True, "interpretations": [],
        "is_fewshot_paraphrase": False, "behaviour_consistent": True, "reference_sound": True,
        "notes": "needs data that is not present",
    }
    llm = ScriptedLLM(_payload(), validation=verdict)
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.validation_status == "invalid"
    assert "schema cannot answer" in reference.validation_notes


def test_paraphrase_flag_only_penalises_pattern_and_schema_only() -> None:
    verdict = {
        "answerable_with_reference": True, "single_interpretation": True, "interpretations": [],
        "is_fewshot_paraphrase": True, "behaviour_consistent": True, "reference_sound": True,
        "notes": "close to the example",
    }
    llm = ScriptedLLM(_payload(), validation=verdict)
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.few_shot_distance == "schema_only"
    assert reference.validation_status == "invalid"
    assert "paraphrase" in reference.validation_notes


# --- robustness ---------------------------------------------------------------------


def test_unparseable_generation_is_skipped_not_raised() -> None:
    class Broken:
        def complete(self, prompt, role, seed=None, temperature=0.7, system=None):
            return "not json at all"

    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 3, "none", Broken(), seed=1, settings=_settings(),
    )
    assert pairs == []


def test_validator_failure_marks_invalid_without_raising() -> None:
    class HalfBroken(ScriptedLLM):
        def complete(self, prompt, role, seed=None, temperature=0.7, system=None):
            if role == "reference_validator":
                raise RuntimeError("validator offline")
            return super().complete(prompt, role, seed, temperature, system)

    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), [], {}, None, 1, "none", HalfBroken(_payload()), seed=1,
        settings=_settings(unanswerable_ratio=0.0),
    )
    _, reference = pairs[0]
    assert reference.validation_status == "invalid"
    assert "validator unavailable" in reference.validation_notes


def test_schema_and_fewshots_never_reach_a_persona_field() -> None:
    """The privilege boundary: nothing schema-shaped may end up in the question."""
    fewshots = load_named_fewshots("insight_fewshots")
    llm = ScriptedLLM(_payload())
    pairs = generate_with_references(
        load_named_persona("junior_analyst"), load_named_product("insight_profile"),
        _schema(), fewshots, {}, None, 1, "none", llm, seed=1, settings=_settings(unanswerable_ratio=0.0),
    )
    question, _ = pairs[0]
    assert "SELECT" not in question.question.upper()
    assert identifier_leaks(question.question, _schema()) == []


def test_multi_table_schema_samples_related_tables() -> None:
    """Subset sampling is general, even though the mock product has one table."""
    schema = SchemaSpec(
        product_version="1.0",
        tables=[
            SchemaTable(name="orders", description="d", columns=[
                SchemaColumn(name="order_id", type="VARCHAR", description="d"),
                SchemaColumn(name="customer_id", type="VARCHAR", description="d"),
            ]),
            SchemaTable(name="customers", description="d", columns=[
                SchemaColumn(name="customer_id", type="VARCHAR", description="d"),
                SchemaColumn(name="name", type="VARCHAR", description="d"),
            ]),
            SchemaTable(name="unrelated", description="d", columns=[
                SchemaColumn(name="other", type="VARCHAR", description="d"),
            ]),
        ],
    )
    plans = plan_items(12, schema, [], _settings(), "none", seed=4)
    assert all(1 <= len(p.tables) <= 3 for p in plans)
    joined = [p for p in plans if len(p.tables) > 1]
    for plan in joined:
        assert "unrelated" not in plan.tables or plan.tables[0] == "unrelated"
