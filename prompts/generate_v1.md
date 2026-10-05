Generate <<QUESTION_COUNT>> distinct questions that this person would genuinely ask this product, as an actual user of it.

This person is a real, regular user of this product as part of their job — not a first-time stranger and not a product expert. They have working familiarity with the domain described in the public product profile below, because they deal with it day to day at work. They do NOT know the product's internal technical implementation or the exact boundaries of what it can and cannot compute — that uncertainty is real, and it should show up in what they ask.

Write in this person's own vocabulary and typical sentence length.
Underspecify wherever this person would underspecify: leave out date ranges, currencies, and filters if they would leave them out.
Do NOT rewrite a question into the clearest possible form. Real users ask badly, and that is the point.
Because their job relates to this domain, questions should sound like they come from someone who belongs here — not a random outsider guessing blindly, and not someone who has read the product's documentation.
If their job would lead them to ask for something the product may not actually support, write that question anyway — they would have no way of knowing it's unsupported until they try.
Spread the questions across simple, medium and hard.
Never mention tables, columns, SQL, or any internal implementation detail — this person has never seen those and would not think in those terms.
Ground the questions in the past queries if provided.

PERSONA:
<<PERSONA_YAML>>

PUBLIC PRODUCT PROFILE:
<<PRODUCT_PROFILE_YAML>>

PAST QUERIES:
<<PAST_QUERIES>>

Return a strict JSON array. Each item must match this shape:
{
  "id": "question-1",
  "question": "...",
  "intent": "...",
  "complexity": "simple|medium|hard",
  "persona_trait": "...",
  "expected_response_type": "data|explanation|either"
}
