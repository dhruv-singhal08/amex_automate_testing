from synthetic_user.core.llm import StubLLMClient

from mock_product.engine import InsightEngine, _clean_sql, _validate_sql


def test_clean_sql_strips_fences_and_semicolon() -> None:
	raw = "```sql\nSELECT COUNT(*) FROM credit_card_billing;\n```"
	assert _clean_sql(raw) == "SELECT COUNT(*) FROM credit_card_billing"


def test_validate_sql_rejects_multiple_statements() -> None:
	assert _validate_sql("SELECT 1; SELECT 2") == "Multiple statements are not allowed"


def test_validate_sql_rejects_non_select() -> None:
	assert _validate_sql("DROP TABLE credit_card_billing") == "Only SELECT statements are allowed"


def test_validate_sql_rejects_forbidden_keyword_in_select() -> None:
	error = _validate_sql("SELECT * FROM credit_card_billing WHERE department = (INSERT INTO x VALUES (1))")
	assert error is not None and "Forbidden keyword" in error


def test_validate_sql_accepts_plain_select() -> None:
	assert _validate_sql("SELECT COUNT(*) FROM credit_card_billing") is None


def test_engine_answer_ok_returns_rows_and_reply() -> None:
	llm = StubLLMClient(["SELECT COUNT(*) AS total FROM credit_card_billing"])
	engine = InsightEngine(llm=llm)
	result = engine.answer([], "How many transactions are there?")
	assert result["status"] == "OK"
	assert result["row_count"] == 1
	assert result["rows"][0]["total"] == 150
	assert "150" in result["reply"]
	assert result["error"] is None


def test_engine_answer_empty_when_no_rows_match() -> None:
	llm = StubLLMClient(
		["SELECT * FROM credit_card_billing WHERE department = 'Nonexistent Department Name'"]
	)
	engine = InsightEngine(llm=llm)
	result = engine.answer([], "Show me nonexistent department spend")
	assert result["status"] == "EMPTY"
	assert result["rows"] == []


def test_engine_answer_ok_with_null_aggregate_reads_as_no_data() -> None:
	llm = StubLLMClient(
		["SELECT SUM(transaction_amount_usd) FROM credit_card_billing WHERE department = 'Nonexistent'"]
	)
	engine = InsightEngine(llm=llm)
	result = engine.answer([], "total spend for a department that doesn't exist")
	assert result["status"] == "OK"
	assert "nothing to report" in result["reply"]


def test_engine_answer_refused_on_forbidden_sql() -> None:
	llm = StubLLMClient(["DROP TABLE credit_card_billing"])
	engine = InsightEngine(llm=llm)
	result = engine.answer([], "delete everything")
	assert result["status"] == "REFUSED"
	assert result["error"] is not None


def test_engine_answer_refused_on_not_supported_sentinel() -> None:
	llm = StubLLMClient(["NOT_SUPPORTED"])
	engine = InsightEngine(llm=llm)
	result = engine.answer([], "what's the weather in Paris?")
	assert result["status"] == "REFUSED"


def test_engine_answer_sql_error_on_bad_sql() -> None:
	llm = StubLLMClient(["SELECT nonexistent_column FROM credit_card_billing"])
	engine = InsightEngine(llm=llm)
	result = engine.answer([], "bad column")
	assert result["status"] == "SQL_ERROR"
	assert result["error"] is not None


def test_engine_answer_never_raises_without_llm() -> None:
	engine = InsightEngine(llm=None)
	result = engine.answer([], "anything")
	assert result["status"] == "LLM_ERROR"
	assert result["reply"]
