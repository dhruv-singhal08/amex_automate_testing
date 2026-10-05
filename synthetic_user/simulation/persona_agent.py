import json
import logging
from typing import Any

import yaml

from synthetic_user.core.models import Conversation, Outcome, Persona, ProductProfile, TurnDecision
from synthetic_user.core.prompts import load_prompt


logger = logging.getLogger(__name__)


def _yaml(value: Any) -> str:
	if hasattr(value, "model_dump"):
		value = value.model_dump(mode="json")
	return yaml.safe_dump(value, sort_keys=False)


def _transcript(state: Any) -> str:
	entries = getattr(state, "transcript", None)
	if entries is None:
		entries = getattr(state, "turns", None)
	if entries is None:
		entries = [
			{"message": question, "response": {"reply": ""}}
			for question in getattr(state, "questions_asked", [])
		]
	if isinstance(entries, str):
		return entries

	lines: list[str] = []
	for entry in entries:
		message = entry.get("message", "") if isinstance(entry, dict) else getattr(entry, "message", "")
		response = entry.get("response") if isinstance(entry, dict) else getattr(entry, "response", None)
		reply = response.get("reply", "") if isinstance(response, dict) else getattr(response, "reply", "")
		lines.append(f"USER: {message}")
		lines.append(f"TOOL: {reply}")
	return "\n".join(lines)


def _render_turn_prompt(
	persona: Persona,
	product_profile: ProductProfile,
	original_intent: str,
	state: Any,
) -> str:
	template = load_prompt("turn_decision_v3.md")
	questions_asked = list(getattr(state, "questions_asked", []))
	remaining = max(0, getattr(state, "max_turns", 0) - getattr(state, "turn", 0))
	replacements = {
		"<<PERSONA_YAML>>": _yaml(persona.character_sheet()),
		"<<AMBIGUITY_RESPONSE>>": persona.ambiguity_response,
		"<<PRODUCT_PROFILE_YAML>>": _yaml(product_profile),
		"<<TRANSCRIPT>>": _transcript(state),
		"<<ORIGINAL_INTENT>>": original_intent,
		"<<FOLLOW_UPS_USED>>": _yaml(questions_asked),
		"<<FOLLOW_UPS_REMAINING>>": str(remaining),
	}
	for marker, value in replacements.items():
		template = template.replace(marker, value)
	return template


def _strip_json_fences(response: str) -> str:
	text = response.strip()
	if text.startswith("```") and text.endswith("```"):
		lines = text.splitlines()
		return "\n".join(lines[1:-1]).strip()
	return text


def _parse_decision(response: str) -> TurnDecision:
	return TurnDecision.model_validate(json.loads(_strip_json_fences(response)))


def _fallback_decision() -> TurnDecision:
	return TurnDecision(
		satisfied=False,
		giving_up=True,
		next_question=None,
		doubt_expressed=False,
		reasoning="parse failure",
	)


def decide(
	persona: Persona,
	product_profile: ProductProfile,
	original_intent: str,
	state: Any,
	llm: Any,
	seed: int,
) -> TurnDecision:
	try:
		prompt = _render_turn_prompt(persona, product_profile, original_intent, state)
		response = llm.complete(prompt=prompt, role="persona", seed=seed)
		try:
			return _parse_decision(response)
		except Exception:
			strict_prompt = (
				prompt
				+ "\n\nSTRICT REMINDER: Return only one valid JSON object matching TurnDecision."
			)
			try:
				return _parse_decision(
					llm.complete(prompt=strict_prompt, role="persona", seed=seed)
				)
			except Exception:
				logger.warning("Could not parse persona turn decision after one retry")
				return _fallback_decision()
	except Exception:
		logger.warning("Could not produce persona turn decision", exc_info=True)
		return _fallback_decision()


_OUTCOME_IN_PLAIN_WORDS = {
	Outcome.COMPLETED: "You got what you needed and stopped there.",
	Outcome.ABANDONED: "You gave up without getting what you needed.",
	Outcome.MAX_TURNS: "You ran out of patience and stopped without getting what you needed.",
	Outcome.LOOPED: "You kept going in circles without getting what you needed, and stopped.",
	Outcome.ADAPTER_ERROR: "The tool kept failing, so you stopped without getting what you needed.",
	Outcome.BUDGET: "You stopped before getting what you needed.",
}


def _how_it_ended(conversation: Conversation) -> str:
	ending = _OUTCOME_IN_PLAIN_WORDS.get(conversation.outcome, "You stopped there.")
	final_decision = next(
		(turn.decision for turn in reversed(conversation.turns) if turn.decision is not None),
		None,
	)
	if final_decision is not None and final_decision.reasoning:
		return f"{ending} Your last thought was: {final_decision.reasoning}"
	return ending


def closing_summary(persona: Persona, conversation: Conversation, llm: Any) -> str:
	try:
		prompt = load_prompt("closing_summary_v2.md")
		prompt = prompt.replace("<<PERSONA_YAML>>", _yaml(persona.character_sheet()))
		prompt = prompt.replace("<<HOW_IT_ENDED>>", _how_it_ended(conversation))
		prompt = prompt.replace("<<TRANSCRIPT>>", _transcript(conversation))
		return llm.complete(prompt=prompt, role="persona").strip()
	except Exception:
		logger.warning("Could not produce persona closing summary", exc_info=True)
		return ""
