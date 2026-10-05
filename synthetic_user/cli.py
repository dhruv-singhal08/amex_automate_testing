import csv
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mock_product.engine import ensure_billing_database
from synthetic_user.adapters.insight import InsightAdapter
from synthetic_user.adapters.mock import MockAdapter
from synthetic_user.assets.persona import load_persona
from synthetic_user.assets.product import load_capability_spec, load_product_profile
from synthetic_user.assets.registry import (
	load_named_fewshots,
	load_named_glossary,
	load_named_schema,
	persona_path,
	product_path,
)
from synthetic_user.core import tracing
from synthetic_user.core.config import get_settings
from synthetic_user.core.llm import LLMClient
from synthetic_user.core.models import GeneratedQuestion, QuestionReference
from synthetic_user.core.storage import (
	get_conversations,
	get_questions,
	get_run,
	get_validations,
	init_db,
	get_judge_results,
	get_references,
	save_judge_result,
	save_question,
	save_reference,
	save_validation,
)
from synthetic_user.evaluation.calibration import (
	calibration_status,
	export_calibration,
	import_calibration,
)
from synthetic_user.evaluation.judge import PROMPT_VERSION as JUDGE_PROMPT_VERSION
from synthetic_user.evaluation.judge import judge as judge_conversation
from synthetic_user.evaluation.metamorphic import check_question, consistency_rates
from synthetic_user.metrics.registry import compute_metrics
from synthetic_user.simulation.runner import run_grid
from synthetic_user.validation.validator import validate


app = typer.Typer(no_args_is_help=True)
console = Console()

OVERALL_HEADERS = (
	"correct",
	"partially_correct",
	"incorrect",
	"reference_suspect",
	"not_evaluable",
)


def _settings():
	return get_settings()


def _questions_from_file(path: Path) -> list[GeneratedQuestion]:
	data = json.loads(path.read_text(encoding="utf-8"))
	return [GeneratedQuestion.model_validate(item) for item in data]


def _questions_path(path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)


def _references_from_file(path: Path) -> list[QuestionReference]:
	"""Load every reference in a file, valid or not.

	Callers filter to valid ones before running. Invalid references are still
	stored, because they are the denominator for reference_invalid_rate.
	"""
	data = json.loads(path.read_text(encoding="utf-8"))
	records = data if isinstance(data, list) else data.get("references", [])
	# Accept both a bare reference and a {question, reference} pair.
	return [QuestionReference.model_validate(item.get("reference", item)) for item in records]


@app.command("init-db")
def init_db_command() -> None:
	settings = _settings()
	init_db(settings.database_url)
	result = ensure_billing_database()
	console.print(f"Application database initialized: {settings.database_url}")
	console.print(f"Insight database initialized: {result['db_path']} ({result['row_count']} rows)")


@app.command("list-personas")
def list_personas() -> None:
	table = Table("ID", "Name", "Expertise", "Approval")
	for path in sorted(persona_path("").parent.glob("*.yaml")):
		persona = load_persona(path)
		table.add_row(persona.id, persona.name, persona.domain_expertise, persona.approval_state)
	console.print(table)


