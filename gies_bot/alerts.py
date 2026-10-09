"""Phone alerts through ntfy. Without NTFY_TOPIC they are only printed and logged."""

import logging
import os

import httpx

log = logging.getLogger(__name__)
PRIORITY = {"loud": 4, "quiet": 2}  # ntfy: 4 buzzes, 2 arrives silently


def send(level: str, title: str, body: str, tag: str | None = None) -> None:
    log.info("ALERT [%s] %s | %s", level, title, body.replace("\n", " | "))
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        httpx.post(
            os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
            json={"topic": topic, "title": title, "message": body, "priority": PRIORITY[level],
                  "tags": [tag] if tag else []},  # ntfy shows a tag as an emoji before the title
            timeout=15,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        log.error("ntfy send failed: %s", exc)
