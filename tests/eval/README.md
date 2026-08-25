# Evaluation

Two different questions live in this directory, and confusing them wastes both
money and trust.

## Does the review board still catch things?

```bash
uv run python -m app.cli eval              # one pass, for a quick look
uv run python -m app.cli eval --repeat 5   # a number worth comparing
```

This is the day-to-day instrument. `scenarios.json` holds drafts with defects
planted in them — a phantom citation, an invented figure, a product spec that
contradicts the catalogue, keyword stuffing, a claim whose passage was never
found at its source, a comparison with a named competitor — plus a clean
control that must not be failed, and one draft whose only flaw is measurable,
which the gate is expected to catch and the reviewers are expected to leave
alone.

It runs the real reviewer agents against fixed inputs, so it is cheap
(no research, no writing, no publishing) and repeatable. Three numbers come out:

| number | what it means |
|---|---|
| defects caught | how many planted defects were found at the severity they deserve |
| controls clean | how often a sound draft was called unpublishable |
| separation | what a clean draft scores over a defective one |

**Separation is the one to watch.** The original failure was reviewers scoring
10/10/9.5 and finding nothing. A board that scores everything alike can show a
respectable detection rate and still not be reviewing — it is applauding.

Model output varies, so a single pass is a sample. Use `--repeat` before
drawing a conclusion. Every run is saved under `data/eval/` and the next run
prints what moved, which is what makes this a regression test for prompt
changes rather than something to look at once.

Run it after touching a review prompt, a model, or the gate.

## Is the whole pipeline any good?

```bash
DRY_RUN=true agents-cli eval run
```

`datasets/basic-dataset.json` drives the real workflow end to end: it picks a
category, invents a subject, researches it, writes, reviews and finalises.

Two warnings. It costs a real run — roughly seven model calls and about $0.22
at the time of writing (`python -m app.cli cost` reports what yours actually
cost). And without `DRY_RUN=true` it will send a draft to the configured site
and consume a category's turn in the rotation.

Use it as a smoke test that the graph still runs end to end, not as a quality
measurement: no fixed answer key can settle whether an article is good, which
is exactly why the review board is measured the other way.
