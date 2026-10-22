"""DeepSeek wire-format checks with synthetic replies and no external calls."""
import io
import json
import unittest
from unittest.mock import patch

import engine


class OfflineTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(json.loads(request.data))
        answer = self.replies.pop(0)
        body = {"choices": [{"message": answer}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
        return io.BytesIO(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def tool_reply(name, call_id, *, reasoning=None):
    args = {"success": True, "reason": "offline protocol check"} if name == "finish_task" else {}
    answer = {"role": "assistant", "content": None,
              "tool_calls": [{"id": call_id, "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}]}
    if reasoning is not None:
        answer["reasoning_content"] = reasoning
    return answer


class DeepSeekCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(vars(engine.MODEL_CONTEXT))
        vars(engine.MODEL_CONTEXT).clear()

    def tearDown(self):
        vars(engine.MODEL_CONTEXT).clear()
        vars(engine.MODEL_CONTEXT).update(self.saved)

    def model_config(self, base="https://api.deepseek.com"):
        return {"base_url": base, "model": "deepseek-flash", "api_key": "SYNTHETIC_ONLY"}

    def test_official_origin_explicitly_disables_thinking(self):
        for base in ("https://api.deepseek.com", "https://api.deepseek.com/v1",
                     "https://api.deepseek.com/chat/completions", "https://api.deepseek.com:443/v1"):
            with self.subTest(base=base):
                transport = OfflineTransport([{"role": "assistant", "content": "OK"}])
                with patch("engine.urllib.request.build_opener", return_value=transport):
                    engine.model_request(self.model_config(base), [{"role": "user", "content": "OK"}])
                payload = transport.requests[0]
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertEqual(payload["max_tokens"], 1200)
                self.assertEqual(payload["temperature"], 0)

    def test_other_origins_do_not_receive_vendor_parameters(self):
        for base in ("https://api.example.invalid/v1", "https://api.deepseek.com.example.invalid/v1",
                     "https://api.deepseek.com:8443/v1", "http://localhost:11434/v1"):
            with self.subTest(base=base):
                transport = OfflineTransport([{"role": "assistant", "content": "OK"}])
                with patch("engine.urllib.request.build_opener", return_value=transport):
                    engine.model_request(self.model_config(base), [{"role": "user", "content": "OK"}])
                self.assertNotIn("thinking", transport.requests[0])

    def test_malformed_or_non_https_origins_are_not_official(self):
        for base in ("http://api.deepseek.com", "https://user@api.deepseek.com",
                     "https://api.deepseek.com/v1?private=value", "https://api.deepseek.com/v1#fragment",
                     "https://api.deepseek.com:bad", None):
            with self.subTest(base=base):
                self.assertFalse(engine._is_deepseek_official({"base_url": base}))

    def test_all_reasoning_is_preserved_in_context_but_not_public_events(self):
        hidden = ["PRIVATE_REASONING_ONE\n\u4e2d\u6587  \n", "PRIVATE_REASONING_NO_TOOL\n  ",
                  "PRIVATE_REASONING_THREE" * 100]
        transport = OfflineTransport([
            tool_reply("query_reservations", "q1", reasoning=hidden[0]),
            {"role": "assistant", "content": "Continue with the tools.", "reasoning_content": hidden[1]},
            tool_reply("reserve_device", "w1", reasoning=hidden[2]),
            tool_reply("finish_task", "f1", reasoning="PRIVATE_FINISH_REASONING"),
        ])
        config = engine.Config(agent_mode="llm", faults=[], model_config=self.model_config())
        with patch("engine.urllib.request.build_opener", return_value=transport):
            result = engine.run_trial(config, [], agent_mode="llm")
        self.assertTrue(result["actual_success"])
        self.assertFalse(result["failed"])
        self.assertEqual(len(transport.requests), 4)
        for index, request in enumerate(transport.requests[1:], 1):
            assistant = [message for message in request["messages"] if message["role"] == "assistant"]
            self.assertEqual([message["reasoning_content"] for message in assistant], hidden[:index])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("reasoning_content", serialized)
        self.assertNotIn("PRIVATE_REASONING", serialized)
        self.assertNotIn("PRIVATE_FINISH_REASONING", serialized)

    def test_absent_reasoning_is_not_added_to_existing_messages(self):
        transport = OfflineTransport([
            tool_reply("reserve_device", "w1"), tool_reply("finish_task", "f1")])
        config = engine.Config(agent_mode="llm", faults=[],
                               model_config=self.model_config("https://api.example.invalid/v1"))
        with patch("engine.urllib.request.build_opener", return_value=transport):
            result = engine.run_trial(config, [], agent_mode="llm")
        self.assertTrue(result["actual_success"])
        assistant = [message for message in transport.requests[-1]["messages"] if message["role"] == "assistant"]
        self.assertTrue(assistant)
        self.assertTrue(all("reasoning_content" not in message for message in assistant))

    def test_public_identity_and_provenance_explain_effective_thinking(self):
        config = engine.Config(agent_mode="llm", model_config=self.model_config())
        identity, provenance = engine.model_identity(config), engine.report_provenance(config)
        self.assertEqual(identity, {"model": "deepseek-flash", "api_origin": "https://api.deepseek.com",
                                    "thinking": "disabled"})
        self.assertEqual(provenance["model_request_settings"], {"thinking": "disabled"})
        self.assertNotIn("SYNTHETIC_ONLY", json.dumps({"identity": identity, "provenance": provenance}))
        config.agent_mode = "rules"
        self.assertIsNone(engine.model_identity(config))
        self.assertNotIn("model_request_settings", engine.report_provenance(config))
        config.agent_mode = "llm"
        config.model_config["base_url"] = "https://api.example.invalid/v1"
        self.assertEqual(engine.model_identity(config), {"model": "deepseek-flash",
                                                        "api_origin": "https://api.example.invalid"})
        self.assertNotIn("model_request_settings", engine.report_provenance(config))


if __name__ == "__main__":
    unittest.main()
