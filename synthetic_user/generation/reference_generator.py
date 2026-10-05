"""Reference-guided question generation.

The existing generator is untouched. This is the opt-in path: every question is
produced together with a reference describing what a correct answer involves.

Planning is done in code, not delegated to the model: few-shot distance, target
behaviour, schema subset and analysis-type coverage are all decided here, so the
shape of a batch is reproducible from the seed rather than left to model whim.

Privilege boundary: schema, few-shots and the glossary reach this module, the
reference validator and the judge. They never reach the persona agent, the
adapter, or any product-facing message. Only QuestionReference.expectation is
ever allowed near a persona.

Nothing here executes SQL.
"""

import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from synthetic_user.core.models import (
	FewShotExample,
	GeneratedQuestion,
	Persona,
	ProductProfile,
	QuestionReference,
	SchemaSpec,
)
from synthetic_user.core.prompts import load_prompt

logger = logging.getLogger(__name__)

PROMPT_VERSION = "reference_generate_v1"
VALIDATE_PROMPT_VERSION = "reference_validate_v1"

ANALYSIS_TYPES = (
	"lookup",
	"aggregate",
	"trend",
	"comparison",
	"top_n",
	"distribution",
)

Distance = Literal["near", "pattern", "schema_only"]
Behaviour = Literal["answer", "clarify", "refuse"]

_BEHAVIOUR_GUIDANCE = {
	"answer": (
		"This must be a question the schema subset genuinely supports. A competent "
		"analyst could answer it from these tables alone."
	),
	"clarify": (
		"This must be a question that is genuinely ambiguous - two or more sensible "
		"readings, so the product SHOULD ask the user which one they mean. Do not "
		"resolve the ambiguity yourself. Record the competing readings in "
		"acceptable_alternatives."
	),
	"refuse": (
		"This must be a question the schema subset genuinely CANNOT support - it needs "
		"data that simply is not there. Keep it realistic: something this person would "
		"plausibly ask at work, not nonsense. Leave tables and columns empty, and say "
		"in expectation what the user wanted that the data cannot provide."
	),
}

_FEW_SHOT_GUIDANCE = {
	"near": (
		"USE THE EXAMPLE BELOW. Write a question with the SAME underlying intent, but "
		"in this person's own words and level of precision. It should be recognisably "
		"the same information need, asked the way they would ask it."
	),
	"pattern": (
		"USE THE EXAMPLE BELOW AS A PATTERN ONLY. Write a question with the same shape "
		"of analysis but DIFFERENT entities, filters and time period. It must not be a "
		"reworded copy - a reader should not be able to tell which example it came from."
	),
	"schema_only": (
		"There is no example for this item. Write the question from the schema and the "
		"persona alone."
	),
}


@dataclass
class ItemPlan:
	index: int
	few_shot_distance: Distance
	expected_behaviour: Behaviour
	tables: list[str]
	analysis_type: str
	few_shot: FewShotExample | None = None


@dataclass
class ValidationOutcome:
	status: Literal["valid", "invalid"]
	notes: list[str] = field(default_factory=list)

	@property
	def note_text(self) -> str | None:
		return "; ".join(self.notes) if self.notes else None


# --------------------------------------------------------------------------- planning


def _largest_remainder(n: int, weights: dict[str, float]) -> dict[str, int]:
	"""Split n across weighted buckets without drifting off by rounding."""
	total = sum(weights.values()) or 1.0
	exact = {key: n * value / total for key, value in weights.items()}
	counts = {key: int(value) for key, value in exact.items()}
	remainder = n - sum(counts.values())
	ordered = sorted(exact, key=lambda key: exact[key] - counts[key], reverse=True)
	for key in ordered[:remainder]:
		counts[key] += 1
	return counts


