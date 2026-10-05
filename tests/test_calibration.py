import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from synthetic_user.core.models import (
    Conversation,
    GeneratedQuestion,
    JudgeResult,
    Outcome,
    QuestionReference,
    Turn,
    AdapterResponse,
)
from synthetic_user.evaluation.calibration import (
    UNCALIBRATED_BANNER,
    calibration_status,
    cohens_kappa,
    export_calibration,
    import_calibration,
)
from synthetic_user.metrics.registry import compute_metrics


# --- Cohen's kappa on known fixtures -------------------------------------------------


def test_kappa_is_one_for_perfect_agreement() -> None:
    pairs = [("correct", "correct"), ("incorrect", "incorrect"), ("correct", "correct")]
    assert cohens_kappa(pairs) == 1.0


def test_kappa_is_zero_for_chance_level_agreement() -> None:
    """Worked fixture: 2x2, 50/50 marginals both sides, observed agreement 0.5.

    po = 0.5, pe = (0.5*0.5) + (0.5*0.5) = 0.5, kappa = (0.5-0.5)/(1-0.5) = 0.
    """
    pairs = [
        ("correct", "correct"),
        ("correct", "incorrect"),
        ("incorrect", "correct"),
        ("incorrect", "incorrect"),
    ]
    assert cohens_kappa(pairs) == 0.0


def test_kappa_matches_hand_computed_value() -> None:
    """Classic 2x2 fixture: a=20, b=5, c=10, d=15, N=50.

    po = (20+15)/50 = 0.7
    judge marginals: correct 25, incorrect 25; human: correct 30, incorrect 20
    pe = (25/50)(30/50) + (25/50)(20/50) = 0.30 + 0.20 = 0.5
    kappa = (0.7 - 0.5) / (1 - 0.5) = 0.4
    """
    pairs = (
        [("correct", "correct")] * 20
        + [("correct", "incorrect")] * 5
        + [("incorrect", "correct")] * 10
        + [("incorrect", "incorrect")] * 15
    )
    assert cohens_kappa(pairs) == pytest.approx(0.4, abs=1e-9)


def test_kappa_is_negative_when_worse_than_chance() -> None:
    pairs = [("correct", "incorrect")] * 5 + [("incorrect", "correct")] * 5
    kappa = cohens_kappa(pairs)
    assert kappa is not None and kappa < 0


def test_kappa_handles_degenerate_and_empty_input() -> None:
    # Both raters used one category throughout: chance agreement is 1, kappa undefined.
    assert cohens_kappa([("correct", "correct")] * 10) == 1.0
    assert cohens_kappa([("correct", "incorrect")] * 10) == 0.0
    assert cohens_kappa([]) is None


# --- uncalibrated banner --------------------------------------------------------------


def _fresh_db(tmp_path: Path) -> None:
    from synthetic_user.core import storage

    storage.init_db(f"sqlite:///{tmp_path / 'cal.db'}")


def test_banner_shown_when_no_labels_exist_for_this_prompt_version(tmp_path) -> None:
    _fresh_db(tmp_path)
    status = calibration_status("judge_sql_v1")

    assert status["calibrated"] is False
    assert status["label_count"] == 0
    assert status["banner"] == UNCALIBRATED_BANNER
    assert "provisional" in status["banner"]


def test_banner_clears_once_labels_exist_for_that_version(tmp_path) -> None:
    _fresh_db(tmp_path)
    csv_path = tmp_path / "labels.csv"
    _write_labels(
        csv_path,
        [
            ("c1", "correct", "correct", "yes", "judge_sql_v1"),
            ("c2", "incorrect", "incorrect", "yes", "judge_sql_v1"),
        ],
    )
    import_calibration(csv_path)

    status = calibration_status("judge_sql_v1")
    assert status["calibrated"] is True
    assert status["label_count"] == 2
    assert status["banner"] is None


def test_labels_do_not_carry_over_to_a_new_judge_prompt_version(tmp_path) -> None:
    """Changing the prompt changes what was measured, so calibration resets."""
    _fresh_db(tmp_path)
    csv_path = tmp_path / "labels.csv"
    _write_labels(csv_path, [("c1", "correct", "correct", "yes", "judge_sql_v1")])
    import_calibration(csv_path)

    assert calibration_status("judge_sql_v1")["calibrated"] is True
    assert calibration_status("judge_sql_v2")["calibrated"] is False
    assert calibration_status("judge_sql_v2")["banner"] == UNCALIBRATED_BANNER


def _write_labels(path: Path, rows) -> None:
    from synthetic_user.evaluation.calibration import CSV_COLUMNS

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for conversation_id, judge_overall, human_overall, reference_ok, version in rows:
            writer.writerow(
                {
                    "conversation_id": conversation_id,
                    "question": "how much did marketing spend",
                    "judge_overall": judge_overall,
                    "judge_prompt_version": version,
                    "human_overall": human_overall,
                    "human_reference_ok": reference_ok,
                    "human_notes": "",
                    "reviewer": "dhruv",
                }
            )


# --- import reporting ------------------------------------------------------------------


