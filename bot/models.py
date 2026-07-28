"""Tipos compartidos y excepciones del dominio."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

# Formato que devuelve el sitio con Language=en, p.ej. "8/21/2026 9:00:00 AM"
SITE_DATETIME_FORMAT = "%m/%d/%Y %I:%M:%S %p"


class ScraperError(Exception):
    """Base de los errores del scraper."""

    def __init__(self, message: str, *, screenshot: Optional[str] = None,
                 html_dump: Optional[str] = None, url: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.screenshot = screenshot
        self.html_dump = html_dump
        self.url = url


class TransientError(ScraperError):
    """Falla probablemente pasajera (red, timeout, 5xx). Se reintenta."""


class StructureError(ScraperError):
    """
    La página no tiene la estructura esperada.

    Esto NUNCA se interpreta como "no hay turnos": significa que el sitio cambió
    o que algo se rompió, y dispara una alerta explícita.
    """


class CaptchaDetected(ScraperError):
    """Apareció un captcha o una protección anti-bot. Frenamos, no la evadimos."""


@dataclass(frozen=True, order=True)
class Slot:
    """Un turno disponible."""

    start: datetime
    raw_value: str = field(compare=False)

    @property
    def day(self) -> date:
        return self.start.date()

    @property
    def key(self) -> str:
        """Identificador estable para guardar en disco."""
        return self.start.strftime("%Y-%m-%dT%H:%M")

    @classmethod
    def from_site_value(cls, value: str) -> "Slot":
        return cls(start=datetime.strptime(value.strip(), SITE_DATETIME_FORMAT),
                   raw_value=value.strip())

    def human(self) -> str:
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        return f"{dias[self.start.weekday()]} {self.start.strftime('%d/%m/%Y')} a las {self.start.strftime('%H:%M')}"


@dataclass(frozen=True)
class WeekReport:
    """Qué se vio en una semana concreta del barrido."""

    monday: date
    slot_count: int
    confirmed_empty: bool
    note: Optional[str] = None

    def line(self) -> str:
        estado = (f"{self.slot_count} turno(s)" if self.slot_count
                  else "vacía (confirmado por el sitio)" if self.confirmed_empty
                  else "sin datos")
        extra = f" — {self.note}" if self.note else ""
        return f"semana del {self.monday.strftime('%d/%m/%Y')}: {estado}{extra}"


@dataclass
class CheckResult:
    """Resultado de una corrida exitosa."""

    slots: list[Slot]
    weeks: list[WeekReport] = field(default_factory=list)
    no_availability_message: Optional[str] = None
    horizon_note: Optional[str] = None
    checked_at: datetime = field(default_factory=datetime.now)
    duration_seconds: float = 0.0

    @property
    def slot_keys(self) -> set[str]:
        return {s.key for s in self.slots}

    @property
    def count(self) -> int:
        return len(self.slots)

    @property
    def weeks_scanned(self) -> int:
        return len(self.weeks)
