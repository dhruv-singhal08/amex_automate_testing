import json
import sys
from pathlib import Path
from types import SimpleNamespace

_here = Path(__file__).resolve()
_ui_dir = _here.parent.parent if _here.parent.name == "pages" else _here.parent
if str(_ui_dir) not in sys.path:
	sys.path.insert(0, str(_ui_dir))

import common
import pandas as pd
import streamlit as st

from synthetic_user.adapters.insight import InsightAdapter
from synthetic_user.adapters.insight_browser import InsightBrowserAdapter
from synthetic_user.adapters.mock import MockAdapter
from synthetic_user.assets.persona import load_persona
from synthetic_user.assets.registry import persona_path
from synthetic_user.core.storage import (
	get_conversations,
	get_questions,
	get_validations,
	list_runs,
	save_question,
	save_validation,
)
from synthetic_user.generation.generator import generate_questions
from synthetic_user.metrics.registry import compute_metrics
from synthetic_user.simulation.runner import run_grid
from synthetic_user.validation.validator import validate

st.set_page_config(page_title="Test Harness", page_icon="🧪", layout="wide")

st.title("Synthetic User Test Harness")
st.caption("Generate realistic persona-driven questions and run them against the product.")

settings, settings_error = common.safe_settings()
if settings_error:
	st.error(f"Settings failed to load: {settings_error}")
	st.stop()

common.ensure_database_ready()

defaults = {
	"harness_questions": [],
	"harness_run_id": None,
	"harness_metrics_report": None,
}
for key, value in defaults.items():
	st.session_state.setdefault(key, value)

personas = {persona.id: persona for persona in common.list_personas()}
profiles = common.list_product_profiles()
capability_specs = common.list_capability_specs()

if not personas:
	st.warning(f"No approved personas found in {common.PERSONAS_DIR}.")
	st.stop()
if not profiles:
	st.warning(f"No product profiles found in {common.PRODUCTS_DIR}.")
	st.stop()

st.header("Setup")
col1, col2 = st.columns(2)

with col1:
	st.subheader("Persona")
	persona_id = st.selectbox(
		"Persona",
		list(personas),
		format_func=lambda pid: f"{personas[pid].name} ({personas[pid].domain_expertise})",
	)
	persona = personas[persona_id]
	with st.expander(f"Preview: {persona.id}"):
		st.code(common.to_yaml(persona), language="yaml")

with col2:
	st.subheader("Product")
	product_name = st.selectbox("Product profile", list(profiles))
	product = profiles[product_name]
	with st.expander(f"Preview: {product_name}"):
		st.code(common.to_yaml(product), language="yaml")

past_queries = st.text_area(
	"Past queries (optional, one per line or CSV)",
	help="Grounds question generation in real historical queries, if you have them.",
)
n_questions = st.slider("Number of questions to generate", min_value=3, max_value=20, value=6)

if st.button("Generate questions", type="primary"):
	llm = common.cached_llm_client()
	with st.spinner("Generating questions..."):
		questions = generate_questions(persona, product, past_queries or None, n_questions, llm, seed=0)
	st.session_state.harness_questions = questions
	st.session_state.harness_run_id = None
	st.session_state.harness_metrics_report = None
	if not questions:
		st.error("Question generation failed or returned no questions. Check the LLM configuration and try again.")

if st.session_state.harness_questions:
	st.subheader(f"Generated questions ({len(st.session_state.harness_questions)})")
	st.dataframe(
		pd.DataFrame([q.model_dump() for q in st.session_state.harness_questions]),
		width="stretch",
	)

st.divider()
st.header("Run")

col_adapter, col_repeats = st.columns(2)
with col_adapter:
	adapter_choice = st.radio(
		"Adapter",
		["mock", "insight", "insight (watch in browser)"],
		horizontal=True,
		help=(
			"mock: instant, no LLM calls. insight: calls the product engine directly. "
			"insight (watch in browser): also types each question into a real browser "
			"running insight_app.py, so you can watch it happen — requires that app to "
			"already be running."
		),
	)
with col_repeats:
	repeats = st.number_input(
		"Repeats per question", min_value=1, max_value=10, value=settings.repeats_per_question
	)

if adapter_choice == "insight (watch in browser)":
	col_url, col_headed = st.columns([3, 1])
	with col_url:
		browser_base_url = st.text_input("insight_app.py URL", value="http://localhost:8501")
	with col_headed:
		headed = st.checkbox("Show window", value=True)

run_disabled = not st.session_state.harness_questions
if st.button("Run conversations", type="primary", disabled=run_disabled):
	llm = common.cached_llm_client()
	if adapter_choice == "insight":
		adapter_instance = InsightAdapter(llm=llm)
	elif adapter_choice == "insight (watch in browser)":
		adapter_instance = InsightBrowserAdapter(base_url=browser_base_url, headed=headed, llm=llm)
	else:
		adapter_instance = MockAdapter()
	run_settings = settings.model_copy(update={"repeats_per_question": int(repeats)})
	total = len(st.session_state.harness_questions) * int(repeats)
	with st.spinner(f"Running {total} conversation(s) for persona '{persona.id}'..."):
		run_record = run_grid(
			[persona],
			{persona.id: st.session_state.harness_questions},
			product,
			adapter_instance,
			llm,
			run_settings,
		)
		for question in st.session_state.harness_questions:
			save_question(question, run_record.id)
	st.session_state.harness_run_id = run_record.id
	st.session_state.harness_metrics_report = None
	st.success(f"Run {run_record.id} completed with {len(run_record.conversation_ids)} conversation(s).")

