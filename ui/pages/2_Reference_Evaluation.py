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

from synthetic_user.assets.persona import load_persona
from synthetic_user.assets.registry import persona_path
from synthetic_user.core.storage import (
	get_conversations,
	get_judge_results,
	get_questions,
	get_references,
	list_runs,
)
from synthetic_user.evaluation.calibration import calibration_status
from synthetic_user.evaluation.judge import PROMPT_VERSION as JUDGE_PROMPT_VERSION
from synthetic_user.metrics.registry import compute_metrics

st.set_page_config(page_title="Reference Evaluation", page_icon="🎯", layout="wide")

st.title("Reference-Guided Evaluation")
st.caption(
	"Whether the product was RIGHT, not just whether the user got stuck. "
	"Nothing here executes SQL — every check is static analysis."
)

settings, settings_error = common.safe_settings()
if settings_error:
	st.error(f"Settings failed to load: {settings_error}")
	st.stop()

common.ensure_database_ready()

RUNS_DIR = common.PROJECT_ROOT / "runs"

VERDICT_COLORS = {
	"correct": ("#e6f4ea", "#137333"),
	"partially_correct": ("#fef7e0", "#b06000"),
	"incorrect": ("#fce8e6", "#c5221f"),
	"reference_suspect": ("#e8eaed", "#5f6368"),
	"not_evaluable": ("#f1f3f4", "#5f6368"),
}

runs = [run for run in list_runs() if get_references(run.id)]
if not runs:
	st.info(
		"No reference-guided run yet. Generate questions with `--reference`, then "
		"`run --reference`, then `evaluate`."
	)
	st.stop()

run_ids = [run.id for run in runs]
run_id = st.selectbox(
	"Run",
	run_ids,
	format_func=lambda rid: f"{rid}  ·  {next(r for r in runs if r.id == rid).finished_at:%Y-%m-%d %H:%M} UTC",
)

references = get_references(run_id)
by_question = {reference.question_id: reference for reference in references}
questions = {question.id: question for question in get_questions(run_id)}
conversations = get_conversations(run_id)
judge_results = {result.conversation_id: result for result in get_judge_results(run_id)}

# --- reference health -----------------------------------------------------------------

st.header("References")

valid = [r for r in references if r.validation_status == "valid"]
invalid = [r for r in references if r.validation_status == "invalid"]

col1, col2, col3, col4 = st.columns(4)
col1.metric("References", len(references))
col2.metric("Valid", len(valid))
col3.metric("Invalid", len(invalid))
col4.metric(
	"Validity rate", f"{round(100 * len(valid) / len(references))}%" if references else "n/a"
)

if invalid:
	with st.expander(f"Why {len(invalid)} reference(s) were rejected"):
		reasons: dict[str, int] = {}
		for reference in invalid:
			for note in (reference.validation_notes or "unknown").split("; "):
				# Strip the variable tail so similar failures group together.
				key = note.split(":")[0].strip()
				reasons[key] = reasons.get(key, 0) + 1
		st.dataframe(
			pd.DataFrame(sorted(reasons.items(), key=lambda kv: -kv[1]), columns=["Reason", "Count"]),
			width="stretch",
			hide_index=True,
		)
		for reference in invalid:
			st.markdown(f"**{reference.question_id}** — {reference.validation_notes}")

# --- one pair per distance ------------------------------------------------------------

st.subheader("A question and its reference, per few-shot distance")
st.caption(
	"Only `expectation` is ever shown to a persona. Everything else below is privileged."
)

for distance in ("near", "pattern", "schema_only"):
	sample = next((r for r in valid if r.few_shot_distance == distance), None)
	if sample is None:
		continue
	question = questions.get(sample.question_id)
	with st.expander(f"{distance} — {question.question if question else sample.question_id}"):
		st.markdown(f"**Question:** {question.question if question else '—'}")
		st.markdown(f"**expectation (persona sees this):** {sample.expectation}")
		st.markdown(f"**expected behaviour:** `{sample.expected_behaviour}`")
		st.json(
			{
				"tables": sample.tables,
				"columns": sample.columns,
				"filters": sample.filters,
				"aggregation": sample.aggregation,
				"grain": sample.grain,
				"time_window": sample.time_window,
				"analysis_type": sample.analysis_type,
				"expected_output_columns": sample.expected_output_columns,
				"must_match": sample.must_match,
				"acceptable_alternatives": sample.acceptable_alternatives,
				"few_shot_ids": sample.few_shot_ids,
			}
		)

