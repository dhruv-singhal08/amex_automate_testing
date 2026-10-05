# Prompt library

Every LLM prompt in the platform lives here, so they can be reviewed together. Nothing else in the codebase contains prompt text.

All prompts are loaded through `synthetic_user.core.prompts.load_prompt(name)`. Placeholders use the `<<MARKER>>` convention and are substituted by the calling module.

## Active prompts

| File | Loaded by | Role | Placeholders |
| --- | --- | --- | --- |
| `generate_v2.md` | `generation/generator.py` | Writes the questions a persona would genuinely ask | `<<PERSONA_YAML>>`, `<<PRODUCT_PROFILE_YAML>>`, `<<PAST_QUERIES>>`, `<<QUESTION_COUNT>>` |
| `turn_decision_v2.md` | `simulation/persona_agent.py` | The persona's in-character decision each turn | `<<PERSONA_YAML>>`, `<<PRODUCT_PROFILE_YAML>>`, `<<AMBIGUITY_RESPONSE>>`, `<<ORIGINAL_INTENT>>`, `<<FOLLOW_UPS_USED>>`, `<<FOLLOW_UPS_REMAINING>>`, `<<TRANSCRIPT>>` |
| `closing_summary_v2.md` | `simulation/persona_agent.py` | How the persona describes the experience afterwards | `<<PERSONA_YAML>>`, `<<HOW_IT_ENDED>>`, `<<TRANSCRIPT>>` |
| `classify_v2.md` | `validation/validator.py` | Classifies one finished conversation | `<<PRODUCT_PROFILE_YAML>>`, `<<CAPABILITY_SPEC_YAML>>`, `<<ORIGINAL_INTENT>>`, `<<DETERMINISTIC_FLAGS>>`, `<<TRANSCRIPT>>` |
| `nl_to_sql_v1.md` | `mock_product/engine.py` | The mock product's own natural-language-to-SQL prompt | `<<SCHEMA>>`, `<<TRANSCRIPT>>`, `<<MESSAGE>>` |

`nl_to_sql_v1.md` belongs to the product under test, not to the harness. It is deliberately held steady while harness prompts change, so runs stay comparable.

## Superseded

`generate_v1.md`, `turn_decision_v1.md`, `closing_summary_v1.md`, `classify_v1.md` are kept because `RunRecord.prompt_versions` pins the prompt version used by each recorded run. Do not edit a version that has already been used in a run — add a new one and repoint the loader.

## Conventions

- **Personas reach prompts as a character sheet, not a record.** `<<PERSONA_YAML>>` is filled from `Persona.character_sheet()`, which drops harness bookkeeping (`id`, `version`, `tail_flag`, `provenance`, `approval_state`) and omits empty optional fields. Prompts should never have to learn which persona fields to ignore.
- **Each persona field has one job.** `typical_tasks` and `functional_role` decide *what* is asked; `example_utterances` and `vocabulary_style` decide *how* it is written; `forbidden_assumptions` decide what is deliberately left *out*. `generate_v2.md` and `turn_decision_v2.md` both state this mapping explicitly rather than dumping the persona and hoping.
- **The capability spec never reaches a persona prompt.** Only `classify_v2.md` receives it. A persona who knew the product's technical limits would not ask the questions that matter most.
- **Transcripts are untrusted data.** Every prompt that receives one says so explicitly, and delimits it with `BEGIN TRANSCRIPT` / `END TRANSCRIPT`.
- **Output contracts are strict.** Prompts returning JSON say "no markdown fences, no commentary"; callers still strip fences and retry once before falling back.
- **The persona is a regular user, not a stranger and not an expert.** They know the domain because it is their job; they do not know the product's internals or where its limits sit.
