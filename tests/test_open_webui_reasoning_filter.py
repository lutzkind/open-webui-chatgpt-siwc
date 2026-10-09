from pathlib import Path
from runpy import run_path

import pytest

FUNCTION_PATH = (
    Path(__file__).parents[1]
    / "integrations"
    / "open-webui"
    / "functions"
    / "siwc_think.py"
)
FUNCTION_NAMESPACE = run_path(str(FUNCTION_PATH))
Filter = FUNCTION_NAMESPACE["Filter"]
MODEL_EFFORTS = FUNCTION_NAMESPACE["MODEL_EFFORTS"]


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        (model, effort)
        for model, supported in MODEL_EFFORTS.items()
        for effort in sorted(supported)
    ],
)
def test_supported_effort_is_forwarded_unchanged(model: str, effort: str) -> None:
    body = {"model": model, "messages": [{"role": "user", "content": "Hi"}]}
    original_messages = body["messages"]

    result = Filter().inlet(body, {"valves": {"reasoning_effort": effort}})

    assert result is body
    assert body["reasoning_effort"] == effort
    assert body["messages"] is original_messages


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol"])
def test_unsupported_none_is_rejected(model: str) -> None:
    with pytest.raises(ValueError, match="supports these Think efforts"):
        Filter().inlet({"model": model}, {"valves": {"reasoning_effort": "none"}})


def test_unknown_model_is_left_unchanged() -> None:
    body = {"model": "unlisted-model", "messages": []}

    result = Filter().inlet(body, {"valves": {"reasoning_effort": "high"}})

    assert result is body
    assert "reasoning_effort" not in body


def test_function_is_toggleable_and_selector_contains_supported_union() -> None:
    function = Filter()
    schema = Filter.UserValves.model_json_schema()

    assert function.toggle is True
    assert set(schema["properties"]["reasoning_effort"]["enum"]) == {
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }
