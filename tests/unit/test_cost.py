"""Counting what a run spends.

Tokens are measured; money is inferred from configured rates. Both have to be
right, because every decision about how often to run this thing rests on them.
"""

from __future__ import annotations

from app.config import _parse_rates
from app.cost import RunCost, usage_from_event

RATES = {"gemini-3.7-flash": (0.30, 2.50), "gemini-pro-latest": (1.25, 10.00)}


class FakeUsage:
    def __init__(self, prompt=0, candidates=0, thoughts=0):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts


class FakeEvent:
    def __init__(self, usage=None, model=None):
        self.usage_metadata = usage
        self.model = model


def test_tokens_accumulate_per_model():
    cost = RunCost()
    cost.record_tokens("gemini-3.7-flash", 1000, 200)
    cost.record_tokens("gemini-3.7-flash", 500, 100)
    cost.record_tokens("gemini-pro-latest", 2000, 800)

    assert cost.total_calls == 3
    assert cost.total_input == 3500
    assert cost.total_output == 1100
    assert cost.by_model["gemini-3.7-flash"].calls == 2


def test_money_follows_the_configured_rates():
    cost = RunCost()
    cost.record_tokens("gemini-pro-latest", 1_000_000, 1_000_000)
    # 1M in at $1.25 + 1M out at $10.00
    assert cost.estimate_usd(RATES, image_usd=0.0) == 11.25


def test_images_are_priced_too():
    """They do not pass through the runner, so they are easy to forget."""
    cost = RunCost()
    cost.record_image()
    cost.record_image()
    assert cost.estimate_usd(RATES, image_usd=0.04) == 0.08


def test_a_model_with_no_configured_rate_costs_zero_rather_than_a_guess():
    cost = RunCost()
    cost.record_tokens("some-unpriced-model", 1_000_000, 1_000_000)
    assert cost.estimate_usd(RATES, image_usd=0.0) == 0.0


def test_the_longest_matching_prefix_wins():
    """A family rate covers its variants; a specific one overrides it."""
    rates = {"gemini-3": (1.0, 1.0), "gemini-3.7-flash": (0.1, 0.1)}
    cost = RunCost()
    cost.record_tokens("gemini-3.7-flash-preview", 1_000_000, 0)
    assert cost.estimate_usd(rates, image_usd=0.0) == 0.1


def test_reasoning_tokens_are_billed_as_output():
    """On a thinking model these are most of the bill; omitting them lies."""
    event = FakeEvent(FakeUsage(prompt=100, candidates=50, thoughts=900), "m")
    assert usage_from_event(event) == ("m", 100, 950)


def test_the_agent_name_is_resolved_to_the_model_that_ran():
    """ADK names the agent, not the model. Without the map, runs report free."""
    event = FakeEvent(FakeUsage(prompt=10, candidates=5))
    event.model = None
    event.author = "writer"
    assert usage_from_event(event, {"writer": "gemini-pro-latest"}) == (
        "gemini-pro-latest",
        10,
        5,
    )


def test_an_unmapped_agent_falls_back_to_its_own_name():
    event = FakeEvent(FakeUsage(prompt=10, candidates=5))
    event.model = None
    event.author = "mystery_agent"
    assert usage_from_event(event, {})[0] == "mystery_agent"


def test_every_agent_in_the_graph_has_a_priced_model():
    """A missing entry shows up as a suspiciously free run, not as an error."""
    from app.agent import agent_models
    from app.config import settings

    known = {settings.models.worker, settings.models.author}
    mapping = agent_models()
    assert set(mapping.values()) <= known
    for name in ("writer", "judge", "researcher", "fact_builder", "topic_planner"):
        assert name in mapping


def test_an_event_with_no_usage_is_skipped_not_counted_as_zero():
    assert usage_from_event(FakeEvent(None)) is None
    assert usage_from_event(FakeEvent(FakeUsage())) is None


def test_rates_parse_from_the_environment_format():
    rates = _parse_rates("a:1:2,b:0.5:4.0")
    assert rates == {"a": (1.0, 2.0), "b": (0.5, 4.0)}


def test_a_malformed_rate_entry_is_dropped_not_fatal():
    assert _parse_rates("good:1:2,broken,also:bad:x") == {"good": (1.0, 2.0)}


# ---------------------------------------------- what a picture actually cost


def test_a_reported_image_price_beats_the_estimate():
    """A picture is a flat charge no token count predicts.

    So the configured price is a guess, and a guess is what made a run report
    a number the invoice disagreed with. Where the provider says what it
    charged, that is what the run costs.
    """
    cost = RunCost()
    cost.record_image(0.04)
    cost.record_image(0.04)

    assert cost.estimate_usd({}, image_usd=0.10) == 0.08


def test_a_provider_that_says_nothing_is_still_estimated():
    cost = RunCost()
    cost.record_image()

    assert cost.estimate_usd({}, image_usd=0.10) == 0.10


def test_the_two_kinds_of_picture_are_counted_apart():
    """One provider reporting and another silent must not double-count."""
    cost = RunCost()
    cost.record_image(0.04)
    cost.record_image()

    assert cost.images == 2
    assert cost.estimate_usd({}, image_usd=0.10) == 0.14


def test_a_free_picture_is_not_the_same_as_an_unpriced_one():
    cost = RunCost()
    cost.record_image(0.0)

    assert cost.estimate_usd({}, image_usd=0.10) == 0.0
