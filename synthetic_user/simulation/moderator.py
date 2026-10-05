from difflib import SequenceMatcher

from synthetic_user.core.config import Settings, get_settings
from synthetic_user.core.models import AdapterResponse, Outcome, Persona, TurnDecision


class ConversationState:
	turn: int
	max_turns: int
	questions_asked: list[str]
	consecutive_errors: int
	tokens_used: int
	token_budget: int
	turns: list[dict[str, object]]

	def __init__(
		self,
		turn: int = 0,
		max_turns: int = 0,
		questions_asked: list[str] | None = None,
		consecutive_errors: int = 0,
		tokens_used: int = 0,
		token_budget: int = 0,
	) -> None:
		self.turn = turn
		self.max_turns = max_turns
		self.questions_asked = questions_asked if questions_asked is not None else []
		self.consecutive_errors = consecutive_errors
		self.tokens_used = tokens_used
		self.token_budget = token_budget
		self.turns = []

	def record_turn(self, message: str, response: AdapterResponse) -> None:
		self.turn += 1
		self.questions_asked.append(message)
		self.turns.append({"message": message, "response": response})
		if response.error:
			self.consecutive_errors += 1
		else:
			self.consecutive_errors = 0


def _normalise_question(question: str) -> str:
	return " ".join(question.casefold().split())


def _is_too_similar(question: str, previous_questions: list[str], threshold: float) -> bool:
	normalised = _normalise_question(question)
	return any(
		SequenceMatcher(None, normalised, _normalise_question(previous)).ratio() >= threshold
		for previous in previous_questions
	)


def check(state: ConversationState, decision: TurnDecision) -> Outcome | None:
	if decision.satisfied:
		return Outcome.COMPLETED
	if decision.giving_up:
		return Outcome.ABANDONED
	if state.turn >= state.max_turns:
		return Outcome.MAX_TURNS
	if decision.next_question and _is_too_similar(
		decision.next_question,
		state.questions_asked,
		get_settings().similarity_threshold,
	):
		return Outcome.LOOPED
	if state.consecutive_errors >= 3:
		return Outcome.ADAPTER_ERROR
	if state.tokens_used > state.token_budget:
		return Outcome.BUDGET
	return None


def max_turns_for(persona: Persona, settings: Settings) -> int:
	return min(persona.patience + 1, settings.hard_max_turns)
