"""Notificaciones por Telegram."""
from __future__ import annotations

import html
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import requests

from .config import Config
from .logging_setup import get_logger
from .models import Slot

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4000  # el límite real es 4096; dejamos aire


class TelegramNotifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger("embajada.notifier")
        self.session = requests.Session()

    # ------------------------------------------------------------- transporte

    def _post(self, method: str, *, data: dict, files: Optional[dict] = None,
              attempts: int = 3) -> bool:
        url = TELEGRAM_API.format(token=self.cfg.telegram_token, method=method)
        delay = 3.0
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.post(url, data=data, files=files, timeout=30)
                if resp.status_code == 200:
                    return True
                # 429: respetamos el retry_after que manda Telegram
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", delay)
                    self.log.warning("Telegram nos limitó; esperando",
                                     extra={"retry_after": retry_after})
                    time.sleep(float(retry_after) + 1)
                    continue
                self.log.error("Telegram devolvió error",
                               extra={"metodo": method, "status": resp.status_code,
                                      "respuesta": resp.text[:300], "intento": attempt})
            except requests.RequestException as exc:
                self.log.warning("fallo de red hablando con Telegram",
                                 extra={"metodo": method, "error": str(exc),
                                        "intento": attempt})
            if attempt < attempts:
                time.sleep(delay)
                delay *= 2
        return False

    def send(self, text: str, *, disable_preview: bool = True) -> bool:
        ok = True
        for chunk in _split(text, MAX_MESSAGE_LEN):
            ok = self._post("sendMessage", data={
                "chat_id": self.cfg.telegram_chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            }) and ok
        return ok

    def send_photo(self, path: str | Path, caption: str = "") -> bool:
        p = Path(path)
        if not p.exists():
            return False
        # Telegram rechaza fotos de más de 10 MB
        if p.stat().st_size > 9 * 1024 * 1024:
            self.log.warning("screenshot demasiado grande para Telegram",
                             extra={"archivo": str(p)})
            return False
        try:
            with p.open("rb") as fh:
                return self._post("sendPhoto",
                                  data={"chat_id": self.cfg.telegram_chat_id,
                                        "caption": caption[:1000],
                                        "parse_mode": "HTML"},
                                  files={"photo": fh})
        except OSError as exc:
            self.log.warning("no se pudo adjuntar el screenshot",
                             extra={"error": str(exc)})
            return False

    def verify_credentials(self) -> tuple[bool, str]:
        """getMe: valida que el token sirva antes de arrancar."""
        url = TELEGRAM_API.format(token=self.cfg.telegram_token, method="getMe")
        try:
            resp = self.session.get(url, timeout=20)
        except requests.RequestException as exc:
            return False, f"No se pudo contactar a Telegram: {exc}"
        if resp.status_code != 200:
            return False, f"Token inválido (HTTP {resp.status_code}): {resp.text[:200]}"
        info = resp.json().get("result", {})
        return True, f"@{info.get('username', '?')} ({info.get('first_name', '')})"

    # -------------------------------------------------------------- mensajes

    def notify_new_slots(self, nuevos: list[Slot], total: list[Slot]) -> bool:
        lineas = [
            "🎉 <b>¡TURNOS NUEVOS EN LA EMBAJADA DE AUSTRIA!</b>",
            f"<i>{html.escape(self.cfg.calendar_label)}</i>",
            "",
            f"<b>Fechas nuevas ({len(nuevos)}):</b>",
        ]
        for dia, slots in _group_by_day(nuevos):
            horas = ", ".join(s.start.strftime("%H:%M") for s in slots)
            lineas.append(f"  🟢 <b>{_fecha_larga(dia)}</b> — {horas}")

        lineas += ["", f"<b>Total disponible ahora:</b> {len(total)} turno(s)"]
        if len(total) > len(nuevos):
            otros = [s for s in total if s not in set(nuevos)]
            lineas.append("<i>Ya conocidos:</i>")
            for dia, slots in _group_by_day(otros)[:10]:
                horas = ", ".join(s.start.strftime("%H:%M") for s in slots)
                lineas.append(f"  ⚪️ {_fecha_larga(dia)} — {horas}")

        lineas += [
            "",
            f'👉 <a href="{self.cfg.entry_url}">RESERVAR ACÁ</a>',
            f'<i>Elegí "{html.escape(self.cfg.calendar_label)}" en '
            '"Reservation for" y seguí los pasos.</i>',
            "",
            "⚠️ Reservá vos mismo. La embajada cancela sin aviso los turnos "
            "gestionados por terceros.",
        ]
        return self.send("\n".join(lineas), disable_preview=False)

    def notify_structure_error(self, message: str, *, screenshot: Optional[str] = None,
                               url: Optional[str] = None,
                               html_dump: Optional[str] = None) -> bool:
        lineas = [
            "🔧 <b>EL BOT SE ROMPIÓ (no es que no haya turnos)</b>",
            "",
            "La página no tiene la estructura esperada, así que "
            "<b>no se puede saber si hay turnos o no</b>.",
            "",
            f"<b>Detalle:</b>\n<code>{html.escape(message[:800])}</code>",
        ]
        if url:
            lineas.append(f"\n<b>URL:</b> {html.escape(url)}")
        if screenshot:
            lineas.append(f"<b>Screenshot:</b> <code>{html.escape(screenshot)}</code>")
        if html_dump:
            lineas.append(f"<b>HTML:</b> <code>{html.escape(html_dump)}</code>")
        lineas.append("\n<i>Hay que revisar los selectores en scraper.py.</i>")

        ok = self.send("\n".join(lineas))
        if screenshot and self.cfg.send_screenshots:
            self.send_photo(screenshot, caption="Estado de la página al fallar")
        return ok

    def notify_captcha(self, message: str, *, screenshot: Optional[str] = None) -> bool:
        lineas = [
            "🛑 <b>CAPTCHA / PROTECCIÓN ANTI-BOT DETECTADA</b>",
            "",
            "El bot <b>frenó</b>. Por diseño no resuelve captchas ni evade protecciones.",
            "",
            f"<b>Detalle:</b>\n<code>{html.escape(message[:600])}</code>",
            "",
            "Revisá el sitio a mano. Para reanudar, corré el bot con "
            "<code>--resume</code>.",
        ]
        ok = self.send("\n".join(lineas))
        if screenshot and self.cfg.send_screenshots:
            self.send_photo(screenshot, caption="Captcha detectado")
        return ok

    def notify_repeated_failures(self, count: int, last_error: str) -> bool:
        return self.send("\n".join([
            f"⚠️ <b>{count} corridas fallidas seguidas</b>",
            "",
            "El bot no logró chequear los turnos. Puede ser el sitio caído, "
            "la red, o algo roto.",
            "",
            f"<b>Último error:</b>\n<code>{html.escape(last_error[:800])}</code>",
        ]))

    def notify_recovery(self, failures: int) -> bool:
        return self.send(
            f"✅ <b>Bot recuperado</b>\nVolvió a chequear bien después de "
            f"{failures} falla(s) seguidas.")

    def send_heartbeat(self, *, stats: dict, last_check: Optional[dict],
                       total_slots: int, now: datetime) -> bool:
        lineas = [
            "💓 <b>El bot sigue vivo</b>",
            f"<i>{now.strftime('%d/%m/%Y %H:%M')} (hora Argentina)</i>",
            "",
            f"<b>Chequeos últimas 24 h:</b> {stats['total']} "
            f"(✅ {stats['ok']} / ❌ {stats['error']})",
        ]
        if last_check:
            ts = last_check.get("ts", "?")
            try:
                ts = datetime.fromisoformat(ts).strftime("%d/%m %H:%M")
            except (ValueError, TypeError):
                pass
            estado = {"ok": "✅ OK", "error": "❌ error",
                      "captcha": "🛑 captcha"}.get(last_check.get("status"), "?")
            lineas.append(f"<b>Último chequeo:</b> {ts} — {estado}")
            if last_check.get("status") == "ok":
                lineas.append(f"<b>Turnos vistos:</b> {last_check.get('slot_count', 0)}")
                # Que el heartbeat diga cuántas semanas barrió es lo que lo hace
                # útil: "0 turnos en 24 semanas" tranquiliza, "0 turnos en 1
                # semana" es una alarma.
                semanas = last_check.get("weeks_scanned")
                if semanas:
                    lineas.append(f"<b>Semanas barridas:</b> {semanas}")
            elif last_check.get("detail"):
                lineas.append(f"<code>{html.escape(str(last_check['detail'])[:200])}</code>")

        lineas += [
            f"<b>Turnos disponibles ahora:</b> {total_slots}",
            "",
            f"<i>Rango vigilado: {self.cfg.date_from.strftime('%d/%m/%Y')} → "
            f"{self.cfg.date_to.strftime('%d/%m/%Y')} · cada "
            f"{self.cfg.interval_minutes} min</i>",
        ]
        return self.send("\n".join(lineas))

    def send_test(self) -> bool:
        return self.send("\n".join([
            "🧪 <b>Notificación de prueba</b>",
            "",
            "Si estás leyendo esto, el bot puede avisarte por Telegram. ✅",
            "",
            f"<b>Vigilando:</b> {html.escape(self.cfg.calendar_label)}",
            f"<b>Oficina:</b> {html.escape(self.cfg.office)}",
            f"<b>Rango:</b> {self.cfg.date_from.strftime('%d/%m/%Y')} → "
            f"{self.cfg.date_to.strftime('%d/%m/%Y')}",
            f"<b>Intervalo:</b> cada {self.cfg.interval_minutes} min",
        ]))


# ------------------------------------------------------------------ helpers

def _group_by_day(slots: Iterable[Slot]) -> list[tuple, ]:
    agrupado: dict = defaultdict(list)
    for s in slots:
        agrupado[s.day].append(s)
    return [(d, sorted(agrupado[d])) for d in sorted(agrupado)]


def _fecha_larga(d) -> str:
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return f"{dias[d.weekday()]} {d.day} de {meses[d.month - 1]} {d.year}"


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    partes, actual = [], ""
    for linea in text.split("\n"):
        if len(actual) + len(linea) + 1 > limit:
            partes.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        partes.append(actual)
    return partes
