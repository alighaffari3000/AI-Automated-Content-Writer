# Roadmap

The architecture was designed in full before any code was written, then
deliberately cut down to a first version small enough to actually finish. This
document is the other half of that decision: what was left out, why, and what
brings it back.

The order matters. Each phase earns the next one — phase 2's stricter review is
only worth its cost once phase 1's simpler review has shown where it misses,
and phase 3's learning loop is meaningless until there is readership data to
learn from.

---

## Phase 1 — a working pipeline, with a person at the end ✅

**Shipped.** Everything in `README.md` describes this phase.

Eight components: a topic planner, one researcher with search, a writer, three
reviewers in parallel, a judge that runs only on the revise path, and a
deterministic gate. A per-run fact registry, no reuse. Three tables. Every
article ends as a draft for a human to approve.

What a real run looks like: a queued topic produces about a dozen sourced
facts, a 900-word article, three independent reviews, and a gate verdict —
usually in one round, in roughly two minutes.

### What the first live runs taught us

These are not open bugs; they are the findings that shaped the phases below.

**Reviewers grade generously unless told not to.** The first runs scored
10/10/9.5 with zero issues raised — a gate that never fires is not a gate. An
explicit calibration rule in the prompts ("7 or 8 is the normal score for a
publishable draft") brought the average to 8.2 and started producing real
findings. This is a prompt-level patch on a measurement problem, which is
exactly what phase 2's evaluation work exists to solve properly.

**Source URLs do not survive.** The researcher grounds its claims in web
search, but the URLs live in the model's grounding metadata, not in its text —
so `source_url` comes back empty even when the claim is well-sourced. Fixing
this properly means harvesting grounding metadata in a callback. It is the
first prerequisite for phase 2's source validator, which cannot check a source
it cannot see.

**Two ADK constraints shape the graph.** `output_schema` disables tool calling,
and `google_search` cannot be combined with function tools. So research is
split into a search agent and a schema-only fact builder, and reviewers receive
site data through state rather than as tools. The second turned out better than
the original design: a reviewer handed the real catalogue cannot hallucinate
it, and the fetch is deterministic and auditable.

---

## Phase 2 — review you can audit

Phase 1 proves the loop runs. Phase 2 makes its verdicts defensible.

**Source validation, as its own step.** Today the researcher records a source
and a reviewer takes its word for it. A dedicated validator would check that
the URL resolves, that the passage quoted actually appears there, that the
source is primary rather than a reseller repeating a manufacturer, and that it
is recent enough to still be true. Sources rank: manufacturer documentation,
then standards bodies, then technical organisations, then publications, then
industry sites, then the general web. A source that cannot be reached lowers a
claim's confidence rather than failing the run.

**Five reviewers instead of three.** The merged reviewers are a compromise: the
technical reviewer currently also validates sources, and the editorial reviewer
also handles SEO. Splitting them gives each a single question to answer, which
measurably improves what a reviewer catches. The cost is more model calls per
article per day, which is why it waits for evidence that the merged pair is
actually missing things.

**A persistent fact registry, with expiry.** Facts currently live for one run
and are archived as JSON on the article row. Making them persistent means a
claim verified once can be reused — cheaper every day, and consistent between
articles, so two articles never quote different numbers for the same product.
Each fact needs a shelf life: prices and stock expire in days, technical
specifications in months. The archived JSON from phase 1 seeds this.

**A real evaluation suite.** Not pytest — model output is not deterministic and
asserting on it produces flaky tests that teach nothing. An eval dataset with
LLM-judge scoring, run against fixed inputs, is what turns "the reviewers seem
generous" into a number that can be tracked across prompt changes.

**The measurable half of SEO moves into the gate.** Today every SEO
judgment is a model's score, but most on-page SEO is measurement, not opinion:
title and meta description length, exactly one H1 and an unbroken heading
hierarchy, the target keyword present in the title and the opening, alt text on
every image marker, a slug that does not collide with an article that already
exists, and internal links that resolve to real published slugs — a broken
internal link is the worst signal an article can send, and nothing checks for
one today. All of it belongs in `rules.py` beside the citation check, where the
same draft always earns the same verdict. The SEO reviewer keeps what is
genuinely judgment — readability, whether the article answers the search
intent — and gives up the measurements.

**Structured data, generated from the registry.** The pipeline emits no
JSON-LD at all. A code step at render time should produce `Article` always,
`FAQPage` when the article answers real searched questions (and the writer
should be asked to answer three or four of them), and `Product` when
`related_products` is filled. The fact registry is what makes this safe:
the schema is filled from registered, sourced values, so no model gets to
invent a specification inside a rich result.

**Internal links as strategy, not accident.** The `/articles` feed is fetched
and offered, but no structure sits behind it. Each category gains a pillar
article; the planner links every new article into its cluster and the pillar
back out. Two checks come with it: an orphan check (an article nothing links
to), and a cannibalisation check — the planner sees existing titles and
keywords before choosing, so two articles never compete for the same query.

**Keywords from the search, not the model's head.** The planner currently
invents its keywords. The researcher already runs real searches — one more
duty: record the questions people actually ask around the topic, so the writer
builds its headings from real queries rather than plausible ones. The other
half of this — knowing which queries the site already surfaces for — is Search
Console data, and waits for phase 3.

**A safety gate.** Phase 1 does not need one: every article reaches a human
anyway, so the human *is* the gate. It becomes mandatory the moment anything
publishes automatically. Some subjects must always reach a person regardless of
score — medical or safety claims, legal and regulatory statements, tariffs and
subsidies, financial promises, prices and warranties, unverified breaking news,
and any claim about a competitor.

---

## Phase 3 — learning from what people actually read

**A weekly analyst.** A separate scheduled run that reads view counts and
adjusts topic scores: subjects that get read rise in the queue, subjects that
do not sink. The site-side counter already records this from day one —
deliberately, because a view that went uncounted yesterday cannot be recovered
today, and an analyst with no history has nothing to learn from.

One boundary holds here: performance data may influence *what gets written* —
topic, angle, title, format. It must never influence what is *true*. A claim's
standard of evidence does not move because an article did well.

**Search data as a second source.** View counts say what people opened;
they say nothing about what people searched for and did not find. Search
Console data answers that, and slots into the analyst without changing the
architecture. Two of its readings are directly actionable: queries with
impressions but few clicks name a title and meta description worth rewriting,
and pages ranking fifth through fifteenth are the update candidates where one
good revision moves real traffic.

**A refresh pipeline.** The analyst today can only queue new topics, but much
of organic growth comes from updating articles that are slipping. The analyst
should be able to queue "update this article" as well as "write that one" —
and phase 2's persistent registry is exactly the infrastructure it needs: a
fact whose shelf life has expired is a signal that the article resting on it
is stale. The two features complete each other.

**Sources on the page.** The source audit already knows each source's
authority tier, but that knowledge stays internal. A sources section at the
end of each article — rendered in code from the registry, so no model can
invent an entry — is a trust signal for readers and search engines alike, and
gives every article outbound links to the authoritative sources it actually
rests on.

---

## Levels of autonomy

The pipeline is designed to earn trust rather than assume it.

| Level | Behaviour | What it takes to get there |
|---|---|---|
| **1 — Draft** (current) | Every article waits for human approval | Where it starts |
| **2 — Conditional** | High-confidence approvals publish themselves; everything else escalates | Weeks of level 1 with a high approval rate and no corrections |
| **3 — Automatic** | Publishes on approval; only safety-gate subjects escalate | A deliberate decision, after level 2 has run stably |

The mandatory escalation list applies at every level, including the last one.

---

## What this project deliberately will not become

A general-purpose content platform. There is no plan for a web UI, a
multi-tenant service, an editorial calendar, or a plugin system. It is a
pipeline: it takes a topic and produces an article a person can trust enough to
approve, for one site at a time, and each site it serves is a configuration
file.
