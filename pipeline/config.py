"""Load a supplier's YAML from config/suppliers/.

The YAML is where every supplier-specific behaviour lives. Each pipeline module
reads the sections it needs and validates them itself; this only finds the file
and checks it is the one that was asked for.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPLIER_CONFIG_DIR = REPO_ROOT / "config" / "suppliers"


def supplier_config_path(code: str, config_dir: Path = SUPPLIER_CONFIG_DIR) -> Path:
    return config_dir / f"{code}.yaml"


def load_supplier_config(code: str, config_dir: Path = SUPPLIER_CONFIG_DIR) -> dict:
    """Return the parsed YAML for one supplier. Raises if it is missing or mislabelled."""
    path = supplier_config_path(code, config_dir)
    if not path.is_file():
        raise FileNotFoundError(f"no supplier config at {path}")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    declared = (config.get("supplier") or {}).get("code")
    if declared != code:
        raise ValueError(f"{path}: supplier.code is {declared!r}, expected {code!r}")
    return config