@app.command()
def generate(
	persona: str = typer.Option(...),
	product: str = typer.Option(...),
	n: int = typer.Option(..., min=1),
	out: Path = typer.Option(Path("runs/questions.json")),
	reference: bool = typer.Option(
		False, "--reference", help="Also generate a reference for each question."
	),
	fewshot_mode: str = typer.Option(
		"mixed", "--fewshot-mode", help="mixed | none (none forces every item to schema_only)."
	),
	schema: str = typer.Option("insight_schema", help="Schema asset name, used only with --reference."),
	fewshots: str = typer.Option("insight_fewshots", help="Few-shot asset name."),
	glossary: str = typer.Option("insight_glossary", help="Glossary asset name; may be absent."),
	references_out: Path = typer.Option(Path("runs/references.json")),
) -> None:
	persona_model = load_persona(persona_path(persona))
	product_model = load_product_profile(Path("assets/products") / f"{product}.yaml")

	if not reference:
		from synthetic_user.generation.generator import generate_questions

		questions = generate_questions(persona_model, product_model, None, n, LLMClient(), seed=0)
		_questions_path(out)
		out.write_text(
			json.dumps([question.model_dump(mode="json") for question in questions], indent=2),
			encoding="utf-8",
		)
		console.print(f"Generated {len(questions)} questions: {out}")
		for question in questions:
			console.print(f"[{question.complexity}] {question.question}")
		return

	if fewshot_mode not in {"mixed", "none"}:
		raise typer.BadParameter("--fewshot-mode must be mixed or none")

	from synthetic_user.generation.reference_generator import generate_with_references

	pairs = generate_with_references(
		persona=persona_model,
		product_profile=product_model,
		schema=load_named_schema(schema),
		fewshots=load_named_fewshots(fewshots),
		glossary=load_named_glossary(glossary),
		past_queries=None,
		n=n,
		fewshot_mode=fewshot_mode,
		llm=LLMClient(),
		seed=0,
		settings=_settings(),
	)

	_questions_path(out)
	_questions_path(references_out)
	out.write_text(
		json.dumps([question.model_dump(mode="json") for question, _ in pairs], indent=2),
		encoding="utf-8",
	)
	references_out.write_text(
		json.dumps([ref.model_dump(mode="json") for _, ref in pairs], indent=2), encoding="utf-8"
	)

	valid = sum(1 for _, ref in pairs if ref.validation_status == "valid")
	console.print(
		f"Generated {len(pairs)} question/reference pairs ({valid} valid, "
		f"{len(pairs) - valid} invalid): {out}, {references_out}"
	)
	for question, ref in pairs:
		marker = "ok " if ref.validation_status == "valid" else "BAD"
		console.print(f"[{marker}][{ref.few_shot_distance}/{ref.expected_behaviour}] {question.question}")
		if ref.validation_notes:
			console.print(f"        {ref.validation_notes}")


@app.command()
def run(
	persona: str = typer.Option(...),
	product: str = typer.Option(...),
	adapter: str = typer.Option("mock"),
	questions: Path = typer.Option(...),
	repeats: int = typer.Option(1, min=1),
	out: Path = typer.Option(Path("runs/latest.json")),
	reference: Path | None = typer.Option(
		None,
		"--reference",
		help="Reference-guided run: a JSON file of QuestionReference records. "
		"Only questions whose reference validation_status is 'valid' are run.",
	),
) -> None:
	if adapter not in {"mock", "insight"}:
		raise typer.BadParameter("adapter must be mock or insight")
	settings = _settings().model_copy(update={"repeats_per_question": repeats})
	persona_model = load_persona(persona_path(persona))
	product_model = load_product_profile(Path("assets/products") / f"{product}.yaml")
	question_list = _questions_from_file(questions)
	adapter_instance = InsightAdapter() if adapter == "insight" else MockAdapter()

	references: dict[str, QuestionReference] = {}
	all_references: list[QuestionReference] = []
	if reference is not None:
		all_references = _references_from_file(reference)
		references = {
			item.question_id: item for item in all_references if item.validation_status == "valid"
		}
		skipped = [q for q in question_list if q.id not in references]
		question_list = [q for q in question_list if q.id in references]
		console.print(
			f"Reference-guided run: {len(question_list)} question(s) with a valid reference, "
			f"{len(skipped)} skipped."
		)
		if not question_list:
			raise typer.BadParameter("No question has a valid reference; nothing to run.")

	run_record = run_grid(
		[persona_model],
		{persona_model.id: question_list},
		product_model,
		adapter_instance,
		LLMClient(),
		settings,
		references_by_question=references or None,
	)
	for question in question_list:
		save_question(question, run_record.id)
	# Store invalid references too: they are excluded from the run but are what
	# reference_invalid_rate is measured over.
	for question_reference in all_references:
		save_reference(question_reference, run_record.id)
	_questions_path(out)
	out.write_text(run_record.model_dump_json(indent=2), encoding="utf-8")
	console.print(f"Run {run_record.id} completed with {len(run_record.conversation_ids)} conversations")
	console.print(f"Saved run record: {out}")


