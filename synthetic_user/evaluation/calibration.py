"""Measuring the judge against human labels.

Without this the judge is unfalsifiable: it produces verdicts nobody can check,
and a systematic bias in it looks exactly like a product regression. Calibration
is per judge prompt version, because changing the prompt invalidates what was
measured about the previous one.
"""

import csv
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select

from synthetic_user.core.storage import (
	_get_engine,
	calibration_label_table,
	get_conversations,
	get_judge_results,
	get_questions,
	get_references,
)

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
	"conversation_id",
	"question",
	"reference",
	"final_sql",
	"final_reply",
	"field_verdicts",
	"judge_overall",
	"judge_reason",
	"judge_prompt_version",
	# Blank for the reviewer to fill in.
	"human_overall",
	"human_reference_ok",
	"human_notes",
	"reviewer",
]

OVERALL_VALUES = ("correct", "partially_correct", "incorrect", "reference_suspect", "not_evaluable")


def _readable_reference(reference: Any) -> str:
	if reference is None:
		return ""
	parts = [
		f"expects: {reference.expectation}",
		f"behaviour: {reference.expected_behaviour}",
		f"tables: {', '.join(reference.tables) or '-'}",
		f"columns: {', '.join(c.split('.')[-1] for c in reference.columns) or '-'}",
		f"filters: {'; '.join(reference.filters) or '-'}",
		f"aggregation: {reference.aggregation or '-'}",
		f"grain: {reference.grain or '-'}",
		f"time window: {reference.time_window or '-'}",
		f"must match: {', '.join(reference.must_match) or '-'}",
		f"also acceptable: {'; '.join(reference.acceptable_alternatives) or '-'}",
	]
	return "\n".join(parts)


def _final_sql(conversation: Any) -> str:
	for turn in reversed(conversation.turns):
		evaluable = turn.evaluable or {}
		if evaluable.get("query"):
			return str(evaluable["query"])
	return ""


