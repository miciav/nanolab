from sonata_tasks.deployment import (
    CONTROL_PLANE_NODE_PORT,
    DEFAULT_NAMESPACE,
    LOCAL_CONTROL_PLANE_API_PORT,
    LOCAL_CONTROL_PLANE_MANAGEMENT_PORT,
    LOCAL_REGISTRY,
    PROMETHEUS_NODE_PORT,
)


def test_the_lab_deployment_constants_have_their_documented_values() -> None:
    """Pinned deliberately: these are the values the scenarios, the Helm values
    and the CI assertions all assume, and a silent change would move them apart."""
    assert DEFAULT_NAMESPACE == "nanofaas-e2e"
    assert LOCAL_REGISTRY == "127.0.0.1:5000"
    assert CONTROL_PLANE_NODE_PORT == 30080
    assert PROMETHEUS_NODE_PORT == 30090
    assert LOCAL_CONTROL_PLANE_API_PORT == 18080
    assert LOCAL_CONTROL_PLANE_MANAGEMENT_PORT == 18081


def test_the_node_ports_are_distinct() -> None:
    ports = {
        CONTROL_PLANE_NODE_PORT,
        PROMETHEUS_NODE_PORT,
        LOCAL_CONTROL_PLANE_API_PORT,
        LOCAL_CONTROL_PLANE_MANAGEMENT_PORT,
    }
    assert len(ports) == 4
