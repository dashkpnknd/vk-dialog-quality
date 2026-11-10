import time
from typing import Any, Dict, Iterator, List, Tuple
import requests


class VKError(RuntimeError):
    pass


class VKClient:
    def __init__(self, token: str, group_id: int, api_version: str = "5.199"):
        self.token = token
        self.group_id = int(group_id)
        self.api_version = api_version
        self.session = requests.Session()
        self.last_request = 0.0

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        body = {**params, "access_token": self.token, "v": self.api_version, "group_id": self.group_id}
        for attempt in range(5):
            delay = max(0.0, 0.36 - (time.monotonic() - self.last_request))
            if delay:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                response = self.session.post("https://api.vk.com/method/" + method, data=body, timeout=30)
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt < 4:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise VKError("VK request failed: " + str(exc)) from exc
            if "error" in data:
                error = data["error"]
                if error.get("error_code") in (6, 9, 10) and attempt < 4:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise VKError("VK API error {}: {}".format(error.get("error_code"), error.get("error_msg")))
            return data["response"]
        raise VKError("VK request retries exhausted")

    def conversations_since(self, since: int) -> Iterator[Dict[str, Any]]:
        seen = set()
        for filter_name in ("all", "archive"):
            offset = 0
            while True:
                try:
                    response = self.call("messages.getConversations", count=200, offset=offset, filter=filter_name)
                except VKError:
                    if filter_name == "archive":
                        break
                    raise
                items = response.get("items", [])
                if not items:
                    break
                old_count = 0
                for item in items:
                    peer = item.get("conversation", {}).get("peer", {})
                    peer_id = peer.get("id")
                    if peer.get("type") != "user" or peer_id is None:
                        continue
                    last_date = int((item.get("last_message") or {}).get("date") or 0)
                    if last_date < since:
                        old_count += 1
                        continue
                    if peer_id not in seen:
                        seen.add(peer_id)
                        yield item
                offset += len(items)
                if old_count == len(items) or offset >= int(response.get("count", offset)):
                    break

    def history_since(self, peer_id: int, since: int,
                      max_messages: int = 10000) -> Tuple[List[Dict[str, Any]], bool, bool]:
        """Recent history, plus a flag showing whether the conversation predates since."""
        result: List[Dict[str, Any]] = []
        offset = 0
        while offset < max_messages:
            count = min(200, max_messages - offset)
            response = self.call("messages.getHistory", peer_id=peer_id, count=count, offset=offset)
            items = response.get("items", [])
            result.extend(item for item in items if int(item.get("date") or 0) >= since)
            offset += len(items)
            if items and int(items[-1].get("date") or 0) < since:
                return list(reversed(result)), True, False
            if not items or offset >= int(response.get("count", offset)):
                return list(reversed(result)), False, False
        return list(reversed(result)), True, True
