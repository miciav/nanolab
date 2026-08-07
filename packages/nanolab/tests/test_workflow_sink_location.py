def test_workflow_sink_importable_from_sonata_tasks() -> None:
    # WorkflowSink lives in sonata_tasks; import must succeed without importing console
    from sonata_tasks.workflow.events import WorkflowSink
    assert hasattr(WorkflowSink, "emit")