def _plan_distances(n: int, mix: dict[str, float], mode: str, rng: random.Random) -> list[Distance]:
	if mode == "none":
		return ["schema_only"] * n
	weights = {key: float(mix.get(key, 0.0)) for key in ("near", "pattern", "schema_only")}
	if not any(weights.values()):
		return ["schema_only"] * n
	counts = _largest_remainder(n, weights)
	distances: list[Distance] = []
	for key, count in counts.items():
		distances.extend([key] * count)  # type: ignore[list-item]
	rng.shuffle(distances)
	return distances


def _plan_behaviours(n: int, unanswerable_ratio: float, rng: random.Random) -> list[Behaviour]:
	unanswerable = min(n, int(round(n * max(0.0, unanswerable_ratio))))
	behaviours: list[Behaviour] = ["answer"] * (n - unanswerable)
	# Alternate refuse/clarify so a small batch gets one of each rather than two of one.
	for position in range(unanswerable):
		behaviours.append("refuse" if position % 2 == 0 else "clarify")
	rng.shuffle(behaviours)
	return behaviours


def _related_tables(schema: SchemaSpec, seed_table: str) -> list[str]:
	"""Tables sharing a column name with the seed table - a cheap join heuristic."""
	by_name = {table.name: {column.name for column in table.columns} for table in schema.tables}
	seed_columns = by_name.get(seed_table, set())
	return [
		name
		for name, columns in by_name.items()
		if name != seed_table and columns & seed_columns
	]


def _sample_tables(schema: SchemaSpec, rng: random.Random) -> list[str]:
	names = [table.name for table in schema.tables]
	if not names:
		return []
	seed_table = rng.choice(names)
	related = _related_tables(schema, seed_table)
	if not related:
		return [seed_table]
	extra = rng.randint(0, min(2, len(related)))
	return [seed_table, *rng.sample(related, extra)]


def _pick_analysis_type(tables: list[str], covered: set[tuple[str, str]], rng: random.Random) -> str:
	"""Bias toward cells of the tables x analysis_type matrix that are still empty."""
	key = ",".join(sorted(tables))
	uncovered = [kind for kind in ANALYSIS_TYPES if (key, kind) not in covered]
	return rng.choice(uncovered or list(ANALYSIS_TYPES))


def _pick_few_shot(
	distance: Distance,
	tables: list[str],
	fewshots: list[FewShotExample],
	used: set[str],
	rng: random.Random,
) -> FewShotExample | None:
	if distance == "schema_only" or not fewshots:
		return None
	relevant = [shot for shot in fewshots if not tables or set(shot.tables) & set(tables)]
	pool = [shot for shot in (relevant or fewshots) if shot.id not in used]
	chosen = rng.choice(pool or relevant or fewshots)
	used.add(chosen.id)
	return chosen


def plan_items(
	n: int,
	schema: SchemaSpec,
	fewshots: list[FewShotExample],
	settings: Any,
	fewshot_mode: str,
	seed: int,
) -> list[ItemPlan]:
	rng = random.Random(seed)
	distances = _plan_distances(n, getattr(settings, "fewshot_mix", {}), fewshot_mode, rng)
	behaviours = _plan_behaviours(n, getattr(settings, "unanswerable_ratio", 0.0), rng)

	covered: set[tuple[str, str]] = set()
	used_shots: set[str] = set()
	plans: list[ItemPlan] = []
	for index in range(n):
		tables = _sample_tables(schema, rng)
		analysis_type = _pick_analysis_type(tables, covered, rng)
		covered.add((",".join(sorted(tables)), analysis_type))
		plans.append(
			ItemPlan(
				index=index,
				few_shot_distance=distances[index],
				expected_behaviour=behaviours[index],
				tables=tables,
				analysis_type=analysis_type,
				few_shot=_pick_few_shot(distances[index], tables, fewshots, used_shots, rng),
			)
		)
	return plans


# --------------------------------------------------------------------------- rendering


def _yaml(value: Any) -> str:
	if hasattr(value, "model_dump"):
		value = value.model_dump(mode="json")
	return yaml.safe_dump(value, sort_keys=False)


