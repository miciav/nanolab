"""Where the lab deployment lives: namespace, registry and node ports.

One home per value. Spelled out in ten files each, they were ten copies of a
decision made once — the shape that let a CI expectation and a dependency pin
drift from the thing they described.
"""

from __future__ import annotations

DEFAULT_NAMESPACE = "nanofaas-e2e"
LOCAL_REGISTRY = "localhost:5000"

# Literal, not f"{DEFAULT_NAMESPACE}-registry": live environments already have
# a container running under this name, and deriving it from the namespace
# would silently rename (and orphan) it the moment the namespace changes.
REGISTRY_CONTAINER_NAME = "nanofaas-e2e-registry"

# NodePorts the stack VM publishes.
CONTROL_PLANE_NODE_PORT = 30080
PROMETHEUS_NODE_PORT = 30090

# Ports the container backend's local control plane binds on the host.
LOCAL_CONTROL_PLANE_API_PORT = 18080
LOCAL_CONTROL_PLANE_MANAGEMENT_PORT = 18081
