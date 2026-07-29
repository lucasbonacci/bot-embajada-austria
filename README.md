# Bot de turnos — Embajada de Austria (Buenos Aires)

Monitorea el sistema oficial de turnos del ministerio austríaco
([appointment.bmeia.gv.at](https://appointment.bmeia.gv.at/?Office=buenos-aires))
y avisa por Telegram **apenas se libera un turno** de *Working Holiday Programm*.

No manda un mensaje cada 15 minutos: solo cuando aparece una fecha que antes no estaba.

## Qué hace y qué no hace

**Hace:**

- Navega el flujo real del sitio (ASP.NET WebForms, con postbacks) hasta el calendario.
- Barre **semana por semana** todo el rango configurado (por defecto, de hoy a fin de enero 2027).
- Compara con la corrida anterior y te avisa solo de las fechas nuevas.
- Te avisa **también cuando se rompe**, que es el riesgo real (ver más abajo).

**No hace, a propósito:**

- **No reserva ni completa el turno.** La embajada cancela sin aviso los turnos que
  detecta gestionados por terceros. El bot detecta y avisa; reservás vos.
- **No resuelve captchas ni evade protecciones anti-bot.** Si aparece una, frena y te avisa.

---

## 1. Instalación

Requiere **Python 3.11 o superior**.

```bash
python3 -m venv .venv
source .venv/bin/activate          # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Playwright necesita bajar su propio Chromium (~150 MB), aparte del pip install:

```bash
python -m playwright install chromium
```

En Linux hacen falta además las librerías de sistema del navegador. Este comando
las instala (pide sudo):

```bash
python -m playwright install --with-deps chromium
```

## 2. Crear el bot de Telegram

**El token:**

1. Abrí Telegram y buscá **@BotFather**.
2. Mandale `/newbot`.
3. Te pide un nombre (cualquiera, ej. `Turnos Austria`) y después un username
   que **tiene que terminar en `bot`** (ej. `turnos_austria_lb_bot`).
4. Te devuelve un token con esta pinta:
   `8123456789:AAHk3l-XyZq0PqRsTuVwXyZ1234567890abc`

Ese token es una contraseña: quien lo tenga controla el bot. No lo subas a un repo público.

**El chat_id:**

1. **Buscá tu bot por su username y mandale cualquier mensaje** (un "hola" sirve).
   Este paso es obligatorio: Telegram no deja que un bot te escriba primero.
2. Abrí esta URL en el navegador, reemplazando `<TOKEN>`:

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

3. Buscá en el JSON `"chat":{"id":123456789`. Ese número es tu `TELEGRAM_CHAT_ID`.

Si `getUpdates` devuelve `{"ok":true,"result":[]}`, es que todavía no le escribiste
al bot, o ya está corriendo en otro lado consumiendo los updates.

## 3. Configurar

```bash
cp .env.example .env
```

Editá `.env` y completá como mínimo `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
Todo lo demás tiene default razonable. Las variables que más vas a querer tocar:

| Variable | Default | Qué es |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `30` | Cada cuánto chequea. **Mínimo forzado: 10.** |
| `DATE_FROM` | hoy | Desde qué fecha te interesan turnos (`YYYY-MM-DD`). |
| `DATE_TO` | `2027-01-31` | Hasta qué fecha. |
| `HEARTBEAT_HOUR` | `9` | Hora argentina del mensaje diario de "sigo vivo". |
| `LOG_LEVEL` | `INFO` | `DEBUG` si querés ver cada paso. |
| `MAX_WEEKS_TO_SCAN` | `40` | Tope duro de semanas por corrida. |

> **Sobre el intervalo:** cada chequeo barre ~24 semanas, y **cada semana es un
> request** al servidor de la embajada. Con 30 minutos son ~48 requests/hora, que es
> razonable. Bajarlo a 10 los triplica. El bot no te deja bajar de 10 minutos.

## 4. Probar que funciona (hacelo antes de dejarlo suelto)

```bash
python -m bot.main --test
```

Esto valida el token, te manda una notificación de prueba, hace **un** chequeo
y te imprime el detalle semana por semana. Tarda ~60-90 segundos.

```
[3/3] Chequeo único de 'Working Holiday Programm'...
  ✅ Scraping OK en 62.87s (24 semana(s) revisada(s))

  DETALLE DEL BARRIDO:
     · semana del 17/08/2026: vacía (confirmado por el sitio)
     · semana del 24/08/2026: vacía (confirmado por el sitio)
     ...
     · semana del 25/01/2027: vacía (confirmado por el sitio)

  Semanas con respuesta concluyente: 24/24

  TURNOS ENCONTRADOS EN EL RANGO: 0
     (ninguno — es lo normal para Working Holiday)
```

**Qué mirar:** que diga *24 semanas* y que las concluyentes sean *24/24*.
Si ves **una sola semana**, el barrido se rompió, aunque el bot diga "0 turnos".

El modo `--test` no toca el estado guardado ni manda alertas de turnos.

## 5. Correr

```bash
python -m bot.main            # loop continuo (es el modo normal)
python -m bot.main --once     # un chequeo y sale (para cron / GitHub Actions)
python -m bot.main --status    # muestra el estado guardado
python -m bot.main --resume    # reanuda si frenó por captcha
```

---

## 6. Dejarlo corriendo 24/7

### Opción A — VPS con systemd (recomendada)

**Ventajas:** el bot corre de verdad cada N minutos y a la hora que decís; el estado
vive en disco; el heartbeat diario sale puntual; los logs quedan en `journalctl`.
Un VPS mínimo (1 vCPU / 1 GB) alcanza y sobra — sale unos USD 4-5 por mes.

**Desventajas:** hay que pagarlo y mantenerlo (updates del SO, disco, etc.).

```bash
# En el VPS, como root
adduser --system --group --home /opt/bot-embajada turnos
cd /opt/bot-embajada
git clone <tu-repo> .      # o copiá los archivos con scp

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium

cp .env.example .env && nano .env     # completá el token y el chat_id
chmod 600 .env
mkdir -p data logs
chown -R turnos:turnos /opt/bot-embajada

# Probá antes de instalar el servicio
sudo -u turnos .venv/bin/python -m bot.main --test

cp deploy/bot-embajada.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bot-embajada
```

Chequeos útiles:

```bash
systemctl status bot-embajada
journalctl -u bot-embajada -f          # logs en vivo
journalctl -u bot-embajada --since "1 hour ago"
```

> El `.service` corre Chromium headless: si el VPS tiene 512 MB puede quedarse sin
> memoria. 1 GB es el mínimo cómodo.

### Opción B — GitHub Actions con cron

**Ventajas:** gratis, sin servidor que mantener, los logs de cada corrida quedan en
la pestaña Actions y el `state.json` commiteado te deja un historial auditable de
qué apareció y cuándo.

**Desventajas, que son reales:**

- **El cron de GitHub no descarta algunas corridas: descarta la mayoría.** Esto no es
  una estimación, está medido en este repo con `cron: */30` y repo público:

  | | |
  |---|---|
  | Intervalo programado | 30 min |
  | Intervalo real | **85 min promedio** (mínimo 66, máximo 110) |
  | Corridas esperadas en la ventana | 11 |
  | Corridas efectivas | **4** (se descartó el 64%) |

  El cron de Actions es *best-effort*: cuando hay congestión, GitHub saltea
  ejecuciones enteras sin avisar. Para cadencia confiable hay que usar el VPS.
  Si te importa agarrar una cancelación, este es **el** motivo para no usar Actions.
- **GitHub desactiva el cron después de 60 días sin actividad** en el repo. Como el
  bot commitea el estado en cada corrida, en la práctica no pasa, pero tenelo presente.
- **El repo tiene que ser público**, por los minutos (ver abajo). Eso hace público el
  código y el historial de `state.json`, o sea qué turnos hubo y cuándo.

> ### Ojo con los minutos
>
> Cada corrida son ~3-4 minutos facturables. A 30 minutos de intervalo son ~1.440
> corridas por mes: **unos 5.700 minutos**.
>
> - En repos **públicos** los minutos de Actions son **gratis e ilimitados**. ✅
> - En repos **privados** el tier gratis son **2.000 minutos/mes**: se agotarían en
>   unos 10 días y el bot se apagaría solo, sin avisarte.
>
> Si querés repo privado, subí `CHECK_INTERVAL_MINUTES` y el `cron` del workflow a
> 2 horas (~1.100 min/mes), o pagá los minutos extra — a USD 0,008/min son ~USD 30
> al mes, bastante más caro que el VPS.
>
> El token y el `chat_id` **no** quedan expuestos por tener el repo público: viven
> en GitHub Secrets, encriptados, y no aparecen ni en el código ni en los logs.

Setup:

1. Subí el proyecto a un repo **público** (o privado, con el cron a 2 h).
2. En *Settings → Secrets and variables → Actions → New repository secret*, creá:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. En *Settings → Actions → General → Workflow permissions*, marcá
   **Read and write permissions** (el workflow commitea `data/state.json`).
4. El workflow ya está en `.github/workflows/check.yml`. Probalo a mano con
   *Actions → Chequear turnos → Run workflow*.

**Cuál elegir:** si el turno te importa de verdad, systemd. Actions te da en la
práctica un chequeo cada ~85 minutos: suficiente para las cargas grandes de la
embajada, que suelen quedar disponibles algunas horas, pero vas a perder casi
seguro las cancelaciones sueltas.

**Migrar de Actions al VPS** es directo y no se pierde nada: seguí la sección A
(el `.service` ya está en `deploy/`), copiá `data/state.json` del repo al VPS para
no arrancar de cero, y desactivá el workflow desde *Actions → Chequear turnos →
`···` → Disable workflow* para no recibir avisos duplicados.

---

## 7. Anti-fallo silencioso

El riesgo no es que no haya turnos: es que el bot se rompa y vos no te enteres.
Por eso:

- **"No encuentro el calendario" nunca se traduce como "no hay turnos".** Si falta el
  selector, el calendario o el campo de la semana, es un `StructureError`: te llega
  una alerta por Telegram con el detalle, y se guardan screenshot + HTML en
  `data/screenshots/` para que puedas ver qué cambió.
- **Una semana vacía solo cuenta como vacía si el sitio lo dice.** El bot exige el
  mensaje literal *"For your selection there are unfortunately no appointments
  available"*. Sin turnos y sin ese mensaje = error de estructura, no cero turnos.
- **Si el sitio devuelve una semana distinta a la pedida**, se registra como fin del
  horizonte de reserva — no como semana vacía.
- **3 corridas fallidas seguidas** disparan una alerta (`FAILURE_ALERT_THRESHOLD`).
- **Heartbeat diario a las 9 AM** hora argentina con cuántos chequeos hubo en 24 h y
  cómo salió el último. **Si un día no te llega el heartbeat, el bot está muerto.**
- **Captcha** → frena, avisa y no reintenta. Para reanudar: `python -m bot.main --resume`.
- Logs estructurados con rotación en `logs/bot.log` (5 MB × 5 archivos).

### Si te llega "EL BOT SE ROMPIÓ"

1. Mirá el screenshot y el HTML que menciona el mensaje, en `data/screenshots/`.
2. Entrá al sitio a mano y fijate qué cambió.
3. Los selectores están todos juntos arriba de `bot/scraper.py`, con un comentario
   que documenta la estructura real del sitio.

---

## 8. Cómo funciona el scraping

El flujo del sitio son 4 pasos, todos por postback:

1. `/?Office=buenos-aires` → `<select id="CalendarId">`, se elige
   `11997661` = *Working Holiday Programm* → botón `Next`.
2. `<select id="PersonCount">` (para WH solo ofrece "1") → `Next`.
3. Página de información legal → `Next`.
4. `<form action="/HomeWeb/Scheduler">`: el calendario, una semana por vez.
   Los turnos libres son `<input name="Start" type="radio" value="8/21/2026 9:00:00 AM">`.

**El detalle que importa:** el botón `Next week` **solo existe cuando el calendario
tiene disponibilidad más adelante**. En Working Holiday, que está vacío, la página
ofrece únicamente `Week before` — y encima clavado, porque el sitio abre directamente
en la primera semana reservable.

Un bot que navegue con ese botón mira **una sola semana para siempre** y reporta
"0 turnos" con total confianza. Es un falso negativo permanente y silencioso.

Por eso el barrido acá es explícito: la semana mostrada la controla el campo oculto
`<input id="Monday">`, así que el bot lo escribe y reenvía el formulario, semana por
semana, sobre todo el rango configurado. El envío nunca incluye un `Command` ni un
turno tildado — se verifica antes — así que re-renderiza la semana sin avanzar el
flujo de reserva.

## 9. Problemas comunes

| Síntoma | Causa probable |
|---|---|
| `Falta TELEGRAM_BOT_TOKEN` | No copiaste `.env.example` a `.env`. |
| El test valida el token pero no llega el mensaje | `TELEGRAM_CHAT_ID` mal, o nunca le escribiste al bot. |
| `getUpdates` devuelve `result: []` | Escribile al bot primero. Si el bot ya está corriendo, paralo. |
| `Executable doesn't exist at .../chrome-linux/chrome` | Falta `python -m playwright install chromium`. |
| El bot arranca y muere en el VPS | Falta `--with-deps`, o el VPS se queda sin RAM. |
| Barre 1 semana en vez de 24 | El sitio cambió. Revisá el screenshot y `bot/scraper.py`. |

## 10. Estructura

```
bot/
  main.py           CLI y orquestación (loop, --test, --once, --status, --resume)
  scraper.py        navegación con Playwright y barrido del calendario
  notifier.py       mensajes de Telegram
  storage.py        estado en JSON con escritura atómica
  config.py         configuración desde entorno / .env
  models.py         tipos y excepciones del dominio
  logging_setup.py  logging estructurado con rotación
deploy/
  bot-embajada.service
.github/workflows/
  check.yml
```

---

**Reservá vos el turno.** El bot solo te avisa.