def _stratified(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
	"""Sample across judge verdicts and few-shot distance.

	Verdict-major on purpose: one row per verdict per round, rotating through
	that verdict's distance groups. Sampling the joint cells directly would let
	two common cells crowd a rare verdict out of a small sheet - and a rare
	verdict is exactly where the judge is most likely to be wrong, so it is the
	last thing a reviewer should lose.
	"""
	cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
	for row in rows:
		cells[(row["_overall"], row["_distance"])].append(row)

	by_verdict: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
	for (overall, _distance), items in sorted(cells.items()):
		by_verdict[overall].append(list(items))

	sampled: list[dict[str, Any]] = []
	while len(sampled) < n:
		progressed = False
		for verdict in sorted(by_verdict):
			if len(sampled) >= n:
				break
			groups = by_verdict[verdict]
			for position, group in enumerate(groups):
				if group:
					sampled.append(group.pop(0))
					# Rotate so the next round draws a different distance.
					by_verdict[verdict] = groups[position + 1 :] + groups[: position + 1]
					progressed = True
					break
		if not progressed:
			break
	return sampled


def export_calibration(run_id: str, n: int = 60, out_path: str | Path | None = None) -> Path:
	"""Write a review sheet for a run, stratified across judge verdicts."""
	conversations = {c.id: c for c in get_conversations(run_id)}
	questions = {q.id: q for q in get_questions(run_id)}
	references = {r.question_id: r for r in get_references(run_id)}
	results = get_judge_results(run_id)

	rows: list[dict[str, Any]] = []
	for result in results:
		conversation = conversations.get(result.conversation_id)
		if conversation is None:
			continue
		question = questions.get(result.question_id)
		reference = references.get(result.question_id)
		rows.append(
			{
				"conversation_id": result.conversation_id,
				"question": question.question if question else (
					conversation.turns[0].message if conversation.turns else ""
				),
				"reference": _readable_reference(reference),
				"final_sql": _final_sql(conversation),
				"final_reply": conversation.turns[-1].response.reply if conversation.turns else "",
				"field_verdicts": json.dumps(result.field_verdicts),
				"judge_overall": result.overall,
				"judge_reason": result.reason,
				"judge_prompt_version": result.prompt_version,
				"human_overall": "",
				"human_reference_ok": "",
				"human_notes": "",
				"reviewer": "",
				"_overall": result.overall,
				"_distance": getattr(reference, "few_shot_distance", "unknown"),
			}
		)

	sampled = _stratified(rows, n)
	path = Path(out_path) if out_path else Path("runs") / f"calibration_{run_id}.csv"
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("w", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
		writer.writeheader()
		writer.writerows(sampled)
	return path


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
	"""Cohen's kappa for two raters over the same items.

	kappa = (observed agreement - chance agreement) / (1 - chance agreement)

	Returns None when there is nothing to measure. When chance agreement is 1.0
	(both raters used a single category throughout) kappa is undefined; we report
	1.0 for total agreement and 0.0 otherwise, which is the usual convention.
	"""
	if not pairs:
		return None
	total = len(pairs)
	observed = sum(1 for left, right in pairs if left == right) / total

	left_counts = Counter(left for left, _ in pairs)
	right_counts = Counter(right for _, right in pairs)
	categories = set(left_counts) | set(right_counts)
	chance = sum((left_counts[c] / total) * (right_counts[c] / total) for c in categories)

	if chance >= 1.0:
		return 1.0 if observed >= 1.0 else 0.0
	return round((observed - chance) / (1 - chance), 4)


def import_calibration(csv_path: str | Path) -> dict[str, Any]:
	"""Store human labels and report how well the judge matched them."""
	path = Path(csv_path)
	with path.open(newline="", encoding="utf-8") as handle:
		rows = [row for row in csv.DictReader(handle) if (row.get("human_overall") or "").strip()]

	pairs: list[tuple[str, str]] = []
	reference_ok: list[bool] = []
	disagreements: list[dict[str, str]] = []
	stored = 0
	skipped: list[str] = []

	for row in rows:
		human = (row.get("human_overall") or "").strip()
		if human not in OVERALL_VALUES:
			skipped.append(f"{row.get('conversation_id')}: unknown human_overall {human!r}")
			continue
		judge_overall = (row.get("judge_overall") or "").strip()
		pairs.append((judge_overall, human))

		ok_raw = (row.get("human_reference_ok") or "").strip().lower()
		is_ok = ok_raw in ("yes", "y", "true", "1") if ok_raw else None
		if is_ok is not None:
			reference_ok.append(is_ok)

		if judge_overall != human:
			disagreements.append(
				{
					"conversation_id": row.get("conversation_id", ""),
					"judge": judge_overall,
					"human": human,
					"question": row.get("question", ""),
					"human_notes": row.get("human_notes", ""),
				}
			)

		_store_label(row, human, is_ok)
		stored += 1

	matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
	for judge_overall, human in pairs:
		matrix[judge_overall][human] += 1

	return {
		"labels_stored": stored,
		"accuracy": round(100.0 * sum(1 for j, h in pairs if j == h) / len(pairs), 2) if pairs else None,
		"cohens_kappa": cohens_kappa(pairs),
		"confusion_matrix": {judge: dict(humans) for judge, humans in sorted(matrix.items())},
		"reference_ok_rate": (
			round(100.0 * sum(reference_ok) / len(reference_ok), 2) if reference_ok else None
		),
		"disagreements": disagreements,
		"skipped": skipped,
	}


def _store_label(row: dict[str, str], human_overall: str, reference_ok: bool | None) -> None:
	values = {
		"conversation_id": row.get("conversation_id", ""),
		"question_id": row.get("question_id") or None,
		"reference_id": row.get("reference_id") or None,
		"human_overall": human_overall,
		"human_behaviour_match": None,
		"reference_is_sound": None if reference_ok is None else int(reference_ok),
		"notes": row.get("human_notes") or None,
		"labelled_by": row.get("reviewer") or None,
		"labelled_at": datetime.now(timezone.utc),
		"judge_prompt_version": row.get("judge_prompt_version") or None,
	}
	from sqlalchemy import delete

	with _get_engine().begin() as connection:
		connection.execute(
			delete(calibration_label_table).where(
				calibration_label_table.c.conversation_id == values["conversation_id"]
			)
		)
		connection.execute(insert(calibration_label_table).values(**values))


UNCALIBRATED_BANNER = (
	"Judge not calibrated - correctness metrics are provisional."
)


def calibration_status(judge_prompt_version: str) -> dict[str, Any]:
	"""Whether human labels exist for THIS judge prompt version.

	Labels for an older prompt version do not carry over: changing the prompt
	changes the thing being measured.
	"""
	try:
		with _get_engine().connect() as connection:
			count = connection.execute(
				select(calibration_label_table).where(
					calibration_label_table.c.judge_prompt_version == judge_prompt_version
				)
			).mappings().all()
	except Exception as error:  # noqa: BLE001 - never block metrics on this
		logger.warning("Could not read calibration labels: %s", error)
		count = []

	return {
		"calibrated": bool(count),
		"label_count": len(count),
		"judge_prompt_version": judge_prompt_version,
		"banner": None if count else UNCALIBRATED_BANNER,
	}
