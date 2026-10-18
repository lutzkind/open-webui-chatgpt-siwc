from __future__ import annotations

from app.continuation import (
    LaneState,
    OutputSignature,
    continuation_eligible,
    matches_output,
    plan_continuation,
    signature_from_output_item,
    strip_replayed_items,
)
from app.telemetry import fingerprint_request

GENERATION = 7
NOW = 1_000.0
IDLE_TTL = 900.0


def message(role: str, text: str) -> dict:
    item_type = "output_text" if role == "assistant" else "input_text"
    part = {"type": item_type, "text": text} if role == "assistant" else {"type": "input_text", "text": text}
    return {"type": "message", "role": role, "content": [part]}


def function_call(call_id: str, name: str = "lookup") -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def function_call_output(call_id: str, output: str) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def request_body(items: list[dict], model: str = "gpt-6.1-sol", tools: list | None = None) -> dict:
    body: dict = {"model": model, "input": items, "store": False}
    if tools is not None:
        body["tools"] = tools
    return body


def lane_for(
    body: dict,
    *,
    response_id: str | None = "resp-1",
    outputs: tuple[OutputSignature, ...] = (),
    generation: int = GENERATION,
    last_used: float = NOW,
    usable: bool = True,
    key: str = "lane-test",
) -> LaneState:
    fingerprint = fingerprint_request("r-test", body)
    return LaneState(
        key=key,
        generation=generation,
        response_id=response_id,
        model=fingerprint.model,
        instructions_sha=fingerprint.instructions_sha16,
        tools_sha=fingerprint.tools_sha16,
        item_hashes=fingerprint.item_hashes,
        outputs=outputs,
        usable=usable,
        last_used=last_used,
    )


def plan(body: dict, lane: LaneState | None, *, generation: int = GENERATION, now: float = NOW, instructions: bool = False):
    fingerprint = fingerprint_request("r-current", body)
    return plan_continuation(
        current_items=body["input"],
        item_hashes=fingerprint.item_hashes,
        model=fingerprint.model,
        instructions_sha=fingerprint.instructions_sha16,
        tools_sha=fingerprint.tools_sha16,
        lane=lane,
        generation=generation,
        now=now,
        idle_ttl=IDLE_TTL,
        instructions_present=instructions,
    )


def test_first_request_uses_full_context():
    body = request_body([message("user", "hello")])
    result = plan(body, None)
    assert result.mode == "full"
    assert result.reason == "no_lane"
    assert result.previous_response_id is None
    assert result.input_items == body["input"]
    assert result.sent_item_count == 1
    assert result.sent_bytes > 0


def test_strict_extension_continues_with_only_the_appended_item():
    first = request_body([message("user", "audit the deployment")])
    lane = lane_for(first)
    second = request_body([*first["input"], message("user", "and the logs")])
    result = plan(second, lane)
    assert result.mode == "continuation"
    assert result.previous_response_id == "resp-1"
    assert result.input_items == [message("user", "and the logs")]
    assert result.sent_item_count == 1
    assert result.full_item_count == 2
    assert result.stripped_items == 0


def test_replayed_assistant_output_is_stripped_from_the_delta():
    first = request_body([message("user", "status?")])
    answer = message("assistant", "all good")
    lane = lane_for(first, outputs=(signature_from_output_item(answer),))
    second = request_body([*first["input"], answer, message("user", "and now?")])
    result = plan(second, lane)
    assert result.mode == "continuation"
    assert result.input_items == [message("user", "and now?")]
    assert result.stripped_items == 1


def test_tool_continuation_sends_only_the_new_tool_output():
    tools = [{"type": "function", "name": "lookup"}]
    first = request_body([message("user", "check the service")], tools=tools)
    call = function_call("call-1")
    lane = lane_for(first, outputs=(signature_from_output_item(call),))
    tool_result = function_call_output("call-1", "x" * 400)
    second = request_body([*first["input"], call, tool_result], tools=tools)
    result = plan(second, lane)
    assert result.mode == "continuation"
    assert result.previous_response_id == "resp-1"
    assert result.input_items == [tool_result]
    assert result.sent_item_count == 1
    assert result.sent_tool_bytes >= 400
    assert result.stripped_items == 1


def test_parallel_tool_calls_are_transported_together():
    tools = [{"type": "function", "name": "lookup"}]
    first = request_body([message("user", "check both")], tools=tools)
    calls = (signature_from_output_item(function_call("call-a")), signature_from_output_item(function_call("call-b")))
    lane = lane_for(first, outputs=calls)
    delta = [
        function_call("call-a"),
        function_call("call-b"),
        function_call_output("call-a", "a"),
        function_call_output("call-b", "b"),
    ]
    result = plan(request_body([*first["input"], *delta], tools=tools), lane)
    assert result.mode == "continuation"
    assert result.input_items == [delta[2], delta[3]]
    assert result.stripped_items == 2


