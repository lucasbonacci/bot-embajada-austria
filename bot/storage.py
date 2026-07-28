"""Estado persistente en JSON (escritura atómica)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .logging_setup import get_logger

STATE_VERSION = 1


class Storage:
    """
    Guarda:
      - known_slots: los turnos vistos en la última corrida exitosa.
        Si un turno desaparece y vuelve a aparecer, se notifica de nuevo
        (es una cancelación aprovechable).
      - historial de chequeos de las últimas 24 h (para el heartbeat)
      - contador de fallas consecutivas
      - marcas de tiempo de las últimas alertas (para no spamear)
    """

    def __init__(self, path: Path):
        self.path = path
        self.log = get_logger("embajada.storage")
        self.data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------ E/S

    def _default(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "known_slots": [],
            "checks": [],
            "consecutive_failures": 0,
            "last_check": None,
            "last_success": None,
            "last_heartbeat_date": None,
            "last_structure_alert": None,
            "last_failure_alert": None,
            "halted": False,
            "halted_reason": None,
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self.log.error("estado ilegible; se arranca de cero",
                           extra={"archivo": str(self.path), "error": str(exc)})
            return self._default()
        base = self._default()
        base.update(data)
        return base

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -------------------------------------------------------------- turnos

    @property
    def known_slots(self) -> set[str]:
        return set(self.data.get("known_slots", []))

    def diff_and_update(self, current: set[str]) -> tuple[set[str], set[str]]:
        """Devuelve (nuevos, desaparecidos) y deja `current` como estado conocido."""
        previous = self.known_slots
        nuevos = current - previous
        desaparecidos = previous - current
        self.data["known_slots"] = sorted(current)
        return nuevos, desaparecidos

    def is_first_run(self) -> bool:
        return self.data.get("last_success") is None

    # ------------------------------------------------------------ historial

    def record_check(self, status: str, *, slot_count: int = 0,
                     new_count: int = 0, detail: Optional[str] = None) -> None:
        now = datetime.now().astimezone()
        entry = {
            "ts": now.isoformat(),
            "status": status,           # ok | error | captcha
            "slot_count": slot_count,
            "new_count": new_count,
        }
        if detail:
            entry["detail"] = detail[:300]

        checks = self.data.setdefault("checks", [])
        checks.append(entry)
        cutoff = now - timedelta(hours=24)
        self.data["checks"] = [
            c for c in checks
            if _parse_iso(c.get("ts")) and _parse_iso(c["ts"]) >= cutoff
        ][-500:]

        self.data["last_check"] = entry
        if status == "ok":
            self.data["last_success"] = entry
            self.data["consecutive_failures"] = 0
        else:
            self.data["consecutive_failures"] = int(
                self.data.get("consecutive_failures", 0)) + 1

    @property
    def consecutive_failures(self) -> int:
        return int(self.data.get("consecutive_failures", 0))

    def checks_last_24h(self) -> list[dict[str, Any]]:
        cutoff = datetime.now().astimezone() - timedelta(hours=24)
        return [c for c in self.data.get("checks", [])
                if _parse_iso(c.get("ts")) and _parse_iso(c["ts"]) >= cutoff]

    def stats_24h(self) -> dict[str, int]:
        checks = self.checks_last_24h()
        return {
            "total": len(checks),
            "ok": sum(1 for c in checks if c.get("status") == "ok"),
            "error": sum(1 for c in checks if c.get("status") != "ok"),
        }

    @property
    def last_check(self) -> Optional[dict[str, Any]]:
        return self.data.get("last_check")

    # -------------------------------------------------------------- alertas

    def should_alert(self, key: str, cooldown_minutes: int) -> bool:
        raw = self.data.get(key)
        if not raw:
            return True
        ts = _parse_iso(raw)
        if ts is None:
            return True
        return datetime.now().astimezone() - ts >= timedelta(minutes=cooldown_minutes)

    def mark_alert(self, key: str) -> None:
        self.data[key] = datetime.now().astimezone().isoformat()

    # ------------------------------------------------------------ heartbeat

    def heartbeat_sent_today(self, today_iso: str) -> bool:
        return self.data.get("last_heartbeat_date") == today_iso

    def mark_heartbeat(self, today_iso: str) -> None:
        self.data["last_heartbeat_date"] = today_iso

    # ----------------------------------------------------------------- halt

    def halt(self, reason: str) -> None:
        self.data["halted"] = True
        self.data["halted_reason"] = reason

    @property
    def halted(self) -> bool:
        return bool(self.data.get("halted"))

    def resume(self) -> None:
        self.data["halted"] = False
        self.data["halted_reason"] = None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()