@app.command()
def evaluate(
	run_id: str = typer.Option(..., "--run-id"),
	glossary: str = typer.Option("insight_glossary"),
) -> None:
	"""Judge each conversation in a run against its reference. Never executes SQL."""
	run_record = get_run(run_id)
	if run_record is None:
		raise typer.BadParameter(f"Unknown run: {run_id}")

	references = {item.question_id: item for item in get_references(run_id)}
	if not references:
		raise typer.BadParameter(
			f"Run {run_id} has no references. Re-run it with --reference to evaluate it."
		)

	glossary_terms = load_named_glossary(glossary)
	llm = LLMClient()
	conversations = get_conversations(run_id)
	judged = 0
	tally: dict[str, int] = {}
	for conversation in conversations:
		reference = references.get(conversation.question_id)
		if reference is None or reference.validation_status != "valid":
			continue
		result = judge_conversation(conversation, reference, llm, glossary=glossary_terms)
		save_judge_result(result, run_id)
		tally[result.overall] = tally.get(result.overall, 0) + 1
		judged += 1

	console.print(f"Judged {judged} of {len(conversations)} conversation(s) for {run_id}")
	for verdict, count in sorted(tally.items()):
		console.print(f"  {verdict}: {count}")


@app.command()
def metamorphic(
	run_id: str = typer.Option(..., "--run-id"),
	adapter: str = typer.Option("insight"),
	schema: str = typer.Option("insight_schema"),
) -> None:
	"""Consistency probes: does the product answer the same question the same way?

	Needs neither execution nor ground truth - each variant is compared against
	the original run's own query.
	"""
	run_record = get_run(run_id)
	if run_record is None:
		raise typer.BadParameter(f"Unknown run: {run_id}")
	if adapter not in {"mock", "insight"}:
		raise typer.BadParameter("adapter must be mock or insight")

	references = {item.question_id: item for item in get_references(run_id)}
	valid = {qid: ref for qid, ref in references.items() if ref.validation_status == "valid"}
	if not valid:
		raise typer.BadParameter(f"Run {run_id} has no valid references to probe.")

	questions = {question.id: question for question in get_questions(run_id)}
	schema_spec = load_named_schema(schema)
	adapter_instance = InsightAdapter() if adapter == "insight" else MockAdapter()
	llm = LLMClient()

	# One conversation per question is enough: the probes compare against the
	# original query, not against each other.
	seen: set[str] = set()
	results = []
	for conversation in get_conversations(run_id):
		question_id = conversation.question_id
		if question_id in seen or question_id not in valid:
			continue
		original_sql = ""
		for turn in reversed(conversation.turns):
			if (turn.evaluable or {}).get("query"):
				original_sql = turn.evaluable["query"]
				break
		if not original_sql:
			continue
		seen.add(question_id)

		question = questions.get(question_id)
		question_text = question.question if question else conversation.turns[0].message
		console.print(f"probing {question_id}: {question_text}")
		results.extend(
			check_question(
				persona=load_persona(persona_path(conversation.persona_id)),
				question_id=question_id,
				question_text=question_text,
				original_sql=original_sql,
				adapter=adapter_instance,
				llm=llm,
				schema=schema_spec,
				seed=conversation.seed,
				dialect=_settings().sql_dialect,
			)
		)

	if not results:
		console.print("No conversation produced a query to probe.")
		return

	report = consistency_rates(results, valid)
	table = Table("Relation", "Consistency", "Comparable", "Unparseable", "No SQL", "By few-shot distance")
	for relation in ("paraphrase", "irrelevant_detail", "time_shift"):
		row = report[relation]
		table.add_row(
			relation,
			"n/a" if row["consistency_rate"] is None else f"{row['consistency_rate']}%",
			str(row["comparable"]),
			str(row["unparseable"]),
			str(row["no_sql"]),
			json.dumps({k: v["consistency_rate"] for k, v in row["by_few_shot_distance"].items()}),
		)
	console.print(Panel(table, title="METAMORPHIC CONSISTENCY"))

	inconsistent = [r for r in results if r.status == "inconsistent"]
	if inconsistent:
		console.print(Panel(f"{len(inconsistent)} inconsistency(ies)", title="REVIEW THESE"))
		for item in inconsistent[:15]:
			console.print(f"  [{item.relation}] {item.variant_question}")
			console.print(f"    differs in: {', '.join(item.differences)}")