def test_edited_earlier_item_falls_back_to_full_context():
    first = request_body([message("user", "one"), message("user", "two")])
    lane = lane_for(first)
    edited = request_body([message("user", "one"), message("user", "TWO"), message("user", "three")])
    result = plan(edited, lane)
    assert result.mode == "full"
    assert result.reason == "prefix_mismatch"
    assert result.input_items == edited["input"]


def test_removed_earlier_item_falls_back_to_full_context():
    first = request_body([message("user", "one"), message("user", "two")])
    lane = lane_for(first)
    removed = request_body([message("user", "one"), message("user", "three")])
    result = plan(removed, lane)
    assert result.mode == "full"
    assert result.input_items == removed["input"]

    shorter = request_body([message("user", "one")])
    assert plan(shorter, lane).mode == "full"


def test_reordered_history_falls_back_to_full_context():
    first = request_body([message("user", "one"), message("assistant", "two")])
    lane = lane_for(first)
    reordered = request_body([message("assistant", "two"), message("user", "one"), message("user", "three")])
    result = plan(reordered, lane)
    assert result.mode == "full"
    assert result.reason == "prefix_mismatch"


def test_regenerated_turn_without_new_items_falls_back():
    first = request_body([message("user", "hello")])
    lane = lane_for(first)
    result = plan(request_body(list(first["input"])), lane)
    assert result.mode == "full"
    assert result.reason == "no_new_items"


def test_changed_developer_instructions_fall_back():
    first = {"model": "gpt-6.1-sol", "instructions": "You are helpful.", "input": [message("user", "hi")]}
    lane = lane_for(first)
    changed = {"model": "gpt-6.1-sol", "instructions": "You are terse.", "input": [message("user", "hi")]}
    result = plan(changed, lane)
    assert result.mode == "full"
    assert result.reason == "instructions_changed"


def test_request_level_instructions_always_use_full_context():
    first = {"model": "gpt-6.1-sol", "instructions": "You are helpful.", "input": [message("user", "hi")]}
    lane = lane_for(first)
    extended = {
        "model": "gpt-6.1-sol",
        "instructions": "You are helpful.",
        "input": [*first["input"], message("user", "more")],
    }
    result = plan(extended, lane, instructions=True)
    assert result.mode == "full"
    assert result.reason == "instructions_present"


def test_changed_tools_fall_back():
    first = request_body([message("user", "hi")], tools=[{"type": "function", "name": "a"}])
    lane = lane_for(first)
    changed = request_body([message("user", "hi"), message("user", "more")], tools=[{"type": "function", "name": "b"}])
    result = plan(changed, lane)
    assert result.mode == "full"
    assert result.reason == "tools_changed"


def test_added_tools_fall_back_and_removed_tools_fall_back():
    first = request_body([message("user", "hi")], tools=[{"type": "function", "name": "a"}])
    lane = lane_for(first)
    more = request_body([message("user", "hi"), message("user", "x")], tools=[{"type": "function", "name": "a"}, {"type": "function", "name": "b"}])
    assert plan(more, lane).reason == "tools_changed"
    fewer = request_body([message("user", "hi"), message("user", "x")], tools=[])
    assert plan(fewer, lane).reason == "tools_changed"


def test_changed_model_falls_back():
    first = request_body([message("user", "hi")], model="gpt-6.1-sol")
    lane = lane_for(first)
    other = request_body([message("user", "hi"), message("user", "x")], model="gpt-6.1-mini")
    result = plan(other, lane)
    assert result.mode == "full"
    assert result.reason == "model_changed"


def test_lost_connection_generation_falls_back():
    first = request_body([message("user", "hi")])
    lane = lane_for(first)
    extended = request_body([*first["input"], message("user", "again")])
    result = plan(extended, lane, generation=GENERATION + 1)
    assert result.mode == "full"
    assert result.reason == "connection_changed"


def test_missing_response_id_falls_back():
    first = request_body([message("user", "hi")])
    lane = lane_for(first, response_id=None)
    extended = request_body([*first["input"], message("user", "again")])
    result = plan(extended, lane)
    assert result.mode == "full"
    assert result.reason == "no_previous_response"


def test_unusable_lane_falls_back():
    first = request_body([message("user", "hi")])
    lane = lane_for(first, usable=False)
    extended = request_body([*first["input"], message("user", "again")])
    result = plan(extended, lane)
    assert result.mode == "full"
    assert result.reason == "lane_unusable"


