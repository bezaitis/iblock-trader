"""A probability you typed into config.yaml. The quickest way to get alerts on a new market.

params: p_yes (required), confidence (default low), note
"""

from . import ModelResult


def fair_prob(market, params, ctx) -> ModelResult:
    return ModelResult(
        p_yes=float(params["p_yes"]),
        confidence=params.get("confidence", "low"),
        data_ok=True,
        notes=params.get("note", "manual estimate"),
        inputs={"p_yes": params["p_yes"]},
    )
