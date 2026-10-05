import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Persona(BaseModel):
	id: str
	version: int
	name: str
	functional_role: str
	domain_expertise: Literal["novice", "intermediate", "expert"]
	technical_proficiency: Literal["low", "medium", "high"]
	vocabulary_style: str = Field(min_length=20)
	typical_tasks: list[str] = Field(default_factory=list)
	example_utterances: list[str] = Field(default_factory=list)
	primary_motivation: str
	success_criteria: str
	constraints: str = ""
	patience: int = Field(ge=1, le=10)
	ambiguity_response: Literal["re_ask", "guess_and_proceed", "stop"]
	forbidden_assumptions: list[str]
	verification_literacy: Literal["low", "medium", "high"]
	tail_flag: bool = False
	provenance: str = "ungrounded"
	approval_state: Literal["draft", "approved"] = "draft"

	# Harness bookkeeping — identity, versioning and approval state. These say nothing
	# about who the person is, so they are withheld from prompts to keep the character
	# description free of noise the model would have to learn to ignore.
	_BOOKKEEPING_FIELDS = ("id", "version", "tail_flag", "provenance", "approval_state")

	def character_sheet(self) -> dict[str, Any]:
		"""The persona as a prompt should see it: character only, no bookkeeping."""
		sheet = self.model_dump(mode="json", exclude=set(self._BOOKKEEPING_FIELDS))
		return {key: value for key, value in sheet.items() if value not in ([], "")}

	@model_validator(mode="after")
	def reject_empty_forbidden_assumptions(self) -> "Persona":
		if not self.forbidden_assumptions:
			raise ValueError(
				"forbidden_assumptions must contain at least one item; this field "
				"stops the simulated user from silently repairing the product's mistakes."
			)
		return self


class ProductProfile(BaseModel):
	product_name: str
	version: str
	description: str
	what_it_can_do: list[str]
	what_it_cannot_do: list[str]


class CapabilitySpec(BaseModel):
	product_version: int
	spec_version: int
	supported: list[str]
	not_supported_yet: list[str]
	known_gaps: list[str]


class GeneratedQuestion(BaseModel):
	id: str
	question: str
	intent: str
	complexity: Literal["simple", "medium", "hard"]
	persona_trait: str
	expected_response_type: Literal["data", "explanation", "either"]
	reference_id: str | None = None


class SchemaColumn(BaseModel):
	name: str
	type: str
	description: str


class SchemaTable(BaseModel):
	name: str
	description: str
	columns: list[SchemaColumn]


class SchemaSpec(BaseModel):
	"""Privileged asset. Reaches the question generator, reference validator and
	judge only - never a persona prompt, an adapter, or a product-facing message."""

	product_version: str
	tables: list[SchemaTable]
	schema_version: str = ""

	@model_validator(mode="after")
	def derive_schema_version(self) -> "SchemaSpec":
		"""schema_version is a stable content hash, so a reference can never be
		silently reused against a schema that has changed underneath it."""
		if not self.schema_version:
			payload = json.dumps(
				[table.model_dump(mode="json") for table in self.tables],
				sort_keys=True,
				separators=(",", ":"),
			)
			self.schema_version = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
		return self


class FewShotExample(BaseModel):
	"""Privileged asset. Same containment rule as SchemaSpec."""

	id: str
	question: str
	sql: str
	tables: list[str]
	notes: str = ""
	source: str = "placeholder"


class QuestionReference(BaseModel):
	"""What a correct answer to one question should look like, in business terms.

	Only `expectation` is ever allowed near a persona. Every other field describes
	structure the persona must not know, because a user who knew which columns the
	answer needed would not ask the questions worth testing.
	"""

	id: str
	question_id: str
	expected_behaviour: Literal["answer", "clarify", "refuse"]
	expectation: str
	tables: list[str] = Field(default_factory=list)
	columns: list[str] = Field(default_factory=list)
	filters: list[str] = Field(default_factory=list)
	aggregation: str | None = None
	grain: str | None = None
	time_window: str | None = None
	analysis_type: Literal[
		"lookup", "aggregate", "trend", "comparison", "top_n", "distribution", "other"
	]
	expected_output_columns: list[str] = Field(default_factory=list)
	must_match: list[str] = Field(default_factory=list)
	acceptable_alternatives: list[str] = Field(default_factory=list)
	few_shot_ids: list[str] = Field(default_factory=list)
	few_shot_distance: Literal["near", "pattern", "schema_only"]
	schema_version: str
	prompt_version: str
	model: str
	validation_status: Literal["pending", "valid", "invalid"] = "pending"
	validation_notes: str | None = None

	@model_validator(mode="after")
	def must_match_names_real_fields(self) -> "QuestionReference":
		allowed = set(type(self).model_fields)
		unknown = [name for name in self.must_match if name not in allowed]
		if unknown:
			raise ValueError(
				f"must_match names fields that do not exist on QuestionReference: {unknown}. "
				f"A typo here silently disables a mandatory check."
			)
		return self


class JudgeResult(BaseModel):
	conversation_id: str
	question_id: str
	evaluated_turn: int | None = None
	parse_ok: bool
	table_precision: float | None = None
	table_recall: float | None = None
	column_precision: float | None = None
	column_recall: float | None = None
	field_verdicts: dict[
		str, Literal["match", "acceptable_alternative", "mismatch", "not_applicable"]
	] = Field(default_factory=dict)
	behaviour_match: bool | None = None
	overall: Literal[
		"correct", "partially_correct", "incorrect", "reference_suspect", "not_evaluable"
	]
	failure_tags: list[str] = Field(default_factory=list)
	reason: str
	judge_model: str
	prompt_version: str


class AdapterResponse(BaseModel):
	reply: str
	artifacts: dict[str, Any] = Field(default_factory=dict)
	error: str | None = None
	latency_ms: int
	raw: dict[str, Any] = Field(default_factory=dict)


class TurnDecision(BaseModel):
	satisfied: bool
	giving_up: bool
	next_question: str | None
	doubt_expressed: bool
	reasoning: str


class Outcome(str, Enum):
	COMPLETED = "COMPLETED"
	ABANDONED = "ABANDONED"
	MAX_TURNS = "MAX_TURNS"
	LOOPED = "LOOPED"
	ADAPTER_ERROR = "ADAPTER_ERROR"
	BUDGET = "BUDGET"


class Turn(BaseModel):
	turn_index: int
	message: str
	response: AdapterResponse
	decision: TurnDecision | None
	# Structured output the adapter chose to expose for evaluation, e.g.
	# {"kind": "sql", "query": ..., "rows": ...}. Opaque to everything except
	# the adapter that produced it and the judge that reads it.
	evaluable: dict[str, Any] | None = None


class Conversation(BaseModel):
	id: str
	question_id: str
	repeat_index: int
	seed: int
	persona_id: str
	persona_version: int
	turns: list[Turn]
	outcome: Outcome
	doubt_anywhere: bool
	closing_summary: str | None


class ValidationResult(BaseModel):
	conversation_id: str
	classification: str | None
	refused_clearly: bool | None
	cleanup_suspected: bool
	deterministic_flags: list[str]
	reason: str


class RunRecord(BaseModel):
	id: str
	started_at: datetime
	finished_at: datetime
	persona_versions: dict
	prompt_versions: dict
	model_versions: dict
	product_version: str
	spec_version: int | None
	seed_base: int
	conversation_ids: list[str]
