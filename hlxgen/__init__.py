"""
hlxgen package

Implements a small command line utility for generating, validating and inspecting
Helix preset files (.hlx). The CLI entry point is exposed via ``python -m hlxgen``.
"""

__all__ = ["main"]
__version__ = "0.1.0"


def main() -> None:
    """Forward to the CLI main entry point."""
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
