def test_workflow_sink_importable_from_sonata_engine() -> None:
    # WorkflowSink lives in sonata_engine; import must succeed without importing console
    from sonata_engine.workflow.events import WorkflowSink
    assert hasattr(WorkflowSink, "emit")
