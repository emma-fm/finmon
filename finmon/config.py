"""Configuration loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real env vars take precedence."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _float_or_none(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


@dataclass
class Settings:
    data_dir: Path
    # Enable Banking (PSD2 aggregator)
    eb_app_id: str
    eb_private_key_path: Path
    eb_redirect_url: str
    eb_aspsp_name: str
    eb_country: str
    eb_consent_days: int
    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    # LLM (see finmon/llm/__init__.py for providers)
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_base_url: str
    llm_command: str
    llm_effort: str
    llm_json_mode: bool
    llm_timeout: int
    claude_bin: str          # only for LLM_PROVIDER=claude-code
    # Analysis tuning
    timezone: ZoneInfo
    language: str
    monthly_budget: float | None
    large_expense_eur: float
    digest_hour: int
    sync_lookback_days: int
    initial_history_days: int
    excluded_categories: set[str] = field(default_factory=set)
    fixed_categories: set[str] = field(default_factory=set)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finmon.sqlite3"


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("FINMON_ENV_FILE", PROJECT_DIR / ".env")))
    env = os.environ.get

    data_dir = Path(env("FINMON_DATA_DIR", "~/.local/share/finmon")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    def _set(name: str, default: str) -> set[str]:
        return {c.strip() for c in env(name, default).split(",") if c.strip()}

    return Settings(
        data_dir=data_dir,
        eb_app_id=env("EB_APP_ID", ""),
        eb_private_key_path=Path(env("EB_PRIVATE_KEY_PATH", "~/.config/finmon/enablebanking.pem")).expanduser(),
        eb_redirect_url=env("EB_REDIRECT_URL", "https://localhost/finmon-callback"),
        eb_aspsp_name=env("EB_ASPSP_NAME", ""),
        eb_country=env("EB_COUNTRY", "ES"),  # Bizum and the merchant rules are Spain-specific
        eb_consent_days=int(env("EB_CONSENT_DAYS", "179")),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env("TELEGRAM_CHAT_ID", ""),
        llm_provider=env("LLM_PROVIDER", "claude-code"),
        llm_model=env("LLM_MODEL", env("CLAUDE_MODEL", "")),  # empty = provider default
        llm_api_key=env("LLM_API_KEY", ""),
        llm_base_url=env("LLM_BASE_URL", ""),
        llm_command=env("LLM_COMMAND", ""),
        llm_effort=env("LLM_EFFORT", ""),
        llm_json_mode=env("LLM_JSON_MODE", "true").lower() in ("1", "true", "yes"),
        llm_timeout=int(env("LLM_TIMEOUT", "300")),
        claude_bin=env("CLAUDE_BIN", "claude"),
        timezone=ZoneInfo(env("FINMON_TIMEZONE", "Europe/Madrid")),
        language=env("FINMON_LANGUAGE", "Spanish"),
        monthly_budget=_float_or_none(env("MONTHLY_BUDGET")),
        large_expense_eur=float(env("LARGE_EXPENSE_EUR", "100")),
        digest_hour=int(env("DIGEST_HOUR", "21")),
        sync_lookback_days=int(env("SYNC_LOOKBACK_DAYS", "10")),
        initial_history_days=int(env("INITIAL_HISTORY_DAYS", "90")),
        excluded_categories=_set("EXCLUDED_CATEGORIES", "internal_transfer,savings"),
        fixed_categories=_set("FIXED_CATEGORIES", "housing,utilities,subscriptions,insurance,loans"),
    )
