"""Configuración del bot, tomada de variables de entorno / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Intervalo mínimo permitido: no bajamos de esto para no castigar al servidor.
MIN_INTERVAL_MINUTES = 10

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ConfigError(RuntimeError):
    """Configuración inválida o incompleta."""


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un entero, no {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un número, no {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "si", "sí", "on"}


def _get_date(name: str, default: date) -> date:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigError(f"{name} debe tener formato YYYY-MM-DD, no {raw!r}") from exc


def _path(name: str, default: str) -> Path:
    raw = _get(name, default)
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p)


@dataclass
class Config:
    # --- Telegram ---
    telegram_token: str
    telegram_chat_id: str

    # --- Sitio ---
    base_url: str
    office: str
    calendar_id: str
    calendar_label: str
    person_count: int

    # --- Rango de fechas de interés ---
    date_from: date
    date_to: date

    # --- Scheduling ---
    interval_minutes: int
    heartbeat_enabled: bool
    heartbeat_hour: int
    timezone: ZoneInfo

    # --- Navegación / robustez ---
    headless: bool
    user_agent: str
    nav_timeout_ms: int
    max_flow_steps: int
    max_weeks_to_scan: int
    polite_delay_seconds: float
    retry_attempts: int
    retry_backoff_seconds: float
    retry_backoff_factor: float

    # --- Alertas ---
    failure_alert_threshold: int
    structure_alert_cooldown_minutes: int
    send_screenshots: bool

    # --- Archivos ---
    state_file: Path
    log_file: Path
    screenshot_dir: Path
    log_level: str
    log_max_bytes: int
    log_backup_count: int

    tz_name: str = field(default="America/Argentina/Buenos_Aires")

    @property
    def entry_url(self) -> str:
        return f"{self.base_url}/?Office={self.office}"

    def validate(self) -> None:
        if not self.telegram_token:
            raise ConfigError("Falta TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            raise ConfigError("Falta TELEGRAM_CHAT_ID")
        if self.date_to < self.date_from:
            raise ConfigError("DATE_TO no puede ser anterior a DATE_FROM")
        if not 0 <= self.heartbeat_hour <= 23:
            raise ConfigError("HEARTBEAT_HOUR debe estar entre 0 y 23")


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    """Carga .env (si existe) y arma el Config."""
    load_dotenv(env_file or (BASE_DIR / ".env"), override=False)

    interval = _get_int("CHECK_INTERVAL_MINUTES", 30)
    if interval < MIN_INTERVAL_MINUTES:
        # No es un error: lo elevamos y lo avisamos por log más adelante.
        interval = MIN_INTERVAL_MINUTES

    tz_name = _get("TIMEZONE", "America/Argentina/Buenos_Aires")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"TIMEZONE inválida: {tz_name!r}") from exc

    today = datetime.now(tz).date()

    cfg = Config(
        telegram_token=_get("TELEGRAM_BOT_TOKEN", "") or "",
        telegram_chat_id=_get("TELEGRAM_CHAT_ID", "") or "",
        base_url=_get("BASE_URL", "https://appointment.bmeia.gv.at").rstrip("/"),
        office=_get("OFFICE", "buenos-aires"),
        calendar_id=_get("CALENDAR_ID", "11997661"),
        calendar_label=_get("CALENDAR_LABEL", "Working Holiday Programm"),
        person_count=_get_int("PERSON_COUNT", 1),
        date_from=_get_date("DATE_FROM", today),
        date_to=_get_date("DATE_TO", date(2027, 1, 31)),
        interval_minutes=interval,
        heartbeat_enabled=_get_bool("HEARTBEAT_ENABLED", True),
        heartbeat_hour=_get_int("HEARTBEAT_HOUR", 9),
        timezone=tz,
        tz_name=tz_name,
        headless=_get_bool("HEADLESS", True),
        user_agent=_get("USER_AGENT", DEFAULT_USER_AGENT),
        nav_timeout_ms=_get_int("NAV_TIMEOUT_MS", 45000),
        max_flow_steps=_get_int("MAX_FLOW_STEPS", 8),
        # Tope duro de semanas por corrida. El rango por defecto (hoy → ene/2027)
        # son ~24; 40 deja margen sin que un rango mal puesto dispare cientos
        # de requests contra el servidor de la embajada.
        max_weeks_to_scan=_get_int("MAX_WEEKS_TO_SCAN", 40),
        polite_delay_seconds=_get_float("POLITE_DELAY_SECONDS", 2.0),
        retry_attempts=_get_int("RETRY_ATTEMPTS", 3),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 10.0),
        retry_backoff_factor=_get_float("RETRY_BACKOFF_FACTOR", 3.0),
        failure_alert_threshold=_get_int("FAILURE_ALERT_THRESHOLD", 3),
        structure_alert_cooldown_minutes=_get_int("STRUCTURE_ALERT_COOLDOWN_MINUTES", 180),
        send_screenshots=_get_bool("SEND_SCREENSHOTS", True),
        state_file=_path("STATE_FILE", "data/state.json"),
        log_file=_path("LOG_FILE", "logs/bot.log"),
        screenshot_dir=_path("SCREENSHOT_DIR", "data/screenshots"),
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_max_bytes=_get_int("LOG_MAX_BYTES", 5 * 1024 * 1024),
        log_backup_count=_get_int("LOG_BACKUP_COUNT", 5),
    )
    cfg.validate()
    return cfg
