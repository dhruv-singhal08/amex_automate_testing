import logging
from typing import Any

from sqlalchemy import (
	JSON,
	Column,
	DateTime,
	Engine,
	Float,
	Integer,
	MetaData,
	String,
	Table,
	create_engine,
	delete,
	insert,
	inspect,
	select,
	text,
)

from synthetic_user.core.config import get_settings
from synthetic_user.core.models import (
	Conversation,
	GeneratedQuestion,
	JudgeResult,
	QuestionReference,
	RunRecord,
	Turn,
	ValidationResult,
)


logger = logging.getLogger(__name__)

metadata = MetaData()

persona_table = Table(
	"persona",
	metadata,
	Column("id", String, primary_key=True),
	Column("version", Integer, nullable=False),
	Column("data", JSON, nullable=False),
)

run_table = Table(
	"run",
	metadata,
	Column("id", String, primary_key=True),
	Column("started_at", DateTime, nullable=False),
	Column("finished_at", DateTime, nullable=False),
	Column("persona_versions", JSON, nullable=False),
	Column("prompt_versions", JSON, nullable=False),
	Column("model_versions", JSON, nullable=False),
	Column("product_version", String, nullable=False),
	Column("spec_version", Integer),
	Column("seed_base", Integer, nullable=False),
	Column("conversation_ids", JSON, nullable=False),
)

question_table = Table(
	"question",
	metadata,
	Column("id", String, primary_key=True),
	Column("run_id", String),
	Column("question", String, nullable=False),
	Column("intent", String, nullable=False),
	Column("complexity", String, nullable=False),
	Column("persona_trait", String, nullable=False),
	Column("expected_response_type", String, nullable=False),
	Column("reference_id", String),
)

# Reference-guided evaluation. References live here and are joined by
# question_id - they are never embedded in a question or conversation row,
# so a reference can be revised or invalidated without rewriting run history.
reference_table = Table(
	"reference",
	metadata,
	Column("id", String, primary_key=True),
	Column("question_id", String, nullable=False),
	Column("run_id", String),
	Column("expected_behaviour", String, nullable=False),
	Column("expectation", String, nullable=False),
	Column("tables", JSON, nullable=False),
	Column("columns", JSON, nullable=False),
	Column("filters", JSON, nullable=False),
	Column("aggregation", String),
	Column("grain", String),
	Column("time_window", String),
	Column("analysis_type", String, nullable=False),
	Column("expected_output_columns", JSON, nullable=False),
	Column("must_match", JSON, nullable=False),
	Column("acceptable_alternatives", JSON, nullable=False),
	Column("few_shot_ids", JSON, nullable=False),
	Column("few_shot_distance", String, nullable=False),
	Column("schema_version", String, nullable=False),
	Column("prompt_version", String, nullable=False),
	Column("model", String, nullable=False),
	Column("validation_status", String, nullable=False),
	Column("validation_notes", String),
)

judge_result_table = Table(
	"judge_result",
	metadata,
	Column("conversation_id", String, primary_key=True),
	Column("question_id", String, nullable=False),
	Column("run_id", String),
	Column("evaluated_turn", Integer),
	Column("parse_ok", Integer, nullable=False),
	Column("table_precision", Float),
	Column("table_recall", Float),
	Column("column_precision", Float),
	Column("column_recall", Float),
	Column("field_verdicts", JSON, nullable=False),
	Column("behaviour_match", Integer),
	Column("overall", String, nullable=False),
	Column("failure_tags", JSON, nullable=False),
	Column("reason", String, nullable=False),
	Column("judge_model", String, nullable=False),
	Column("prompt_version", String, nullable=False),
)

# Human labels, for measuring the judge's own accuracy. Without these the judge
# is unfalsifiable.
calibration_label_table = Table(
	"calibration_label",
	metadata,
	Column("conversation_id", String, primary_key=True),
	Column("question_id", String),
	Column("reference_id", String),
	Column("human_overall", String, nullable=False),
	Column("human_behaviour_match", Integer),
	Column("reference_is_sound", Integer),
	Column("notes", String),
	Column("labelled_by", String),
	Column("labelled_at", DateTime),
	# Calibration is per judge prompt version: changing the prompt changes what
	# was measured, so old labels must not be counted as covering a new judge.
	Column("judge_prompt_version", String),
)

