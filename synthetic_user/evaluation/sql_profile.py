"""Deterministic SQL analysis for reference-guided evaluation.

Static analysis only: SQL is parsed, never executed. Nothing here opens a
database connection, and it must stay that way - the real product environment
does not allow execution, so any check that needs a result set is off limits.
"""

import logging
from typing import Any

import sqlglot
from sqlglot import exp

from synthetic_user.core.models import QuestionReference
from synthetic_user.evaluation.reference_profile import ReferenceProfile

logger = logging.getLogger(__name__)


def _precision_recall(predicted: set[str], expected: set[str]) -> tuple[float | None, float | None]:
	"""None where the ratio is undefined, rather than a misleading zero."""
	overlap = len(predicted & expected)
	precision = overlap / len(predicted) if predicted else None
	recall = overlap / len(expected) if expected else None
	return precision, recall


def _alias_map(tree: exp.Expression) -> dict[str, str]:
	"""alias -> real table name, so `c.department` resolves to its table."""
	aliases: dict[str, str] = {}
	for table in tree.find_all(exp.Table):
		aliases[table.alias_or_name] = table.name
		aliases[table.name] = table.name
	return aliases


def parse_sql(query: str, dialect: str = "duckdb") -> dict[str, Any]:
	"""Extract the structure of a SELECT. Never raises.

	Returns parse_ok False with empty structure when the SQL cannot be parsed;
	callers are expected to continue to the LLM judge rather than give up.
	"""
	blank: dict[str, Any] = {
		"parse_ok": False,
		"tables": [],
		"columns": [],
		"where_predicates": [],
		"group_by": [],
		"aggregates": [],
		"order_by": [],
		"limit": None,
	}
	if not query or not query.strip():
		return blank

	try:
		tree = sqlglot.parse_one(query, dialect=dialect)
	except Exception as error:  # noqa: BLE001 - malformed SQL is data, not a crash
		logger.info("Could not parse SQL for evaluation: %s", error)
		return blank
	if tree is None:
		return blank

	try:
		aliases = _alias_map(tree)
		tables = sorted({table.name for table in tree.find_all(exp.Table)})
		sole_table = tables[0] if len(tables) == 1 else None

		# Names introduced by `AS` in the projection. An unqualified reference to
		# one of these (typically in ORDER BY) is an output label, not a real
		# column - counting it would invent a column the schema does not have.
		output_aliases = {
			node.alias for node in tree.find_all(exp.Alias) if node.alias and not node.find(exp.Table)
		}

		def qualify(column: exp.Column) -> str | None:
			if not column.table and column.name in output_aliases:
				return None
			qualifier = aliases.get(column.table, column.table) if column.table else sole_table
			return f"{qualifier}.{column.name}" if qualifier else column.name

		columns: set[str] = set()
		for column in tree.find_all(exp.Column):
			resolved = qualify(column)
			if resolved:
				columns.add(resolved)

		def normalise(node: exp.Expression) -> exp.Expression:
			"""Rewrite column references to their resolved table, so an aliased
			query and a plain one yield the same predicate text."""
			if isinstance(node, exp.Column):
				resolved = qualify(node)
				if resolved and "." in resolved:
					table_name, column_name = resolved.split(".", 1)
					return exp.column(column_name, table=table_name)
			return node

		where = tree.find(exp.Where)
		predicates = []
		if where is not None:
			for predicate in where.find_all(exp.Condition):
				# Keep only leaf comparisons, not the AND/OR nodes joining them.
				if isinstance(predicate, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Like, exp.Between)):
					predicates.append(predicate.copy().transform(normalise).sql(dialect=dialect))

		def render(item: exp.Expression) -> str:
			"""Render with aliases resolved, so `c.department` and `department`
			produce the same structure for otherwise-equivalent queries."""
			if isinstance(item, exp.Column):
				return qualify(item) or item.name
			return item.sql(dialect=dialect)

		group = tree.find(exp.Group)
		group_by = sorted(render(item) for item in group.expressions) if group else []

		aggregates = sorted(
			{node.sql_name().upper() for node in tree.find_all(exp.AggFunc)}
		)

		order = tree.find(exp.Order)
		order_by = [render(item) for item in order.expressions] if order else []

		limit_node = tree.find(exp.Limit)
		limit = limit_node.expression.sql(dialect=dialect) if limit_node is not None else None

		return {
			"parse_ok": True,
			"tables": tables,
			"columns": sorted(columns),
			"where_predicates": sorted(predicates),
			"group_by": group_by,
			"aggregates": aggregates,
			"order_by": order_by,
			"limit": limit,
		}
	except Exception as error:  # noqa: BLE001
		logger.warning("SQL parsed but could not be profiled: %s", error)
		return blank


class SqlReferenceProfile(ReferenceProfile):
	"""Evaluation profile for products whose evaluable output is a SQL query."""

	def __init__(self, dialect: str = "duckdb") -> None:
		self.dialect = dialect

	@property
	def reference_model(self) -> type[QuestionReference]:
		return QuestionReference

	@property
	def judge_prompt(self) -> str:
		return "judge_sql_v1.md"

	@property
	def evaluable_kind(self) -> str:
		return "sql"

	def deterministic_checks(
		self, reference: QuestionReference, evaluable: dict[str, Any] | None
	) -> dict[str, Any]:
		query = (evaluable or {}).get("query") or ""
		structure = parse_sql(query, self.dialect)

		table_precision = table_recall = column_precision = column_recall = None
		if structure["parse_ok"]:
			table_precision, table_recall = _precision_recall(
				set(structure["tables"]), set(reference.tables)
			)
			column_precision, column_recall = _precision_recall(
				set(structure["columns"]), set(reference.columns)
			)

		return {
			"parse_ok": structure["parse_ok"],
			"structure": structure,
			"table_precision": table_precision,
			"table_recall": table_recall,
			"column_precision": column_precision,
			"column_recall": column_recall,
		}