def _schema_subset_yaml(schema: SchemaSpec, tables: list[str]) -> str:
	wanted = set(tables)
	subset = [table.model_dump(mode="json") for table in schema.tables if table.name in wanted]
	return yaml.safe_dump(subset, sort_keys=False)


def _glossary_text(glossary: dict[str, str]) -> str:
	if not glossary:
		return "No glossary is available."
	return "\n".join(f"- {term}: {definition}" for term, definition in glossary.items())


def _few_shot_block(plan: ItemPlan) -> str:
	if plan.few_shot is None:
		return "FEW-SHOT EXAMPLE: none for this item."
	return (
		"FEW-SHOT EXAMPLE (validated pair - internal, never quote it in the question):\n"
		f"  question: {plan.few_shot.question}\n"
		f"  sql: {plan.few_shot.sql.strip()}\n"
		f"  notes: {plan.few_shot.notes}"
	)


def _render_generate_prompt(
	persona: Persona,
	product_profile: ProductProfile,
	schema: SchemaSpec,
	glossary: dict[str, str],
	past_queries: str | None,
	plan: ItemPlan,
) -> str:
	prompt = load_prompt("reference_generate_v1.md")
	replacements = {
		"<<PERSONA_YAML>>": _yaml(persona.character_sheet()),
		"<<PRODUCT_PROFILE_YAML>>": _yaml(product_profile),
		"<<SCHEMA_SUBSET>>": _schema_subset_yaml(schema, plan.tables),
		"<<GLOSSARY>>": _glossary_text(glossary),
		"<<FEW_SHOT>>": _few_shot_block(plan),
		"<<FEW_SHOT_GUIDANCE>>": _FEW_SHOT_GUIDANCE[plan.few_shot_distance],
		"<<TARGET_BEHAVIOUR>>": plan.expected_behaviour,
		"<<BEHAVIOUR_GUIDANCE>>": _BEHAVIOUR_GUIDANCE[plan.expected_behaviour],
		"<<TARGET_ANALYSIS_TYPE>>": plan.analysis_type,
		"<<PAST_QUERIES>>": past_queries or "No past queries are available.",
	}
	for marker, value in replacements.items():
		prompt = prompt.replace(marker, value)
	return prompt


# --------------------------------------------------------------------------- parsing


def _strip_json_fences(response: str) -> str:
	text = response.strip()
	if text.startswith("```"):
		text = "\n".join(text.splitlines()[1:])
	if text.endswith("```"):
		text = "\n".join(text.splitlines()[:-1])
	return text.strip()


def _parse_json(response: str) -> dict[str, Any]:
	data = json.loads(_strip_json_fences(response))
	if not isinstance(data, dict):
		raise ValueError("expected a JSON object")
	return data


def _qualify_columns(columns: list[str], schema: SchemaSpec, tables: list[str]) -> list[str]:
	"""Accept a bare column name when it is unambiguous within the subset."""
	lookup: dict[str, list[str]] = {}
	for table in schema.tables:
		if table.name in tables:
			for column in table.columns:
				lookup.setdefault(column.name, []).append(table.name)

	qualified = []
	for column in columns:
		if "." in column or column not in lookup or len(lookup[column]) != 1:
			qualified.append(column)
		else:
			qualified.append(f"{lookup[column][0]}.{column}")
	return qualified


# --------------------------------------------------------------------- deterministic checks


def identifier_leaks(question: str, schema: SchemaSpec) -> list[str]:
	"""Technical identifiers that must never appear in a user-facing question.

	Flags exact table names, and any schema identifier containing an underscore.
	Plain English words that happen to be column names ("amount", "department")
	are allowed - a real user says those.
	"""
	text = question.lower()
	leaks: set[str] = set()
	for table in schema.tables:
		if re.search(rf"\b{re.escape(table.name.lower())}\b", text):
			leaks.add(table.name)
		for column in table.columns:
			if "_" in column.name and re.search(rf"\b{re.escape(column.name.lower())}\b", text):
				leaks.add(column.name)
	return sorted(leaks)