st.divider()
st.header("Runs")

runs = list_runs()
if not runs:
	st.info("No runs yet. Generate questions and run conversations above.")
	st.stop()

run_ids = [run.id for run in runs]
default_index = run_ids.index(st.session_state.harness_run_id) if st.session_state.harness_run_id in run_ids else 0
selected_run_id = st.selectbox(
	"Select a run to inspect",
	run_ids,
	index=default_index,
	format_func=lambda rid: f"{rid}  ·  {next(r for r in runs if r.id == rid).finished_at:%Y-%m-%d %H:%M} UTC",
)

conversations = get_conversations(selected_run_id)
questions_by_id = {question.id: question for question in get_questions(selected_run_id)}

st.subheader(f"Conversations ({len(conversations)})")
for conversation in conversations:
	question = questions_by_id.get(conversation.question_id)
	title = question.question if question else conversation.question_id
	with st.expander(f"{title}  ·  {conversation.outcome.value}  ·  {len(conversation.turns)} turn(s)"):
		st.markdown(common.badge_html(conversation.outcome.value, common.OUTCOME_COLORS), unsafe_allow_html=True)
		if conversation.doubt_anywhere:
			st.caption("Persona expressed doubt at some point in this conversation.")
		for turn in conversation.turns:
			with st.chat_message("user"):
				st.write(turn.message)
			with st.chat_message("assistant"):
				st.write(turn.response.reply)
				sql = turn.response.artifacts.get("sql")
				if sql:
					st.code(sql, language="sql")
				if turn.response.error:
					st.caption(f"Adapter error: {turn.response.error}")
			if turn.decision is not None:
				doubt = " (doubt expressed)" if turn.decision.doubt_expressed else ""
				st.caption(f"Persona: {turn.decision.reasoning}{doubt}")
		if conversation.closing_summary:
			st.markdown(f"**Closing summary:** {conversation.closing_summary}")

st.divider()
st.header("Validate")

capability_name = st.selectbox(
	"Capability spec (internal; used only by the validator, never by the persona)",
	["(none)", *capability_specs],
)
capability_spec = None if capability_name == "(none)" else capability_specs[capability_name]

if st.button("Validate this run"):
	llm = common.cached_llm_client()
	with st.spinner(f"Validating {len(conversations)} conversation(s)..."):
		for conversation in conversations:
			question = questions_by_id.get(conversation.question_id)
			result = validate(
				conversation,
				question.intent if question else conversation.question_id,
				product,
				capability_spec,
				llm,
				settings,
			)
			save_validation(result, selected_run_id)
	st.success(f"Validated {len(conversations)} conversation(s).")

validations = get_validations(selected_run_id)
if validations:
	st.dataframe(pd.DataFrame([v.model_dump() for v in validations]), width="stretch")
else:
	st.info("No validations yet for this run.")

st.divider()
st.header("Metrics")

if st.button("Compute metrics"):
	personas_for_run: dict[str, object] = {}
	for conversation in conversations:
		try:
			personas_for_run[conversation.persona_id] = load_persona(persona_path(conversation.persona_id))
		except (FileNotFoundError, ValueError):
			pass
	report = compute_metrics(
		SimpleNamespace(personas=personas_for_run, questions=questions_by_id, artifacts={}),
		conversations,
		validations,
	)
	st.session_state.harness_metrics_report = report

report = st.session_state.harness_metrics_report
if report:
	product_rows = []
	harness_rows = []
	for name, value in report["metrics"].items():
		if isinstance(value, dict) and value.get("label") == "HARNESS HEALTH":
			harness_rows.append({"Metric": name, "Aggregate": json.dumps(value["values"]["aggregate"], default=str)})
		else:
			aggregate = value["aggregate"] if isinstance(value, dict) else value
			product_rows.append({"Metric": name, "Aggregate": json.dumps(aggregate, default=str)})

	col_product, col_harness = st.columns(2)
	with col_product:
		st.subheader("PRODUCT METRICS")
		st.dataframe(pd.DataFrame(product_rows), width="stretch", hide_index=True)
	with col_harness:
		st.subheader("HARNESS HEALTH")
		st.caption("Low hesitation/cleanup rates mean the personas are too weak, not that the product is good.")
		st.dataframe(pd.DataFrame(harness_rows), width="stretch", hide_index=True)

	if report["skipped"]:
		with st.expander("Skipped metrics"):
			st.json(report["skipped"])

st.divider()
st.header("Export")

validations_by_conversation = {v.conversation_id: v for v in validations}
export_rows = [
	{
		"conversation_id": conversation.id,
		"persona_id": conversation.persona_id,
		"question_id": conversation.question_id,
		"repeat_index": conversation.repeat_index,
		"outcome": conversation.outcome.value,
		"turn_count": len(conversation.turns),
		"doubt_anywhere": conversation.doubt_anywhere,
		"classification": (
			validations_by_conversation[conversation.id].classification
			if conversation.id in validations_by_conversation
			else None
		),
	}
	for conversation in conversations
]

if export_rows:
	export_df = pd.DataFrame(export_rows)
	csv_bytes = export_df.to_csv(index=False).encode("utf-8")
	json_bytes = json.dumps(export_rows, indent=2, default=str).encode("utf-8")
	col_csv, col_json = st.columns(2)
	col_csv.download_button(
		"Download CSV", data=csv_bytes, file_name=f"{selected_run_id}.csv", mime="text/csv"
	)
	col_json.download_button(
		"Download JSON", data=json_bytes, file_name=f"{selected_run_id}.json", mime="application/json"
	)
else:
	st.info("No conversations to export yet.")
