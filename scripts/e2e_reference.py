"""End-to-end reference-guided evaluation on the mock product.

Phases are checkpointed to runs/ so a quota failure part-way through does not
lose everything before it. Run with a phase name to resume:

    uv run python scripts/e2e_reference.py            # all phases
    uv run python scripts/e2e_reference.py evaluate   # from evaluate onwards
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from synthetic_user.adapters.insight import InsightAdapter
from synthetic_user.assets.registry import (
	load_named_fewshots,
	load_named_glossary,
	load_named_persona,
	load_named_product,
	load_named_schema,
)
from synthetic_user.core.config import get_settings
from synthetic_user.core.llm import LLMClient
from synthetic_user.core.models import GeneratedQuestion, QuestionReference
from synthetic_user.core.storage import (
	get_conversations,
	get_judge_results,
	get_questions,
	get_references,
	init_db,
	save_judge_result,
	save_question,
	save_reference,
)
from synthetic_user.evaluation.calibration import export_calibration
from synthetic_user.evaluation.judge import judge as judge_conversation
from synthetic_user.evaluation.metamorphic import check_question, consistency_rates
from synthetic_user.generation.reference_generator import generate_with_references
from synthetic_user.simulation.runner import run_grid

RUNS = Path("runs")
STATE = RUNS / "e2e_state.json"
PHASES = ("generate", "run", "evaluate", "metamorphic", "calibration")


def log(message: str) -> None:
	stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
	print(f"[{stamp}] {message}", flush=True)


def load_state() -> dict:
	return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(state: dict) -> None:
	RUNS.mkdir(parents=True, exist_ok=True)
	STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def pairs_to_json(pairs) -> list[dict]:
	return [
		{"question": question.model_dump(mode="json"), "reference": reference.model_dump(mode="json")}
		for question, reference in pairs
	]


def phase_generate(state: dict) -> dict:
	settings = get_settings()
	llm = LLMClient()
	schema = load_named_schema("insight_schema")
	fewshots = load_named_fewshots("insight_fewshots")
	glossary = load_named_glossary("insight_glossary")
	product = load_named_product("insight_profile")

	batches = {
		"mixed": [("junior_analyst", 12, "mixed"), ("finance_controller", 12, "mixed")],
		"none": [("junior_analyst", 6, "none")],
	}

	generated: dict[str, list[dict]] = {}
	for batch_name, specs in batches.items():
		rows: list[dict] = []
		for persona_name, n, mode in specs:
			log(f"generating {batch_name}: {persona_name} n={n} mode={mode}")
			pairs = generate_with_references(
				persona=load_named_persona(persona_name),
				product_profile=product,
				schema=schema,
				fewshots=fewshots,
				glossary=glossary,
				past_queries=None,
				n=n,
				fewshot_mode=mode,
				llm=llm,
				seed=1000 if batch_name == "mixed" else 2000,
				settings=settings,
			)
			# Question ids restart at question-1 for every persona AND every batch, so
			# they must be namespaced by both. Namespacing by persona alone let the
			# "none" batch's junior_analyst-question-1..6 collide with "mixed"'s, and
			# save_reference is keyed by id - so saving the second batch silently
			# reassigned six of the first batch's references to the wrong run.
			for question, reference in pairs:
				suffix = f"{batch_name}-{persona_name}-{question.id}"
				question = question.model_copy(update={"id": suffix, "reference_id": f"ref-{suffix}"})
				reference = reference.model_copy(
					update={"id": f"ref-{suffix}", "question_id": suffix}
				)
				rows.append(
					{
						"persona": persona_name,
						"question": question.model_dump(mode="json"),
						"reference": reference.model_dump(mode="json"),
					}
				)
			valid = sum(1 for r in rows if r["reference"]["validation_status"] == "valid")
			log(f"  {persona_name}: {len(pairs)} pairs, {valid} valid so far in batch")
		generated[batch_name] = rows
		(RUNS / f"e2e_pairs_{batch_name}.json").write_text(
			json.dumps(rows, indent=2), encoding="utf-8"
		)

	state["generated"] = {name: len(rows) for name, rows in generated.items()}
	save_state(state)
	return state


def _run_batch(batch_name: str, repeats: int) -> str:
	settings = get_settings().model_copy(update={"repeats_per_question": repeats})
	rows = json.loads((RUNS / f"e2e_pairs_{batch_name}.json").read_text(encoding="utf-8"))

	questions_by_persona: dict[str, list[GeneratedQuestion]] = {}
	personas = {}
	references: dict[str, QuestionReference] = {}
	all_references: list[QuestionReference] = []

	for row in rows:
		reference = QuestionReference.model_validate(row["reference"])
		all_references.append(reference)
		if reference.validation_status != "valid":
			continue
		persona = load_named_persona(row["persona"])
		personas[persona.id] = persona
		questions_by_persona.setdefault(persona.id, []).append(
			GeneratedQuestion.model_validate(row["question"])
		)
		references[reference.question_id] = reference

	total = sum(len(v) for v in questions_by_persona.values())
	log(f"running batch {batch_name}: {total} valid question(s) x {repeats} repeat(s)")

	record = run_grid(
		list(personas.values()),
		questions_by_persona,
		load_named_product("insight_profile"),
		InsightAdapter(llm=LLMClient()),
		LLMClient(),
		settings,
		references_by_question=references,
	)
	for questions in questions_by_persona.values():
		for question in questions:
			save_question(question, record.id)
	for reference in all_references:
		save_reference(reference, record.id)
	log(f"  batch {batch_name} -> {record.id} ({len(record.conversation_ids)} conversations)")
	return record.id


def phase_run(state: dict) -> dict:
	state.setdefault("runs", {})
	for batch_name in ("mixed", "none"):
		state["runs"][batch_name] = _run_batch(batch_name, repeats=2)
		save_state(state)
	return state


def phase_evaluate(state: dict) -> dict:
	llm = LLMClient()
	glossary = load_named_glossary("insight_glossary")
	for batch_name, run_id in state["runs"].items():
		references = {r.question_id: r for r in get_references(run_id)}
		conversations = get_conversations(run_id)
		log(f"judging {batch_name} ({len(conversations)} conversations)")
		for index, conversation in enumerate(conversations, start=1):
			reference = references.get(conversation.question_id)
			if reference is None or reference.validation_status != "valid":
				continue
			result = judge_conversation(conversation, reference, llm, glossary=glossary)
			save_judge_result(result, run_id)
			if index % 10 == 0:
				log(f"  {index}/{len(conversations)}")
	save_state(state)
	return state


def phase_metamorphic(state: dict) -> dict:
	llm = LLMClient()
	schema = load_named_schema("insight_schema")
	adapter = InsightAdapter(llm=LLMClient())
	for batch_name, run_id in state["runs"].items():
		references = {
			r.question_id: r for r in get_references(run_id) if r.validation_status == "valid"
		}
		questions = {q.id: q for q in get_questions(run_id)}
		seen: set[str] = set()
		results = []
		for conversation in get_conversations(run_id):
			question_id = conversation.question_id
			if question_id in seen or question_id not in references:
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
			results.extend(
				check_question(
					persona=load_named_persona(conversation.persona_id),
					question_id=question_id,
					question_text=question.question if question else conversation.turns[0].message,
					original_sql=original_sql,
					adapter=adapter,
					llm=llm,
					schema=schema,
					seed=conversation.seed,
					dialect=get_settings().sql_dialect,
				)
			)
			log(f"  probed {len(seen)} question(s) in {batch_name}")

		payload = {
			"run_id": run_id,
			"rates": consistency_rates(results, references),
			"results": [vars(result) for result in results],
		}
		(RUNS / f"metamorphic_{run_id}.json").write_text(
			json.dumps(payload, indent=2), encoding="utf-8"
		)
		log(f"  metamorphic {batch_name} -> runs/metamorphic_{run_id}.json")
	save_state(state)
	return state


def phase_calibration(state: dict) -> dict:
	for batch_name, run_id in state["runs"].items():
		if not get_judge_results(run_id):
			log(f"  no judge results for {batch_name}; skipping calibration export")
			continue
		path = export_calibration(run_id, n=30)
		log(f"  calibration sheet for {batch_name}: {path}")
	save_state(state)
	return state


def main() -> None:
	init_db(get_settings().database_url)
	start = sys.argv[1] if len(sys.argv) > 1 else PHASES[0]
	if start not in PHASES:
		raise SystemExit(f"Unknown phase {start!r}. Choose from {PHASES}.")

	state = load_state()
	runners = {
		"generate": phase_generate,
		"run": phase_run,
		"evaluate": phase_evaluate,
		"metamorphic": phase_metamorphic,
		"calibration": phase_calibration,
	}
	for phase in PHASES[PHASES.index(start) :]:
		log(f"=== phase: {phase} ===")
		state = runners[phase](state)
	log("=== done ===")
	log(json.dumps(state, indent=2))


main()
