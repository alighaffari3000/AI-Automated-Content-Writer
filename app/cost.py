"""What a run costs.

Without this, every decision about the pipeline — run daily or weekly, three
reviewers or five, a stronger writer model — is made blind. Token counts come
from the model responses themselves; the money is an estimate from rates you
configure, because published prices change and a number hard-coded today is a
lie next quarter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ModelUse:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunCost:
    """Everything one run consumed, per model."""

    by_model: dict[str, ModelUse] = field(default_factory=dict)
    images: int = 0
    # Only the pictures whose provider told us what they cost. The rest are
    # priced from configuration, so the two are counted apart.
    image_usd_reported: float = 0.0
    images_reported: int = 0

    def record_tokens(self, model: str, input_tokens: int, output_tokens: int) -> None:
        use = self.by_model.setdefault(model or "unknown", ModelUse())
        use.calls += 1
        use.input_tokens += input_tokens
        use.output_tokens += output_tokens

    def record_image(self, cost_usd: float | None = None) -> None:
        self.images += 1
        if cost_usd is not None:
            self.images_reported += 1
            self.image_usd_reported += cost_usd

    @property
    def total_input(self) -> int:
        return sum(u.input_tokens for u in self.by_model.values())

    @property
    def total_output(self) -> int:
        return sum(u.output_tokens for u in self.by_model.values())

    @property
    def total_calls(self) -> int:
        return sum(u.calls for u in self.by_model.values())

    def estimate_usd(self, rates: dict[str, tuple[float, float]], image_usd: float) -> float:
        """Dollars, per the rates given. Unknown models are counted at zero.

        A model missing from the rate table costs nothing here rather than
        guessing — an unpriced model should show up as a suspiciously cheap
        run, not as a confidently wrong number.
        """
        # A picture is a flat charge no token count predicts, so a provider
        # that reports what it charged is believed, and only the silent ones
        # are estimated.
        total = self.image_usd_reported + (
            (self.images - self.images_reported) * image_usd
        )
        for name, use in self.by_model.items():
            rate_in, rate_out = _match_rate(name, rates)
            total += (use.input_tokens / 1_000_000) * rate_in
            total += (use.output_tokens / 1_000_000) * rate_out
        return round(total, 4)

    def summary(self, rates: dict[str, tuple[float, float]], image_usd: float) -> str:
        lines = [
            f"{self.total_calls} model call(s), "
            f"{self.total_input:,} in / {self.total_output:,} out tokens"
            + (f", {self.images} image(s)" if self.images else "")
        ]
        for name, use in sorted(self.by_model.items()):
            lines.append(
                f"  {name}: {use.calls} call(s), "
                f"{use.input_tokens:,} in / {use.output_tokens:,} out"
            )
        lines.append(f"  estimated cost: ${self.estimate_usd(rates, image_usd):.4f}")
        return "\n".join(lines)

    def to_dict(self, rates: dict[str, tuple[float, float]], image_usd: float) -> dict:
        return {
            "calls": self.total_calls,
            "input_tokens": self.total_input,
            "output_tokens": self.total_output,
            "images": self.images,
            "estimated_usd": self.estimate_usd(rates, image_usd),
            "by_model": {
                name: {
                    "calls": u.calls,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                }
                for name, u in self.by_model.items()
            },
        }


def _match_rate(
    model: str, rates: dict[str, tuple[float, float]]
) -> tuple[float, float]:
    """Longest configured prefix wins, so a family rate covers its variants."""
    best: tuple[float, float] = (0.0, 0.0)
    best_len = -1
    for prefix, rate in rates.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = rate, len(prefix)
    return best


def usage_from_event(
    event, agent_models: dict[str, str] | None = None
) -> tuple[str, int, int] | None:
    """Pull (model, input, output) out of one runner event, if it has any.

    Different ADK versions expose usage in slightly different places, so this
    reads defensively: a run must never fail because accounting could not find
    a field.
    """
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        response = getattr(event, "llm_response", None)
        usage = getattr(response, "usage_metadata", None) if response else None
    if usage is None:
        return None

    input_tokens = getattr(usage, "prompt_token_count", None) or 0
    output_tokens = (
        getattr(usage, "candidates_token_count", None)
        or getattr(usage, "response_token_count", None)
        or 0
    )
    thoughts = getattr(usage, "thoughts_token_count", None) or 0
    if not input_tokens and not output_tokens and not thoughts:
        return None

    # ADK events name the agent, not the model. Without the mapping, every
    # call is attributed to an agent name that matches no rate, and the run
    # reports as free.
    author = getattr(event, "author", None)
    model = (
        getattr(event, "model", None)
        or getattr(usage, "model", None)
        or (agent_models or {}).get(str(author))
        or author
        or "unknown"
    )
    # Reasoning tokens are billed as output, and on a thinking model they are
    # most of it — leaving them out understates the bill by a wide margin.
    return str(model), int(input_tokens), int(output_tokens + thoughts)
