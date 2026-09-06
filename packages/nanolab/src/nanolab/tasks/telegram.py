"""Best-effort Telegram notifications for completed workflows."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from typing import final, override
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sonata_engine import WorkflowCompletion, WorkflowObserver


MessageSender = Callable[[str], None]


@final
class TelegramWorkflowObserver(WorkflowObserver):
    def __init__(self, *, scenario: str, send: MessageSender) -> None:
        self._scenario: str = scenario
        self._send: MessageSender = send

    @override
    def finished(self, completion: WorkflowCompletion) -> None:
        try:
            self._send(_message(completion, self._scenario))
        except Exception:
            pass


def telegram_observer_from_environment(
    scenario: str,
    environ: Mapping[str, str] | None = None,
    *,
    send: MessageSender | None = None,
) -> TelegramWorkflowObserver | None:
    values = os.environ if environ is None else environ
    token = values.get("NANOLAB_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = values.get("NANOLAB_TELEGRAM_CHAT_ID", "").strip()
    if values.get("CI") or not token or not chat_id:
        return None
    return TelegramWorkflowObserver(
        scenario=scenario,
        send=send or _telegram_sender(token, chat_id),
    )


def _message(completion: WorkflowCompletion, scenario: str) -> str:
    duration_seconds = max(0, int((completion.finished_at - completion.started_at).total_seconds()))
    minutes, seconds = divmod(duration_seconds, 60)
    duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    lines = [
        "✅ NanoLab workflow succeeded" if completion.error is None else "❌ NanoLab workflow failed",
        f"Workflow: {completion.workflow_id}",
        f"Scenario: {scenario}",
        f"Duration: {duration}",
    ]
    if completion.error is not None:
        lines.append(f"Error: {type(completion.error).__name__}: {completion.error}")
    return "\n".join(lines)


def _telegram_sender(token: str, chat_id: str) -> MessageSender:
    def send(message: str) -> None:
        payload = urlencode({"chat_id": chat_id, "text": message}).encode()
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            response.read()

    return send
