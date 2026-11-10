from dataclasses import dataclass
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set


DEFAULT_SETTINGS = {
    "button_labels": ["Начать", "Да", "Нет", "iPhone", "AirPods", "Другое",
                      "Наличный расчет", "Кредит/рассрочка", "Trade-in"],
    "timezone": "Europe/Moscow",
}

ID_PATTERN = re.compile(r"(?<!\d)(\d{6,12})(?!\d)")


@dataclass
class Observation:
    peer_id: int
    episode_key: str
    first_at: int
    label_id: Optional[str]
    label_source: Optional[str]
    qualified: bool
    inbound_count: int
    button_count: int
    uncertain_count: int
    status: str


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass
    return {}


def keyboard_labels(messages: Iterable[Dict[str, Any]]) -> Set[str]:
    result: Set[str] = set()
    for message in messages:
        keyboard = _as_dict(message.get("keyboard"))
        for row in keyboard.get("buttons", []):
            for button in row:
                action = button.get("action") or {}
                label = action.get("label")
                if isinstance(label, str) and label.strip():
                    result.add(label.strip().casefold())
    return result


def source_id(message: Dict[str, Any]) -> (Optional[str], Optional[str]):
    for field in ("ref", "ref_source"):
        value = message.get(field)
        if value is not None:
            match = ID_PATTERN.search(str(value))
            if match:
                return match.group(1), field
    return None, None


def classify(peer_id: int, episode_key: str, label_id: Optional[str], label_source: Optional[str],
             messages: List[Dict[str, Any]], settings: Dict[str, Any], truncated: bool = False,
             first_at_override: Optional[int] = None) -> Observation:
    if not messages:
        raise ValueError("Cannot classify an empty conversation")
    messages = sorted(messages, key=lambda item: (int(item.get("date", 0)), int(item.get("id", 0))))
    first_at = first_at_override if first_at_override is not None else int(messages[0]["date"])
    labels = keyboard_labels(messages)
    labels.update(str(value).strip().casefold() for value in settings.get("button_labels", []) if str(value).strip())
    qualified = False
    inbound_count = button_count = uncertain_count = 0
    for message in messages:
        if int(message.get("out", 0)) != 0:
            continue
        inbound_count += 1
        if message.get("payload") not in (None, "", {}):
            button_count += 1
            continue
        text = str(message.get("text") or "").strip()
        if text and text.casefold() in labels:
            uncertain_count += 1
            continue
        if text or message.get("attachments"):
            qualified = True
    if truncated and not qualified:
        status = "review"
    elif qualified:
        status = "qualified"
    elif uncertain_count:
        status = "review"
    else:
        status = "not_qualified"
    return Observation(peer_id, episode_key, first_at, label_id, label_source, qualified,
                       inbound_count, button_count, uncertain_count, status)


def _message_id(message: Dict[str, Any]) -> str:
    identifier = message.get("id") or message.get("conversation_message_id")
    if identifier is None:
        raise ValueError("VK message has no stable ID")
    return str(identifier)


def episodes(peer_id: int, messages: List[Dict[str, Any]], settings: Dict[str, Any],
             since: int, has_older: bool = False, truncated: bool = False,
             anchor: Optional[Dict[str, Any]] = None) -> List[Observation]:
    """Each new numeric source begins a visit; identical repeats remain in that visit."""
    ordered = sorted(messages, key=lambda m: (int(m.get("date", 0)), int(m.get("id") or m.get("conversation_message_id") or 0)))
    result: List[Observation] = []
    current: List[Dict[str, Any]] = []
    label_id = anchor.get("label_id") if anchor else None
    label_source = anchor.get("label_source") if anchor else None
    episode_key = anchor.get("episode_key") if anchor else None
    anchor_key = episode_key
    all_labels = keyboard_labels(ordered)
    config = {**settings, "button_labels": list(set(settings.get("button_labels", [])) | all_labels)}
    for message in ordered:
        timestamp = int(message.get("date", 0))
        marker, marker_source = source_id(message)
        if marker and timestamp >= since and (episode_key is None or marker != label_id):
            if current:
                result.append(classify(peer_id, episode_key, label_id, label_source, current, config, truncated,
                                       anchor["first_at"] if anchor and episode_key == anchor_key else None))
            episode_key = "ad:" + _message_id(message)
            label_id, label_source = marker, marker_source
            current = []
        elif episode_key is None and not has_older and timestamp >= since:
            episode_key = "start:" + _message_id(message)
            label_id = label_source = None
        if episode_key is not None and timestamp >= since:
            current.append(message)
    if current:
        result.append(classify(peer_id, episode_key, label_id, label_source, current, config, truncated,
                               anchor["first_at"] if anchor and episode_key == anchor_key else None))
    return result