conversation_table = Table(
	"conversation",
	metadata,
	Column("id", String, primary_key=True),
	Column("run_id", String),
	Column("question_id", String, nullable=False),
	Column("repeat_index", Integer, nullable=False),
	Column("seed", Integer, nullable=False),
	Column("persona_id", String, nullable=False),
	Column("persona_version", Integer, nullable=False),
	Column("turns", JSON, nullable=False),
	Column("outcome", String, nullable=False),
	Column("doubt_anywhere", Integer, nullable=False),
	Column("closing_summary", String),
)

turn_table = Table(
	"turn",
	metadata,
	Column("conversation_id", String, primary_key=True),
	Column("turn_index", Integer, primary_key=True),
	Column("message", String, nullable=False),
	Column("response", JSON, nullable=False),
	Column("decision", JSON),
	Column("evaluable", JSON),
)

validation_table = Table(
	"validation",
	metadata,
	Column("conversation_id", String, primary_key=True),
	Column("run_id", String),
	Column("classification", String),
	Column("refused_clearly", Integer),
	Column("cleanup_suspected", Integer, nullable=False),
	Column("deterministic_flags", JSON, nullable=False),
	Column("reason", String, nullable=False),
)

_engine: Engine | None = None


def init_db(database_url: str | None = None) -> None:
	global _engine
	if database_url is not None or _engine is None:
		_engine = create_engine(database_url or get_settings().database_url)
	metadata.create_all(_engine)
	_add_missing_columns(_engine)


def _add_missing_columns(engine: Engine) -> None:
	"""Add columns declared in metadata but absent from an existing table.

	create_all() only creates tables that do not exist, so a database written by
	an earlier version keeps its old shape and inserts fail on the new columns.
	This is deliberately additive only: it never drops, renames or retypes, so it
	cannot lose recorded run history.
	"""
	inspector = inspect(engine)
	existing_tables = set(inspector.get_table_names())
	for table in metadata.tables.values():
		if table.name not in existing_tables:
			continue
		present = {column["name"] for column in inspector.get_columns(table.name)}
		for column in table.columns:
			if column.name in present:
				continue
			column_type = column.type.compile(engine.dialect)
			with engine.begin() as connection:
				connection.execute(
					text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}')
				)
			logger.info("Added missing column %s.%s", table.name, column.name)


def _get_engine() -> Engine:
	if _engine is None:
		init_db()
	assert _engine is not None
	return _engine


def _replace(table: Table, key_column: Any, key: Any, values: dict[str, Any]) -> None:
	with _get_engine().begin() as connection:
		connection.execute(delete(table).where(key_column == key))
		connection.execute(insert(table).values(**values))


def save_run(run: RunRecord) -> None:
	_replace(
		run_table,
		run_table.c.id,
		run.id,
		run.model_dump(),
	)


def save_question(question: GeneratedQuestion, run_id: str | None = None) -> None:
	values = question.model_dump()
	values["run_id"] = run_id
	_replace(question_table, question_table.c.id, question.id, values)


def save_conversation(conversation: Conversation, run_id: str | None = None) -> None:
	conversation_values = conversation.model_dump(mode="json")
	conversation_values["run_id"] = run_id
	conversation_values["outcome"] = conversation.outcome.value
	conversation_values["doubt_anywhere"] = int(conversation.doubt_anywhere)

	with _get_engine().begin() as connection:
		connection.execute(delete(turn_table).where(turn_table.c.conversation_id == conversation.id))
		connection.execute(delete(conversation_table).where(conversation_table.c.id == conversation.id))
		connection.execute(insert(conversation_table).values(**conversation_values))
		connection.execute(
			insert(turn_table),
			[
				_turn_row(conversation.id, turn)
				for turn in conversation.turns
			],
		)


def _turn_row(conversation_id: str, turn: Turn) -> dict[str, Any]:
	"""Every JSON column goes through Pydantic's json serializer.

	`evaluable` used to be written raw, straight off the model. Adapter rows can
	contain database objects - a DATE column yields datetime.date - which the
	JSON column cannot encode, and the failure took the WHOLE conversation with
	it. Worse, it was biased: only queries touching date columns were lost, which
	is exactly the behaviour under evaluation.
	"""
	dumped = turn.model_dump(mode="json")
	return {
		"conversation_id": conversation_id,
		"turn_index": turn.turn_index,
		"message": turn.message,
		"response": dumped["response"],
		"decision": dumped["decision"],
		"evaluable": dumped["evaluable"],
	}


