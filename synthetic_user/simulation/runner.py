import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from synthetic_user.adapters.base import Adapter, evaluable_from
from synthetic_user.core.models import (
	Conversation,
	GeneratedQuestion,
	Outcome,
	Persona,
	ProductProfile,
	QuestionReference,
	RunRecord,
	Turn,
)
from synthetic_user.core import tracing
from synthetic_user.core.storage import save_conversation, save_run
from synthetic_user.simulation import moderator, persona_agent


logger = logging.getLogger(__name__)


def _conversation_id(question: GeneratedQuestion, persona: Persona, seed: int) -> str:
	return f"{persona.id}:{question.id}:{seed}"


def _failure_conversation(
	question: GeneratedQuestion,
	persona: Persona,
	seed: int,
	turns: list[Turn],
	) -> Conversation:
	return Conversation(
		id=_conversation_id(question, persona, seed),
		question_id=question.id,
		repeat_index=0,
		seed=seed,
		persona_id=persona.id,
		persona_version=persona.version,
		turns=turns,
		outcome=Outcome.ADAPTER_ERROR,
		doubt_anywhere=any(
			turn.decision is not None and turn.decision.doubt_expressed for turn in turns
		),
		closing_summary=None,
	)


def run_conversation(
	question: GeneratedQuestion,
	persona: Persona,
	product_profile: ProductProfile,
	adapter: Adapter,
	llm: Any,
	seed: int,
	settings: Any,
	reference: QuestionReference | None = None,
	) -> Conversation:
	with tracing.trace(  # tracing
		question.question,
		question_id=question.id,
		persona_id=persona.id,
		complexity=question.complexity,
		intent=question.intent,
		seed=seed,
	):
		return _run_conversation(
			question, persona, product_profile, adapter, llm, seed, settings, reference
		)


def _run_conversation(
	question: GeneratedQuestion,
	persona: Persona,
	product_profile: ProductProfile,
	adapter: Adapter,
	llm: Any,
	seed: int,
	settings: Any,
	reference: QuestionReference | None = None,
	) -> Conversation:
	# The ONLY reference field the persona may see. Everything else about the
	# reference - tables, columns, filters, aggregation - would tell the user how
	# the answer is built, which is exactly what a real user does not know.
	original_intent = question.intent
	if reference is not None and reference.validation_status == "valid":
		original_intent = reference.expectation

	turns: list[Turn] = []
	state = moderator.ConversationState(
		max_turns=moderator.max_turns_for(persona, settings),
		token_budget=settings.token_budget_per_run,
	)
	session = None
	conversation: Conversation | None = None
	usage_at_start = getattr(llm, "tokens_used", 0)
	try:
		session = adapter.open_session()
		message = question.question
		while True:
			with tracing.span(f"turn {state.turn}", question_id=question.id):  # tracing
				try:
					response = adapter.send(session, message)
				except Exception as error:
					logger.warning("Adapter failed in conversation %s: %s", question.id, error)
					response = _adapter_error_response(error)
					state.record_turn(message, response)
					turns.append(
						Turn(
							turn_index=state.turn - 1,
							message=message,
							response=response,
							decision=None,
						)
					)
					conversation = _failure_conversation(question, persona, seed, turns)
					break

				state.record_turn(message, response)
				decision = persona_agent.decide(
					persona,
					product_profile,
					original_intent,
					state,
					llm,
					seed,
				)
				turns.append(
					Turn(
						turn_index=state.turn - 1,
						message=message,
						response=response,
						decision=decision,
						evaluable=evaluable_from(adapter, response),
					)
				)
				state.tokens_used = getattr(llm, "tokens_used", usage_at_start) - usage_at_start
				outcome = moderator.check(state, decision)
				if outcome is not None:
					conversation = _build_conversation(question, persona, seed, turns, outcome)
					break
				if decision.next_question is None:
					conversation = _build_conversation(question, persona, seed, turns, Outcome.ABANDONED)
					break
				message = decision.next_question
	finally:
		if session is not None:
			try:
				adapter.close_session(session)
			except Exception as error:
				logger.warning("Adapter close failed in conversation %s: %s", question.id, error)

	if conversation is None:
		conversation = _failure_conversation(question, persona, seed, turns)
	conversation = conversation.model_copy(
		update={
			"closing_summary": persona_agent.closing_summary(persona, conversation, llm),
			"doubt_anywhere": any(
				turn.decision is not None and turn.decision.doubt_expressed for turn in conversation.turns
			),
		}
	)
	return conversation