def _schema_reference_errors(
	reference_tables: list[str],
	reference_columns: list[str],
	schema: SchemaSpec,
	subset: list[str],
) -> list[str]:
	known: dict[str, set[str]] = {
		table.name: {column.name for column in table.columns} for table in schema.tables
	}
	errors: list[str] = []

	for name in reference_tables:
		if name not in known:
			errors.append(f"unknown table '{name}'")
		elif name not in subset:
			errors.append(f"table '{name}' is outside the assigned schema subset")

	for column in reference_columns:
		if "." not in column:
			errors.append(f"column '{column}' is not qualified as table.column")
			continue
		table_name, column_name = column.split(".", 1)
		if table_name not in known:
			errors.append(f"unknown table '{table_name}' in column '{column}'")
		elif column_name not in known[table_name]:
			errors.append(f"unknown column '{column}'")
		elif table_name not in subset:
			errors.append(f"column '{column}' is outside the assigned schema subset")
	return errors


def _deterministic_checks(
	question_text: str,
	reference_tables: list[str],
	reference_columns: list[str],
	dropped_must_match: list[str],
	schema: SchemaSpec,
	plan: ItemPlan,
) -> list[str]:
	problems: list[str] = []

	leaks = identifier_leaks(question_text, schema)
	if leaks:
		problems.append(f"question leaks technical identifiers: {', '.join(leaks)}")

	# A refuse item is expected to have no supporting tables, so do not demand them.
	if plan.expected_behaviour != "refuse":
		problems.extend(
			_schema_reference_errors(reference_tables, reference_columns, schema, plan.tables)
		)

	if dropped_must_match:
		problems.append(f"must_match named unknown fields: {', '.join(dropped_must_match)}")
	return problems


# --------------------------------------------------------------------- LLM validation


def _render_validate_prompt(
	question_text: str,
	reference: QuestionReference,
	schema: SchemaSpec,
	glossary: dict[str, str],
	plan: ItemPlan,
) -> str:
	prompt = load_prompt("reference_validate_v1.md")
	check_paraphrase = plan.few_shot_distance in ("pattern", "schema_only")
	guidance = (
		"This item must NOT be a paraphrase: report true if it reads like a reworded copy."
		if check_paraphrase
		else "This item is allowed to restate the example's intent in the user's own words, "
		"so a close resemblance is expected. Report false unless it is a verbatim copy."
	)
	few_shot_questions = plan.few_shot.question if plan.few_shot else "none"

	reference_for_judge = reference.model_dump(
		mode="json",
		include={
			"expected_behaviour",
			"expectation",
			"tables",
			"columns",
			"filters",
			"aggregation",
			"grain",
			"time_window",
			"analysis_type",
			"expected_output_columns",
			"must_match",
			"acceptable_alternatives",
		},
	)

	replacements = {
		"<<QUESTION>>": question_text,
		"<<EXPECTED_BEHAVIOUR>>": plan.expected_behaviour,
		"<<REFERENCE_JSON>>": json.dumps(reference_for_judge, indent=2),
		"<<SCHEMA_SUBSET>>": _schema_subset_yaml(schema, plan.tables),
		"<<GLOSSARY>>": _glossary_text(glossary),
		"<<FEW_SHOT_QUESTIONS>>": few_shot_questions,
		"<<PARAPHRASE_GUIDANCE>>": guidance,
	}
	for marker, value in replacements.items():
		prompt = prompt.replace(marker, value)
	return prompt


