import time
from nanolab.cli import comparison

def test_heartbeat_reports_while_a_long_command_runs(capsys):
    """Without it, twenty minutes of compiling and twenty minutes of being wedged
    look identical from the log."""
    with comparison._heartbeat("Compile native image", interval=0.05):
        time.sleep(0.17)
    out = capsys.readouterr().out
    assert "still running" in out
    assert out.count("still running") >= 2


def test_heartbeat_stops_when_the_command_returns(capsys):
    with comparison._heartbeat("Quick push", interval=0.05):
        pass
    time.sleep(0.15)
    assert "still running" not in capsys.readouterr().out


def _log_event(line: str):
    from sonata_engine.workflow.events import WorkflowEvent

    return WorkflowEvent(kind="log.line", flow_id="prepare", line=line, stream="stdout")


def test_the_sink_can_render_what_a_command_writes() -> None:
    """The bus carried these lines all along; the renderer dropped them.

    A twenty-minute image build showed two lines and nothing between, so telling
    "compiling" from "wedged" meant reading the VM's load average by hand.
    """
    from nanolab.cli.progress import ConsoleProgressSink

    written: list[str] = []
    sink = ConsoleProgressSink(write=written.append, log_lines=True)

    sink.emit(_log_event("#14 590.5 building native image"))

    assert written == ["#14 590.5 building native image"]


def test_output_is_silent_unless_asked_for() -> None:
    """A k6 run or a helm install would bury the task list under its own output."""
    from nanolab.cli.progress import ConsoleProgressSink

    written: list[str] = []
    ConsoleProgressSink(write=written.append).emit(_log_event("noise"))

    assert written == []
