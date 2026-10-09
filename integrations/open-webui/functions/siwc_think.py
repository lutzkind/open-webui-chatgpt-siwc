"""
title: Think
author: Open WebUI local
version: 2.0.0
description: Toggle Responses reasoning effort; choose effort in this user's Function settings.
"""

from typing import Literal

from pydantic import BaseModel, Field


EFFORT_OPTIONS = ["none", "low", "medium", "high", "xhigh", "max"]

MODEL_EFFORTS = {
    "gpt-5.6-luna": frozenset(EFFORT_OPTIONS),
    "gpt-5.6-terra": frozenset(EFFORT_OPTIONS),
    "gpt-5.6-sol": frozenset(EFFORT_OPTIONS),
    "gpt-6-sol": frozenset(EFFORT_OPTIONS),
    "gpt-6-luna": frozenset(EFFORT_OPTIONS),
    "gpt-6-astra": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "gpt-6.1-sol": frozenset({"low", "medium", "high", "xhigh", "max"}),
}


class Filter:
    """Toggleable per-request reasoning control for attached SIWC models."""

    class UserValves(BaseModel):
        reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = Field(
            default="high",
            description="Effort used while Think is enabled. Auto is the Think toggle off.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": EFFORT_OPTIONS,
                }
            },
        )

    def __init__(self):
        self.toggle = True

    def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        model = body.get("model")
        supported = MODEL_EFFORTS.get(model)
        if supported is None:
            # The toggle is globally registered in this Open WebUI instance,
            # whose current catalog is SIWC-only. Stay inert if another model
            # source is added later.
            return body

        user = __user__ or {}
        valves = user.get("valves") or {}
        if isinstance(valves, dict):
            effort = valves.get("reasoning_effort", "high")
        else:
            effort = getattr(valves, "reasoning_effort", "high")

        if effort not in supported:
            choices = ", ".join(sorted(supported))
            raise ValueError(f"{model} supports these Think efforts: {choices}.")

        body["reasoning_effort"] = effort
        return body