@app.command("export-calibration")
def export_calibration_command(
	run_id: str = typer.Option(..., "--run-id"),
	n: int = typer.Option(60, "--n", min=1),
	out: Path | None = typer.Option(None),
) -> None:
	"""Write a stratified review sheet so the judge itself can be measured."""
	if get_run(run_id) is None:
		raise typer.BadParameter(f"Unknown run: {run_id}")
	if not get_judge_results(run_id):
		raise typer.BadParameter(f"Run {run_id} has no judge results. Run `evaluate` first.")
	path = export_calibration(run_id, n=n, out_path=out)
	console.print(f"Wrote calibration sheet: {path}")
	console.print("Fill in human_overall, human_reference_ok, human_notes and reviewer, then import it.")


@app.command("import-calibration")
def import_calibration_command(file: Path = typer.Option(..., "--file")) -> None:
	"""Store human labels and report how closely the judge matched them."""
	if not file.exists():
		raise typer.BadParameter(f"No such file: {file}")
	report = import_calibration(file)

	console.print(f"Stored {report['labels_stored']} label(s)")
	console.print(f"Agreement with judge: {report['accuracy']}%")
	console.print(f"Cohen's kappa: {report['cohens_kappa']}")
	if report["reference_ok_rate"] is not None:
		console.print(f"References judged sound by a human: {report['reference_ok_rate']}%")

	if report["confusion_matrix"]:
		table = Table("judge \ human", *OVERALL_HEADERS)
		for judge_verdict, humans in report["confusion_matrix"].items():
			table.add_row(judge_verdict, *[str(humans.get(h, 0)) for h in OVERALL_HEADERS])
		console.print(Panel(table, title="CONFUSION MATRIX"))

	if report["disagreements"]:
		console.print(Panel(f"{len(report['disagreements'])} disagreement(s)", title="REVIEW THESE"))
		for item in report["disagreements"][:20]:
			console.print(f"  {item['conversation_id']}: judge={item['judge']} human={item['human']}")
			console.print(f"    {item['question']}")
			if item["human_notes"]:
				console.print(f"    note: {item['human_notes']}")
	for note in report["skipped"]:
		console.print(f"[skipped] {note}")


@app.command("validate")
def validate_run(run_id: str = typer.Option(..., "--run-id")) -> None:
	run_record = get_run(run_id)
	if run_record is None:
		raise typer.BadParameter(f"Unknown run: {run_id}")
	product = load_product_profile(Path("assets/products") / "insight_profile.yaml")
	spec = load_capability_spec(Path("assets/products") / "insight_capability.yaml")
	questions = {question.id: question for question in get_questions(run_id)}
	for conversation in get_conversations(run_id):
		question = questions.get(conversation.question_id)
		result = validate(
			conversation,
			question.intent if question else conversation.question_id,
			product,
			spec,
			LLMClient(),
			_settings(),
		)
		save_validation(result, run_id)
	console.print(f"Validated {len(get_conversations(run_id))} conversations for {run_id}")


