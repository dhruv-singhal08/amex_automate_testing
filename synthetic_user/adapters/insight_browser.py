from typing import Any

from playwright.sync_api import Browser, Page, Playwright, expect, sync_playwright

from mock_product.engine import InsightEngine
from synthetic_user.core.models import AdapterResponse

DEFAULT_BASE_URL = "http://localhost:8501"
DEFAULT_TIMEOUT_MS = 45_000

_DONE_MARKER = '[data-testid="insight-turn-done"]'


class _BrowserSession:
	def __init__(self, playwright: Playwright, browser: Browser, page: Page) -> None:
		self.playwright = playwright
		self.browser = browser
		self.page = page
		self.transcript: list[dict[str, Any]] = []


class InsightBrowserAdapter:
	"""Types each question into the live insight_app.py UI so a conversation is
	visible in a real browser, exactly as a human using the product would see it.

	The browser is for visibility only: it is not scraped for results. The
	response returned (and recorded) comes from a direct backend call to
	InsightEngine, the same single source of truth InsightAdapter uses, so the
	recorded conversation never depends on parsing Streamlit's rendered DOM.
	"""

	def __init__(
		self,
		base_url: str = DEFAULT_BASE_URL,
		headed: bool = True,
		timeout_ms: int = DEFAULT_TIMEOUT_MS,
		llm: Any = None,
	) -> None:
		if llm is None:
			from synthetic_user.core.llm import LLMClient

			llm = LLMClient()
		self._engine = InsightEngine(llm=llm)
		self.base_url = base_url
		self.headed = headed
		self.timeout_ms = timeout_ms

	def open_session(self) -> _BrowserSession:
		playwright = sync_playwright().start()
		try:
			browser = playwright.chromium.launch(headless=not self.headed)
			page = browser.new_page()
			page.goto(self.base_url, wait_until="networkidle", timeout=self.timeout_ms)
		except Exception:
			playwright.stop()
			raise
		return _BrowserSession(playwright, browser, page)

	def send(self, session: _BrowserSession, message: str) -> AdapterResponse:
		self._show_in_browser(session, message)

		result = self._engine.answer(session.transcript, message)
		session.transcript.append({"question": message, "sql": result["sql"]})
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

	def _show_in_browser(self, session: _BrowserSession, message: str) -> None:
		"""Best-effort visual mirroring. Failures here never affect the recorded
		result — they only mean the browser fell behind or wasn't watchable."""
		try:
			done_markers = session.page.locator(_DONE_MARKER)
			before = done_markers.count()
			textarea = session.page.get_by_test_id("stChatInputTextArea")
			textarea.click()
			textarea.fill(message)
			textarea.press("Enter")
			expect(done_markers).to_have_count(before + 1, timeout=self.timeout_ms)
		except Exception:  # noqa: BLE001
			pass

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

	def close_session(self, session: _BrowserSession) -> None:
		try:
			session.page.close()
		except Exception:  # noqa: BLE001
			pass
		try:
			session.browser.close()
		finally:
			session.playwright.stop()
