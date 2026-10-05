"""Metamorphic consistency checks.

These need neither execution nor ground truth. Instead of asking "is this answer
right?", they ask "does the product answer the SAME question the same way?" - a
question you can settle by comparing two queries to each other.

That makes this the cheapest signal in the platform: no reference has to be
correct for an inconsistency to be real. If a paraphrase changes which tables
the product reads, something is wrong regardless of what any reference says.

Nothing here executes SQL.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from synthetic_user.adapters.base import evaluable_from
from synthetic_user.core.models import Persona, QuestionReference, SchemaSpec
from synthetic_user.core.prompts import load_prompt
from synthetic_user.evaluation.sql_profile import parse_sql

logger = logging.getLogger(__name__)

PROMPT_VERSION = "metamorphic_variants_v1"

RELATIONS = ("paraphrase", "irrelevant_detail", "time_shift")

# Column types that carry a date. Detection is by TYPE, not by column name, so a
# schema that names something "month_key" is still handled correctly.
_DATE_TYPES = ("DATE", "TIMESTAMP", "DATETIME", "TIME")

# Date arithmetic is a date predicate even when the column type does not say so.
_DATE_FUNCTIONS = ("current_date", "date_trunc", "interval", "now()", "datediff", "dateadd")

Status = Literal["consistent", "inconsistent", "unparseable", "no_sql"]

# Structural facets compared between the original query and a variant's.
_COMPARED = ("tables", "columns", "group_by", "aggregates")


@dataclass
class VariantResult:
	question_id: str
	relation: str
	original_question: str
	variant_question: str
	status: Status
	differences: dict[str, Any] = field(default_factory=dict)
	sql: str = ""


def date_columns(schema: SchemaSpec) -> set[str]:
	"""Qualified names of every date-typed column in the schema."""
	return {
		f"{table.name}.{column.name}"
		for table in schema.tables
		for column in table.columns
		if any(date_type in column.type.upper() for date_type in _DATE_TYPES)
	}


def is_date_predicate(predicate: str, known_date_columns: set[str]) -> bool:
	lowered = predicate.lower()
	for qualified in known_date_columns:
		if qualified.lower() in lowered or qualified.split(".")[-1].lower() in lowered:
			return True
	return any(function in lowered for function in _DATE_FUNCTIONS)


def split_predicates(
	predicates: list[str], known_date_columns: set[str]
) -> tuple[set[str], set[str]]:
	"""(non-date, date) - the split every relation is compared across."""
	date_side = {p for p in predicates if is_date_predicate(p, known_date_columns)}
	return set(predicates) - date_side, date_side


def compare_structures(
	original: dict[str, Any],
	variant: dict[str, Any],
	schema: SchemaSpec,
	relation: str,
) -> tuple[Status, dict[str, Any]]:
	"""Compare a variant's query against the original's.

	Date predicates are excluded from the comparison for every relation: a
	paraphrase should not change them, but an equivalent rewrite of a date range
	is not something structural comparison can adjudicate. For time_shift they
	are expected to change, and a variant where they did NOT change is reported
	so a limp variant is not mistaken for a passing product.
	"""
	if not variant["parse_ok"]:
		return "unparseable", {"reason": "variant SQL could not be parsed"}
	if not original["parse_ok"]:
		return "unparseable", {"reason": "original SQL could not be parsed"}

	known_date_columns = date_columns(schema)
	differences: dict[str, Any] = {}

	for facet in _COMPARED:
		before, after = set(original[facet]), set(variant[facet])
		if before != after:
			differences[facet] = {
				"missing": sorted(before - after),
				"unexpected": sorted(after - before),
			}

	original_other, original_dates = split_predicates(original["where_predicates"], known_date_columns)
	variant_other, variant_dates = split_predicates(variant["where_predicates"], known_date_columns)
	if original_other != variant_other:
		differences["non_date_predicates"] = {
			"missing": sorted(original_other - variant_other),
			"unexpected": sorted(variant_other - original_other),
		}

	if relation == "time_shift" and original_dates == variant_dates:
		# Not a product failure: the variant never actually moved the period.
		differences["unchanged_time_window"] = True
		return "consistent", differences

	return ("inconsistent" if differences else "consistent"), differences


def generate_variants(persona: Persona, question: str, llm: Any, seed: int) -> dict[str, str]:
	"""Three rewrites in the persona's voice. Returns {} on failure; never raises."""
	try:
		prompt = load_prompt("metamorphic_variants_v1.md")
		prompt = prompt.replace("<<PERSONA_YAML>>", _persona_yaml(persona))
		prompt = prompt.replace("<<QUESTION>>", question)
		raw = llm.complete(prompt=prompt, role="reference_generation", seed=seed)
		data = json.loads(_strip_json_fences(raw))
	except Exception as error:  # noqa: BLE001
		logger.warning("Could not generate metamorphic variants: %s", error)
		return {}

	if not isinstance(data, dict):
		return {}
	return {
		relation: str(data[relation]).strip()
		for relation in RELATIONS
		if isinstance(data.get(relation), str) and data[relation].strip()
	}


