"""Reference-guided judging of one conversation.

Sits alongside validation/ rather than inside it: validation asks whether the
SIMULATION was sound, this asks whether the PRODUCT was right.

`overall` is computed in code, never by the model. A judge that can pick its own
headline verdict can quietly contradict its own field verdicts, and there is no
way to audit that afterwards.

Nothing here executes SQL.
"""

import json
import logging
from typing import Any

from synthetic_user.core.models import Conversation, JudgeResult, QuestionReference, Turn
from synthetic_user.core.prompts import load_prompt
from synthetic_user.evaluation.reference_profile import ReferenceProfile
from synthetic_user.evaluation.sql_profile import SqlReferenceProfile

logger = logging.getLogger(__name__)

PROMPT_VERSION = "judge_sql_v1"

JUDGED_FIELDS = (
	"tables",
	"columns",
	"filters",
	"aggregation",
	"grain",
	"time_window",
	"analysis_type",
	"expected_output_columns",
)

FAILURE_TAGS = frozenset(
	{
		"wrong_table",
		"wrong_column",
		"missing_filter",
		"extra_filter",
		"wrong_aggregation",
		"wrong_grain",
		"wrong_time_window",
		"wrong_analysis",
		"should_have_refused",
		"should_have_clarified",
		"other",
	}
)

_VERDICTS = frozenset({"match", "acceptable_alternative", "mismatch", "not_applicable"})

# Field verdict -> the tag that explains it, where one exists.
_FIELD_TAGS = {
	"tables": "wrong_table",
	"columns": "wrong_column",
	"filters": "missing_filter",
	"aggregation": "wrong_aggregation",
	"grain": "wrong_grain",
	"time_window": "wrong_time_window",
	"analysis_type": "wrong_analysis",
}


def _strip_json_fences(response: str) -> str:
	text = response.strip()
	if text.startswith("```"):
		text = "\n".join(text.splitlines()[1:])
	if text.endswith("```"):
		text = "\n".join(text.splitlines()[:-1])
	return text.strip()


def _transcript(conversation: Conversation) -> str:
	lines = []
	for turn in conversation.turns:
		lines.append(f"USER: {turn.message}")
		lines.append(f"PRODUCT: {turn.response.reply}")
	return "\n".join(lines)


def _last_sql_turn(conversation: Conversation, kind: str) -> tuple[Turn | None, dict[str, Any] | None]:
	for turn in reversed(conversation.turns):
		evaluable = turn.evaluable
		if evaluable and evaluable.get("kind") == kind and evaluable.get("query"):
			return turn, evaluable
	return None, None


def _glossary_text(glossary: dict[str, str] | None) -> str:
	if not glossary:
		return "No glossary is available."
	return "\n".join(f"- {term}: {definition}" for term, definition in glossary.items())


def _render_prompt(
	conversation: Conversation,
	reference: QuestionReference,
	evaluable: dict[str, Any] | None,
	checks: dict[str, Any],
	glossary: dict[str, str] | None,
	profile: ReferenceProfile,
) -> str:
	prompt = load_prompt(profile.judge_prompt)
	final_turn = conversation.turns[-1] if conversation.turns else None
	# Few-shots are deliberately absent: the judge must decide from the reference
	# and the schema, not pattern-match against examples it has already seen.
	replacements = {
		"<<QUESTION>>": conversation.turns[0].message if conversation.turns else "",
		"<<TRANSCRIPT>>": _transcript(conversation),
		"<<EXPECTED_BEHAVIOUR>>": reference.expected_behaviour,
		"<<FINAL_SQL>>": (evaluable or {}).get("query") or "The product produced no query.",
		"<<FINAL_REPLY>>": final_turn.response.reply if final_turn else "",
		"<<REFERENCE_JSON>>": json.dumps(
			reference.model_dump(
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
			),
			indent=2,
		),
		"<<PARSED_STRUCTURE>>": json.dumps(checks.get("structure", {}), indent=2),
		"<<GLOSSARY>>": _glossary_text(glossary),
	}
	for marker, value in replacements.items():
		prompt = prompt.replace(marker, value)
	return prompt


def _clean_verdicts(raw: Any) -> dict[str, str]:
	if not isinstance(raw, dict):
		return {}
	return {
		str(field): str(verdict)
		for field, verdict in raw.items()
		if str(field) in JUDGED_FIELDS and str(verdict) in _VERDICTS
	}


def _derive_tags(
	verdicts: dict[str, str], reference: QuestionReference, behaviour_match: bool | None
) -> set[str]:
	"""Tags the field verdicts imply, independent of what the model volunteered."""
	tags = {
		_FIELD_TAGS[field]
		for field, verdict in verdicts.items()
		if verdict == "mismatch" and field in _FIELD_TAGS
	}
	if behaviour_match is False:
		if reference.expected_behaviour == "refuse":
			tags.add("should_have_refused")
		elif reference.expected_behaviour == "clarify":
			tags.add("should_have_clarified")
	return tags


