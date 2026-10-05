import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any

import yaml

from synthetic_user.core import tracing
from synthetic_user.core.models import (
	CapabilitySpec,
	Conversation,
	ProductProfile,
	ValidationResult,
)
from synthetic_user.core.prompts import load_prompt


logger = logging.getLogger(__name__)
_CLASSIFICATIONS = {
	"capability_failure",
	"not_supported_yet",
	"undocumented_gap",
	"too_generic",
	"off_domain",
	"handled_correctly",
}
_STOP_WORDS = {"a", "an", "and", "did", "for", "is", "my", "of", "the", "to", "what"}


def _normalise(text: str) -> str:
	return " ".join(text.casefold().split())


def _tokens(text: str) -> set[str]:
	return {
		token
		for token in re.findall(r"[a-z0-9]+", text.casefold())
		if token not in _STOP_WORDS and len(token) > 2
	}


def _questions_repeat(conversation: Conversation, threshold: float) -> bool:
	questions = [_normalise(turn.message) for turn in conversation.turns]
	return any(
		SequenceMatcher(None, current, previous).ratio() >= threshold
		for index, current in enumerate(questions)
		for previous in questions[:index]
	)


def _final_question_matches_intent(conversation: Conversation, original_intent: str) -> bool:
	if not conversation.turns:
		return True
	final_question = conversation.turns[-1].message
	final_tokens = _tokens(final_question)
	intent_tokens = _tokens(original_intent)
	if final_tokens & intent_tokens:
		return True
	return SequenceMatcher(
		None,
		_normalise(final_question),
		_normalise(original_intent),
	).ratio() >= 0.35


def _deterministic_flags(
	conversation: Conversation,
	original_intent: str,
	settings: Any,
) -> list[str]:
	flags: list[str] = []
	if not conversation.turns or not any(
		turn.message.strip() or turn.response.reply.strip() for turn in conversation.turns
	):
		flags.append("zero_substantive_turns")

	threshold = getattr(settings, "similarity_threshold", 0.92)
	if _questions_repeat(conversation, threshold):
		flags.append("repeated_question")

	if conversation.turns:
		error_count = sum(bool(turn.response.error) for turn in conversation.turns)
		if error_count > len(conversation.turns) / 2:
			flags.append("adapter_error_majority")
		if all(len(turn.response.reply.strip()) < 20 for turn in conversation.turns):
			flags.append("empty_or_too_short_replies")

	if conversation.turns and not _final_question_matches_intent(conversation, original_intent):
		flags.append("final_question_off_intent")
	return flags


def _yaml(value: Any) -> str:
	if hasattr(value, "model_dump"):
		value = value.model_dump(mode="json")
	return yaml.safe_dump(value, sort_keys=False)


def _transcript(conversation: Conversation) -> str:
	lines: list[str] = []
	for turn in conversation.turns:
		lines.append(f"USER: {turn.message}")
		lines.append(f"TOOL: {turn.response.reply}")
	return "\n".join(lines)


def _render_prompt(
	conversation: Conversation,
	original_intent: str,
	product_profile: ProductProfile,
	capability_spec: CapabilitySpec,
	deterministic_flags: list[str],
) -> str:
	prompt = load_prompt("classify_v2.md")
	replacements = {
		"<<PRODUCT_PROFILE_YAML>>": _yaml(product_profile),
		"<<CAPABILITY_SPEC_YAML>>": _yaml(capability_spec),
		"<<ORIGINAL_INTENT>>": original_intent,
		"<<TRANSCRIPT>>": _transcript(conversation),
		"<<DETERMINISTIC_FLAGS>>": _yaml(deterministic_flags),
	}
	for marker, value in replacements.items():
		prompt = prompt.replace(marker, value)
	return prompt


def _strip_json_fences(response: str) -> str:
	text = response.strip()
	if text.startswith("```") and text.endswith("```"):
		return "\n".join(text.splitlines()[1:-1]).strip()
	return text


def validate(
	conversation: Conversation,
	original_intent: str,
	product_profile: ProductProfile,
	capability_spec: CapabilitySpec | None,
	llm: Any,
	settings: Any,
) -> ValidationResult:
	flags = _deterministic_flags(conversation, original_intent, settings)
	if capability_spec is None:
		return ValidationResult(
			conversation_id=conversation.id,
			classification=None,
			refused_clearly=None,
			cleanup_suspected=False,
			deterministic_flags=[*flags, "capability_spec_missing"],
			reason="Capability specification missing; deterministic validation only.",
		)

	try:
		with tracing.trace(  # tracing
			"validate",
			conversation_id=conversation.id,
			question_id=conversation.question_id,
			persona_id=conversation.persona_id,
			outcome=conversation.outcome.value,
		):
			response = llm.complete(
				prompt=_render_prompt(
					conversation,
					original_intent,
					product_profile,
					capability_spec,
					flags,
				),
				role="validator",
				seed=conversation.seed,
			)
		result = json.loads(_strip_json_fences(response))
		classification = result.get("classification")
		if classification not in _CLASSIFICATIONS:
			raise ValueError("invalid validation classification")
		return ValidationResult(
			conversation_id=conversation.id,
			classification=classification,
			refused_clearly=result.get("refused_clearly"),
			cleanup_suspected=bool(result.get("cleanup_suspected", False)),
			deterministic_flags=flags,
			reason=str(result.get("reason", "")),
		)
	except Exception as error:
		logger.warning("Validation classification failed: %s", error)
		return ValidationResult(
			conversation_id=conversation.id,
			classification=None,
			refused_clearly=None,
			cleanup_suspected=False,
			deterministic_flags=[*flags, "classification_failed"],
			reason="Classification failed; deterministic flags retained.",
		)
