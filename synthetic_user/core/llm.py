from collections import deque
from collections.abc import Iterable
from typing import Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from synthetic_user.core import tracing
from synthetic_user.core.config import Settings, get_settings


def _status_code(error: BaseException) -> int | None:
	for candidate in (
		getattr(error, "status_code", None),
		getattr(error, "code", None),
		getattr(getattr(error, "response", None), "status_code", None),
	):
		if isinstance(candidate, int):
			return candidate
	return None


def _is_retryable(error: BaseException) -> bool:
	status_code = _status_code(error)
	return status_code == 429 or status_code is not None and 500 <= status_code <= 599


class LLMClient:
	_role_fields = {
		"generation": "model_generation",
		"persona": "model_persona",
		"validator": "model_validator",
		"product": "model_product",
		"reference_generation": "model_reference_generation",
		"reference_validator": "model_reference_validator",
		"judge": "model_judge",
	}

	def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
		self._settings = settings or get_settings()
		self._client = client
		self._usage = 0

		if self._client is None:
			from google import genai

			self._client = genai.Client(api_key=self._settings.llm_api_key)

	def complete(
		self,
		prompt: str,
		role: str,
		seed: int | None = None,
		temperature: float = 0.7,
		system: str | None = None,
	) -> str:
		try:
			model_field = self._role_fields[role]
		except KeyError as error:
			raise ValueError(f"Unsupported LLM role: {role}") from error

		model = getattr(self._settings, model_field)
		with tracing.llm_call(  # tracing
			name=f"llm.{role}",
			model=model,
			prompt=prompt,
			role=role,
			seed=seed,
			temperature=temperature,
		) as call:
			response = self._generate(
				model=model,
				prompt=prompt,
				seed=seed,
				temperature=temperature,
				system=system,
			)
			usage_metadata = getattr(response, "usage_metadata", None)
			total_tokens = int(getattr(usage_metadata, "total_token_count", 0) or 0)
			self._usage += total_tokens
			call.finish(output=response.text, usage={"total": total_tokens})
			return response.text

	@property
	def tokens_used(self) -> int:
		return self._usage

	def reset_usage(self) -> None:
		self._usage = 0

	@retry(
		retry=retry_if_exception(_is_retryable),
		wait=wait_random_exponential(multiplier=1, max=8),
		stop=stop_after_attempt(4),
		reraise=True,
	)
	def _generate(
		self,
		model: str,
		prompt: str,
		seed: int | None,
		temperature: float,
		system: str | None,
	) -> Any:
		from google.genai import types

		config_values: dict[str, Any] = {"temperature": temperature}
		if system is not None:
			config_values["system_instruction"] = system
		if seed is not None:
			config_values["seed"] = seed % (2**31 - 1)

		try:
			config = types.GenerateContentConfig(**config_values)
		except TypeError:
			config_values.pop("seed", None)
			config = types.GenerateContentConfig(**config_values)

		try:
			return self._client.models.generate_content(
				model=model,
				contents=prompt,
				config=config,
			)
		except TypeError:
			if "seed" not in config_values:
				raise
			config_values.pop("seed", None)
			config = types.GenerateContentConfig(**config_values)
			return self._client.models.generate_content(
				model=model,
				contents=prompt,
				config=config,
			)


class StubLLMClient:
	def __init__(self, responses: Iterable[str]) -> None:
		self._responses = deque(responses)
		self._usage = 0

	def complete(
		self,
		prompt: str,
		role: str,
		seed: int | None = None,
		temperature: float = 0.7,
		system: str | None = None,
	) -> str:
		del prompt, role, seed, temperature, system
		if not self._responses:
			raise RuntimeError("StubLLMClient has no queued responses")
		return self._responses.popleft()

	@property
	def tokens_used(self) -> int:
		return self._usage

	def reset_usage(self) -> None:
		self._usage = 0
