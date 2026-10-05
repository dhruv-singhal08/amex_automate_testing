import hashlib
from typing import Any

from synthetic_user.core.models import AdapterResponse

# Canned failure modes, so the judge can be exercised against known-wrong answers
# without paying for an LLM or waiting on a real product.
VARIANTS = (
	"correct",
	"wrong_table",
	"missing_filter",
	"wrong_grain",
	"wrong_aggregation",
	"refusal",
	"clarifying_question",
)

_CANNED: dict[str, dict[str, Any]] = {
	"correct": {
		"reply": "Marketing spent $12,345.67 last month.",
		"sql": (
			"SELECT department, SUM(transaction_amount_usd) AS total_spend_usd "
			"FROM credit_card_billing "
			"WHERE department = 'Marketing' "
			"AND transaction_date >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
			"AND transaction_date < date_trunc('month', CURRENT_DATE) "
			"GROUP BY department"
		),
		"rows": [{"department": "Marketing", "total_spend_usd": 12345.67}],
	},
	# Right shape, wrong source: a table that is not in the schema at all.
	"wrong_table": {
		"reply": "Marketing was billed $9,876.54 last month.",
		"sql": "SELECT customer, SUM(amount) AS total FROM invoices WHERE customer = 'Marketing' GROUP BY customer",
		"rows": [{"customer": "Marketing", "total": 9876.54}],
	},
	# Drops the filter the question asked for, so the number is company-wide.
	"missing_filter": {
		"reply": "Total spend last month was $148,203.11.",
		"sql": (
			"SELECT SUM(transaction_amount_usd) AS total_spend_usd "
			"FROM credit_card_billing "
			"WHERE transaction_date >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
			"AND transaction_date < date_trunc('month', CURRENT_DATE)"
		),
		"rows": [{"total_spend_usd": 148203.11}],
	},
	# Answers per transaction when the question asked for a total.
	"wrong_grain": {
		"reply": "Here are Marketing's transactions from last month.",
		"sql": (
			"SELECT transaction_id, transaction_date, merchant_name, transaction_amount_usd "
			"FROM credit_card_billing WHERE department = 'Marketing'"
		),
		"rows": [
			{"transaction_id": "T-1001", "merchant_name": "United Airlines", "transaction_amount_usd": 842.10},
			{"transaction_id": "T-1002", "merchant_name": "Marriott", "transaction_amount_usd": 412.55},
		],
	},
	# Counts rows when the question asked how much was spent.
	"wrong_aggregation": {
		"reply": "Marketing had 42 transactions last month.",
		"sql": (
			"SELECT department, COUNT(*) AS transaction_count "
			"FROM credit_card_billing WHERE department = 'Marketing' GROUP BY department"
		),
		"rows": [{"department": "Marketing", "transaction_count": 42}],
	},
	"refusal": {
		"reply": "I can't look that up.",
		"error": "This isn't something the product can look up.",
	},
	# Asks the user a question back instead of answering. Not an error: the
	# persona is expected to answer it in character.
	"clarifying_question": {
		"reply": "Which department did you mean?",
	},
}


class MockAdapter:
	"""Offline adapter. Deterministic: the same message always gets the same reply.

	Pass `variant` to pin one behaviour; leave it unset to derive the behaviour
	from the message, so a grid exercises a spread of them.
	"""

	def __init__(self, variant: str | None = None) -> None:
		if variant is not None and variant not in VARIANTS:
			raise ValueError(f"Unknown MockAdapter variant: {variant}. Choose from {VARIANTS}.")
		self.variant = variant

	def open_session(self) -> dict[str, Any]:
		return {}

	def variant_for(self, message: str) -> str:
		if self.variant is not None:
			return self.variant
		digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
		return VARIANTS[int(digest[:8], 16) % len(VARIANTS)]

	def send(self, session: dict[str, Any], message: str) -> AdapterResponse:
		del session
		variant = self.variant_for(message)
		canned = _CANNED[variant]
		artifacts: dict[str, Any] = {"variant": variant}
		if "sql" in canned:
			artifacts["sql"] = canned["sql"]
		if "rows" in canned:
			artifacts["rows"] = canned["rows"]
		return AdapterResponse(
			reply=canned["reply"],
			artifacts=artifacts,
			error=canned.get("error"),
			latency_ms=0,
		)

	def extract_evaluable(self, response: AdapterResponse) -> dict[str, Any] | None:
		query = response.artifacts.get("sql")
		if not query:
			return None
		evaluable: dict[str, Any] = {"kind": "sql", "query": query}
		rows = response.artifacts.get("rows")
		if rows:
			evaluable["rows"] = rows
		return evaluable

	def close_session(self, session: dict[str, Any]) -> None:
		del session
