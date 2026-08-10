from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sonata_engine import WorkflowCompletion

from sonata_tasks.telegram import TelegramWorkflowObserver, telegram_observer_from_environment


def _completion(error: BaseException | None = None) -> WorkflowCompletion:
    started_at = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    return WorkflowCompletion(
        workflow_id="validate-k8s",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=65),
        error=error,
    )


def test_telegram_observer_sends_success_with_workflow_scenario_and_duration() -> None:
    messages: list[str] = []

    TelegramWorkflowObserver(scenario="deployment-lifecycle-k8s.yaml", send=messages.append).finished(
        _completion()
    )

    assert messages == [
        "✅ NanoLab workflow succeeded\n"
        "Workflow: validate-k8s\n"
        "Scenario: deployment-lifecycle-k8s.yaml\n"
        "Duration: 1m 5s"
    ]


def test_telegram_observer_sends_failure_without_masking_transport_error() -> None:
    messages: list[str] = []

    TelegramWorkflowObserver(scenario="deployment-lifecycle-k8s.yaml", send=messages.append).finished(
        _completion(RuntimeError("control plane unavailable"))
    )

    assert messages == [
        "❌ NanoLab workflow failed\n"
        "Workflow: validate-k8s\n"
        "Scenario: deployment-lifecycle-k8s.yaml\n"
        "Duration: 1m 5s\n"
        "Error: RuntimeError: control plane unavailable"
    ]


def test_telegram_observer_ignores_telegram_errors() -> None:
    def fail_to_send(_message: str) -> None:
        raise OSError("network unavailable")

    TelegramWorkflowObserver(scenario="scenario.yaml", send=fail_to_send).finished(_completion())


def test_telegram_observer_is_disabled_in_ci_or_without_complete_credentials() -> None:
    assert telegram_observer_from_environment(
        "scenario.yaml", {"NANOLAB_TELEGRAM_BOT_TOKEN": "token"}
    ) is None


def test_telegram_observer_uses_environment_credentials_locally() -> None:
    messages: list[str] = []

    observer = telegram_observer_from_environment(
        "scenario.yaml",
        {
            "NANOLAB_TELEGRAM_BOT_TOKEN": "token",
            "NANOLAB_TELEGRAM_CHAT_ID": "chat",
        },
        send=messages.append,
    )

    assert observer is not None
    observer.finished(_completion())
    assert messages[0].startswith("✅ NanoLab workflow succeeded")
    assert telegram_observer_from_environment(
        "scenario.yaml",
        {
            "NANOLAB_TELEGRAM_BOT_TOKEN": "token",
            "NANOLAB_TELEGRAM_CHAT_ID": "chat",
            "CI": "true",
        },
    ) is None
