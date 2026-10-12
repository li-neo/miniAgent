import json
import unittest

import httpx

from miniagent.plugins.model import OpenAIModel


def event(delta=None, reason=None, usage=None):
    chunk = {
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}],
        "usage": usage,
    }
    return "data: " + json.dumps(chunk) + "\n\n"


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_reassembles_tool_calls_and_keeps_reasoning_fields(self):
        captured = []
        body = event({"role": "assistant", "reasoning_content": "reason "})
        body += event({"content": "working", "reasoning_content": "continued"})
        body += event({"tool_calls": [{
            "index": 1, "id": "b", "type": "function",
            "function": {"name": "shell", "arguments": '{"command":'},
        }, {
            "index": 0, "id": "a", "type": "function",
            "function": {"name": "file", "arguments": '{"action":"read",'},
        }]})
        body += event({"tool_calls": [{
            "index": 0, "function": {"arguments": '"path":"test.txt"}'},
        }, {
            "index": 1, "function": {"arguments": '"pwd"}'},
        }]}, "tool_calls", {"total_tokens": 12})
        body += "data: [DONE]\n\n"

        def handler(request):
            captured.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://test.invalid/v1/chat/completions")
            return httpx.Response(200, text=body)

        async with httpx.AsyncClient(
            base_url="https://test.invalid/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            model = OpenAIModel(client, "test-model")
            request = model.prepare([{"role": "user", "content": "test"}], [])
            streamed = []
            reply = await model.complete(request, streamed.append)
        self.assertEqual(captured, [request])
        self.assertEqual(streamed, ["working"])
        self.assertEqual(reply.message["reasoning_content"], "reason continued")
        self.assertEqual(reply.finish_reason, "tool_calls")
        self.assertEqual(reply.usage["total_tokens"], 12)
        calls = reply.message["tool_calls"]
        self.assertEqual([call["id"] for call in calls], ["a", "b"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"])["path"], "test.txt")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"])["command"], "pwd")
        self.assertEqual(calls[1]["function"]["name"], "shell")

    async def test_stream_without_finish_reason_fails(self):
        async with httpx.AsyncClient(
            base_url="https://test.invalid/",
            transport=httpx.MockTransport(lambda _: httpx.Response(
                200, text=event({"content": "partial"}) + "data: [DONE]\n\n"
            )),
        ) as client:
            with self.assertRaisesRegex(ValueError, "without a finish reason"):
                await OpenAIModel(client, "test").complete({}, lambda _: None)

    async def test_http_error_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(429)

        async with httpx.AsyncClient(
            base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await OpenAIModel(client, "test").complete({}, lambda _: None)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