# --- judge results --------------------------------------------------------------------

st.divider()
st.header("Judge results")

if not judge_results:
	st.info("This run has not been judged yet. Run `evaluate --run-id` to populate this.")
else:
	status = calibration_status(JUDGE_PROMPT_VERSION)
	if status["banner"]:
		st.warning(status["banner"], icon="⚠️")
	else:
		st.success(
			f"Judge calibrated against {status['label_count']} human label(s) "
			f"for {status['judge_prompt_version']}."
		)

	rows = []
	for conversation in conversations:
		result = judge_results.get(conversation.id)
		if result is None:
			continue
		reference = by_question.get(conversation.question_id)
		question = questions.get(conversation.question_id)
		rows.append(
			{
				"question": question.question if question else conversation.question_id,
				"persona": conversation.persona_id,
				"distance": getattr(reference, "few_shot_distance", "—"),
				"overall": result.overall,
				"tags": ", ".join(result.failure_tags),
				"table_recall": result.table_recall,
				"column_recall": result.column_recall,
				"reason": result.reason,
				"conversation_id": conversation.id,
			}
		)

	verdict_counts = pd.Series([row["overall"] for row in rows]).value_counts()
	cols = st.columns(len(VERDICT_COLORS))
	for col, verdict in zip(cols, VERDICT_COLORS):
		col.metric(verdict, int(verdict_counts.get(verdict, 0)))

	st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

	st.subheader("Inspect one conversation")
	chosen = st.selectbox("Conversation", [row["conversation_id"] for row in rows])
	conversation = next(c for c in conversations if c.id == chosen)
	result = judge_results[chosen]
	reference = by_question.get(conversation.question_id)

	left, right = st.columns(2)
	with left:
		st.markdown("**Conversation**")
		for turn in conversation.turns:
			with st.chat_message("user"):
				st.write(turn.message)
			with st.chat_message("assistant"):
				st.write(turn.response.reply)
				query = (turn.evaluable or {}).get("query")
				if query:
					st.code(query, language="sql")
	with right:
		st.markdown("**Judge**")
		st.markdown(
			common.badge_html(result.overall, VERDICT_COLORS), unsafe_allow_html=True
		)
		st.write(result.reason)
		st.json(result.field_verdicts)
		if result.failure_tags:
			st.markdown(f"**Tags:** {', '.join(result.failure_tags)}")
		if reference is not None:
			st.markdown("**Reference**")
			st.write(reference.expectation)
			st.json(
				{
					"tables": reference.tables,
					"columns": reference.columns,
					"filters": reference.filters,
					"aggregation": reference.aggregation,
					"must_match": reference.must_match,
					"acceptable_alternatives": reference.acceptable_alternatives,
				}
			)

# --- metrics ---------------------------------------------------------------------------

st.divider()
st.header("Metrics")

personas = {}
for conversation in conversations:
	try:
		personas[conversation.persona_id] = load_persona(persona_path(conversation.persona_id))
	except (FileNotFoundError, ValueError):
		pass

artifacts = {}
if references:
	artifacts["reference"] = True
if judge_results:
	artifacts["judge_result"] = True

report = compute_metrics(
	SimpleNamespace(
		personas=personas,
		questions=questions,
		references=by_question,
		judge_results=judge_results,
		artifacts=artifacts,
	),
	conversations,
	[],
)

REFERENCE_METRICS = {
	"correct_rate", "partially_correct_rate", "incorrect_rate", "failure_tag_distribution",
	"table_precision", "table_recall", "column_precision", "column_recall",
	"behaviour_accuracy", "generalisation_gap",
}

reference_rows, product_rows, health_rows = [], [], []
for name, value in report["metrics"].items():
	if isinstance(value, dict) and value.get("label") == "HARNESS HEALTH":
		health_rows.append({"Metric": name, "Aggregate": json.dumps(value["values"]["aggregate"], default=str)})
	elif name in REFERENCE_METRICS:
		aggregate = value["aggregate"] if isinstance(value, dict) else value
		reference_rows.append({"Metric": name, "Aggregate": json.dumps(aggregate, default=str)})
	else:
		aggregate = value["aggregate"] if isinstance(value, dict) else value
		product_rows.append({"Metric": name, "Aggregate": json.dumps(aggregate, default=str)})