def test_import_reports_accuracy_kappa_and_disagreements(tmp_path) -> None:
    _fresh_db(tmp_path)
    csv_path = tmp_path / "labels.csv"
    _write_labels(
        csv_path,
        [
            ("c1", "correct", "correct", "yes", "judge_sql_v1"),
            ("c2", "correct", "incorrect", "no", "judge_sql_v1"),
            ("c3", "incorrect", "incorrect", "yes", "judge_sql_v1"),
            ("c4", "incorrect", "incorrect", "yes", "judge_sql_v1"),
        ],
    )
    report = import_calibration(csv_path)

    assert report["labels_stored"] == 4
    assert report["accuracy"] == 75.0
    assert report["cohens_kappa"] is not None
    assert report["reference_ok_rate"] == 75.0
    assert len(report["disagreements"]) == 1
    assert report["disagreements"][0]["conversation_id"] == "c2"
    assert report["confusion_matrix"]["correct"]["incorrect"] == 1


def test_import_skips_unlabelled_and_invalid_rows(tmp_path) -> None:
    _fresh_db(tmp_path)
    from synthetic_user.evaluation.calibration import CSV_COLUMNS

    csv_path = tmp_path / "labels.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({"conversation_id": "c1", "judge_overall": "correct", "human_overall": ""})
        writer.writerow({"conversation_id": "c2", "judge_overall": "correct", "human_overall": "sort_of"})
        writer.writerow({"conversation_id": "c3", "judge_overall": "correct", "human_overall": "correct"})

    report = import_calibration(csv_path)
    assert report["labels_stored"] == 1
    assert any("sort_of" in note for note in report["skipped"])


# --- export ----------------------------------------------------------------------------


def _seed_run(tmp_path: Path, run_id: str = "run-cal") -> None:
    from synthetic_user.core import storage

    storage.init_db(f"sqlite:///{tmp_path / 'cal.db'}")
    for index, (verdict, distance) in enumerate(
        [("correct", "near"), ("incorrect", "schema_only"), ("partially_correct", "pattern"),
         ("correct", "schema_only")]
    ):
        question_id = f"question-{index}"
        storage.save_question(
            GeneratedQuestion(
                id=question_id, question=f"q{index}", intent="i", complexity="simple",
                persona_trait="t", expected_response_type="data", reference_id=f"ref-{index}",
            ),
            run_id,
        )
        storage.save_reference(
            QuestionReference(
                id=f"ref-{index}", question_id=question_id, expected_behaviour="answer",
                expectation=f"expectation {index}", tables=["credit_card_billing"],
                columns=["credit_card_billing.transaction_amount_usd"], filters=["department = X"],
                aggregation="sum", grain="one row", time_window=None, analysis_type="aggregate",
                expected_output_columns=["total"], must_match=["tables"], acceptable_alternatives=[],
                few_shot_ids=[], few_shot_distance=distance, schema_version="v", prompt_version="p",
                model="m", validation_status="valid",
            ),
            run_id,
        )
        conversation = Conversation(
            id=f"conv-{index}", question_id=question_id, repeat_index=0, seed=1,
            persona_id="junior_analyst", persona_version=2,
            turns=[Turn(turn_index=0, message=f"q{index}",
                        response=AdapterResponse(reply="an answer", latency_ms=1), decision=None,
                        evaluable={"kind": "sql", "query": "SELECT 1 FROM credit_card_billing"})],
            outcome=Outcome.COMPLETED, doubt_anywhere=False, closing_summary=None,
        )
        storage.save_conversation(conversation, run_id)
        storage.save_judge_result(
            JudgeResult(
                conversation_id=f"conv-{index}", question_id=question_id, parse_ok=True,
                overall=verdict, field_verdicts={"tables": "match"}, behaviour_match=True,
                reason="because", judge_model="m", prompt_version="judge_sql_v1",
            ),
            run_id,
        )


def test_export_writes_blank_reviewer_columns_and_readable_reference(tmp_path) -> None:
    _seed_run(tmp_path)
    path = export_calibration("run-cal", n=10, out_path=tmp_path / "sheet.csv")

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 4
    for row in rows:
        assert row["human_overall"] == ""
        assert row["human_reference_ok"] == ""
        assert row["reviewer"] == ""
        assert row["judge_overall"]
        assert "expects:" in row["reference"]  # readable, not raw JSON
        assert "SELECT" in row["final_sql"]


def test_export_stratifies_across_verdicts_rather_than_taking_the_first_n(tmp_path) -> None:
    _seed_run(tmp_path)
    path = export_calibration("run-cal", n=3, out_path=tmp_path / "sheet.csv")

    with path.open(newline="", encoding="utf-8") as handle:
        verdicts = {row["judge_overall"] for row in csv.DictReader(handle)}

    assert len(verdicts) == 3  # one from each verdict bucket, not three of the same


# --- metrics banner wiring ---------------------------------------------------------------


def test_reference_metrics_are_skipped_without_references() -> None:
    run = SimpleNamespace(personas={}, questions={}, references={}, judge_results={}, artifacts={})
    report = compute_metrics(run, [], [])
    assert "correct_rate" in report["skipped"]
