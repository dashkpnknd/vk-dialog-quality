import unittest
from unittest.mock import patch

from quality.vk import VKClient, VKError, group_from_token


class VKIdentityTests(unittest.TestCase):
    @patch("quality.vk.requests.post")
    def test_discovers_community_from_token_alone(self, post):
        post.return_value.json.return_value = {
            "response": {"groups": [{"id": 240453069, "name": "9:41 Store | Калуга"}]}}
        self.assertEqual(group_from_token("secret"), (240453069, "9:41 Store | Калуга"))
        self.assertEqual(post.call_args.kwargs["data"],
                         {"access_token": "secret", "v": "5.199"})

    @patch("quality.vk.requests.post")
    def test_invalid_token_is_rejected(self, post):
        post.return_value.json.return_value = {
            "error": {"error_code": 5, "error_msg": "Invalid access token"}}
        with self.assertRaises(VKError):
            group_from_token("bad")


class VKHistoryTests(unittest.TestCase):
    def test_batch_reads_short_histories_and_falls_back_for_long_one(self):
        client = VKClient("secret", 42)
        calls = []
        def fake_call(method, **params):
            calls.append(method)
            if method == "execute":
                self.assertIn("API.messages.getHistory", params["code"])
                return [{"count": 2, "items": [{"id": 2, "date": 200}, {"id": 1, "date": 100}]},
                        {"count": 101, "items": [{"id": 3, "date": 300}]}]
            return {"count": 1, "items": [{"id": 3, "date": 300}]}
        client.call = fake_call
        histories = client.histories_since([(123, 150), (456, 150)])
        self.assertEqual([item["id"] for item in histories[123][0]], [2])
        self.assertTrue(histories[123][1])
        self.assertEqual([item["id"] for item in histories[456][0]], [3])
        self.assertEqual(calls, ["execute", "messages.getHistory"])

    def test_reads_recent_messages_backwards_and_returns_chronological_order(self):
        client = VKClient("secret", 42)
        calls = []
        def fake_call(method, **params):
            calls.append((method, params))
            if params["offset"] == 0:
                return {"count": 4, "items": [{"id": 4, "date": 400}, {"id": 3, "date": 300}]}
            return {"count": 4, "items": [{"id": 2, "date": 200}, {"id": 1, "date": 100}]}
        client.call = fake_call
        history, older, truncated = client.history_since(123, 150)
        self.assertEqual([item["id"] for item in history], [2, 3, 4])
        self.assertTrue(older)
        self.assertFalse(truncated)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(method == "messages.getHistory" for method, _ in calls))

    def test_old_conversation_can_have_new_source(self):
        client = VKClient("secret", 42)
        client.call = lambda method, **params: {"count": 2, "items": [
            {"id": 2, "date": 200, "ref": "222222222"},
            {"id": 1, "date": 100, "ref": "111111111"}]}
        history, older, _ = client.history_since(123, 150)
        self.assertEqual([item["id"] for item in history], [2])
        self.assertTrue(older)


if __name__ == "__main__":
    unittest.main()
