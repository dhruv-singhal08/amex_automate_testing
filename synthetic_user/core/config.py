from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
	model_config = SettingsConfigDict(
		env_file=".env",
		env_file_encoding="utf-8",
		env_prefix="",
		extra="ignore",
	)

	llm_provider: str = Field(
		default="gemini",
		validation_alias=AliasChoices("LLM_PROVIDER", "GEMINI_PROVIDER"),
	)
	llm_api_key: str = Field(validation_alias=AliasChoices("LLM_API_KEY", "GEMINI_API_KEY"))
	model_generation: str = Field(
		validation_alias=AliasChoices("LLM_MODEL_GENERATION", "MODEL_GENERATION", "GEMINI_MODEL")
	)
	model_persona: str = Field(
		validation_alias=AliasChoices("LLM_MODEL_PERSONA", "MODEL_PERSONA", "GEMINI_MODEL")
	)
	model_validator: str = Field(
		validation_alias=AliasChoices("LLM_MODEL_VALIDATOR", "MODEL_VALIDATOR", "GEMINI_MODEL")
	)
	model_product: str = Field(
		validation_alias=AliasChoices("LLM_MODEL_PRODUCT", "MODEL_PRODUCT", "GEMINI_MODEL")
	)
	# Reference-guided evaluation roles. Separately configurable so the judge can
	# run on a different model family from the product it is judging.
	model_reference_generation: str = Field(
		validation_alias=AliasChoices(
			"LLM_MODEL_REFERENCE_GENERATION", "MODEL_REFERENCE_GENERATION", "GEMINI_MODEL"
		)
	)
	model_reference_validator: str = Field(
		validation_alias=AliasChoices(
			"LLM_MODEL_REFERENCE_VALIDATOR", "MODEL_REFERENCE_VALIDATOR", "GEMINI_MODEL"
		)
	)
	model_judge: str = Field(
		validation_alias=AliasChoices("LLM_MODEL_JUDGE", "MODEL_JUDGE", "GEMINI_MODEL")
	)
	database_url: str = "sqlite:///runs/platform.db"
	repeats_per_question: int = 3
	hard_max_turns: int = 8
	token_budget_per_run: int = 500_000
	similarity_threshold: float = 0.92
	reject_flagged: bool = False

	sql_dialect: str = "duckdb"
	fewshot_mix: dict[str, float] = Field(
		default_factory=lambda: {"near": 0.3, "pattern": 0.4, "schema_only": 0.3}
	)
	unanswerable_ratio: float = 0.1
	reference_consistency_samples: int = 1


@lru_cache(maxsize=1)
def get_settings() -> Settings:
	return Settings()
