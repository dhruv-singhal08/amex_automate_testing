"""Per-product-shape evaluation profiles.

A profile knows three things about one KIND of product output: what a reference
for it looks like, what can be checked about it deterministically, and which
judge prompt reads it. Everything else in evaluation/ is shape-agnostic.

Only SqlReferenceProfile is implemented. Two more shapes are expected, and are
described here so the abstraction is judged against them rather than against SQL
alone:

  Q/A bot - the evaluable output is retrieved passages, not a query. A reference
      would carry source documents and the key facts an answer must contain;
      deterministic checks would be citation overlap and whether the cited
      sources exist, rather than table/column precision.

  Voice of customer - the evaluable output is a set of extracted themes. A
      reference would carry expected themes, sentiment polarity and customer
      segment; deterministic checks would be theme overlap and sentiment
      agreement, with no notion of "columns" at all.

Neither is implemented. They are named so that anyone extending this does not
mistake SQL's vocabulary for the interface.

Nothing in this module or its implementations executes anything. Evaluation is
static analysis only - the real product environment does not permit execution.
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ReferenceProfile(ABC):
	"""How to evaluate one kind of product output against a reference."""

	@property
	@abstractmethod
	def reference_model(self) -> type[BaseModel]:
		"""The model describing a reference for this output shape."""

	@property
	@abstractmethod
	def judge_prompt(self) -> str:
		"""Name of the judge prompt in the shared prompt library."""

	@property
	@abstractmethod
	def evaluable_kind(self) -> str:
		"""The AdapterResponse evaluable `kind` this profile can read."""

	@abstractmethod
	def deterministic_checks(
		self, reference: BaseModel, evaluable: dict[str, Any] | None
	) -> dict[str, Any]:
		"""Everything measurable without an LLM.

		Must never raise and never execute anything. Returns at least
		`parse_ok`; metrics are None when they cannot be computed, so that a
		failure here degrades to an LLM-only judgement rather than a crash.
		"""