def _adapter_error_response(error: Exception):
	from synthetic_user.core.models import AdapterResponse

	return AdapterResponse(reply="", error=str(error), latency_ms=0)


def _build_conversation(
	question: GeneratedQuestion,
	persona: Persona,
	seed: int,
	turns: list[Turn],
	outcome: Outcome,
) -> Conversation:
	return Conversation(
		id=_conversation_id(question, persona, seed),
		question_id=question.id,
		repeat_index=0,
		seed=seed,
		persona_id=persona.id,
		persona_version=persona.version,
		turns=turns,
		outcome=outcome,
		doubt_anywhere=any(
			turn.decision is not None and turn.decision.doubt_expressed for turn in turns
		),
		closing_summary=None,
	)


def _stable_seed(seed_base: int, persona_id: str, question_id: str, repeat_index: int) -> int:
	value = f"{persona_id}\0{question_id}\0{repeat_index}".encode("utf-8")
	digest = hashlib.sha256(value).digest()
	return seed_base + int.from_bytes(digest[:8], "big") % (2**63 - 1)


def run_grid(
	personas: list[Persona],
	questions_by_persona: dict[str, list[GeneratedQuestion]],
	product_profile: ProductProfile,
	adapter: Adapter,
	llm: Any,
	settings: Any,
	references_by_question: dict[str, QuestionReference] | None = None,
) -> RunRecord:
	started_at = datetime.now(timezone.utc)
	seed_base = getattr(settings, "seed_base", 0)
	run_id = f"run-{seed_base}-{started_at.strftime('%Y%m%d%H%M%S%f')}"
	conversation_ids: list[str] = []
	repeats = settings.repeats_per_question

	# Claim the run id before any conversation is written. save_run only used to
	# happen at the end, so an interrupted grid left its conversations in the
	# database with no run record - invisible to get_run, metrics and every other
	# read path. The record is rewritten with the full conversation list below.
	_save_run_record(
		run_id, started_at, started_at, personas, product_profile, settings, seed_base, []
	)

	with tracing.session(run_id, tags=["run"]):  # tracing
		for persona in personas:
			for question in questions_by_persona.get(persona.id, []):
				for repeat_index in range(repeats):
					seed = _stable_seed(seed_base, persona.id, question.id, repeat_index)
					reference = (references_by_question or {}).get(question.id)
					try:
						conversation = run_conversation(
							question,
							persona,
							product_profile,
							adapter,
							llm,
							seed,
							settings,
							reference,
						)
						conversation = conversation.model_copy(
							update={
								"id": f"{run_id}:{conversation.id}",
								"repeat_index": repeat_index,
							}
						)
					except Exception as error:
						logger.exception("Conversation failed; continuing grid: %s", error)
						conversation = _failure_conversation(question, persona, seed, [])
						conversation = conversation.model_copy(
							update={
								"id": f"{run_id}:{conversation.id}",
								"repeat_index": repeat_index,
							}
						)
					try:
						save_conversation(conversation, run_id=run_id)
					except Exception as error:
						logger.exception("Could not save conversation %s: %s", conversation.id, error)
					conversation_ids.append(conversation.id)

	ended_at = datetime.now(timezone.utc)
	return _save_run_record(
		run_id, started_at, ended_at, personas, product_profile, settings, seed_base, conversation_ids
	)


def _save_run_record(
	run_id: str,
	started_at: datetime,
	finished_at: datetime,
	personas: list[Persona],
	product_profile: ProductProfile,
	settings: Any,
	seed_base: int,
	conversation_ids: list[str],
) -> RunRecord:
	run = RunRecord(
		id=run_id,
		started_at=started_at,
		finished_at=finished_at,
		persona_versions={persona.id: persona.version for persona in personas},
		prompt_versions={
			"generation": "generate_v2",
			"turn_decision": "turn_decision_v3",
			"closing_summary": "closing_summary_v2",
		},
		model_versions={
			"generation": settings.model_generation,
			"persona": settings.model_persona,
			"validator": settings.model_validator,
			"product": settings.model_product,
		},
		product_version=product_profile.version,
		spec_version=getattr(settings, "spec_version", None),
		seed_base=seed_base,
		conversation_ids=conversation_ids,
	)
	save_run(run)
	return run
