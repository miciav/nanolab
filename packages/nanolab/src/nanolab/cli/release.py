from __future__ import annotations

import typer

from nanolab.release.model import git_state
from nanolab.release.versioning import prepare_version
from nanolab.workspace.paths import default_tool_paths


def _bad_parameter(error: Exception) -> typer.BadParameter:
    return typer.BadParameter(str(error))


def install_release_commands(app: typer.Typer) -> None:
    release = typer.Typer(help="Prepare a version for the guarded Azure image release.")

    @release.command("prepare")
    def prepare_command(version: str = typer.Argument(...)) -> None:
        paths = default_tool_paths()
        source = git_state(paths.nanofaas_root)
        if not source.clean:
            raise typer.BadParameter("release preparation requires a clean Git tree")
        try:
            updated = prepare_version(paths.nanofaas_root, version)
        except (FileNotFoundError, PermissionError, RuntimeError, ValueError) as error:
            raise _bad_parameter(error) from error
        for path in updated:
            typer.echo(path.relative_to(paths.nanofaas_root))
        typer.echo("Commit the prepared version changes before running the release workflow.")

    app.add_typer(release, name="release")