if reference_rows:
	st.subheader("REFERENCE-GUIDED CORRECTNESS")
	st.dataframe(pd.DataFrame(reference_rows), width="stretch", hide_index=True)

	st.markdown("**By few-shot distance**")
	distance_rows = []
	for name in ("correct_rate", "partially_correct_rate", "incorrect_rate"):
		value = report["metrics"].get(name)
		if isinstance(value, dict):
			segment = value.get("by_few_shot_distance", {})
			distance_rows.append({"Metric": name, **{k: v for k, v in segment.items()}})
	if distance_rows:
		st.dataframe(pd.DataFrame(distance_rows), width="stretch", hide_index=True)

	gap = report["metrics"].get("generalisation_gap")
	if isinstance(gap, dict):
		st.markdown(
			f"**Generalisation gap:** `{gap.get('aggregate')}` "
			f"(near {gap.get('correct_rate_near')}% − schema_only {gap.get('correct_rate_schema_only')}%). "
			"A large positive gap means near-example results are flattering."
		)

col_a, col_b = st.columns(2)
with col_a:
	st.subheader("PRODUCT METRICS")
	st.dataframe(pd.DataFrame(product_rows), width="stretch", hide_index=True)
with col_b:
	st.subheader("HARNESS HEALTH")
	st.caption("If these rise, the references need attention — not the product.")
	st.dataframe(pd.DataFrame(health_rows), width="stretch", hide_index=True)

if report["skipped"]:
	with st.expander("Skipped metrics"):
		st.json(report["skipped"])

# --- metamorphic -------------------------------------------------------------------------

st.divider()
st.header("Metamorphic consistency")
st.caption("Does the product answer the same question the same way? No ground truth needed.")

metamorphic_path = RUNS_DIR / f"metamorphic_{run_id}.json"
if not metamorphic_path.exists():
	st.info(f"No probes yet for this run. Run `metamorphic --run-id {run_id}`.")
else:
	payload = json.loads(metamorphic_path.read_text(encoding="utf-8"))
	rates = payload.get("rates", {})
	rate_rows = []
	for relation in ("paraphrase", "irrelevant_detail", "time_shift"):
		row = rates.get(relation, {})
		rate_rows.append(
			{
				"Relation": relation,
				"Consistency": row.get("consistency_rate"),
				"Comparable": row.get("comparable"),
				"Unparseable": row.get("unparseable"),
				"No SQL": row.get("no_sql"),
				"By distance": json.dumps(
					{k: v.get("consistency_rate") for k, v in row.get("by_few_shot_distance", {}).items()}
				),
			}
		)
	st.dataframe(pd.DataFrame(rate_rows), width="stretch", hide_index=True)

	inconsistent = [r for r in payload.get("results", []) if r.get("status") == "inconsistent"]
	if inconsistent:
		st.subheader(f"{len(inconsistent)} inconsistency(ies)")
		for item in inconsistent:
			with st.expander(f"[{item['relation']}] {item['variant_question']}"):
				st.markdown(f"**Original:** {item['original_question']}")
				st.markdown(f"**Differs in:** {', '.join(item['differences'])}")
				st.json(item["differences"])
				if item.get("sql"):
					st.code(item["sql"], language="sql")

# --- calibration ----------------------------------------------------------------------------

st.divider()
st.header("Calibration")
st.caption("Until the judge is measured against human labels, its verdicts are provisional.")

sheets = sorted(RUNS_DIR.glob("calibration_*.csv"))
if not sheets:
	st.info("No calibration sheet yet. Run `export-calibration --run-id <id> --n 30`.")
else:
	chosen_sheet = st.selectbox("Sheet", [p.name for p in sheets])
	frame = pd.read_csv(RUNS_DIR / chosen_sheet)
	labelled = frame["human_overall"].notna().sum() if "human_overall" in frame else 0
	st.metric("Rows / labelled", f"{len(frame)} / {labelled}")
	st.dataframe(frame, width="stretch", hide_index=True)
	st.caption(
		"Fill in human_overall, human_reference_ok, human_notes and reviewer, then run "
		"`import-calibration --file runs/<sheet>.csv`."
	)
