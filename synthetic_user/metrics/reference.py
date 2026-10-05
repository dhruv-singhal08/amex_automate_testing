"""Reference-guided correctness metrics.

Layer "class": these describe how well the product answered, which only means
something when references and judge results exist. The registry skips them
otherwise rather than reporting zeros.

Denominator note: correct/partially_correct/incorrect are computed over SCORABLE
conversations only - those whose judge verdict was one of those three. Verdicts
of reference_suspect or not_evaluable are excluded from the denominator and
reported separately as harness health, because folding "we could not tell" into
"the product was wrong" is how a harness starts lying about the product.
"""

from collections import Counter
from collections.abc import Callable
from statistics import mean
from typing import Any

from synthetic_user.core.models import Conversation
from synthetic_user.metrics.registry import MetricDefinition, MetricRegistry

_DIMENSIONS = ("few_shot_distance", "question_complexity", "domain_expertise")
_SCORABLE = ("correct", "partially_correct", "incorrect")


def _run_value(run: Any, name: str, default: Any = None) -> Any:
	if isinstance(run, dict):
		return run.get(name, default)
	return getattr(run, name, default)


def _lookup(run: Any, plural: str, key: str) -> Any:
	values = _run_value(run, plural, {}) or {}
	if isinstance(values, dict):
		return values.get(key)
	return next((value for value in values if getattr(value, "id", None) == key), None)


def _judge_result(run: Any, conversation: Conversation) -> Any:
	results = _run_value(run, "judge_results", {}) or {}
	if isinstance(results, dict):
		return results.get(conversation.id)
	return next((item for item in results if item.conversation_id == conversation.id), None)


def _reference(run: Any, conversation: Conversation) -> Any:
	return _lookup(run, "references", conversation.question_id)


def _dimension_value(run: Any, conversation: Conversation, dimension: str) -> str:
	if dimension == "few_shot_distance":
		reference = _reference(run, conversation)
		return str(getattr(reference, "few_shot_distance", "unknown"))
	if dimension == "question_complexity":
		question = _lookup(run, "questions", conversation.question_id)
		return str(getattr(question, "complexity", "unknown"))
	persona = _lookup(run, "personas", conversation.persona_id)
	return str(getattr(persona, dimension, "unknown"))


def _segmented(
	run: Any, conversations: list[Conversation], compute_group: Callable[[list[Conversation]], Any]
) -> dict[str, Any]:
	result: dict[str, Any] = {"aggregate": compute_group(conversations)}
	for dimension in _DIMENSIONS:
		groups: dict[str, list[Conversation]] = {}
		for conversation in conversations:
			groups.setdefault(_dimension_value(run, conversation, dimension), []).append(conversation)
		result[f"by_{dimension}"] = {
			label: compute_group(group) for label, group in sorted(groups.items())
		}
	return result


def _scorable(run: Any, conversations: list[Conversation]) -> list[tuple[Conversation, Any]]:
	pairs = []
	for conversation in conversations:
		result = _judge_result(run, conversation)
		if result is not None and result.overall in _SCORABLE:
			pairs.append((conversation, result))
	return pairs


def _verdict_rate(run: Any, verdict: str) -> Callable[[list[Conversation]], float | None]:
	def compute(group: list[Conversation]) -> float | None:
		pairs = _scorable(run, group)
		if not pairs:
			return None
		return round(100.0 * sum(1 for _, r in pairs if r.overall == verdict) / len(pairs), 2)

	return compute


def _correct_rate_for(run: Any, conversations: list[Conversation], distance: str) -> float | None:
	group = [c for c in conversations if _dimension_value(run, c, "few_shot_distance") == distance]
	return _verdict_rate(run, "correct")(group)


def _judged_rate(run: Any, predicate: Callable[[Any], bool]) -> Callable[[list[Conversation]], float | None]:
	"""Rate over every conversation that has a judge result at all."""

	def compute(group: list[Conversation]) -> float | None:
		results = [r for r in (_judge_result(run, c) for c in group) if r is not None]
		if not results:
			return None
		return round(100.0 * sum(1 for r in results if predicate(r)) / len(results), 2)

	return compute


def _mean_metric(run: Any, attribute: str) -> Callable[[list[Conversation]], float | None]:
	def compute(group: list[Conversation]) -> float | None:
		values = []
		for conversation in group:
			result = _judge_result(run, conversation)
			value = getattr(result, attribute, None) if result else None
			if value is not None:
				values.append(value)
		return round(mean(values), 3) if values else None

	return compute


