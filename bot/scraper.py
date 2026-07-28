"""
Scraper del sistema de turnos del BMEIA (appointment.bmeia.gv.at).

Estructura real del sitio, verificada navegándolo (julio 2026):

  Paso 1  /?Office=buenos-aires
          <select id="CalendarId"> con la opción value="11997661"
          = "Working Holiday Programm".  Se envía con
          <input type="submit" name="Command" value="Next">
  Paso 2  <select id="PersonCount"> (para WH sólo ofrece "1") -> Next
  Paso 3  Página de información (texto legal) -> Next
  Paso 4  <form action="/HomeWeb/Scheduler"> = el calendario semanal.
          - <input id="Monday" value="8/17/2026 12:00:00 AM">  semana mostrada
          - Turnos libres:
              <input name="Start" type="radio" value="8/21/2026 9:00:00 AM">
          - Sin disponibilidad:
              <p class="message-error">For your selection there are
               unfortunately no appointments available</p>

NAVEGACIÓN ENTRE SEMANAS — ojo con esto:

  El botón Command="Next week" SÓLO existe cuando el calendario tiene
  disponibilidad más adelante.  En "Working Holiday Programm", que está vacío,
  la página ofrece únicamente "Week before" (y encima clavado, porque el sitio
  abre ya en la primera semana reservable).  Si el barrido dependiera de ese
  botón, el bot miraría UNA sola semana para siempre y reportaría "0 turnos"
  con total confianza: un falso negativo permanente.

  Lo que sí funciona (verificado): la semana mostrada la manda enteramente el
  campo oculto #Monday.  Escribiéndolo y enviando el form, el servidor renderiza
  la semana que le pidamos.  Por eso el barrido es explícito, semana por semana,
  sobre todo el rango configurado.

Reglas de oro de este módulo:
  * Si NO encontramos el selector o el calendario -> StructureError.
    Eso no es "cero turnos", es "el bot se rompió".
  * Sólo consideramos "cero turnos" cuando el sitio lo dice explícitamente
    (message-error) y además no hay ningún radio de turno.
  * Si el #Monday que vuelve no es el que pedimos, NO es "semana vacía":
    es el fin del horizonte de reserva, y se registra como tal.
  * Nunca se envía el form con un Command ni con un turno tildado: este bot
    detecta y avisa, no reserva.
  * Si aparece un captcha -> CaptchaDetected. No se resuelve ni se evade.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

from .config import Config
from .logging_setup import get_logger
from .models import (CaptchaDetected, CheckResult, Slot, StructureError,
                     TransientError, WeekReport)

# --- Selectores (todos verificados contra el sitio real) ---
SEL_CALENDAR_SELECT = "#CalendarId"
SEL_LANGUAGE_SELECT = "#Language"
SEL_PERSON_COUNT = "#PersonCount"
SEL_NEXT_BUTTON = "input[type=submit][name=Command][value='Next']"
SEL_SCHEDULER_FORM = "form[action*='Scheduler']"
SEL_MONDAY = "#Monday"
SEL_SLOT_RADIO = "input[name='Start'][type='radio']"
SEL_ERROR_MSG = "p.message-error"
SEL_SLOT_CHECKED = "input[name='Start'][type=radio]:checked"

NO_AVAILABILITY_PATTERNS = [
    "no appointments available",
    "keine termine",
    "no hay citas",
    "no turnos disponibles",
]

# OJO: el sitio precarga la hoja de estilos de BotDetect
# (/BotDetectCaptcha.ashx?get=layout-stylesheet) en TODAS las páginas, incluso
# cuando no hay ningún captcha. Por eso no alcanza con buscar "captcha" en el
# HTML: hay que buscar el widget de verdad (la imagen o el input del código).
SEL_CAPTCHA_ELEMENTS = ", ".join([
    "img[src*='BotDetectCaptcha.ashx'][src*='get=image']",
    "img[src*='botdetectcaptcha.ashx'][src*='get=image']",
    "input[name*='CaptchaCode' i]",
    "input[id*='CaptchaCode' i]",
    "div.BDC_CaptchaDiv",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div.g-recaptcha",
    "div.h-captcha",
    "#cf-challenge-running",
    "#challenge-form",
])

# Frases de interstitials (Cloudflare y similares). Se buscan en el TEXTO
# VISIBLE, no en el HTML crudo, para no comernos falsos positivos.
CAPTCHA_TEXT_PATTERNS = [
    "just a moment",
    "checking your browser",
    "verify you are human",
    "unusual traffic",
    "enable javascript and cookies to continue",
    "please complete the security check",
]

MONDAY_VALUE_FORMATS = ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"]


def _week_monday(d: date) -> date:
    """El lunes de la semana en la que cae `d`."""
    return d - timedelta(days=d.weekday())


class AppointmentScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger("embajada.scraper")

    # ------------------------------------------------------------------ utils

    def _artifact_paths(self, tag: str) -> tuple[Path, Path]:
        self.cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return (self.cfg.screenshot_dir / f"{stamp}-{tag}.png",
                self.cfg.screenshot_dir / f"{stamp}-{tag}.html")

    def _capture(self, page: Page, tag: str) -> tuple[Optional[str], Optional[str]]:
        """Guarda screenshot + HTML del estado actual, para poder diagnosticar."""
        png, html = self._artifact_paths(tag)
        shot = dump = None
        try:
            page.screenshot(path=str(png), full_page=True)
            shot = str(png)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("no se pudo sacar screenshot", extra={"error": str(exc)})
        try:
            html.write_text(page.content(), encoding="utf-8")
            dump = str(html)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("no se pudo guardar el HTML", extra={"error": str(exc)})
        return shot, dump

    def _fail_structure(self, page: Page, tag: str, message: str) -> StructureError:
        shot, dump = self._capture(page, tag)
        return StructureError(message, screenshot=shot, html_dump=dump, url=page.url)

    def _guard_captcha(self, page: Page) -> None:
        """
        Detecta protecciones anti-bot. No las resolvemos ni las evadimos:
        avisamos y frenamos.
        """
        try:
            element = page.query_selector(SEL_CAPTCHA_ELEMENTS)
        except PlaywrightError:
            element = None
        if element is not None:
            shot, dump = self._capture(page, "captcha")
            raise CaptchaDetected(
                "Apareció un widget de captcha en la página "
                f"(elemento <{element.evaluate('e => e.tagName').lower()}>)",
                screenshot=shot, html_dump=dump, url=page.url)

        try:
            texto = (page.inner_text("body") or "").lower()
        except PlaywrightError:
            return
        for pattern in CAPTCHA_TEXT_PATTERNS:
            if pattern in texto:
                shot, dump = self._capture(page, "interstitial")
                raise CaptchaDetected(
                    f"Interstitial anti-bot detectado (texto: {pattern!r})",
                    screenshot=shot, html_dump=dump, url=page.url)

    def _click_and_wait(self, page: Page, selector: str) -> None:
        page.click(selector, timeout=self.cfg.nav_timeout_ms)
        page.wait_for_load_state("networkidle", timeout=self.cfg.nav_timeout_ms)
        time.sleep(self.cfg.polite_delay_seconds)

    @staticmethod
    def _parse_monday(value: str) -> Optional[date]:
        for fmt in MONDAY_VALUE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------- flujo

    def _open_entry(self, page: Page) -> None:
        url = self.cfg.entry_url
        try:
            resp = page.goto(url, wait_until="networkidle", timeout=self.cfg.nav_timeout_ms)
        except PlaywrightTimeout as exc:
            raise TransientError(f"Timeout abriendo {url}: {exc}") from exc
        except PlaywrightError as exc:
            raise TransientError(f"Error de red abriendo {url}: {exc}") from exc

        if resp is not None and resp.status >= 500:
            raise TransientError(f"El sitio respondió {resp.status} en {url}")
        if resp is not None and resp.status >= 400:
            shot, dump = self._capture(page, f"http{resp.status}")
            raise StructureError(f"El sitio respondió {resp.status} en {url}",
                                 screenshot=shot, html_dump=dump, url=url)
        self._guard_captcha(page)

    def _ensure_english(self, page: Page) -> None:
        """
        El sitio devuelve las fechas en formato US (8/21/2026) cuando Language=en.
        Nos aseguramos de estar en inglés para que el parseo sea estable.
        """
        select = page.query_selector(SEL_LANGUAGE_SELECT)
        if select is None:
            return  # no es crítico; el flujo sigue con hidden Language
        current = page.eval_on_selector(SEL_LANGUAGE_SELECT, "e => e.value")
        if current == "en":
            return
        self.log.info("cambiando idioma del sitio a inglés", extra={"idioma_actual": current})
        page.select_option(SEL_LANGUAGE_SELECT, "en")
        change = page.query_selector("input[type=submit][name=Command][value='Change']")
        if change:
            self._click_and_wait(page, "input[type=submit][name=Command][value='Change']")

    def _select_calendar(self, page: Page) -> None:
        if page.query_selector(SEL_CALENDAR_SELECT) is None:
            raise self._fail_structure(
                page, "sin-selector-calendario",
                f"No se encontró el desplegable de servicios ({SEL_CALENDAR_SELECT}). "
                "El sitio cambió de estructura.")

        option = page.query_selector(
            f"{SEL_CALENDAR_SELECT} option[value='{self.cfg.calendar_id}']")
        if option is None:
            # Fallback: buscar por texto, por si cambió el ID del calendario.
            options = page.query_selector_all(f"{SEL_CALENDAR_SELECT} option")
            disponibles = [(o.get_attribute("value"), (o.inner_text() or "").strip())
                           for o in options]
            match = next(
                (v for v, t in disponibles
                 if self.cfg.calendar_label.lower() in t.lower()), None)
            if match is None:
                raise self._fail_structure(
                    page, "sin-opcion-working-holiday",
                    f"No existe la opción '{self.cfg.calendar_label}' "
                    f"(id {self.cfg.calendar_id}) en el desplegable. "
                    f"Opciones actuales: {disponibles}")
            self.log.warning("el CALENDAR_ID no coincide; se usó coincidencia por texto",
                             extra={"esperado": self.cfg.calendar_id, "encontrado": match})
            page.select_option(SEL_CALENDAR_SELECT, match)
        else:
            page.select_option(SEL_CALENDAR_SELECT, self.cfg.calendar_id)

        if page.query_selector(SEL_NEXT_BUTTON) is None:
            raise self._fail_structure(
                page, "sin-boton-next",
                "No se encontró el botón 'Next' en la pantalla inicial.")
        self._click_and_wait(page, SEL_NEXT_BUTTON)

    def _advance_to_scheduler(self, page: Page) -> None:
        """Avanza por los pasos intermedios (cantidad de personas, info) hasta el calendario."""
        for step in range(self.cfg.max_flow_steps):
            self._guard_captcha(page)
            if page.query_selector(SEL_SCHEDULER_FORM) is not None:
                self.log.debug("calendario alcanzado", extra={"pasos": step})
                return

            # OJO: #PersonCount aparece dos veces en el flujo. En el paso 2 es un
            # <select> visible; en el paso 3 (la página de info) es un <input
            # type=hidden> que ya trae el valor. Intentar select_option sobre el
            # hidden no falla rápido: se cuelga hasta agotar el timeout completo
            # (45 s tirados en cada corrida). Por eso miramos qué es antes de tocarlo.
            person = page.query_selector(SEL_PERSON_COUNT)
            if person is not None and person.evaluate("e => e.tagName") == "SELECT" \
                    and person.is_visible():
                try:
                    page.select_option(SEL_PERSON_COUNT, str(self.cfg.person_count),
                                       timeout=self.cfg.nav_timeout_ms)
                except PlaywrightError:
                    self.log.warning("no se pudo fijar la cantidad de personas; se deja el default",
                                     extra={"person_count": self.cfg.person_count})

            if page.query_selector(SEL_NEXT_BUTTON) is None:
                raise self._fail_structure(
                    page, "flujo-trabado",
                    f"El flujo se trabó en el paso {step + 1}: no hay botón 'Next' "
                    "ni se llegó al calendario.")
            self._click_and_wait(page, SEL_NEXT_BUTTON)

        raise self._fail_structure(
            page, "flujo-sin-calendario",
            f"Después de {self.cfg.max_flow_steps} pasos nunca apareció el calendario "
            f"({SEL_SCHEDULER_FORM}).")

    # --------------------------------------------------------------- calendario

    def _extract_week(self, page: Page) -> list[Slot]:
        slots: list[Slot] = []
        for radio in page.query_selector_all(SEL_SLOT_RADIO):
            value = radio.get_attribute("value")
            if not value:
                continue
            try:
                slots.append(Slot.from_site_value(value))
            except ValueError:
                # Un valor con formato desconocido es señal de que algo cambió.
                raise self._fail_structure(
                    page, "formato-fecha-desconocido",
                    f"No se pudo interpretar la fecha de un turno: {value!r}. "
                    "Cambió el formato del sitio.") from None
        return slots

    def _goto_week(self, page: Page, target: date) -> date:
        """
        Fuerza la semana escribiendo #Monday y enviando el form.

        Devuelve el lunes que el servidor terminó renderizando, que puede NO ser
        el pedido (fin del horizonte de reserva). Distinguir esos dos casos es
        justamente lo que evita confundir "no me dejan ver esa semana" con
        "esa semana está vacía".
        """
        # Guarda dura: este bot no reserva. Si por lo que sea hay un turno
        # tildado, no enviamos nada.
        if page.query_selector(SEL_SLOT_CHECKED) is not None:
            raise self._fail_structure(
                page, "turno-preseleccionado",
                "Había un turno tildado en el calendario. No se envía el "
                "formulario: este bot no reserva turnos.")

        # El sitio espera el formato US sin ceros a la izquierda: "8/17/2026 12:00:00 AM"
        value = f"{target.month}/{target.day}/{target.year} 12:00:00 AM"
        page.eval_on_selector(SEL_MONDAY, "(el, v) => { el.value = v; }", value)
        # Se envía sin Command: sólo re-renderiza la semana, no avanza el flujo.
        page.eval_on_selector(SEL_SCHEDULER_FORM, "f => f.submit()")
        page.wait_for_load_state("networkidle", timeout=self.cfg.nav_timeout_ms)
        time.sleep(self.cfg.polite_delay_seconds)
        self._guard_captcha(page)

        if page.query_selector(SEL_SCHEDULER_FORM) is None or \
                page.query_selector(SEL_MONDAY) is None:
            raise self._fail_structure(
                page, "calendario-roto-al-avanzar",
                f"Al pedir la semana del {target.isoformat()} la página dejó de "
                "ser el calendario (se perdió el form o el campo Monday).")

        rendered = self._parse_monday(page.get_attribute(SEL_MONDAY, "value") or "")
        if rendered is None:
            raise self._fail_structure(
                page, "monday-ilegible",
                "No se pudo interpretar el campo Monday devuelto por el sitio: "
                f"{page.get_attribute(SEL_MONDAY, 'value')!r}")
        return rendered

    def _read_week(self, page: Page, monday: date) -> tuple[list[Slot], WeekReport, Optional[str]]:
        """Lee la semana que está renderizada ahora mismo."""
        error_el = page.query_selector(SEL_ERROR_MSG)
        error_text = error_el.inner_text().strip() if error_el else None
        slots = self._extract_week(page)

        if slots:
            return slots, WeekReport(monday, len(slots), confirmed_empty=False), error_text

        explicit_empty = bool(error_text) and any(
            p in error_text.lower() for p in NO_AVAILABILITY_PATTERNS)
        if explicit_empty:
            # Único caso en que "no hay turnos" es una conclusión válida.
            return [], WeekReport(monday, 0, confirmed_empty=True), error_text

        # Ni turnos ni mensaje de "sin disponibilidad" -> no sabemos qué pasó.
        raise self._fail_structure(
            page, "calendario-ambiguo",
            f"La semana del {monday.isoformat()} no muestra turnos PERO tampoco "
            "el mensaje de 'no appointments available'. No se puede concluir que "
            f"no haya turnos. Mensaje encontrado: {error_text!r}")

    def _scan_calendar(self, page: Page) -> tuple[list[Slot], list[WeekReport],
                                                  Optional[str], Optional[str]]:
        """
        Barre semana por semana todo el rango configurado.

        No se apoya en el botón "Next week" porque en Working Holiday no existe
        (ver el docstring del módulo).
        """
        if page.query_selector(SEL_MONDAY) is None:
            raise self._fail_structure(
                page, "calendario-sin-monday",
                f"La página del calendario no tiene el campo {SEL_MONDAY}.")

        initial = self._parse_monday(page.get_attribute(SEL_MONDAY, "value") or "")
        if initial is None:
            raise self._fail_structure(
                page, "monday-inicial-ilegible",
                "No se pudo interpretar el Monday inicial: "
                f"{page.get_attribute(SEL_MONDAY, 'value')!r}")

        # El sitio abre en la primera semana reservable (hoy, 17/8/2026 para WH):
        # pedir semanas anteriores no tiene sentido, no se pueden reservar.
        start = max(_week_monday(self.cfg.date_from), initial)
        end = _week_monday(self.cfg.date_to)

        if start > end:
            nota = (f"La primera semana reservable ({initial.isoformat()}) es "
                    f"posterior a DATE_TO ({self.cfg.date_to.isoformat()}): no hay "
                    "ninguna semana que vigilar. Revisá el rango de fechas.")
            self.log.warning("rango de fechas vacío", extra={"detalle": nota})
            return [], [], None, nota

        self.log.info("barriendo el calendario",
                      extra={"desde": start.isoformat(), "hasta": end.isoformat(),
                             "semanas": (end - start).days // 7 + 1})

        all_slots: list[Slot] = []
        reports: list[WeekReport] = []
        horizon_note: Optional[str] = None
        last_error_text: Optional[str] = None
        rendered = initial
        target = start

        while target <= end and len(reports) < self.cfg.max_weeks_to_scan:
            if rendered != target:
                rendered = self._goto_week(page, target)
                if rendered != target:
                    # El servidor nos devolvió otra semana. NO es "vacía":
                    # es que no nos deja ir más allá. Cortamos y lo dejamos claro.
                    horizon_note = (
                        f"El sitio no permitió ver la semana del "
                        f"{target.strftime('%d/%m/%Y')} (devolvió la del "
                        f"{rendered.strftime('%d/%m/%Y')}). Se interpreta como el "
                        f"fin del horizonte de reserva; las semanas posteriores "
                        f"hasta {end.strftime('%d/%m/%Y')} no se pudieron revisar.")
                    self.log.info("fin del horizonte de reserva",
                                  extra={"pedida": target.isoformat(),
                                         "devuelta": rendered.isoformat()})
                    break

            slots, report, error_text = self._read_week(page, target)
            all_slots.extend(slots)
            reports.append(report)
            last_error_text = error_text or last_error_text
            target += timedelta(days=7)

        if len(reports) >= self.cfg.max_weeks_to_scan and target <= end:
            horizon_note = (f"Se alcanzó el tope de {self.cfg.max_weeks_to_scan} "
                            f"semanas (MAX_WEEKS_TO_SCAN) antes de llegar a "
                            f"{end.strftime('%d/%m/%Y')}. Subí el tope para cubrir "
                            "todo el rango.")
            self.log.warning("tope de semanas alcanzado", extra={"detalle": horizon_note})

        return all_slots, reports, last_error_text, horizon_note

    # -------------------------------------------------------------------- API

    def check(self) -> CheckResult:
        """Una corrida completa. Devuelve CheckResult o lanza ScraperError."""
        started = time.monotonic()
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.cfg.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                context = browser.new_context(
                    user_agent=self.cfg.user_agent,
                    viewport={"width": 1400, "height": 1200},
                    locale="en-US",
                )
                context.set_default_timeout(self.cfg.nav_timeout_ms)
                page = context.new_page()

                self._open_entry(page)
                self._ensure_english(page)
                self._select_calendar(page)
                self._advance_to_scheduler(page)
                slots, weeks, error_text, horizon_note = self._scan_calendar(page)
            except (PlaywrightTimeout,) as exc:
                raise TransientError(f"Timeout durante la navegación: {exc}") from exc
            except PlaywrightError as exc:
                msg = str(exc)
                if any(k in msg.upper() for k in ("ERR_", "NET::", "ECONNRESET", "TIMEOUT")):
                    raise TransientError(f"Error de red/navegador: {msg}") from exc
                raise
            finally:
                browser.close()

        in_range = sorted({
            s for s in slots if self.cfg.date_from <= s.day <= self.cfg.date_to
        })
        descartados = len(slots) - len(in_range)
        if descartados:
            self.log.debug("turnos fuera del rango configurado descartados",
                           extra={"descartados": descartados})

        return CheckResult(
            slots=in_range,
            weeks=weeks,
            no_availability_message=error_text,
            horizon_note=horizon_note,
            duration_seconds=round(time.monotonic() - started, 2),
        )


def check_with_retries(cfg: Config) -> CheckResult:
    """
    Corre el chequeo con hasta N intentos y backoff exponencial.

    - CaptchaDetected NO se reintenta: hay que frenar.
    - StructureError se reintenta (una carga parcial puede simularlo), pero si
      persiste se propaga como error de estructura para alertar.
    """
    log = get_logger("embajada.scraper")
    scraper = AppointmentScraper(cfg)
    delay = cfg.retry_backoff_seconds
    last: Exception | None = None

    for attempt in range(1, cfg.retry_attempts + 1):
        try:
            result = scraper.check()
            if attempt > 1:
                log.info("chequeo exitoso tras reintentos", extra={"intento": attempt})
            return result
        except CaptchaDetected:
            raise
        except (TransientError, StructureError) as exc:
            last = exc
            log.warning("intento fallido",
                        extra={"intento": attempt, "de": cfg.retry_attempts,
                               "tipo": type(exc).__name__, "error": str(exc)[:300]})
            if attempt < cfg.retry_attempts:
                time.sleep(delay)
                delay *= cfg.retry_backoff_factor
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.exception("error inesperado en el chequeo", extra={"intento": attempt})
            if attempt < cfg.retry_attempts:
                time.sleep(delay)
                delay *= cfg.retry_backoff_factor

    assert last is not None
    raise last
