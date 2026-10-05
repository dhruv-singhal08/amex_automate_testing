from pathlib import Path

import yaml

from synthetic_user.core.models import Persona


def load_persona(path: str | Path) -> Persona:
	with Path(path).open(encoding="utf-8") as file:
		persona = Persona.model_validate(yaml.safe_load(file))
	if persona.approval_state != "approved":
		raise ValueError(f"Persona {persona.id} is not approved for use")
	return persona