def test_idle_lane_falls_back():
    first = request_body([message("user", "hi")])
    lane = lane_for(first, last_used=NOW - IDLE_TTL - 1)
    extended = request_body([*first["input"], message("user", "again")])
    result = plan(extended, lane)
    assert result.mode == "full"
    assert result.reason == "lane_idle"


def test_unrecognized_replay_falls_back_instead_of_guessing():
    first = request_body([message("user", "hi")])
    answer = message("assistant", "hello")
    lane = lane_for(first, outputs=(signature_from_output_item(answer),))
    # The caller replays an edited copy of the assistant answer: neither the
    # canonical projection nor the text digest matches, so the only matching
    # action is a full-context resend.
    rewritten = message("assistant", "HELLO")
    second = request_body([*first["input"], rewritten, message("user", "next")])
    result = plan(second, lane)
    assert result.mode == "continuation"
    # The unrecognized item is not silently dropped: it is sent upstream.
    assert result.input_items == [rewritten, message("user", "next")]


def test_item_replayed_out_of_order_is_treated_as_ambiguous():
    first = request_body([message("user", "hi")])
    one = function_call("call-1")
    two = function_call("call-2")
    lane = lane_for(first, outputs=(signature_from_output_item(two),))
    # call-1 is new, call-2 is a replay; the replay is not a contiguous prefix
    # of the delta, so the plan must fall back to full context.
    delta = [one, two]
    body = request_body([*first["input"], *delta])
    fingerprint = fingerprint_request("r", body)
    result = plan_continuation(
        current_items=body["input"],
        item_hashes=fingerprint.item_hashes,
        model=fingerprint.model,
        instructions_sha=fingerprint.instructions_sha16,
        tools_sha=fingerprint.tools_sha16,
        lane=lane,
        generation=GENERATION,
        now=NOW,
        idle_ttl=IDLE_TTL,
        instructions_present=False,
    )
    assert result.mode == "full"
    assert result.reason == "ambiguous_replay"


def test_output_signature_matches_replayed_items_by_call_id_and_text():
    call = {"type": "function_call", "call_id": "c-9", "name": "lookup", "arguments": "{}", "id": "fc_1", "status": "completed"}
    signature = signature_from_output_item(call)
    assert signature is not None
    replayed = {"type": "function_call", "call_id": "c-9", "name": "lookup", "arguments": "{}"}
    assert matches_output(replayed, signature)
    assert not matches_output({"type": "function_call", "call_id": "c-8", "name": "lookup", "arguments": "{}"}, signature)

    answer = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "done", "annotations": []}],
    }
    message_signature = signature_from_output_item(answer)
    replayed_message = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}
    assert matches_output(replayed_message, message_signature)
    assert not matches_output(
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "other"}]},
        message_signature,
    )


def test_skipped_outputs_do_not_block_continuation():
    first = request_body([message("user", "hi")])
    replayed = function_call("call-1")
    skipped = {"type": "reasoning", "id": "rs_1", "summary": []}
    lane = lane_for(first, outputs=(signature_from_output_item(skipped), signature_from_output_item(replayed)))
    tool_result = function_call_output("call-1", "ok")
    body = request_body([*first["input"], replayed, tool_result])
    result = plan(body, lane)
    assert result.mode == "continuation"
    assert result.input_items == [tool_result]


def test_strip_replayed_items_returns_none_for_unrecognized_replay():
    outputs = (signature_from_output_item(function_call("call-1")),)
    assert strip_replayed_items([function_call_output("call-1", "x")], outputs) == ([{"type": "function_call_output", "call_id": "call-1", "output": "x"}], 0)
    assert strip_replayed_items([function_call("call-1")], ()) == ([function_call("call-1")], 0)
    assert strip_replayed_items([], outputs) == ([], 0)


def test_continuation_eligibility_matches_plan_decision():
    first = request_body([message("user", "hi")])
    lane = lane_for(first)
    fingerprint = fingerprint_request("r", request_body([*first["input"], message("user", "next")]))
    assert continuation_eligible(
        item_hashes=fingerprint.item_hashes,
        model=fingerprint.model,
        instructions_sha=fingerprint.instructions_sha16,
        tools_sha=fingerprint.tools_sha16,
        lane=lane,
        generation=GENERATION,
        now=NOW,
        idle_ttl=IDLE_TTL,
    )
    assert not continuation_eligible(
        item_hashes=fingerprint.item_hashes,
        model=fingerprint.model,
        instructions_sha=fingerprint.instructions_sha16,
        tools_sha=fingerprint.tools_sha16,
        lane=lane,
        generation=GENERATION + 1,
        now=NOW,
        idle_ttl=IDLE_TTL,
    )
