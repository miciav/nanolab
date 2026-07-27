from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FunctionManifest:
    """What the control plane needs to register one function.

    The same body used to be built twice — once as a file for the CLI, once as a
    curl payload — with the CLI copy hard-coding the four tuning values. The
    numbers happened to agree, so a change to either default would have drifted
    silently. This is the single place that knows the shape.
    """

    name: str
    image: str
    execution_mode: str = "DEPLOYMENT"
    timeout_ms: int = 5000
    concurrency: int = 2
    queue_size: int = 20
    max_retries: int = 3
    resources: dict[str, Any] | None = None
    scaling_config: dict[str, Any] | None = None

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "image": self.image,
            "executionMode": self.execution_mode,
            "timeoutMs": self.timeout_ms,
            "concurrency": self.concurrency,
            "queueSize": self.queue_size,
            "maxRetries": self.max_retries,
        }
        if self.resources is not None:
            body["resources"] = self.resources
        if self.scaling_config is not None:
            body["scalingConfig"] = self.scaling_config
        return body

    def json(self) -> str:
        """Compact JSON, so the payload survives being embedded in a shell word."""
        return json.dumps(self.body(), separators=(",", ":"))