def _llm_validation_problems(verdict: dict[str, Any], plan: ItemPlan) -> list[str]:
	"""Interpret validator output against what this item was PLANNED to be.

	A refuse item should not be answerable, and a clarify item should not have a
	single interpretation - so those checks invert rather than simply requiring true.
	"""
	problems: list[str] = []
	answerable = verdict.get("answerable_with_reference")
	single = verdict.get("single_interpretation")

	if plan.expected_behaviour == "answer":
		if answerable is False:
			problems.append("validator says the schema cannot answer this")
		if single is False:
			readings = verdict.get("interpretations") or []
			problems.append(f"question has multiple readings: {readings}")
	elif plan.expected_behaviour == "refuse":
		if answerable is True:
			problems.append("planned as refuse, but the schema can answer it")
	elif plan.expected_behaviour == "clarify":
		if single is True:
			problems.append("planned as clarify, but the question is unambiguous")

	if plan.few_shot_distance in ("pattern", "schema_only") and verdict.get("is_fewshot_paraphrase") is True:
		problems.append("question is a paraphrase of its few-shot example")
	if verdict.get("behaviour_consistent") is False:
		problems.append("expected behaviour is inconsistent with the question")
	if verdict.get("reference_sound") is False:
		problems.append(f"reference unsound: {verdict.get('notes', '')}".strip())
	return problems


# --------------------------------------------------------------------------- main entry


def generate_with_references(
	persona: Persona,
	product_profile: ProductProfile,
	schema: SchemaSpec,
	fewshots: list[FewShotExample],
	glossary: dict[str, str],
	past_queries: str | None,
	n: int,
	fewshot_mode: Literal["mixed", "none"],
	llm: Any,
	seed: int,
	settings: Any = None,
) -> list[tuple[GeneratedQuestion, QuestionReference]]:
	"""Generate n question/reference pairs. Never raises.

	Pairs that fail validation are returned with validation_status "invalid" and
	notes explaining why, so they can be stored and inspected. Callers must
	exclude them from runs.
	"""
	if settings is None:
		from synthetic_user.core.config import get_settings

		settings = get_settings()

	plans = plan_items(n, schema, fewshots, settings, fewshot_mode, seed)
	model_name = getattr(settings, "model_reference_generation", "unknown")
	samples = max(1, int(getattr(settings, "reference_consistency_samples", 1)))

	pairs: list[tuple[GeneratedQuestion, QuestionReference]] = []
	for plan in plans:
		pair = _generate_one(
			persona, product_profile, schema, glossary, past_queries,
			plan, llm, seed, model_name, samples,
		)
		if pair is not None:
			pairs.append(pair)
	return pairs


def _generate_one(
	persona: Persona,
	product_profile: ProductProfile,
	schema: SchemaSpec,
	glossary: dict[str, str],
	past_queries: str | None,
	plan: ItemPlan,
	llm: Any,
	seed: int,
	model_name: str,
	samples: int,
) -> tuple[GeneratedQuestion, QuestionReference] | None:
	item_seed = seed + plan.index
	try:
		prompt = _render_generate_prompt(persona, product_profile, schema, glossary, past_queries, plan)
		raw = llm.complete(prompt=prompt, role="reference_generation", seed=item_seed)
		data = _parse_json(raw)
	except Exception as error:
		logger.warning("Reference generation failed for item %s: %s", plan.index, error)
		return None

	question_text = str(data.get("question", "")).strip()
	if not question_text:
		logger.warning("Reference generation returned no question for item %s", plan.index)
		return None

	reference_data = data.get("reference") or {}
	question_id = f"question-{plan.index + 1}"
	reference_id = f"ref-{plan.index + 1}"

	allowed_fields = set(QuestionReference.model_fields)
	raw_must_match = [str(name) for name in (reference_data.get("must_match") or [])]
	must_match = [name for name in raw_must_match if name in allowed_fields]
	dropped_must_match = [name for name in raw_must_match if name not in allowed_fields]

	reference_tables = [str(name) for name in (reference_data.get("tables") or [])]
	reference_columns = _qualify_columns(
		[str(name) for name in (reference_data.get("columns") or [])], schema, plan.tables
	)

	try:
		reference = QuestionReference(
			id=reference_id,
			question_id=question_id,
			expected_behaviour=reference_data.get("expected_behaviour") or plan.expected_behaviour,
			expectation=str(reference_data.get("expectation", "")),
			tables=reference_tables,
			columns=reference_columns,
			filters=[str(item) for item in (reference_data.get("filters") or [])],
			aggregation=reference_data.get("aggregation"),
			grain=reference_data.get("grain"),
			time_window=reference_data.get("time_window"),
			analysis_type=reference_data.get("analysis_type") or plan.analysis_type,
			expected_output_columns=[
				str(item) for item in (reference_data.get("expected_output_columns") or [])
			],
			must_match=must_match,
			acceptable_alternatives=[
				str(item) for item in (reference_data.get("acceptable_alternatives") or [])
			],
			few_shot_ids=[plan.few_shot.id] if plan.few_shot else [],
			few_shot_distance=plan.few_shot_distance,
			schema_version=schema.schema_version,
			prompt_version=PROMPT_VERSION,
			model=model_name,
		)
	except Exception as error:
		logger.warning("Reference did not validate for item %s: %s", plan.index, error)
		return None

	try:
		question = GeneratedQuestion(
			id=question_id,
			question=question_text,
			intent=str(data.get("intent", "")) or reference.expectation,
			complexity=data.get("complexity") or "medium",
			persona_trait=str(data.get("persona_trait", "")) or "unspecified",
			expected_response_type=data.get("expected_response_type") or "either",
			reference_id=reference_id,
		)
	except Exception as error:
		logger.warning("Question did not validate for item %s: %s", plan.index, error)
		return None

	outcome = _validate_pair(
		question, reference, schema, glossary, plan, llm, item_seed, dropped_must_match, samples
	)
	reference = reference.model_copy(
		update={"validation_status": outcome.status, "validation_notes": outcome.note_text}
	)
	return question, reference


