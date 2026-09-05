"""``python -m g1000_softkey.gui`` -- open the window directly.

Equivalent to ``python -m g1000_softkey.main gui``; this spelling exists so a
desktop shortcut does not have to carry a subcommand.
"""

from __future__ import annotations

import argparse
import sys

from . import launch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="g1000-softkey-gui",
        description="Graphical interface for the G1000 softkey daemon.",
    )
    parser.add_argument(
        "-c", "--config", default=None,
        help="the configuration file to open with (otherwise the last one used, "
             "or config.toml beside the project)",
    )
    args = parser.parse_args(argv)
    return launch(args.config)


if __name__ == "__main__":
    sys.exit(main())
