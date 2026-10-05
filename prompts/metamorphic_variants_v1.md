# ROLE

You rewrite one question three ways, staying in the voice of the person who asked it. These are consistency probes: each rewrite should still be the same person asking, on the same day, about their work.

# THE THREE REWRITES

`paraphrase`
- The same request, worded differently. Same information need, same scope, same time period.
- Change the wording, not the meaning. A correct answer to the original must also be a correct answer to this.

`irrelevant_detail`
- The same request, plus something that does not change the answer: a throwaway aside, context about why they are asking, or a pleasantry.
- The added detail must NOT narrow or widen the request. "for the board deck" is fine; "for the board deck, just EMEA" is not - that changes the answer.

`time_shift`
- The same request over a DIFFERENT time period. If the original said last month, ask about last quarter, or a named month, or last year.
- Change ONLY the period. Everything else - the grouping, the filters, the thing being measured - stays exactly as it was.
- If the original names no period at all, introduce one.

# VOICE

Write all three as this person would type them. Match their casing, punctuation, sentence length and vocabulary. Someone reading the four questions together should not be able to tell which was the original.

Never mention tables, columns, SQL or any database identifier. This person has never seen those.

# INPUTS

PERSONA:
<<PERSONA_YAML>>

ORIGINAL QUESTION:
<<QUESTION>>

# OUTPUT

Return one strict JSON object and nothing else: no markdown fences, no commentary.

{
  "paraphrase": "...",
  "irrelevant_detail": "...",
  "time_shift": "..."
}