def _validate_pair(
	question: GeneratedQuestion,
	reference: QuestionReference,
	schema: SchemaSpec,
	glossary: dict[str, str],
	plan: ItemPlan,
	llm: Any,
	item_seed: int,
	dropped_must_match: list[str],
	samples: int,
) -> ValidationOutcome:
	problems = _deterministic_checks(
		question.question, reference.tables, reference.columns, dropped_must_match, schema, plan
	)

	try:
		raw = llm.complete(
			prompt=_render_validate_prompt(question.question, reference, schema, glossary, plan),
			role="reference_validator",
			seed=item_seed,
			temperature=0.0,
		)
		verdict = _parse_json(raw)
		problems.extend(_llm_validation_problems(verdict, plan))
	except Exception as error:
		logger.warning("Reference validation failed for %s: %s", reference.id, error)
		problems.append(f"validator unavailable: {error}")

	if samples > 1:
		problems.extend(_consistency_problems(reference, schema, glossary, plan, llm, item_seed, samples))

	return ValidationOutcome("invalid" if problems else "valid", problems)


def _consistency_problems(
	reference: QuestionReference,
	schema: SchemaSpec,
	glossary: dict[str, str],
	plan: ItemPlan,
	llm: Any,
	item_seed: int,
	samples: int,
) -> list[str]:
	"""Regenerate the reference independently; keep only if tables and columns agree."""
	del schema, glossary
	for attempt in range(1, samples):
		try:
			raw = llm.complete(
				prompt=_CONSISTENCY_PROMPT.format(question=reference.expectation),
				role="reference_generation",
				seed=item_seed + 1000 * attempt,
			)
			repeat = _parse_json(raw).get("reference") or {}
		except Exception as error:
			logger.warning("Consistency sample failed for %s: %s", reference.id, error)
			return [f"consistency sample unavailable: {error}"]

		if set(repeat.get("tables") or []) != set(reference.tables):
			return ["consistency samples disagree on tables"]
		if set(repeat.get("columns") or []) != set(reference.columns):
			return ["consistency samples disagree on columns"]
	return []


_CONSISTENCY_PROMPT = (
	"Independently decide which tables and columns are needed to satisfy this request. "
	"Return only JSON: {{\"reference\": {{\"tables\": [...], \"columns\": [\"table.column\"]}}}}\n\n"
	"REQUEST: {question}"
)
