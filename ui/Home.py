import sys
from pathlib import Path

_here = Path(__file__).resolve()
_ui_dir = _here.parent.parent if _here.parent.name == "pages" else _here.parent
if str(_ui_dir) not in sys.path:
	sys.path.insert(0, str(_ui_dir))

import common
import streamlit as st

st.set_page_config(page_title="Synthetic User Platform", page_icon="🧪", layout="wide")

st.title("Synthetic User Platform")
st.caption(
	"Simulate persona-driven users against an AI product to find where real users would get stuck."
)

settings, settings_error = common.safe_settings()

if settings_error:
	st.error(
		"Settings failed to load — check your .env file (LLM_API_KEY / GEMINI_API_KEY is required).\n\n"
		f"{settings_error}"
	)
else:
	col1, col2, col3 = st.columns(3)
	col1.metric("Provider", settings.llm_provider)
	col2.metric("Generation model", settings.model_generation)
	col3.metric("Repeats per question", settings.repeats_per_question)

	if st.button("Initialize databases", type="primary"):
		with st.spinner("Initializing application and billing databases..."):
			status = common.ensure_database_ready()
		st.success(status)

st.divider()

st.subheader("Synthetic User Test Harness")
st.write(
	"Pick a persona and a product, generate the questions that persona would realistically ask, "
	"run multi-turn conversations against the product, validate the results, and inspect metrics."
)
st.page_link("pages/1_Test_Harness.py", label="Open Test Harness", icon="🧪")

st.subheader("Reference-Guided Evaluation")
st.write(
	"Whether the product was *right*, not just whether the user got stuck. Shows reference "
	"validity, judge verdicts per conversation, correctness metrics by few-shot distance, "
	"metamorphic consistency, and the calibration sheet. Nothing here executes SQL."
)
st.page_link("pages/2_Reference_Evaluation.py", label="Open Reference Evaluation", icon="🎯")

st.info(
	"The Insight product itself is a separate application, not a page here — it's the product "
	"under test, not part of the harness. Run it on its own:\n\n"
	"`uv run streamlit run insight_app.py`",
	icon="💬",
)

st.divider()
st.subheader("Assets on disk")

personas = common.list_personas()
profiles = common.list_product_profiles()

col_p, col_q = st.columns(2)
with col_p:
	st.write(f"**Approved personas** ({len(personas)})")
	for persona in personas:
		st.write(f"- `{persona.id}` — {persona.name} ({persona.domain_expertise})")
with col_q:
	st.write(f"**Product profiles** ({len(profiles)})")
	for name, profile in profiles.items():
		st.write(f"- `{name}` — {profile.product_name} v{profile.version}")
