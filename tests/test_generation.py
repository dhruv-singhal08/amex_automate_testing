import pytest

from synthetic_user.assets.persona import load_persona
from synthetic_user.assets.registry import load_named_capability, load_named_persona, load_named_product
from synthetic_user.assets.product import load_capability_spec, load_product_profile
from synthetic_user.core.llm import StubLLMClient
from synthetic_user.core.models import CapabilitySpec, Persona, ProductProfile
from synthetic_user.generation.generator import generate_questions


class RecordingStubLLMClient(StubLLMClient):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str, role: str, seed: int | None = None, **kwargs) -> str:
        self.prompts.append(prompt)
        return super().complete(prompt, role, seed, **kwargs)


def test_assets_load_and_generation_excludes_capability_spec() -> None:
    persona = load_named_persona("junior_analyst")
    product = load_named_product("insight_profile")
    capability = load_named_capability("insight_capability")
    llm = RecordingStubLLMClient(
        [
            """[
              {"id": "q1", "question": "How much did we bill?", "intent": "total billing", "complexity": "simple", "persona_trait": "casual wording", "expected_response_type": "data"},
              {"id": "q2", "question": "Can you show what changed?", "intent": "explain change", "complexity": "medium", "persona_trait": "low verification literacy", "expected_response_type": "explanation"},
              {"id": "q3", "question": "What did we spend in each currency?", "intent": "currency totals", "complexity": "hard", "persona_trait": "naive product knowledge", "expected_response_type": "data"},
              {"id": "q4", "question": "What was biggest last month?", "intent": "largest billing", "complexity": "medium", "persona_trait": "underspecification", "expected_response_type": "either"},
              {"id": "q5", "question": "Can I compare those accounts?", "intent": "compare accounts", "complexity": "hard", "persona_trait": "follow-up", "expected_response_type": "either"},
              {"id": "q6", "question": "Why is this number different?", "intent": "explain discrepancy", "complexity": "simple", "persona_trait": "verification literacy", "expected_response_type": "explanation"}
            ]"""
        ]
    )

    questions = generate_questions(
        persona,
        product,
        "How much did we bill last time?",
        6,
        llm,
        seed=21,
    )
    rendered_prompt = llm.prompts[0]

    assert len(questions) == 6
    assert {question.complexity for question in questions} == {"simple", "medium", "hard"}
    assert "Single-table aggregation" not in rendered_prompt
    assert "Three-or-more-table joins" not in rendered_prompt
    assert "Currency conversion" not in rendered_prompt
    assert "known_gaps" not in rendered_prompt
    assert "How much did we bill last time?" in rendered_prompt


def test_generation_retries_then_returns_empty_list() -> None:
    persona = load_named_persona("junior_analyst")
    product = load_named_product("insight_profile")
    llm = RecordingStubLLMClient(["not json", "still not json"])

    assert generate_questions(persona, product, None, 2, llm, seed=4) == []
    assert len(llm.prompts) == 2
    assert "STRICT REMINDER" in llm.prompts[1]


def test_loaders_validate_and_reject_unapproved_persona(tmp_path) -> None:
    unapproved = tmp_path / "draft.yaml"
    unapproved.write_text(
        """id: draft\nversion: 1\nname: Draft\nfunctional_role: Analyst\ndomain_expertise: novice\ntechnical_proficiency: low\nvocabulary_style: Everyday words in short sentences\nprimary_motivation: Finish the task\nsuccess_criteria: Get an answer\npatience: 1\nambiguity_response: stop\nforbidden_assumptions:\n  - Never fill in missing details\nverification_literacy: low\napproval_state: draft\n""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not approved"):
        load_persona(unapproved)

    assert isinstance(load_named_persona("finance_controller"), Persona)
    assert isinstance(load_named_product("insight_profile"), ProductProfile)
    assert isinstance(load_named_capability("insight_capability"), CapabilitySpec)
    assert load_product_profile("assets/products/insight_profile.yaml").product_name == "Insight Billing Assistant"
    assert load_capability_spec("assets/products/insight_capability.yaml").spec_version == 1
