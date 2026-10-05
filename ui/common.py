"""Shared helpers for the Streamlit UI. Imported as `common` by every page."""

from pathlib import Path
from typing import Any

import streamlit as st
import yaml

from synthetic_user.assets.persona import load_persona
from synthetic_user.assets.product import load_capability_spec, load_product_profile
from synthetic_user.core.config import Settings, get_settings
from synthetic_user.core.llm import LLMClient
from synthetic_user.core.models import CapabilitySpec, Persona, ProductProfile
from synthetic_user.core.storage import init_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
PERSONAS_DIR = ASSETS_DIR / "personas"
PRODUCTS_DIR = ASSETS_DIR / "products"

OUTCOME_COLORS = {
	"COMPLETED": ("#e6f4ea", "#137333"),
	"ABANDONED": ("#fce8e6", "#c5221f"),
	"MAX_TURNS": ("#fef7e0", "#b06000"),
	"LOOPED": ("#fef7e0", "#b06000"),
	"ADAPTER_ERROR": ("#fce8e6", "#c5221f"),
	"BUDGET": ("#fef7e0", "#b06000"),
}

STATUS_COLORS = {
	"OK": ("#e6f4ea", "#137333"),
	"EMPTY": ("#f1f3f4", "#5f6368"),
	"REFUSED": ("#fce8e6", "#c5221f"),
	"SQL_ERROR": ("#fce8e6", "#c5221f"),
	"LLM_ERROR": ("#fce8e6", "#c5221f"),
}

DEFAULT_COLORS = ("#f1f3f4", "#5f6368")


def badge_html(label: str | None, colors: dict[str, tuple[str, str]]) -> str:
	bg, fg = colors.get(label or "", DEFAULT_COLORS)
	return (
		'<span style="display:inline-block;padding:2px 10px;border-radius:12px;'
		f'background:{bg};color:{fg};font-size:0.8rem;font-weight:600;">{label or "UNKNOWN"}</span>'
	)


def safe_settings() -> tuple[Settings | None, str | None]:
	try:
		return get_settings(), None
	except Exception as error:  # noqa: BLE001 - surfaced as a friendly banner, not a crash
		return None, str(error)


@st.cache_resource(show_spinner=False)
def cached_llm_client() -> LLMClient:
	return LLMClient()


@st.cache_resource(show_spinner=False)
def ensure_database_ready() -> str:
	from mock_product.engine import ensure_billing_database

	settings = get_settings()
	init_db(settings.database_url)
	result = ensure_billing_database()
	return f"App database ready at {settings.database_url}; billing database has {result['row_count']} rows."


def list_personas() -> list[Persona]:
	personas = []
	for path in sorted(PERSONAS_DIR.glob("*.yaml")):
		try:
			personas.append(load_persona(path))
		except Exception:  # noqa: BLE001 - skip unapproved or invalid persona files
			continue
	return personas


def list_product_profiles() -> dict[str, ProductProfile]:
	profiles: dict[str, ProductProfile] = {}
	for path in sorted(PRODUCTS_DIR.glob("*_profile.yaml")):
		try:
			profiles[path.stem] = load_product_profile(path)
		except Exception:  # noqa: BLE001
			continue
	return profiles


def list_capability_specs() -> dict[str, CapabilitySpec]:
	specs: dict[str, CapabilitySpec] = {}
	for path in sorted(PRODUCTS_DIR.glob("*_capability.yaml")):
		try:
			specs[path.stem] = load_capability_spec(path)
		except Exception:  # noqa: BLE001
			continue
	return specs


def to_yaml(model: Any) -> str:
	return yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False)
