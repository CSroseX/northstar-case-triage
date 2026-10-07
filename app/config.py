"""Configuration from environment variables only.

Nothing secret is ever hard-coded. For local development a .env file is read if present
(and it is gitignored); in Docker the values are passed at `docker run` time.

Secrets are held in a wrapper whose repr/str is redacted, so a stray log line or a
traceback cannot print a key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

NORTHSTAR_API_BASE = "https://hiring.celecolabs.com/api/assessment"
MODEL_ALIAS = "celeco-assessment-chat-v1"


class Secret:
    """A string that will not reveal itself in logs, reprs or tracebacks."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None) -> None:
        self._value = value or ""

    def reveal(self) -> str:
        """The only way to read the value. Use at the call site, never store the result."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(***redacted***)" if self._value else "Secret(unset)"

    __str__ = __repr__


def _load_dotenv(path: Path) -> None:
    """Local dev convenience. This project's .env uses `Key: value` lines, not KEY=value.

    Existing environment variables always win, so Docker/CI values are never overwritten.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        separator = min(
            (stripped.index(c) for c in ":=" if c in stripped),
            default=-1,
        )
        if separator <= 0:
            continue
        key = stripped[:separator].strip()
        value = stripped[separator + 1 :].strip()
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    northstar_api_base: str
    northstar_token: Secret
    model_endpoint: str
    model_key: Secret
    model_alias: str
    port: int
    request_timeout_seconds: float
    # Northstar reads get a tighter deadline than the model call. A business read that
    # has not answered in a few seconds is not going to; the model legitimately takes
    # longer, and sharing one timeout meant either starving it or letting a hanging
    # read hold a hazard open (POL-SAFETY-001).
    northstar_timeout_seconds: float = 5.0

    @property
    def northstar_configured(self) -> bool:
        return bool(self.northstar_token)

    @property
    def model_configured(self) -> bool:
        return bool(self.model_key)


def load_settings(project_root: Path | None = None) -> Settings:
    root = project_root or Path(__file__).resolve().parent.parent
    _load_dotenv(root / ".env")

    # Accept both the documented NORTHSTAR_*/CELECO_* names and the key names used in
    # this project's .env, so local dev and container runs use the same code path.
    northstar = os.getenv("NORTHSTAR_ACCESS_TOKEN") or os.getenv("API_Access_Key")
    model_key = os.getenv("CELECO_MODEL_GATEWAY_KEY") or os.getenv("Model_Access_key")

    base = os.getenv("NORTHSTAR_API_BASE", NORTHSTAR_API_BASE)

    return Settings(
        northstar_api_base=base,
        northstar_token=Secret(northstar),
        model_endpoint=os.getenv("CELECO_MODEL_ENDPOINT", f"{base}?route=model"),
        model_key=Secret(model_key),
        model_alias=os.getenv("CELECO_MODEL_ALIAS", MODEL_ALIAS),
        port=int(os.getenv("PORT", "8080")),
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        northstar_timeout_seconds=float(os.getenv("NORTHSTAR_TIMEOUT_SECONDS", "5")),
    )
