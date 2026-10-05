from pathlib import Path

from synthetic_user.assets.persona import load_persona
from synthetic_user.assets.product import (
	load_capability_spec,
	load_fewshots,
	load_glossary,
	load_product_profile,
	load_schema_spec,
)
from synthetic_user.core.models import (
	CapabilitySpec,
	FewShotExample,
	Persona,
	ProductProfile,
	SchemaSpec,
)


_ASSETS_DIR = Path(__file__).parents[2] / "assets"


def persona_path(name: str) -> Path:
	return _ASSETS_DIR / "personas" / f"{name}.yaml"


def product_path(name: str) -> Path:
	return _ASSETS_DIR / "products" / f"{name}.yaml"


def load_named_persona(name: str) -> Persona:
	return load_persona(persona_path(name))


def load_named_product(name: str) -> ProductProfile:
	return load_product_profile(product_path(name))


def load_named_capability(name: str) -> CapabilitySpec:
	return load_capability_spec(product_path(name))


def load_named_schema(name: str) -> SchemaSpec:
	return load_schema_spec(product_path(name))


def load_named_fewshots(name: str) -> list[FewShotExample]:
	return load_fewshots(product_path(name))


def load_named_glossary(name: str) -> dict[str, str]:
	return load_glossary(product_path(name))
