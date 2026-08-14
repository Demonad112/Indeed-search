"""Config loading. Validates loudly at startup rather than blowing up mid-run."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class ConfigError(Exception):
    """Raised when config is missing or malformed. Exit code 3."""


@dataclass(frozen=True)
class Settings:
    contact_email: str
    db_path: Path
    min_interval: float
    jitter: float
    http_timeout: float
    http_retries: int


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")

    email = (os.environ.get("JOBPIPE_CONTACT_EMAIL") or "").strip()
    if not email or "@" not in email:
        raise ConfigError(
            "JOBPIPE_CONTACT_EMAIL is unset or not an email address.\n"
            "Constraint #4 requires a descriptive User-Agent carrying your contact address.\n"
            "Copy .env.example to .env and fill it in."
        )

    db_path = Path(os.environ.get("JOBPIPE_DB", "data/jobpipe.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    def _num(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{key}={raw!r} is not a number") from exc

    min_interval = _num("JOBPIPE_MIN_INTERVAL", 2.0)
    if min_interval < 2.0:
        raise ConfigError(
            f"JOBPIPE_MIN_INTERVAL={min_interval} is below the 2.0s floor set by constraint #4."
        )

    return Settings(
        contact_email=email,
        db_path=db_path,
        min_interval=min_interval,
        jitter=_num("JOBPIPE_JITTER", 0.75),
        http_timeout=_num("JOBPIPE_HTTP_TIMEOUT", 30.0),
        http_retries=int(_num("JOBPIPE_HTTP_RETRIES", 3)),
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping at the top level")
    return data


@dataclass
class Sources:
    """Parsed sources.yaml."""

    raw: dict[str, Any]
    location_allow: list[str] = field(default_factory=list)
    location_deny: list[str] = field(default_factory=list)

    @property
    def indeed(self) -> dict[str, Any]:
        return self.raw.get("indeed", {}) or {}

    def board(self, kind: str) -> dict[str, Any]:
        return self.raw.get(kind, {}) or {}

    def slugs(self, kind: str) -> list[dict[str, Any]]:
        cfg = self.board(kind)
        if not cfg.get("enabled", False):
            return []
        out = []
        for entry in cfg.get("companies", []) or []:
            if isinstance(entry, str):
                out.append({"slug": entry, "name": entry})
            elif isinstance(entry, dict) and entry.get("slug"):
                out.append({"slug": entry["slug"], "name": entry.get("name", entry["slug"])})
            else:
                raise ConfigError(f"{kind}.companies entry is malformed: {entry!r}")
        return out


def load_sources(path: Path | None = None) -> Sources:
    data = _read_yaml(path or CONFIG_DIR / "sources.yaml")
    geo = data.get("location_filter", {}) or {}
    allow = [s.lower() for s in (geo.get("allow") or [])]
    deny = [s.lower() for s in (geo.get("deny") or [])]
    if not allow:
        raise ConfigError(
            "sources.yaml: location_filter.allow is empty. Without it every board's "
            "worldwide postings get ingested. Set it to your commutable area."
        )
    return Sources(raw=data, location_allow=allow, location_deny=deny)


def load_criteria(path: Path | None = None) -> dict[str, Any]:
    """Loaded in Phase 1 only for the salary floor and hours-per-week basis.

    Phase 2 (score.py) is what actually consumes the rest of this file.
    """
    data = _read_yaml(path or CONFIG_DIR / "criteria.yaml")
    must = data.get("must_have", {}) or {}
    floor = must.get("salary_floor")
    if floor is not None:
        if not isinstance(floor, dict) or "amount" not in floor or "period" not in floor:
            raise ConfigError(
                "criteria.yaml: must_have.salary_floor must be a mapping with "
                "'amount' and 'period' (e.g. {amount: 20, period: hourly}), or null."
            )
    return data
