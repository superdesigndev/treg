# Honesty and tone rules for comparison pages

Goal: every page is fully honest and never talks treg down. State the other product's real
strengths plainly; state treg's real strengths plainly; let the reader weigh them. A reader should
leave feeling treg is a strong, confident choice, without one unfair word about the other product.

## Facts
1. A fact about the other product comes from its own public pages, with the URL in `sources` and
   the read day in `checked`. No fact from memory, third-party blogs or guesses.
2. Where its pages say nothing, write "Not published", never "No" or "does not have".
3. Where its own pages disagree with each other, say so plainly and state both (for example a
   docs page and the terms giving two different fee rules), dated.
4. treg facts come only from `TREG_CELLS` and `TREG_SPEC`. Never invent a treg feature and never
   promise one. A feature that is coming stays out until it ships (`OPTIONAL_ROWS` hides a row
   until its flag turns on).
5. No guessed motives ("they do this because..."), no superlatives ("best", "leading", "much
   better"), no attacks.

## Never talk treg down
6. In prose (`sub`, `tldr`, `better`, `different`, `fit_*`, `both`, `description`) never describe
   treg by what it lacks. Banned about treg: "only", "just", "a few", "a short list", "none", "has
   no X", "does not offer", "lacks", "limited", "smaller", "today" (as in "treg takes cards today").
   - Bad: "Models on the same key. 110+ language models on one balance. treg's catalog has only a
     few model endpoints."
   - Good: "Language models on the same key. 110+ LLMs on the same balance as its data tools."
   If a contrast is needed, frame treg by its focus: "treg's catalog centres on data and
   generation tools".
7. A `better` item states the other product's strength and stops: no treg clause after it.
8. "No plan", "no seats", "no subscription", "no top-up fee" about treg are strengths, not lacks.
9. Table cells and FAQ answers may state a plain fact when a reader asks directly, then turn to
   what treg does do. Neutral, never apologetic.

## Structure and repetition
10. One differentiator in at most two places on a page (for example a payment protocol: the table
    row plus one `better` item).
11. `different` (Where treg is the better choice): 4-5 concrete items, strongest first.
12. Size comparisons only when treg is clearly bigger in the same unit (tools vs tools), never
    against "apps" or "integrations".
13. For treg say "tools" and "the catalog"; never "marketplace" or "vault" (AGENTS.md).

## FAQ
14. 6-7 questions: 3-4 about the other product that people search, 2 naming both, 1-2 about treg.
15. The first sentence answers the question ("Yes.", "No.", "It depends on ...", a number or
    "X pricing ..."), then a full sentence naming both products that still makes sense quoted
    alone. 2-4 sentences, about 60 words at most.
16. No `{ENDPOINTS}` / `{PROVIDERS}` and no catalog counts: the FAQPage structured data repeats
    the answers verbatim.