def save_validation(validation: ValidationResult, run_id: str | None = None) -> None:
	if run_id is None:
		with _get_engine().connect() as connection:
			run_id = connection.execute(
				select(conversation_table.c.run_id).where(
					conversation_table.c.id == validation.conversation_id
				)
			).scalar_one_or_none()
	values = validation.model_dump()
	values["run_id"] = run_id
	values["refused_clearly"] = (
		None if validation.refused_clearly is None else int(validation.refused_clearly)
	)
	values["cleanup_suspected"] = int(validation.cleanup_suspected)
	_replace(
		validation_table,
		validation_table.c.conversation_id,
		validation.conversation_id,
		values,
	)


def save_reference(reference: QuestionReference, run_id: str | None = None) -> None:
	values = reference.model_dump(mode="json")
	values["run_id"] = run_id
	_replace(reference_table, reference_table.c.id, reference.id, values)


def save_judge_result(result: JudgeResult, run_id: str | None = None) -> None:
	values = result.model_dump(mode="json")
	values["run_id"] = run_id
	values["parse_ok"] = int(result.parse_ok)
	values["behaviour_match"] = (
		None if result.behaviour_match is None else int(result.behaviour_match)
	)
	_replace(
		judge_result_table,
		judge_result_table.c.conversation_id,
		result.conversation_id,
		values,
	)


def get_reference(reference_id: str) -> QuestionReference | None:
	with _get_engine().connect() as connection:
		row = connection.execute(
			select(reference_table).where(reference_table.c.id == reference_id)
		).mappings().one_or_none()
	return _reference_from_row(row) if row else None


def get_references(run_id: str) -> list[QuestionReference]:
	with _get_engine().connect() as connection:
		rows = connection.execute(
			select(reference_table).where(reference_table.c.run_id == run_id)
		).mappings().all()
	return [_reference_from_row(row) for row in rows]


def get_judge_results(run_id: str) -> list[JudgeResult]:
	with _get_engine().connect() as connection:
		rows = connection.execute(
			select(judge_result_table).where(judge_result_table.c.run_id == run_id)
		).mappings().all()
	return [_judge_result_from_row(row) for row in rows]


def _reference_from_row(row: Any) -> QuestionReference:
	values = dict(row)
	values.pop("run_id", None)
	return QuestionReference.model_validate(values)


def _judge_result_from_row(row: Any) -> JudgeResult:
	values = dict(row)
	values.pop("run_id", None)
	values["parse_ok"] = bool(values["parse_ok"])
	if values.get("behaviour_match") is not None:
		values["behaviour_match"] = bool(values["behaviour_match"])
	return JudgeResult.model_validate(values)


def get_run(run_id: str) -> RunRecord | None:
	with _get_engine().connect() as connection:
		row = connection.execute(select(run_table).where(run_table.c.id == run_id)).mappings().one_or_none()
	return RunRecord.model_validate(dict(row)) if row else None


def list_runs() -> list[RunRecord]:
	with _get_engine().connect() as connection:
		rows = connection.execute(select(run_table).order_by(run_table.c.started_at.desc())).mappings().all()
	return [RunRecord.model_validate(dict(row)) for row in rows]


def get_conversations(run_id: str) -> list[Conversation]:
	with _get_engine().connect() as connection:
		rows = connection.execute(
			select(conversation_table).where(conversation_table.c.run_id == run_id)
		).mappings().all()
	return [Conversation.model_validate(dict(row)) for row in rows]


def get_validations(run_id: str) -> list[ValidationResult]:
	with _get_engine().connect() as connection:
		rows = connection.execute(
			select(validation_table).where(validation_table.c.run_id == run_id)
		).mappings().all()
	return [ValidationResult.model_validate(dict(row)) for row in rows]


def get_questions(run_id: str) -> list[GeneratedQuestion]:
	with _get_engine().connect() as connection:
		rows = connection.execute(
			select(question_table).where(question_table.c.run_id == run_id)
		).mappings().all()
	return [GeneratedQuestion.model_validate(dict(row)) for row in rows]
