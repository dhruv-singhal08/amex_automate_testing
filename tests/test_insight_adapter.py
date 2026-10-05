from synthetic_user.adapters.insight import InsightAdapter
from synthetic_user.core.llm import StubLLMClient


def test_adapter_multi_turn_builds_transcript_for_followups() -> None:
	llm = StubLLMClient(
		[
			"SELECT department, COUNT(*) AS n FROM credit_card_billing "
			"WHERE department = 'Engineering' GROUP BY department",
			"SELECT department, COUNT(*) AS n FROM credit_card_billing "
			"WHERE department = 'Engineering' AND payment_status = 'Pending' GROUP BY department",
		]
	)
	adapter = InsightAdapter(llm=llm)
	session = adapter.open_session()

	first = adapter.send(session, "How many Engineering transactions are there?")
	assert first.error is None
	assert first.artifacts["status"] == "OK"

	second = adapter.send(session, "Just the pending ones")
	assert second.error is None
	assert len(session) == 2
	assert session[0]["question"] == "How many Engineering transactions are there?"
	assert "Pending" in session[1]["sql"]

	adapter.close_session(session)
	assert session == []


def test_adapter_surfaces_refusal_as_error() -> None:
	llm = StubLLMClient(["NOT_SUPPORTED"])
	adapter = InsightAdapter(llm=llm)
	session = adapter.open_session()

	response = adapter.send(session, "what's the weather?")
	assert response.error is not None
	assert response.artifacts["status"] == "REFUSED"
