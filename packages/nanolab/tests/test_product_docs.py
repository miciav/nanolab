from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
NANOLAB_ROOT = Path(__file__).resolve().parents[1]
DOCS = (WORKSPACE_ROOT / "README.md", NANOLAB_ROOT / "README.md")
COMMAND_PREFIX = "NANOFAAS_ROOT=/path/to/nanofaas uv run --package nanolab nanolab"


def test_product_docs_use_the_standalone_command_surface() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in DOCS)

    assert f"{COMMAND_PREFIX} plan" in text
    assert f"{COMMAND_PREFIX} run" in text
    assert "packages/nanolab/environments/multipass.yaml" in text
    assert "packages/nanolab/environments/external.yaml.example" in text
    assert "scripts/controlplane.sh" not in text
    for legacy in ("--saved-profile", "cli-test", "e2e run", "loadtest run", "--profile core"):
        assert legacy not in text


def test_nanolab_readme_describes_the_adapted_tui_surface() -> None:
    text = (NANOLAB_ROOT / "README.md").read_text(encoding="utf-8")

    assert f"{COMMAND_PREFIX} tui" in text
    for section in ("Validation", "CLI", "Load Testing", "Tools"):
        assert section in text
    assert "branded header" in text
    assert "live workflow" in text
    assert "`l`" in text
    for removed_menu in (
        "Build menu",
        "Environment menu",
        "Catalog menu",
        "saved-profile menu",
        "image-publishing menu",
    ):
        assert removed_menu not in text


def test_nanolab_readme_explains_provider_setup_entries() -> None:
    text = (NANOLAB_ROOT / "README.md").read_text(encoding="utf-8")
    marker = "When only the provider templates are present"
    assert marker in text
    setup_block = marker + text.split(marker, maxsplit=1)[1].split("\n\n", maxsplit=1)[0]

    for phrase in (
        "Azure (setup required)",
        "Proxmox (setup required)",
        ".yaml.example",
        "azure.yaml",
        "proxmox.yaml",
        "The TUI never loads or executes",
        "provider values and configuration",
        "external authentication",
        "az login",
        "password_env",
        "environment variable",
        "Do not store secrets in YAML",
        "writes no files",
        "starts no workflow",
    ):
        assert phrase in setup_block
