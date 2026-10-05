import pytest

from synthetic_user.core import tracing


@pytest.fixture(autouse=True, scope="session")
def _never_trace_from_tests() -> None:
	"""Keep the test suite out of Langfuse.

	run_grid and run_conversation open spans, so without this every pytest run
	would publish traces to whatever project the developer's .env points at.
	"""
	tracing._resolved = True
	tracing._client = None