def _overall(
	reference: QuestionReference,
	verdicts: dict[str, str],
	behaviour_match: bool | None,
	reference_suspect: bool,
	parse_ok: bool,
	llm_ok: bool,
) -> str:
	if reference_suspect:
		return "reference_suspect"
	if not parse_ok and not llm_ok:
		return "not_evaluable"
	if behaviour_match is False:
		return "incorrect"
	if any(verdicts.get(field) == "mismatch" for field in reference.must_match):
		return "incorrect"
	if any(verdict == "mismatch" for verdict in verdicts.values()):
		return "partially_correct"
	return "correct"


def _not_evaluable(
	conversation: Conversation,
	reference: QuestionReference,
	evaluated_turn: int | None,
	checks: dict[str, Any],
	reason: str,
	judge_model: str,
) -> JudgeResult:
	return JudgeResult(
		conversation_id=conversation.id,
		question_id=reference.question_id,
		evaluated_turn=evaluated_turn,
		parse_ok=bool(checks.get("parse_ok")),
		table_precision=checks.get("table_precision"),
		table_recall=checks.get("table_recall"),
		column_precision=checks.get("column_precision"),
		column_recall=checks.get("column_recall"),
		field_verdicts={},
		behaviour_match=None,
		overall="not_evaluable",
		failure_tags=[],
		reason=reason,
		judge_model=judge_model,
		prompt_version=PROMPT_VERSION,
	)


def judge(
	conversation: Conversation,
	reference: QuestionReference,
	llm: Any,
	glossary: dict[str, str] | None = None,
	profile: ReferenceProfile | None = None,
	settings: Any = None,
) -> JudgeResult:
	"""Judge one conversation against its reference. Never raises."""
	if settings is None:
		try:
			from synthetic_user.core.config import get_settings

			settings = get_settings()
		except Exception:  # noqa: BLE001 - judging must work without full config
			settings = None

	dialect = getattr(settings, "sql_dialect", "duckdb")
	judge_model = getattr(settings, "model_judge", "unknown")
	profile = profile or SqlReferenceProfile(dialect=dialect)

	turn, evaluable = _last_sql_turn(conversation, profile.evaluable_kind)
	evaluated_turn = turn.turn_index if turn is not None else None
	behaviour_only = reference.expected_behaviour in ("refuse", "clarify")

	checks = profile.deterministic_checks(reference, evaluable)

	if not conversation.turns:
		return _not_evaluable(
			conversation, reference, None, checks, "Conversation has no turns.", judge_model
		)

	try:
		prompt = _render_prompt(conversation, reference, evaluable, checks, glossary, profile)
		raw = llm.complete(prompt=prompt, role="judge", seed=conversation.seed, temperature=0.0)
		verdict = json.loads(_strip_json_fences(raw))
		if not isinstance(verdict, dict):
			raise ValueError("judge response was not a JSON object")
		llm_ok = True
	except Exception as error:  # noqa: BLE001
		logger.warning("Judge call failed for %s: %s", conversation.id, error)
		verdict, llm_ok = {}, False

	# A behaviour-only item has no SQL to parse, so parse_ok must not be read as
	# a failure - there was nothing to attempt.
	parse_ok = True if behaviour_only else bool(checks.get("parse_ok"))

	if not llm_ok and not parse_ok:
		return _not_evaluable(
			conversation,
			reference,
			evaluated_turn,
			checks,
			"Query could not be parsed and the judge was unavailable.",
			judge_model,
		)

	verdicts = _clean_verdicts(verdict.get("field_verdicts"))
	behaviour_match = verdict.get("behaviour_match")
	if not isinstance(behaviour_match, bool):
		behaviour_match = None
	reference_suspect = verdict.get("reference_suspect") is True

	tags = {str(tag) for tag in (verdict.get("failure_tags") or []) if str(tag) in FAILURE_TAGS}
	tags |= _derive_tags(verdicts, reference, behaviour_match)
	if behaviour_only and not behaviour_only_satisfied(reference, evaluable, behaviour_match):
		tags.add("should_have_refused" if reference.expected_behaviour == "refuse" else "should_have_clarified")

	overall = _overall(
		reference, verdicts, behaviour_match, reference_suspect, parse_ok, llm_ok
	)

	return JudgeResult(
		conversation_id=conversation.id,
		question_id=reference.question_id,
		evaluated_turn=evaluated_turn,
		parse_ok=parse_ok,
		table_precision=checks.get("table_precision"),
		table_recall=checks.get("table_recall"),
		column_precision=checks.get("column_precision"),
		column_recall=checks.get("column_recall"),
		field_verdicts=verdicts,
		behaviour_match=behaviour_match,
		overall=overall,
		failure_tags=sorted(tags),
		reason=str(verdict.get("reason", "")) or "No reason given.",
		judge_model=judge_model,
		prompt_version=PROMPT_VERSION,
	)


def behaviour_only_satisfied(
	reference: QuestionReference, evaluable: dict[str, Any] | None, behaviour_match: bool | None
) -> bool:
	"""A refuse/clarify item that still produced a query has failed, whatever the
	model said about it - producing SQL is itself the evidence."""
	if evaluable and evaluable.get("query"):
		return False
	return behaviour_match is not False
