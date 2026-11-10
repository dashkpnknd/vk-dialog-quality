import unittest

from quality.vk import VKClient


class VKHistoryTests(unittest.TestCase):
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
