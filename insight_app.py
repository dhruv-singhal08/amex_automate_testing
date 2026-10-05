"""Standalone UI for the Insight billing product — the product under test.

Run on its own, separate from the Synthetic User Platform harness:

    uv run streamlit run insight_app.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_UI_DIR = _PROJECT_ROOT / "ui"
if str(_UI_DIR) not in sys.path:
	sys.path.insert(0, str(_UI_DIR))

import common
import pandas as pd
import streamlit as st

from synthetic_user.adapters.insight import InsightAdapter

st.set_page_config(page_title="Insight Product", page_icon="💬", layout="centered")

st.title("Insight — Ask your billing data")
st.caption(
	"Ask a plain-English question about corporate credit card billing data. It's translated to SQL, "
	"run against the billing database, and the answer is shown below. Follow-up questions "
	"('what about last month?', 'just Engineering') are resolved against this conversation."
)

settings, settings_error = common.safe_settings()
if settings_error:
	st.error(f"Settings failed to load: {settings_error}")
	st.stop()

if "insight_session" not in st.session_state:
	st.session_state.insight_session = []
if "insight_history" not in st.session_state:
	st.session_state.insight_history = []


def _adapter() -> InsightAdapter:
	return InsightAdapter(llm=common.cached_llm_client())


with st.sidebar:
	st.subheader("Connection")
	st.caption(f"Provider: {settings.llm_provider}")
	st.caption(f"Product model: {settings.model_product}")
	st.divider()
	st.subheader("Session")
	st.caption(f"{len(st.session_state.insight_history)} turn(s) so far")
	if st.button("Reset conversation"):
		st.session_state.insight_session = []
		st.session_state.insight_history = []
		st.rerun()


def _render_turn(question: str, reply: str, sql: str, status: str, rows: list, error: str | None, latency_ms: int) -> None:
	with st.chat_message("user"):
		st.write(question)
	with st.chat_message("assistant"):
		st.markdown(common.badge_html(status, common.STATUS_COLORS), unsafe_allow_html=True)
		st.write(reply)
		if error:
			st.caption(f"⚠️ {error}")
		if sql:
			with st.expander("Generated SQL"):
				st.code(sql, language="sql")
		if status == "OK" and rows:
			st.dataframe(pd.DataFrame(rows), width="stretch")
		st.caption(f"Latency: {latency_ms} ms")
		# Invisible marker only: lets InsightBrowserAdapter (Playwright) know this
		# turn finished rendering, so it can pace typing the next question. It is
		# not scraped for data — the adapter gets the actual result from a direct
		# backend call, not from this UI.
		st.markdown('<span data-testid="insight-turn-done" style="display:none"></span>', unsafe_allow_html=True)


for turn in st.session_state.insight_history:
	_render_turn(
		turn["question"], turn["reply"], turn["sql"], turn["status"], turn["rows"], turn["error"], turn["latency_ms"]
	)

question = st.chat_input("e.g. What is the total spend by department?")
if question:
	with st.spinner("Generating SQL..."):
		response = _adapter().send(st.session_state.insight_session, question)
	status = response.artifacts.get("status", "OK")
	sql = response.artifacts.get("sql") or ""
	rows = response.artifacts.get("rows") or []

	_render_turn(question, response.reply, sql, status, rows, response.error, response.latency_ms)

	st.session_state.insight_history.append(
		{
			"question": question,
			"reply": response.reply,
			"sql": sql,
			"status": status,
			"rows": rows,
			"error": response.error,
			"latency_ms": response.latency_ms,
		}
	)
