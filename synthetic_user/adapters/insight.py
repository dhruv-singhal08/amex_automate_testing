from pathlib import Path
from typing import Any

from mock_product.engine import DB_PATH, SCHEMA_PATH, InsightEngine
from synthetic_user.core.models import AdapterResponse


class InsightAdapter:
	"""Adapter over the mock Insight billing product's multi-turn NL-to-SQL engine."""

	def __init__(
		self,
		database_path: str | Path = DB_PATH,
		schema_path: str | Path = SCHEMA_PATH,
		llm: Any = None,
	) -> None:
		if llm is None:
			from synthetic_user.core.llm import LLMClient

			llm = LLMClient()
		self._engine = InsightEngine(db_path=database_path, schema_path=schema_path, llm=llm)

	def open_session(self) -> list[dict[str, Any]]:
		return []

	def send(self, session: list[dict[str, Any]], message: str) -> AdapterResponse:
		result = self._engine.answer(session, message)
		session.append({"question": message, "sql": result["sql"]})
		return AdapterResponse(
			reply=result["reply"],
			artifacts={
				"sql": result["sql"],
				"status": result["status"],
				"row_count": result["row_count"],
				"rows": result["rows"],
			},
			error=result["error"],
			latency_ms=result["latency_ms"],
		)

	def extract_evaluable(self, response: AdapterResponse) -> dict[str, Any] | None:
		"""The adapter is the only component allowed to read its own artifacts;
		this hands the judge a standard shape instead."""
		query = response.artifacts.get("sql")
		if not query:
			return None
		evaluable: dict[str, Any] = {"kind": "sql", "query": query}
		rows = response.artifacts.get("rows")
		if rows:
			evaluable["rows"] = rows
		return evaluable

	def close_session(self, session: list[dict[str, Any]]) -> None:
		session.clear()