@app.command()
def metrics(run_id: str = typer.Option(..., "--run-id")) -> None:
	run_record = get_run(run_id)
	if run_record is None:
		raise typer.BadParameter(f"Unknown run: {run_id}")
	conversations = get_conversations(run_id)
	validations = get_validations(run_id)
	personas = {}
	for conversation in conversations:
		try:
			personas[conversation.persona_id] = load_persona(persona_path(conversation.persona_id))
		except FileNotFoundError:
			pass
	questions = {question.id: question for question in get_questions(run_id)}
	references = {item.question_id: item for item in get_references(run_id)}
	judge_results = {item.conversation_id: item for item in get_judge_results(run_id)}

	# `requires` is satisfied from these keys, so reference metrics are skipped
	# rather than zeroed when a run was not reference-guided.
	artifacts = {}
	if references:
		artifacts["reference"] = True
	if judge_results:
		artifacts["judge_result"] = True

	report = compute_metrics(
		SimpleNamespace(
			personas=personas,
			questions=questions,
			references=references,
			judge_results=judge_results,
			artifacts=artifacts,
		),
		conversations,
		validations,
	)

	reference_metric_names = {
		"correct_rate", "partially_correct_rate", "incorrect_rate", "failure_tag_distribution",
		"table_precision", "table_recall", "column_precision", "column_recall",
		"behaviour_accuracy", "generalisation_gap",
	}
	product_rows = []
	harness_rows = []
	reference_rows = []
	for name, value in report["metrics"].items():
		if isinstance(value, dict) and value.get("label") == "HARNESS HEALTH":
			harness_rows.append((name, value["values"]["aggregate"]))
		elif name in reference_metric_names:
			reference_rows.append((name, value["aggregate"] if isinstance(value, dict) else value))
		else:
			product_rows.append((name, value["aggregate"] if isinstance(value, dict) else value))
	panels = [("PRODUCT METRICS", product_rows)]
	if reference_rows:
		panels.append(("REFERENCE-GUIDED CORRECTNESS", reference_rows))
	panels.append(("HARNESS HEALTH", harness_rows))

	for title, rows in panels:
		table = Table("Metric", "Aggregate")
		for name, value in rows:
			table.add_row(name, json.dumps(value, default=str))
		console.print(Panel(table, title=title))
		if title == "REFERENCE-GUIDED CORRECTNESS":
			status = calibration_status(JUDGE_PROMPT_VERSION)
			if status["banner"]:
				console.print(f"[bold yellow]{status['banner']}[/bold yellow]")
			else:
				console.print(
					f"Judge calibrated against {status['label_count']} human label(s) "
					f"for {status['judge_prompt_version']}."
				)
	if report["skipped"]:
		console.print(Panel(json.dumps(report["skipped"], indent=2), title="SKIPPED METRICS"))


@app.command()
def export(
	run_id: str = typer.Option(..., "--run-id"),
	format: str = typer.Option(..., "--format"),
	out: Path | None = typer.Option(None),
) -> None:
	if format not in {"csv", "json"}:
		raise typer.BadParameter("format must be csv or json")
	conversations = get_conversations(run_id)
	validations = {item.conversation_id: item for item in get_validations(run_id)}
	path = out or Path("runs") / f"{run_id}.{format}"
	path.parent.mkdir(parents=True, exist_ok=True)
	rows = [
		{
			"conversation_id": conversation.id,
			"persona_id": conversation.persona_id,
			"question_id": conversation.question_id,
			"repeat_index": conversation.repeat_index,
			"outcome": conversation.outcome.value,
			"turn_count": len(conversation.turns),
			"doubt_anywhere": conversation.doubt_anywhere,
			"classification": validations.get(conversation.id).classification if conversation.id in validations else None,
		}
		for conversation in conversations
	]
	if format == "json":
		path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
	else:
		with path.open("w", newline="", encoding="utf-8") as file:
			writer = csv.DictWriter(file, fieldnames=list(rows[0]) if rows else ["conversation_id"])
			writer.writeheader()
			writer.writerows(rows)
	console.print(f"Exported {len(rows)} rows to {path}")


@app.callback(result_callback=lambda *_args, **_kwargs: tracing.flush())  # tracing
def _main() -> None:
	"""Synthetic user platform."""


if __name__ == "__main__":
	app()
