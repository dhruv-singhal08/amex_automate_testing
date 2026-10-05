from datetime import datetime
from types import SimpleNamespace

from synthetic_user.assets.registry import load_named_persona
from synthetic_user.core.models import (
    AdapterResponse,
    Conversation,
    GeneratedQuestion,
    Outcome,
    Turn,
    ValidationResult,
)
from synthetic_user.metrics.registry import MetricDefinition, MetricRegistry, compute_metrics


def conversation(
    identifier: str,
    persona_id: str,
    question_id: str,
    outcome: Outcome,
    turn_count: int,
    doubt: bool,
) -> Conversation:
    return Conversation(
        id=identifier,
        question_id=question_id,
        repeat_index=0,
        seed=1,
        persona_id=persona_id,
        persona_version=1,
        turns=[
            Turn(
                turn_index=index,
                message=f"Question {index}",
                response=AdapterResponse(reply="A sufficiently detailed response.", latency_ms=1),
                decision=None,
            )
            for index in range(turn_count)
        ],
        outcome=outcome,
        doubt_anywhere=doubt,
        closing_summary=None,
    )


def synthetic_run() -> tuple[SimpleNamespace, list[Conversation], list[ValidationResult]]:
    junior = load_named_persona("junior_analyst")
    controller = load_named_persona("finance_controller")
    questions = {
        "simple": GeneratedQuestion(
            id="simple",
            question="How much did we bill?",
            intent="total billing",
            complexity="simple",
            persona_trait="casual",
            expected_response_type="data",
        ),
        "hard": GeneratedQuestion(
            id="hard",
            question="Provide a period reconciliation.",
            intent="reconcile billing",
            complexity="hard",
            persona_trait="precise",
            expected_response_type="explanation",
        ),
    }
    conversations = [
        conversation("c1", junior.id, "simple", Outcome.COMPLETED, 1, False),
        conversation("c2", junior.id, "hard", Outcome.ABANDONED, 2, True),
        conversation("c3", controller.id, "simple", Outcome.LOOPED, 2, False),
        conversation("c4", controller.id, "hard", Outcome.COMPLETED, 3, False),
    ]
    validations = [
        ValidationResult(
            conversation_id="c1",
            classification="handled_correctly",
            refused_clearly=None,
            cleanup_suspected=False,
            deterministic_flags=[],
            reason="Handled.",
        ),
        ValidationResult(
            conversation_id="c2",
            classification="too_generic",
            refused_clearly=False,
            cleanup_suspected=True,
            deterministic_flags=[],
            reason="Generic.",
        ),
        ValidationResult(
            conversation_id="c3",
            classification="capability_failure",
            refused_clearly=False,
            cleanup_suspected=False,
            deterministic_flags=[],
            reason="Failure.",
        ),
        ValidationResult(
            conversation_id="c4",
            classification="handled_correctly",
            refused_clearly=None,
            cleanup_suspected=False,
            deterministic_flags=[],
            reason="Handled.",
        ),
    ]
    run = SimpleNamespace(
        personas={junior.id: junior, controller.id: controller},
        questions=questions,
        artifacts={},
        started_at=datetime.now(),
    )
    return run, conversations, validations


def test_universal_metrics_are_aggregated_and_segmented() -> None:
    run, conversations, validations = synthetic_run()
    report = compute_metrics(run, conversations, validations)

    # A run without references skips the reference-guided metrics and says why,
    # rather than reporting zeros that would read as "the product got it wrong".
    assert set(report["skipped"]) == {
        "correct_rate",
        "partially_correct_rate",
        "incorrect_rate",
        "failure_tag_distribution",
        "table_precision",
        "table_recall",
        "column_precision",
        "column_recall",
        "behaviour_accuracy",
        "generalisation_gap",
        "reference_invalid_rate",
        "reference_suspect_rate",
        "not_evaluable_rate",
    }
    assert all("missing required artifacts" in reason for reason in report["skipped"].values())
    assert report["metrics"]["resolution_rate"]["aggregate"] == 50.0
    assert report["metrics"]["abandonment_rate"]["aggregate"] == 25.0
    assert report["metrics"]["loop_rate"]["aggregate"] == 25.0
    assert report["metrics"]["median_turns_to_resolution"]["aggregate"] == 2.0
    assert report["metrics"]["outcome_distribution"]["aggregate"]["COMPLETED"] == 2
    assert report["metrics"]["hesitation_rate"]["label"] == "HARNESS HEALTH"
    assert report["metrics"]["hesitation_rate"]["values"]["aggregate"] == 25.0
    assert report["metrics"]["cleanup_rate"]["label"] == "HARNESS HEALTH"
    assert report["metrics"]["cleanup_rate"]["values"]["aggregate"] == 25.0
    assert report["metrics"]["classification_distribution"]["aggregate"] == {
        "handled_correctly": 2,
        "too_generic": 1,
        "capability_failure": 1,
    }

    for metric in report["metrics"].values():
        values = metric["values"] if "values" in metric else metric
        assert "by_domain_expertise" in values
        assert "by_verification_literacy" in values
        assert "by_question_complexity" in values


def test_registry_skips_metrics_without_required_artifacts() -> None:
    registry = MetricRegistry()
    registry.register(
        MetricDefinition(
            name="product_specific",
            layer="class",
            requires=["product_artifact"],
            compute=lambda run, conversations, validations: 1.0,
        )
    )
    run, conversations, validations = synthetic_run()

    report = registry.compute(run, conversations, validations)

    assert report["metrics"] == {}
    assert report["skipped"] == {"product_specific": "missing required artifacts: product_artifact"}

    report = registry.compute(run, conversations, validations, artifacts={"product_artifact": True})
    assert report["metrics"] == {"product_specific": 1.0}
