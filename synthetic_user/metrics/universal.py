from collections import Counter
from collections.abc import Callable, Iterable
from statistics import median
from typing import Any

from synthetic_user.core.models import Conversation, Outcome, ValidationResult
from synthetic_user.metrics.registry import MetricDefinition, MetricRegistry


_DIMENSIONS = ("domain_expertise", "verification_literacy", "question_complexity")


def _run_value(run: Any, name: str, default: Any = None) -> Any:
	if isinstance(run, dict):
		return run.get(name, default)
	return getattr(run, name, default)


def _lookup(run: Any, plural: str, key: str) -> Any:
	values = _run_value(run, plural, {}) or {}
	if isinstance(values, dict):
		return values.get(key)
	return next((value for value in values if getattr(value, "id", None) == key), None)


def _dimension_value(run: Any, conversation: Conversation, dimension: str) -> str:
	if dimension == "question_complexity":
		question = _lookup(run, "questions", conversation.question_id)
		return str(getattr(question, "complexity", "unknown"))
	persona = _lookup(run, "personas", conversation.persona_id)
	return str(getattr(persona, dimension, "unknown"))


def _segmented(
	run: Any,
	conversations: list[Conversation],
	compute_group: Callable[[list[Conversation]], Any],
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


def _rate(conversations: list[Conversation], predicate: Callable[[Conversation], bool]) -> float:
	return 100.0 * sum(predicate(conversation) for conversation in conversations) / len(conversations) if conversations else 0.0


def _outcome_rate(outcome: Outcome) -> Callable[[Conversation], bool]:
	return lambda conversation: conversation.outcome == outcome


def _resolution_rate(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	return _segmented(run, conversations, lambda group: _rate(group, _outcome_rate(Outcome.COMPLETED)))


def _abandonment_rate(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	return _segmented(run, conversations, lambda group: _rate(group, _outcome_rate(Outcome.ABANDONED)))


def _loop_rate(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	return _segmented(run, conversations, lambda group: _rate(group, _outcome_rate(Outcome.LOOPED)))


def _median_turns(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	def calculate(group: list[Conversation]) -> float:
		turn_counts = [len(conversation.turns) for conversation in group if conversation.outcome == Outcome.COMPLETED]
		return float(median(turn_counts)) if turn_counts else 0.0

	return _segmented(run, conversations, calculate)


def _outcome_distribution(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	def calculate(group: list[Conversation]) -> dict[str, int]:
		counts = Counter(conversation.outcome.value for conversation in group)
		return {outcome.value: counts.get(outcome.value, 0) for outcome in Outcome}

	return _segmented(run, conversations, calculate)


def _hesitation_rate(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	return _segmented(run, conversations, lambda group: _rate(group, lambda item: item.doubt_anywhere))


def _validation_map(validations: list[ValidationResult]) -> dict[str, ValidationResult]:
	return {validation.conversation_id: validation for validation in validations}


def _cleanup_rate(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	by_conversation = _validation_map(validations)
	return _segmented(
		run,
		conversations,
		lambda group: _rate(
			group,
			lambda item: by_conversation.get(item.id) is not None
			and by_conversation[item.id].cleanup_suspected,
		),
	)


def _classification_distribution(run: Any, conversations: list[Conversation], validations: list[Any]) -> dict[str, Any]:
	by_conversation = _validation_map(validations)

	def calculate(group: list[Conversation]) -> dict[str, int]:
		classes = [
			by_conversation[conversation.id].classification or "unclassified"
			for conversation in group
			if conversation.id in by_conversation
		]
		return dict(Counter(classes))

	return _segmented(run, conversations, calculate)


def register_universal_metrics(registry: MetricRegistry) -> None:
	registry.register(MetricDefinition("resolution_rate", "universal", ["outcome"], _resolution_rate))
	registry.register(MetricDefinition("abandonment_rate", "universal", ["outcome"], _abandonment_rate))
	registry.register(MetricDefinition("loop_rate", "universal", ["outcome"], _loop_rate))
	registry.register(MetricDefinition("median_turns_to_resolution", "universal", ["outcome", "turns"], _median_turns))
	registry.register(MetricDefinition("outcome_distribution", "universal", ["outcome"], _outcome_distribution))
	registry.register(
		MetricDefinition("hesitation_rate", "universal", ["doubt_anywhere"], _hesitation_rate, harness_health=True)
	)
	registry.register(
		MetricDefinition("cleanup_rate", "universal", ["cleanup_suspected"], _cleanup_rate, harness_health=True)
	)
	registry.register(
		MetricDefinition("classification_distribution", "universal", ["classification"], _classification_distribution)
	)
