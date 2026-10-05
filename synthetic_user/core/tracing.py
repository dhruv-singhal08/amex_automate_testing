"""Optional Langfuse tracing for local verification.

Entirely inert unless BOTH conditions hold:
  1. the `langfuse` package is installed (`uv sync --extra tracing`), and
  2. LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set.

Otherwise every function here is a no-op that costs a dictionary lookup. Tracing
never raises: a monitoring failure must never break a run, so errors are logged
at debug level and swallowed.

TO REMOVE TRACING COMPLETELY: delete this file, drop the `tracing` extra from
pyproject.toml, and remove the `tracing.` call sites — there are five, all
marked with a `# tracing` comment:
  core/llm.py, simulation/runner.py (x2), validation/validator.py,
  generation/generator.py
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

_client: Any = None
_resolved = False

MAX_FIELD_CHARS = 20_000


def _get_client() -> Any:
	global _client, _resolved
	if _resolved:
		return _client
	_resolved = True
	try:
		from dotenv import load_dotenv

		load_dotenv()
	except Exception:  # noqa: BLE001 - .env is optional
		pass

	if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
		return None
	try:
		from langfuse import Langfuse

		_client = Langfuse()
	except Exception as error:  # noqa: BLE001 - tracing is optional
		logger.debug("Langfuse tracing unavailable: %s", error)
		_client = None
	return _client


def enabled() -> bool:
	return _get_client() is not None


def _truncate(value: Any) -> Any:
	if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
		return f"{value[:MAX_FIELD_CHARS]}… [truncated {len(value) - MAX_FIELD_CHARS} chars]"
	return value


@contextmanager
def _guarded(factory: Callable[[], Any]) -> Iterator[None]:
	"""Enter a tracing context manager, degrading to a no-op if it misbehaves.

	Exceptions raised by the wrapped body propagate untouched; only tracing's own
	failures are suppressed.
	"""
	manager = None
	try:
		manager = factory()
		manager.__enter__()
	except Exception as error:  # noqa: BLE001
		logger.debug("Langfuse span could not start: %s", error)
		manager = None
	try:
		yield
	finally:
		if manager is not None:
			try:
				manager.__exit__(None, None, None)
			except Exception as error:  # noqa: BLE001
				logger.debug("Langfuse span could not close: %s", error)


@contextmanager
def session(session_id: str, **attributes: Any) -> Iterator[None]:
	"""Group every trace created inside this block under one Langfuse session."""
	client = _get_client()
	if client is None:
		yield
		return

	def factory() -> Any:
		from langfuse import propagate_attributes

		return propagate_attributes(session_id=session_id, **attributes)

	with _guarded(factory):
		yield


@contextmanager
def trace(name: str, **metadata: Any) -> Iterator[None]:
	"""A top-level unit of work — one conversation, one generation, one validation."""
	client = _get_client()
	if client is None:
		yield
		return

	def factory() -> Any:
		return client.start_as_current_observation(
			name=name[:200],
			as_type="span",
			metadata=metadata or None,
		)

	with _guarded(factory):
		yield


@contextmanager
def span(name: str, **metadata: Any) -> Iterator[None]:
	"""A nested step inside a trace — one turn of a conversation."""
	client = _get_client()
	if client is None:
		yield
		return

	def factory() -> Any:
		return client.start_as_current_observation(
			name=name[:200],
			as_type="span",
			metadata=metadata or None,
		)

	with _guarded(factory):
		yield


class _NullCall:
	def finish(self, **_: Any) -> None:
		return None


class _LangfuseCall:
	def __init__(self, observation: Any) -> None:
		self._observation = observation

	def finish(self, *, output: Any = None, usage: dict[str, int] | None = None) -> None:
		try:
			self._observation.update(
				output=_truncate(output),
				usage_details=usage or None,
			)
		except Exception as error:  # noqa: BLE001
			logger.debug("Langfuse generation could not record output: %s", error)

	def fail(self, error: BaseException) -> None:
		try:
			self._observation.update(level="ERROR", status_message=str(error)[:500])
		except Exception as inner:  # noqa: BLE001
			logger.debug("Langfuse generation could not record error: %s", inner)


@contextmanager
def llm_call(*, name: str, model: str, prompt: str, **metadata: Any) -> Iterator[Any]:
	"""Wrap one LLM request. Yields a handle whose .finish() records the response."""
	client = _get_client()
	if client is None:
		yield _NullCall()
		return

	observation = None
	try:
		manager = client.start_as_current_observation(
			name=name,
			as_type="generation",
			input=_truncate(prompt),
			model=model,
			metadata=metadata or None,
		)
		observation = manager.__enter__()
	except Exception as error:  # noqa: BLE001
		logger.debug("Langfuse generation could not start: %s", error)
		yield _NullCall()
		return

	handle = _LangfuseCall(observation)
	try:
		yield handle
	except Exception as error:
		handle.fail(error)
		raise
	finally:
		try:
			manager.__exit__(None, None, None)
		except Exception as error:  # noqa: BLE001
			logger.debug("Langfuse generation could not close: %s", error)


def flush() -> None:
	"""Push buffered events. Langfuse batches in the background, so a short-lived
	process (a CLI command) can exit before anything is sent."""
	client = _get_client()
	if client is None:
		return
	try:
		client.flush()
	except Exception as error:  # noqa: BLE001
		logger.debug("Langfuse flush failed: %s", error)
