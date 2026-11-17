import json
import time
from typing import Any, Dict, Iterator, List, Tuple
import requests


class VKError(RuntimeError):
    pass


def group_from_token(token: str, api_version: str = "5.199") -> Tuple[int, str]:
    """Resolve the community that owns a token without asking for its ID."""
    try:
        response = requests.post("https://api.vk.com/method/groups.getById",
                                 data={"access_token": token, "v": api_version}, timeout=30)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise VKError("Не удалось связаться с VK: " + str(exc)) from exc
    if "error" in data:
        error = data["error"]
        raise VKError("VK API {}: {}".format(error.get("error_code"), error.get("error_msg")))
    groups = data.get("response", {}).get("groups", [])
    if len(groups) != 1 or not groups[0].get("id") or not groups[0].get("name"):
        raise VKError("VK не вернул сообщество для этого ключа")
    return int(groups[0]["id"]), str(groups[0]["name"]).strip()


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

    def histories_since(self, peers: List[Tuple[int, int]]) -> Dict[int, Tuple[List[Dict[str, Any]], bool, bool]]:
        """Read the first history page for several chats in one VK execute call.

        Long histories and failed subcalls use the regular paginated reader.
        """
        if not peers:
            return {}
        ids = [peer_id for peer_id, _ in peers]
        code = ("var ids = " + json.dumps(ids) + "; var result = []; var i = 0; "
                "while (i < ids.length) { result.push(API.messages.getHistory({peer_id: ids[i], "
                "count: 100, group_id: " + str(self.group_id) + "})); i = i + 1; } return result;")
        try:
            pages = self.call("execute", code=code)
        except VKError:
            pages = None
        if not isinstance(pages, list) or len(pages) != len(peers):
            pages = [None] * len(peers)
        result = {}
        for (peer_id, since), page in zip(peers, pages):
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                result[peer_id] = self.history_since(peer_id, since)
                continue
            items = page["items"]
            recent = [item for item in items if int(item.get("date") or 0) >= since]
            if items and int(items[-1].get("date") or 0) < since:
                result[peer_id] = (list(reversed(recent)), True, False)
            elif len(items) >= int(page.get("count", len(items))):
                result[peer_id] = (list(reversed(recent)), False, False)
            else:
                result[peer_id] = self.history_since(peer_id, since)
        return result
