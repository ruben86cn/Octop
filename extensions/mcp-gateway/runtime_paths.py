"""Keep runtime data outside the checkout; explicit legacy paths remain supported."""

import os
from pathlib import Path


def data_dir():
    root = Path(os.environ.get("OCTOP_GATEWAY_DATA", str(Path.home() / ".octop" / "gateway")))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def data_path(name):
    return data_dir() / name
