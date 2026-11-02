from station.llm import from_responses_output, to_responses_input


def test_round_trip_tool_call_and_reasoning():
    output = [
        {"type": "reasoning", "encrypted_content": "enc", "summary": [{"type": "summary_text", "text": "s"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
        {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{\"command\": \"ls\"}"},
    ]
    content = from_responses_output(output)
    assert [b["type"] for b in content] == ["openai_reasoning", "text", "tool_use"]
    history = [
        {"role": "user", "content": [{"type": "text", "text": "welcome"}]},
        {"role": "assistant", "content": content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a b"},
                                     {"type": "text", "text": "<board>x</board>"}]},
    ]
    items = to_responses_input(history)
    assert [i["type"] for i in items] == ["message", "reasoning", "message", "function_call",
                                          "function_call_output", "message"]
    assert items[3]["arguments"] == "{\"command\": \"ls\"}"
    assert items[4] == {"type": "function_call_output", "call_id": "c1", "output": "a b"}


def test_anthropic_thinking_blocks_are_dropped():
    history = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                                 {"type": "text", "text": "ok"}]}]
    assert [i["type"] for i in to_responses_input(history)] == ["message"]


def test_bad_arguments_do_not_crash():
    content = from_responses_output([{"type": "function_call", "call_id": "c", "name": "x", "arguments": "{bad"}])
    assert content[0]["input"] == {"_raw": "{bad"}
