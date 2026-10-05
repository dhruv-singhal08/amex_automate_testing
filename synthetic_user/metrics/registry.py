from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal


MetricCompute = Callable[[Any, list[Any], list[Any]], float | dict[str, Any]]


@dataclass(frozen=True)
class MetricDefinition:
	name: str
	layer: Literal["universal", "class", "instance"]
	requires: list[str]
	compute: MetricCompute
	harness_health: bool = False


class MetricRegistry:
	def __init__(self) -> None:
		self._metrics: dict[str, MetricDefinition] = {}

	def register(self, metric: MetricDefinition) -> MetricDefinition:
		if metric.name in self._metrics:
			raise ValueError(f"Metric already registered: {metric.name}")
		self._metrics[metric.name] = metric
		return metric

	def get(self, name: str) -> MetricDefinition:
		return self._metrics[name]

	def definitions(self) -> tuple[MetricDefinition, ...]:
		return tuple(self._metrics.values())

	def compute(
		self,
		run: Any,
		conversations: Iterable[Any],
		validations: Iterable[Any],
		artifacts: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		conversation_list = list(conversations)
		validation_list = list(validations)
		available = _available_artifacts(run, conversation_list, validation_list, artifacts)
		metrics: dict[str, Any] = {}
		skipped: dict[str, str] = {}
		for metric in self._metrics.values():
			missing = [requirement for requirement in metric.requires if requirement not in available]
			if missing:
				skipped[metric.name] = f"missing required artifacts: {', '.join(missing)}"
				continue
			try:
				computed = metric.compute(run, conversation_list, validation_list)
				metrics[metric.name] = (
					{"label": "HARNESS HEALTH", "values": computed}
					if metric.harness_health
					else computed
				)
			except Exception as error:
				skipped[metric.name] = f"metric computation failed: {error}"
		return {"metrics": metrics, "skipped": skipped}


def _available_artifacts(
	run: Any,
	conversations: list[Any],
	validations: list[Any],
	artifacts: dict[str, Any] | None,
) -> set[str]:
	available = set((artifacts or {}).keys())
	if isinstance(run, dict):
		available.update(run.get("artifacts", {}).keys())
	else:
		available.update(getattr(run, "artifacts", {}).keys())
	if conversations:
		available.update({"outcome", "turns", "doubt_anywhere"})
	if validations:
		available.update({"classification", "cleanup_suspected"})
	return available


default_registry = MetricRegistry()


def compute_metrics(
	run: Any,
	conversations: Iterable[Any],
	validations: Iterable[Any],
	artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
	from synthetic_user.metrics.reference import register_reference_metrics
	from synthetic_user.metrics.universal import register_universal_metrics

	if not default_registry.definitions():
		register_universal_metrics(default_registry)
		register_reference_metrics(default_registry)
	return default_registry.compute(run, conversations, validations, artifacts)
