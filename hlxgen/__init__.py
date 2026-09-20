"""
hlxgen package

Implements a small command line utility for generating, validating and inspecting
Helix preset files (.hlx). The CLI entry point is exposed via ``python -m hlxgen``.
"""

__all__ = ["main"]


def _installed_version() -> str:
    """The version recorded by the installer, so pyproject.toml is the only
    place it is written down. A source checkout that was never installed has no
    metadata to read, which is not worth failing over."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("helixpy")
    except PackageNotFoundError:  # pragma: no cover - only in a bare checkout
        return "0.0.0+unknown"


__version__ = _installed_version()


def main() -> None:
    """Forward to the CLI main entry point."""
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
