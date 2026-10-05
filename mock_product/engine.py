import re
import time
from pathlib import Path
from typing import Any

import duckdb

from synthetic_user.core.prompts import load_prompt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_SOURCE = PROJECT_ROOT / "data" / "credit_card_billing_data.csv"
DB_PATH = PROJECT_ROOT / "mock_product" / "data" / "billing.duckdb"
SCHEMA_PATH = PROJECT_ROOT / "mock_product" / "data" / "schema.sql"

MAX_ROWS = 100

_SQL_FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_NOT_SUPPORTED_SENTINEL = "NOT_SUPPORTED"
_FORBIDDEN_KEYWORDS = (
	"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "ATTACH", "DETACH", "COPY",
	"CREATE", "REPLACE", "TRUNCATE", "GRANT", "REVOKE", "CALL", "EXPORT",
	"IMPORT", "PRAGMA", "INSTALL", "LOAD",
)


def ensure_billing_database(db_path: str | Path = DB_PATH) -> dict[str, object]:
	path = Path(db_path)
	path.parent.mkdir(parents=True, exist_ok=True)
	connection = duckdb.connect(str(path))
	try:
		connection.execute(
			"""
			CREATE TABLE IF NOT EXISTS credit_card_billing AS
			SELECT * FROM read_csv_auto(?, header=true)
			""",
			[str(DATA_SOURCE)],
		)
		row_count = connection.execute("SELECT COUNT(*) FROM credit_card_billing").fetchone()[0]
		return {"db_path": str(path), "row_count": int(row_count)}
	finally:
		connection.close()


def _clean_sql(raw_text: str) -> str:
	text = raw_text.strip()
	text = _SQL_FENCE_RE.sub("", text).strip()
	return text.rstrip(";").strip()


def _validate_sql(sql: str) -> str | None:
	"""Return an error message if sql is not a safe single SELECT, else None."""
	if not sql:
		return "Empty SQL"
	if ";" in sql:
		return "Multiple statements are not allowed"
	if not re.match(r"^\s*(WITH|SELECT)\b", sql, re.IGNORECASE):
		return "Only SELECT statements are allowed"
	for keyword in _FORBIDDEN_KEYWORDS:
		if re.search(rf"\b{keyword}\b", sql, re.IGNORECASE):
			return f"Forbidden keyword: {keyword}"
	return None


def _format_transcript(transcript: list[dict[str, Any]]) -> str:
	if not transcript:
		return "No prior turns."
	lines = []
	for turn in transcript:
		question = turn.get("question", "")
		lines.append(f"Q: {question}")
		if turn.get("sql"):
			lines.append(f"SQL: {turn['sql']}")
	return "\n".join(lines)


def _render_prompt(schema_text: str, transcript: list[dict[str, Any]], message: str) -> str:
	prompt = load_prompt("nl_to_sql_v1.md")
	replacements = {
		"<<SCHEMA>>": schema_text,
		"<<TRANSCRIPT>>": _format_transcript(transcript),
		"<<MESSAGE>>": message,
	}
	for marker, value in replacements.items():
		prompt = prompt.replace(marker, value)
	return prompt


def _summarize_rows(columns: list[str], rows: list[dict[str, Any]]) -> str:
	if len(rows) == 1 and len(columns) == 1:
		value = rows[0][columns[0]]
		if value is None:
			return "That matched no data, so there's nothing to report."
		return f"{columns[0].replace('_', ' ')}: {value}"
	if len(rows) == 1:
		return ", ".join(f"{col.replace('_', ' ')}: {rows[0][col]}" for col in columns)
	preview = "; ".join(
		", ".join(f"{col}: {row[col]}" for col in columns) for row in rows[:3]
	)
	suffix = "" if len(rows) <= 3 else f" (+{len(rows) - 3} more row(s))"
	return f"Found {len(rows)} result(s). {preview}{suffix}"


def _reply_for(status: str, error: str | None, columns: list[str], rows: list[dict[str, Any]]) -> str:
	if status == "OK":
		return _summarize_rows(columns, rows)
	if status == "EMPTY":
		return "That query ran successfully but returned no matching records."
	if status == "REFUSED":
		return f"I can't run that request: {error}" if error else "I can't run that request."
	if status == "SQL_ERROR":
		return "That request hit a database error and could not be completed."
	return "Something went wrong while processing that request."


class InsightEngine:
	"""Multi-turn-aware natural-language-to-SQL engine over the billing database."""

	def __init__(
		self,
		db_path: str | Path = DB_PATH,
		schema_path: str | Path = SCHEMA_PATH,
		llm: Any = None,
	) -> None:
		self.db_path = str(db_path)
		self.schema_path = Path(schema_path)
		self._llm = llm

	def answer(self, transcript: list[dict[str, Any]], message: str) -> dict[str, Any]:
		start = time.perf_counter()
		try:
			return self._answer(transcript, message, start)
		except Exception as error:  # noqa: BLE001 - engine must never raise
			return self._result(start, "", [], "LLM_ERROR", f"Unexpected error: {error}")

	def _answer(self, transcript: list[dict[str, Any]], message: str, start: float) -> dict[str, Any]:
		if self._llm is None:
			return self._result(start, "", [], "LLM_ERROR", "No LLM client configured")

		try:
			schema_text = self.schema_path.read_text(encoding="utf-8")
		except OSError as error:
			return self._result(start, "", [], "LLM_ERROR", f"Failed to read schema: {error}")

		prompt = _render_prompt(schema_text, transcript, message)
		try:
			raw_text = self._llm.complete(prompt=prompt, role="product", temperature=0.0)
		except Exception as error:  # noqa: BLE001
			return self._result(start, "", [], "LLM_ERROR", f"LLM request failed: {error}")

		sql = _clean_sql(raw_text)
		if sql.upper() == _NOT_SUPPORTED_SENTINEL:
			return self._result(start, "", [], "REFUSED", "This isn't something the product can look up.")

		validation_error = _validate_sql(sql)
		if validation_error:
			return self._result(start, sql, [], "REFUSED", validation_error)

		try:
			connection = duckdb.connect(self.db_path, read_only=True)
		except Exception as error:  # noqa: BLE001
			return self._result(start, sql, [], "SQL_ERROR", f"Failed to open database: {error}")

		try:
			try:
				cursor = connection.execute(sql)
				columns = [description[0] for description in cursor.description]
				fetched = cursor.fetchmany(MAX_ROWS)
			except Exception as error:  # noqa: BLE001
				return self._result(start, sql, [], "SQL_ERROR", str(error))
		finally:
			connection.close()

		rows = [dict(zip(columns, row)) for row in fetched]
		if not rows:
			return self._result(start, sql, [], "EMPTY", None)
		return self._result(start, sql, rows, "OK", None)

	def _result(
		self,
		start: float,
		sql: str,
		rows: list[dict[str, Any]],
		status: str,
		error: str | None,
	) -> dict[str, Any]:
		columns = list(rows[0].keys()) if rows else []
		return {
			"reply": _reply_for(status, error, columns, rows),
			"sql": sql,
			"rows": rows,
			"row_count": len(rows),
			"status": status,
			"error": error,
			"latency_ms": int((time.perf_counter() - start) * 1000),
		}
