"""
Punto de entrada del bot de turnos de la Embajada de Austria.

Modos:
  python -m bot.main            # loop continuo (cada CHECK_INTERVAL_MINUTES)
  python -m bot.main --test     # notificación de prueba + 1 chequeo, imprime todo
  python -m bot.main --once     # 1 chequeo y sale (para cron / GitHub Actions)
  python -m bot.main --resume   # saca al bot del estado "frenado" por captcha
  python -m bot.main --status   # muestra el estado guardado y sale
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime

from .config import MIN_INTERVAL_MINUTES, Config, ConfigError, load_config
from .logging_setup import get_logger, setup_logging
from .models import CaptchaDetected, CheckResult, ScraperError, Slot, StructureError
from .notifier import TelegramNotifier
from .scraper import AppointmentScraper, check_with_retries
from .storage import Storage

_parar = False


def _signal_handler(signum, _frame):
    global _parar
    _parar = True
    get_logger().info("señal recibida, cerrando prolijamente", extra={"señal": signum})


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger("embajada.main")
        self.storage = Storage(cfg.state_file)
        self.notifier = TelegramNotifier(cfg)

    # --------------------------------------------------------------- chequeo

    def run_check(self, *, notify: bool = True, verbose: bool = False) -> bool:
        """Un ciclo completo. Devuelve True si el chequeo salió bien."""
        if self.storage.halted and notify:
            self.log.error("el bot está frenado; usá --resume para reanudar",
                           extra={"motivo": self.storage.data.get("halted_reason")})
            return False

        self.log.info("iniciando chequeo",
                      extra={"calendario": self.cfg.calendar_label,
                             "rango": f"{self.cfg.date_from} → {self.cfg.date_to}"})
        try:
            result = check_with_retries(self.cfg)
        except CaptchaDetected as exc:
            self._handle_captcha(exc, notify=notify)
            return False
        except StructureError as exc:
            self._handle_structure_error(exc, notify=notify)
            return False
        except Exception as exc:  # noqa: BLE001
            self._handle_failure(exc, notify=notify)
            return False

        self._handle_success(result, notify=notify, verbose=verbose)
        return True

    # ------------------------------------------------------------ resultados

    def _handle_success(self, result: CheckResult, *, notify: bool, verbose: bool) -> None:
        current = result.slot_keys
        primera_vez = self.storage.is_first_run()
        fallas_previas = self.storage.consecutive_failures
        nuevos_keys, desaparecidos = self.storage.diff_and_update(current)
        nuevos = sorted(s for s in result.slots if s.key in nuevos_keys)

        self.log.info(
            "chequeo ok",
            extra={"turnos": result.count, "nuevos": len(nuevos),
                   "desaparecidos": len(desaparecidos),
                   "semanas_revisadas": result.weeks_scanned,
                   "segundos": result.duration_seconds},
        )
        if result.horizon_note:
            # Cobertura parcial del rango: no es un fallo, pero tiene que verse.
            self.log.warning("el barrido no cubrió todo el rango",
                             extra={"detalle": result.horizon_note})

        self.storage.record_check("ok", slot_count=result.count, new_count=len(nuevos),
                                  weeks_scanned=result.weeks_scanned)

        if verbose:
            self._print_result(result, nuevos, desaparecidos, primera_vez)

        if notify:
            if fallas_previas >= self.cfg.failure_alert_threshold:
                self.notifier.notify_recovery(fallas_previas)

            if nuevos and primera_vez:
                # Primera corrida: avisamos lo que hay, pero aclarando que es
                # la línea de base, no una novedad.
                self.log.info("primera corrida: se notifica la línea de base")
                self.notifier.send(
                    f"👀 <b>Primera corrida del bot</b>\nYa hay <b>{len(nuevos)}</b> "
                    f"turno(s) disponible(s) en el rango vigilado. Te aviso cuando "
                    f"aparezcan nuevos.")
                self.notifier.notify_new_slots(nuevos, result.slots)
            elif nuevos:
                self.log.info("¡turnos nuevos! notificando",
                              extra={"fechas": [s.key for s in nuevos]})
                self.notifier.notify_new_slots(nuevos, result.slots)
            else:
                self.log.debug("sin novedades; no se notifica")

        self.storage.save()

    def _print_result(self, result: CheckResult, nuevos: list[Slot],
                      desaparecidos: set[str], primera_vez: bool) -> None:
        print(f"\n--- Chequeo {datetime.now(self.cfg.timezone):%d/%m/%Y %H:%M:%S} ---")
        print(f"Semanas revisadas : {result.weeks_scanned} ({result.duration_seconds}s)")
        if result.horizon_note:
            print(f"⚠️  {result.horizon_note}")
        if result.no_availability_message:
            print(f"Mensaje del sitio : {result.no_availability_message}")
        print(f"Turnos en rango   : {result.count}")
        for slot in result.slots:
            marca = "🟢 NUEVO" if slot in nuevos else "⚪️"
            print(f"   {marca} {slot.human()}")
        if desaparecidos:
            print(f"Ya no están       : {len(desaparecidos)} "
                  f"({', '.join(sorted(desaparecidos)[:5])})")
        if primera_vez:
            print("(primera corrida: se guarda la línea de base)")
        print("-" * 40)

    def _handle_structure_error(self, exc: StructureError, *, notify: bool) -> None:
        self.log.error("ERROR DE ESTRUCTURA: no se puede concluir que no haya turnos",
                       extra={"error": str(exc), "screenshot": exc.screenshot,
                              "html": exc.html_dump, "url": exc.url})
        self.storage.record_check("error", detail=f"[estructura] {exc}")

        if notify and self.storage.should_alert(
                "last_structure_alert", self.cfg.structure_alert_cooldown_minutes):
            if self.notifier.notify_structure_error(
                    str(exc), screenshot=exc.screenshot, url=exc.url,
                    html_dump=exc.html_dump):
                self.storage.mark_alert("last_structure_alert")
        elif notify:
            self.log.info("alerta de estructura silenciada por cooldown")

        self._maybe_alert_repeated_failures(str(exc), notify=notify)
        self.storage.save()

    def _handle_captcha(self, exc: CaptchaDetected, *, notify: bool) -> None:
        self.log.error("CAPTCHA detectado: el bot frena",
                       extra={"error": str(exc), "screenshot": exc.screenshot})
        self.storage.record_check("captcha", detail=str(exc))
        self.storage.halt(f"Captcha detectado: {exc}")
        if notify:
            self.notifier.notify_captcha(str(exc), screenshot=exc.screenshot)
        self.storage.save()

    def _handle_failure(self, exc: Exception, *, notify: bool) -> None:
        detalle = f"{type(exc).__name__}: {exc}"
        self.log.error("corrida fallida", extra={"error": detalle})
        self.storage.record_check("error", detail=detalle)
        self._maybe_alert_repeated_failures(detalle, notify=notify)
        self.storage.save()

    def _maybe_alert_repeated_failures(self, last_error: str, *, notify: bool) -> None:
        fallas = self.storage.consecutive_failures
        if not notify or fallas < self.cfg.failure_alert_threshold:
            return
        # Avisamos al llegar al umbral y después como mucho una vez por cooldown.
        if fallas == self.cfg.failure_alert_threshold or self.storage.should_alert(
                "last_failure_alert", self.cfg.structure_alert_cooldown_minutes):
            if self.notifier.notify_repeated_failures(fallas, last_error):
                self.storage.mark_alert("last_failure_alert")

    # ------------------------------------------------------------- heartbeat

    def maybe_send_heartbeat(self) -> None:
        if not self.cfg.heartbeat_enabled:
            return
        ahora = datetime.now(self.cfg.timezone)
        hoy = ahora.date().isoformat()
        if ahora.hour < self.cfg.heartbeat_hour or self.storage.heartbeat_sent_today(hoy):
            return

        self.log.info("enviando heartbeat diario", extra={"hora_local": ahora.isoformat()})
        if self.notifier.send_heartbeat(
                stats=self.storage.stats_24h(),
                last_check=self.storage.last_check,
                total_slots=len(self.storage.known_slots),
                now=ahora):
            self.storage.mark_heartbeat(hoy)
            self.storage.save()

    # ------------------------------------------------------------------ loop

    def loop(self) -> int:
        self.log.info("bot arrancado",
                      extra={"intervalo_min": self.cfg.interval_minutes,
                             "rango": f"{self.cfg.date_from} → {self.cfg.date_to}",
                             "heartbeat": f"{self.cfg.heartbeat_hour}:00 {self.cfg.tz_name}"})
        while not _parar:
            inicio = time.monotonic()
            try:
                self.run_check()
                self.maybe_send_heartbeat()
            except Exception:  # noqa: BLE001
                self.log.exception("error no manejado en el ciclo")

            if self.storage.halted:
                self.log.error("bot frenado por captcha; saliendo del loop")
                return 2

            espera = max(5.0, self.cfg.interval_minutes * 60 - (time.monotonic() - inicio))
            self.log.debug("esperando al próximo ciclo", extra={"segundos": round(espera)})
            # Dormimos en tramos para poder cortar rápido con Ctrl-C / SIGTERM.
            fin = time.monotonic() + espera
            while time.monotonic() < fin and not _parar:
                time.sleep(min(5.0, fin - time.monotonic()))

        self.log.info("bot detenido")
        return 0

    # ------------------------------------------------------------------ test

    def run_test(self) -> int:
        print("=" * 68)
        print("  MODO TEST — turnos Embajada de Austria (Buenos Aires)")
        print("=" * 68)

        print("\n[1/3] Verificando credenciales de Telegram...")
        ok, info = self.notifier.verify_credentials()
        if not ok:
            print(f"  ❌ {info}")
            return 1
        print(f"  ✅ Bot conectado: {info}")

        print("\n[2/3] Enviando notificación de prueba...")
        if not self.notifier.send_test():
            print("  ❌ No se pudo enviar. Revisá TELEGRAM_CHAT_ID "
                  "(¿le escribiste al bot al menos una vez?).")
            return 1
        print("  ✅ Enviada. Miralo en Telegram.")

        print(f"\n[3/3] Chequeo único de '{self.cfg.calendar_label}'...")
        print(f"      Rango: {self.cfg.date_from} → {self.cfg.date_to}")
        print("      (esto tarda ~15-40 s)\n")

        try:
            result = AppointmentScraper(self.cfg).check()
        except CaptchaDetected as exc:
            print(f"  🛑 CAPTCHA detectado: {exc}")
            print(f"     Screenshot: {exc.screenshot}")
            return 1
        except StructureError as exc:
            print(f"  🔧 ERROR DE ESTRUCTURA (¡no es 'no hay turnos'!):\n     {exc}")
            print(f"     Screenshot: {exc.screenshot}")
            print(f"     HTML:       {exc.html_dump}")
            print("\n     Hay que actualizar los selectores en bot/scraper.py.")
            return 1
        except ScraperError as exc:
            print(f"  ❌ Falló el chequeo: {exc}")
            return 1

        print(f"  ✅ Scraping OK en {result.duration_seconds}s "
              f"({result.weeks_scanned} semana(s) revisada(s))")
        if result.no_availability_message:
            print(f"  ℹ️  El sitio dice: \"{result.no_availability_message}\"")

        # El detalle semana por semana es el punto del modo test: si acá ves UNA
        # sola semana, el barrido se rompió, aunque el bot diga "0 turnos".
        print("\n  DETALLE DEL BARRIDO:")
        for w in result.weeks:
            print(f"     {'🟢' if w.slot_count else '·'} {w.line()}")
        confirmadas = sum(1 for w in result.weeks if w.confirmed_empty or w.slot_count)
        print(f"\n  Semanas con respuesta concluyente: {confirmadas}/{result.weeks_scanned}")
        if result.horizon_note:
            print(f"  ⚠️  {result.horizon_note}")

        print(f"\n  TURNOS ENCONTRADOS EN EL RANGO: {result.count}")
        if result.slots:
            for slot in result.slots:
                print(f"     🟢 {slot.human()}")
        else:
            print("     (ninguno — es lo normal para Working Holiday)")

        conocidos = self.storage.known_slots
        nuevos = result.slot_keys - conocidos
        print(f"\n  Estado guardado: {len(conocidos)} turno(s) conocido(s)")
        print(f"  Serían novedad:  {len(nuevos)}")
        print("\n  (El modo test NO modifica el estado ni manda alerta de turnos.)")
        print("\n✅ Todo listo. El scraping funciona.")
        return 0

    # ---------------------------------------------------------------- status

    def show_status(self) -> int:
        d = self.storage.data
        stats = self.storage.stats_24h()
        print(json.dumps({
            "frenado": d.get("halted"),
            "motivo": d.get("halted_reason"),
            "turnos_conocidos": len(self.storage.known_slots),
            "fallas_consecutivas": self.storage.consecutive_failures,
            "chequeos_24h": stats,
            "ultimo_chequeo": d.get("last_check"),
            "ultimo_exito": d.get("last_success"),
            "ultimo_heartbeat": d.get("last_heartbeat_date"),
            "archivo_estado": str(self.cfg.state_file),
        }, indent=2, ensure_ascii=False))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bot-embajada",
        description="Monitorea turnos de Working Holiday en la Embajada de Austria (BA).")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--test", action="store_true",
                       help="manda notificación de prueba + 1 chequeo verboso")
    grupo.add_argument("--once", action="store_true",
                       help="1 chequeo y salir (cron / GitHub Actions)")
    grupo.add_argument("--status", action="store_true", help="muestra el estado y sale")
    grupo.add_argument("--resume", action="store_true",
                       help="reanuda el bot frenado por captcha")
    parser.add_argument("--env-file", default=None, help="ruta a un .env alternativo")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.env_file)
    except ConfigError as exc:
        print(f"❌ Error de configuración: {exc}", file=sys.stderr)
        print("   Copiá .env.example a .env y completalo.", file=sys.stderr)
        return 1

    setup_logging(cfg.log_file, cfg.log_level, cfg.log_max_bytes, cfg.log_backup_count)
    log = get_logger("embajada.main")

    pedido = (os.getenv("CHECK_INTERVAL_MINUTES") or "").strip()
    if pedido.isdigit() and int(pedido) < MIN_INTERVAL_MINUTES:
        log.warning("intervalo elevado al mínimo permitido",
                    extra={"pedido_min": int(pedido), "usado_min": MIN_INTERVAL_MINUTES})

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    bot = Bot(cfg)

    if args.status:
        return bot.show_status()
    if args.resume:
        bot.storage.resume()
        bot.storage.save()
        print("✅ Bot reanudado.")
        return 0
    if args.test:
        return bot.run_test()
    if args.once:
        ok = bot.run_check(verbose=True)
        bot.maybe_send_heartbeat()
        return 0 if ok else 1
    return bot.loop()


if __name__ == "__main__":
    sys.exit(main())
