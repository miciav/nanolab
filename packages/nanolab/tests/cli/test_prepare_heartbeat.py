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
