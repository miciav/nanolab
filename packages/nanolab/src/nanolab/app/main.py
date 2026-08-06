from __future__ import annotations

import sys

import typer
from rich.traceback import install as install_rich_tracebacks

from nanolab.cli.product import install_product_commands
from nanolab.cli.release import install_release_commands
from nanolab.tui.setup import setup_ui

app = typer.Typer(
    help="Control plane orchestration product for building, test, and reporting."
)


@app.command("tui")
def tui() -> None:
    from nanolab.tui.app import NanofaasTUI

    setup_ui()
    NanofaasTUI().run()


install_product_commands(app)
install_release_commands(app)


def main() -> None:
    setup_ui()
    install_rich_tracebacks(show_locals=False)
    # No arguments → launch the interactive Rich TUI
    if len(sys.argv) == 1:
        from nanolab.tui.app import NanofaasTUI
        NanofaasTUI().run()
        return
    app()


if __name__ == "__main__":
    main()