def _persona_yaml(persona: Persona) -> str:
	import yaml

	return yaml.safe_dump(persona.character_sheet(), sort_keys=False)


def _strip_json_fences(response: str) -> str:
	text = response.strip()
	if text.startswith("```"):
		text = "\n".join(text.splitlines()[1:])
	if text.endswith("```"):
		text = "\n".join(text.splitlines()[:-1])
	return text.strip()


def _ask_product(adapter: Any, message: str) -> str:
	"""One turn, no persona loop. Returns the SQL the product produced, if any."""
	session = None
	try:
		session = adapter.open_session()
		response = adapter.send(session, message)
		evaluable = evaluable_from(adapter, response) or {}
		return str(evaluable.get("query") or "")
	except Exception as error:  # noqa: BLE001
		logger.warning("Adapter failed on a metamorphic variant: %s", error)
		return ""
	finally:
		if session is not None:
			try:
				adapter.close_session(session)
			except Exception as error:  # noqa: BLE001
				logger.warning("Adapter close failed: %s", error)


def check_question(
	persona: Persona,
	question_id: str,
	question_text: str,
	original_sql: str,
	adapter: Any,
	llm: Any,
	schema: SchemaSpec,
	seed: int = 0,
	dialect: str = "duckdb",
) -> list[VariantResult]:
	"""Probe one question with all three relations."""
	original = parse_sql(original_sql, dialect)
	variants = generate_variants(persona, question_text, llm, seed)

	results: list[VariantResult] = []
	for relation in RELATIONS:
		variant_question = variants.get(relation)
		if not variant_question:
			results.append(
				VariantResult(
					question_id=question_id,
					relation=relation,
					original_question=question_text,
					variant_question="",
					status="no_sql",
					differences={"reason": "variant could not be generated"},
				)
			)
			continue

		variant_sql = _ask_product(adapter, variant_question)
		if not variant_sql:
			results.append(
				VariantResult(
					question_id=question_id,
					relation=relation,
					original_question=question_text,
					variant_question=variant_question,
					status="no_sql",
					differences={"reason": "product produced no query"},
				)
			)
			continue

		status, differences = compare_structures(
			original, parse_sql(variant_sql, dialect), schema, relation
		)
		results.append(
			VariantResult(
				question_id=question_id,
				relation=relation,
				original_question=question_text,
				variant_question=variant_question,
				status=status,
				differences=differences,
				sql=variant_sql,
			)
		)
	return results


def consistency_rates(
	results: list[VariantResult], references: dict[str, QuestionReference] | None = None
) -> dict[str, Any]:
	"""consistency_rate per relation, segmented by few_shot_distance.

	Denominator is comparable probes only: unparseable and no_sql are excluded
	and counted separately, because "we could not compare" is not the same as
	"the product was inconsistent".
	"""
	references = references or {}

	def rate(items: list[VariantResult]) -> dict[str, Any]:
		comparable = [item for item in items if item.status in ("consistent", "inconsistent")]
		return {
			"consistency_rate": (
				round(100.0 * sum(1 for i in comparable if i.status == "consistent") / len(comparable), 2)
				if comparable
				else None
			),
			"comparable": len(comparable),
			"unparseable": sum(1 for i in items if i.status == "unparseable"),
			"no_sql": sum(1 for i in items if i.status == "no_sql"),
		}

	by_relation: dict[str, list[VariantResult]] = defaultdict(list)
	for result in results:
		by_relation[result.relation].append(result)

	report: dict[str, Any] = {"overall": rate(results)}
	for relation in RELATIONS:
		items = by_relation.get(relation, [])
		segmented: dict[str, Any] = {}
		by_distance: dict[str, list[VariantResult]] = defaultdict(list)
		for item in items:
			reference = references.get(item.question_id)
			by_distance[str(getattr(reference, "few_shot_distance", "unknown"))].append(item)
		for distance, group in sorted(by_distance.items()):
			segmented[distance] = rate(group)
		report[relation] = {**rate(items), "by_few_shot_distance": segmented}
	return report