# --------------------------------------------------------------------------- metrics


def _correct_rate(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _verdict_rate(run, "correct"))


def _partially_correct_rate(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _verdict_rate(run, "partially_correct"))


def _incorrect_rate(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _verdict_rate(run, "incorrect"))


def _failure_tag_distribution(run, conversations, validations):
	del validations

	def compute(group: list[Conversation]) -> dict[str, int]:
		counter: Counter[str] = Counter()
		for conversation in group:
			result = _judge_result(run, conversation)
			if result is not None:
				counter.update(result.failure_tags)
		return dict(sorted(counter.items()))

	return _segmented(run, conversations, compute)


def _table_precision(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _mean_metric(run, "table_precision"))


def _table_recall(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _mean_metric(run, "table_recall"))


def _column_precision(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _mean_metric(run, "column_precision"))


def _column_recall(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _mean_metric(run, "column_recall"))


def _behaviour_accuracy(run, conversations, validations):
	"""Only meaningful on items where the product was supposed to refuse or ask."""
	del validations

	def compute(group: list[Conversation]) -> float | None:
		relevant = []
		for conversation in group:
			reference = _reference(run, conversation)
			if getattr(reference, "expected_behaviour", None) in ("refuse", "clarify"):
				result = _judge_result(run, conversation)
				if result is not None and result.behaviour_match is not None:
					relevant.append(result)
		if not relevant:
			return None
		return round(100.0 * sum(1 for r in relevant if r.behaviour_match) / len(relevant), 2)

	return _segmented(run, conversations, compute)


def _generalisation_gap(run, conversations, validations):
	"""How much worse the product does without a near example to lean on.

	A large positive gap means the near-example results are flattering: the
	product is matching a pattern it was shown rather than generalising.
	"""
	del validations
	near = _correct_rate_for(run, conversations, "near")
	schema_only = _correct_rate_for(run, conversations, "schema_only")
	return {
		"aggregate": round(near - schema_only, 2) if near is not None and schema_only is not None else None,
		"correct_rate_near": near,
		"correct_rate_schema_only": schema_only,
	}


def _reference_invalid_rate(run, conversations, validations):
	"""Share of references for this run that failed their own validation."""
	del conversations, validations
	references = _run_value(run, "references", {}) or {}
	items = list(references.values()) if isinstance(references, dict) else list(references)
	if not items:
		return {"aggregate": None}
	invalid = sum(1 for item in items if getattr(item, "validation_status", None) == "invalid")
	return {"aggregate": round(100.0 * invalid / len(items), 2), "reference_count": len(items)}


def _reference_suspect_rate(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _judged_rate(run, lambda r: r.overall == "reference_suspect"))


def _not_evaluable_rate(run, conversations, validations):
	del validations
	return _segmented(run, conversations, _judged_rate(run, lambda r: r.overall == "not_evaluable"))


def register_reference_metrics(registry: MetricRegistry) -> None:
	requires = ["reference", "judge_result"]
	registry.register(MetricDefinition("correct_rate", "class", requires, _correct_rate))
	registry.register(MetricDefinition("partially_correct_rate", "class", requires, _partially_correct_rate))
	registry.register(MetricDefinition("incorrect_rate", "class", requires, _incorrect_rate))
	registry.register(MetricDefinition("failure_tag_distribution", "class", requires, _failure_tag_distribution))
	registry.register(MetricDefinition("table_precision", "class", requires, _table_precision))
	registry.register(MetricDefinition("table_recall", "class", requires, _table_recall))
	registry.register(MetricDefinition("column_precision", "class", requires, _column_precision))
	registry.register(MetricDefinition("column_recall", "class", requires, _column_recall))
	registry.register(MetricDefinition("behaviour_accuracy", "class", requires, _behaviour_accuracy))
	registry.register(MetricDefinition("generalisation_gap", "class", requires, _generalisation_gap))
	registry.register(
		MetricDefinition("reference_invalid_rate", "class", requires, _reference_invalid_rate, harness_health=True)
	)
	registry.register(
		MetricDefinition("reference_suspect_rate", "class", requires, _reference_suspect_rate, harness_health=True)
	)
	registry.register(
		MetricDefinition("not_evaluable_rate", "class", requires, _not_evaluable_rate, harness_health=True)
	)
