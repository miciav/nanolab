from __future__ import annotations

import json

from workflow_tasks.tasks.models import TaskResult


def verify_invocation(result: TaskResult) -> None:
    """Assert the control plane reported a usable invocation.

    Lives apart from the tasks that call it because the response is the same
    whether the request went through curl or the nanofaas CLI — only the
    transport differs, and both need to read the answer the same way.

    This is the whole point of porting invoke to a real task: the shell version
    piped the response through two `grep -q` calls, which could not tell "not
    JSON" from "status was error" from "no output at all".
    """
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"invocation response was not JSON: {result.stdout[:200]!r}") from error
    if not isinstance(response, dict):
        raise RuntimeError(f"invocation response was not JSON object: {result.stdout[:200]!r}")
    if response.get("status") != "success":
        raise RuntimeError(
            f"invocation did not report success: {response.get('status')!r}"
            f"{_reason(response.get('error'))}"
        )
    if "output" not in response:
        raise RuntimeError("invocation carried no output")


def _reason(error: object) -> str:
    """The control plane's own account of the failure, if it gave one.

    `InvocationResponse` carries an `ErrorInfo(code, message)` beside the
    status. Reporting only the status turned a run that said exactly what went
    wrong into one that said 'error' — leaving the reader to guess between a
    function that threw, a pod that was not ready, and a dispatch that never
    found an endpoint.
    """
    if not isinstance(error, dict):
        return ""
    parts = [str(part) for part in (error.get("code"), error.get("message")) if part]
    return f" ({': '.join(parts)})" if parts else ""
