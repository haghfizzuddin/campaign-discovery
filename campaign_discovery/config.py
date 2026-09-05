"""Configuration loading: engine home + the active profile.

A *profile* is the theme-specific knowledge (term lists, lure rules, brands,
sources, thresholds) that the theme-neutral engine runs with. Profiles ship
inside the package under ``campaign_discovery/profiles/<name>/``; ``cdisc init``
copies the active one to ``$CDISC_HOME/profiles/<name>/`` where it can be edited.
An edited copy always wins over the packaged default.

Active profile: ``--profile`` on the CLI, else ``CDISC_PROFILE``, else
``recruitment-threat``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

PROFILES_DIR = Path(__file__).parent / "profiles"
DEFAULT_PROFILE = "recruitment-threat"


def home() -> Path:
    return Path(os.environ.get("CDISC_HOME", "data")).expanduser().resolve()


def profile() -> str:
    return os.environ.get("CDISC_PROFILE", DEFAULT_PROFILE)


def available_profiles() -> list[str]:
    return sorted(p.name for p in PROFILES_DIR.iterdir() if (p / "lexicon.yaml").exists())


def db_path() -> Path:
    return home() / "cdisc.db"


def raw_store() -> Path:
    return home() / "raw"


def _profile_file(name: str) -> Path:
    override = home() / "profiles" / profile() / name
    if override.exists():
        return override
    packaged = PROFILES_DIR / profile() / name
    if not packaged.exists():
        raise FileNotFoundError(f"profile {profile()!r} has no {name} (available: {available_profiles()})")
    return packaged


def _load_yaml(name: str) -> dict[str, Any]:
    with open(_profile_file(name), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


def lexicon() -> dict[str, Any]:
    return _load_yaml("lexicon.yaml")


def init_home(force: bool = False) -> list[Path]:
    """Create CDISC_HOME and copy the active profile into it for editing."""
    h = home()
    (h / "raw").mkdir(parents=True, exist_ok=True)
    dst_dir = h / "profiles" / profile()
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for name in ("sources.yaml", "lexicon.yaml"):
        dst = dst_dir / name
        if force or not dst.exists():
            shutil.copy(PROFILES_DIR / profile() / name, dst)
            copied.append(dst)
    return copied


def env(name: str, default: str | None = None) -> str | None:
    """Read an env var, also honouring a ``.env`` file in the CWD or CDISC_HOME."""
    val = os.environ.get(name)
    if val:
        return val
    for candidate in (Path(".env"), home() / ".env"):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name and v.strip():
                    return v.strip().strip('"').strip("'")
    return default
