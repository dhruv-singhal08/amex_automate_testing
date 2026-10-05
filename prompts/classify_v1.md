Classify this simulation outcome, not the general quality of the product.
Use exactly one classification:
- capability_failure: the capability spec says this is supported, but the product got it wrong.
- not_supported_yet: the request is genuinely outside the current product scope.
- undocumented_gap: the public product profile implies it works, but the capability spec says it does not.
- too_generic: no specific answer was possible.
- off_domain: the request has nothing to do with this product.
- handled_correctly: the capability is supported and the product answered appropriately.

Also return:
- refused_clearly: true, false, or null. If unsupported, did the product say so rather than invent an answer?
- cleanup_suspected: true if the user's follow-up became suddenly precise after a vague reply.
- reason: one concise explanation.

PUBLIC PRODUCT PROFILE:
<<PRODUCT_PROFILE_YAML>>

INTERNAL CAPABILITY SPECIFICATION:
<<CAPABILITY_SPEC_YAML>>

ORIGINAL INTENT:
<<ORIGINAL_INTENT>>

DETERMINISTIC FLAGS:
<<DETERMINISTIC_FLAGS>>

TRANSCRIPT:
<<TRANSCRIPT>>

Return only strict JSON with exactly these fields:
{"classification": "one allowed value", "refused_clearly": true, "cleanup_suspected": false, "reason": "..."}