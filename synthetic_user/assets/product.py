from pathlib import Path

import yaml

from synthetic_user.core.models import CapabilitySpec, FewShotExample, ProductProfile, SchemaSpec


def _load_yaml(path: str | Path) -> dict:
	with Path(path).open(encoding="utf-8") as file:
		data = yaml.safe_load(file)
	if not isinstance(data, dict):
		raise ValueError(f"Expected a YAML mapping in {path}")
	return data


def load_product_profile(path: str | Path) -> ProductProfile:
	return ProductProfile.model_validate(_load_yaml(path))


def load_capability_spec(path: str | Path) -> CapabilitySpec:
	return CapabilitySpec.model_validate(_load_yaml(path))


def load_schema_spec(path: str | Path) -> SchemaSpec:
	return SchemaSpec.model_validate(_load_yaml(path))


def load_fewshots(path: str | Path) -> list[FewShotExample]:
	data = _load_yaml(path)
	examples = data.get("examples")
	if not isinstance(examples, list):
		raise ValueError(f"Expected an 'examples' list in {path}")
	return [FewShotExample.model_validate(item) for item in examples]


def load_glossary(path: str | Path) -> dict[str, str]:
	"""Optional asset: an absent file is not an error, it just means no glossary."""
	if not Path(path).exists():
		return {}
	terms = _load_yaml(path).get("terms") or {}
	if not isinstance(terms, dict):
		raise ValueError(f"Expected a 'terms' mapping in {path}")
	return {str(key): str(value) for key, value in terms.items()}
