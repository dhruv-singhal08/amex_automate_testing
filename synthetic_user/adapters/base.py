import logging
from typing import Any, Protocol

from synthetic_user.core.models import AdapterResponse

logger = logging.getLogger(__name__)

# No code outside adapters/ may read a named key from AdapterResponse.artifacts.
# Product-specific data lives only in that dict. If you find yourself writing
# response.artifacts['sql'] anywhere else, the abstraction has leaked.
#
# extract_evaluable is the sanctioned way out: the adapter - which is allowed to
# read its own artifacts - hands back a standard-shaped dict the judge can read
# without knowing anything about this product.


class Adapter(Protocol):
	def open_session(self) -> Any:
		...

	def send(self, session: Any, message: str) -> AdapterResponse:
		...

	def close_session(self, session: Any) -> None:
		...

	def extract_evaluable(self, response: AdapterResponse) -> dict[str, Any] | None:
		"""Structured output for evaluation, or None if there is nothing to judge."""
		return None


def evaluable_from(adapter: Any, response: AdapterResponse) -> dict[str, Any] | None:
	"""Ask an adapter for its evaluable output, tolerating adapters without one.

	Extraction is never load-bearing for a run, so a failure here is logged and
	dropped rather than allowed to end a conversation.
	"""
	extractor = getattr(adapter, "extract_evaluable", None)
	if extractor is None:
		return None
	try:
		return extractor(response)
	except Exception as error:  # noqa: BLE001
		logger.warning("Adapter could not extract evaluable output: %s", error)
		return None
