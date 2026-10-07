"""Prices in rubles per 1M tokens and the worst case of one attempt.

Prices: Timeweb AI Gateway cabinet, «Модели», 07.10.2026 18:45 MSK. The page
does not say whether VAT is included. The gateway does not return prices in the
API, so they live here and must be updated by hand.
"""

from __future__ import annotations

PRICES_RUB_PER_1M: dict[str, tuple[float, float]] = {
    "timeweb/gpt-oss-120b": (20, 82),
    "yandex/yandexgpt-lite": (210, 210),
    "timeweb/gemma4:31b": (189, 540),
    "timeweb/glm-5.3": (267, 1120),
    "openai/gpt-5.6-terra": (270, 1620),
}
# Unknown model: assume the most expensive known price, so budgets stay on the safe side.
FALLBACK = max(PRICES_RUB_PER_1M.values(), key=lambda p: p[0] + p[1])

# Worst case of one model call in an attempt: the conversation grows to ~4k tokens.
WORST_CALL_IN = 4_000
WORST_FILTER_IN = 1_500


def call_cost_rub(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICES_RUB_PER_1M.get(model, FALLBACK)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def worst_attempt_rub(model: str, filter_model: str | None, model_calls: int, max_tokens: int,
                      filter_max_tokens: int) -> float:
    cost = model_calls * call_cost_rub(model, WORST_CALL_IN, max_tokens)
    if filter_model:
        cost += call_cost_rub(filter_model, WORST_FILTER_IN, filter_max_tokens)
    return cost
