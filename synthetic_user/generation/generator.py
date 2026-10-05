import json
import logging
from typing import Any

import yaml

from synthetic_user.core import tracing
from synthetic_user.core.models import GeneratedQuestion, Persona, ProductProfile
from synthetic_user.core.prompts import load_prompt


logger = logging.getLogger(__name__)


def _yaml(value: Any) -> str:
	if hasattr(value, "model_dump"):
		value = value.model_dump(mode="json")
	return yaml.safe_dump(value, sort_keys=False)


def _strip_json_fences(response: str) -> str:
	text = response.strip()
	if text.startswith("```") and text.endswith("```"):
		lines = text.splitlines()
		return "\n".join(lines[1:-1]).strip()
	return text


def _parse_questions(response: str) -> list[GeneratedQuestion]:
	data = json.loads(_strip_json_fences(response))
	if not isinstance(data, list):
		raise ValueError("generation response must be a JSON array")
	return [GeneratedQuestion.model_validate(item) for item in data]


def _render_prompt(
	persona: Persona,
	product_profile: ProductProfile,
	past_queries: str | None,
	n: int,
) -> str:
	prompt = load_prompt("generate_v2.md")
	replacements = {
		"<<PERSONA_YAML>>": _yaml(persona.character_sheet()),
		"<<PRODUCT_PROFILE_YAML>>": _yaml(product_profile),
		"<<PAST_QUERIES>>": past_queries or "No past queries are available.",
		"<<QUESTION_COUNT>>": str(n),
	}
	for marker, value in replacements.items():
		prompt = prompt.replace(marker, value)
	return prompt


def generate_questions(
	persona: Persona,
	product_profile: ProductProfile,
	past_queries: str | None,
	n: int,
	llm: Any,
	seed: int,
) -> list[GeneratedQuestion]:
	with tracing.trace(  # tracing
		f"generate {n} questions",
		persona_id=persona.id,
		product=product_profile.product_name,
		count=n,
	):
		return _generate_questions(persona, product_profile, past_queries, n, llm, seed)


def _generate_questions(
	persona: Persona,
	product_profile: ProductProfile,
	past_queries: str | None,
	n: int,
	llm: Any,
	seed: int,
) -> list[GeneratedQuestion]:
	try:
		prompt = _render_prompt(persona, product_profile, past_queries, n)
		try:
			return _parse_questions(llm.complete(prompt=prompt, role="generation", seed=seed))
		except Exception:
			strict_prompt = prompt + "\n\nSTRICT REMINDER: Return only a valid JSON array."
			try:
				return _parse_questions(
					llm.complete(prompt=strict_prompt, role="generation", seed=seed)
				)
			except Exception as error:
				logger.warning("Could not parse generated questions after one retry: %s", error)
				return []
	except Exception as error:
		logger.warning("Could not generate questions: %s", error)
		return []
