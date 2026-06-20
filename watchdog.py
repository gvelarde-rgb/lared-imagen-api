"""
Watchdog para el scenario de Make 'La Red RSS a Facebook - NUEVO' (id 4634525).

Vigila 24/7 si Make dejo de publicar en Facebook. Corre como hilo de fondo
dentro del mismo proceso Flask (servicio Render lared-imagen-api).

Logica de alerta (dispara SOLO si ambas son ciertas):
  1. Han pasado +ALERT_THRESHOLD_MIN min desde el ultimo run con operations>=4
     (= ultima publicacion real en FB) consultando la API de Make.
  2. El feed /rss-proxy tiene notas que pasan el filtro publicadas DESPUES de
     esa ultima publicacion (si no hay nada nuevo, NO es falla: el sitio no publico).

Acciones al dispararse:
  1. Auto-recuperar: forzar un run del scenario (POST .../run) y esperar.
  2. Si tras el intento sigue sin publicar -> enviar correo (SMTP Gmail).
  3. Anti-spam: 1 correo por incidente; no reenvia hasta normalizar y volver a fallar.

Proteccion multi-worker (gunicorn corre 2 workers): lock por archivo, solo un
worker ejecuta el chequeo en cada ciclo.

Variables de entorno necesarias:
  MAKE_API_TOKEN   token de Make
  ALERT_SMTP_USER  cuenta Gmail que envia (ej. guillermo1309@gmail.com)
  ALERT_SMTP_PASS  App Password de Gmail (16 caracteres)
  ALERT_TO         destino del aviso (gvelarde@rcn.com.gt)
  WATCHDOG_ENABLED 1 para activar (default 1)
"""

import os
import re
import json
import time
import smtplib
import threading
import requests
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

# ---- Config ----
SCENARIO_ID = os.environ.get("WATCHDOG_SCENARIO_ID", "4634525")
MAKE_BASE = os.environ.get("MAKE_BASE_URL", "https://us1.make.com")
MAKE_TOKEN = os.environ.get("MAKE_API_TOKEN", "")
ALERT_THRESHOLD_MIN = int(os.environ.get("ALERT_THRESHOLD_MIN", "45"))
CHECK_EVERY_SEC = int(os.environ.get("WATCHDOG_INTERVAL_SEC", "600"))  # 10 min
RECOVER_WAIT_SEC = int(os.environ.get("WATCHDOG_RECOVER_WAIT_SEC", "120"))
ENABLED = os.environ.get("WATCHDOG_ENABLED", "1") == "1"

SMTP_USER = os.environ.get("ALERT_SMTP_USER", "")
SMTP_PASS = os.environ.get("ALERT_SMTP_PASS", "")
ALERT_TO = os.environ.get("ALERT_TO", "gvelarde@rcn.com.gt")
SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("ALERT_SMTP_PORT", "587"))

FEED_URL = os.environ.get("WATCHDOG_FEED_URL", "http://127.0.0.1:" + os.environ.get("PORT", "5000") + "/rss-proxy")
SCENARIO_LINK = f"{MAKE_BASE}/12505/scenarios/{SCENARIO_ID}/edit"

# Categorias que pasan el filtro de Make (mismas del scenario)
ALLOWED_CATS = {"Nacionales", "Futbol Nacional", "Futbol Internacional", "Internacionales"}

LOCK_FILE = "/tmp/lared_watchdog.lock"
STATE_FILE = "/tmp/lared_watchdog_state.json"
LOCK_TTL = CHECK_EVERY_SEC - 30  # un worker "posee" el ciclo por casi todo el intervalo

UA = {"User-Agent": "Mozilla/5.0", "Authorization": f"Token {MAKE_TOKEN}"}


def _log(msg):
    print(f"[watchdog] {datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


# ---- Lock por archivo (evita doble ejecucion entre workers) ----
def _acquire_lock():
    now = time.time()
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                ts = float(f.read().strip() or 0)
            if now - ts < LOCK_TTL:
                return False  # otro worker tiene el ciclo
        with open(LOCK_FILE, "w") as f:
            f.write(str(now))
        return True
    except Exception:
        return True  # ante duda, dejar correr (peor caso: 1 correo extra)


# ---- Estado persistido (anti-spam) ----
def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerted": False, "last_alert_ts": 0, "last_check": None,
                "last_post_min_ago": None, "pending_new": None}


def _save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception as e:
        _log(f"no se pudo guardar estado: {e}")


# ---- Consultar Make: minutos desde la ultima publicacion real ----
def _parse_iso(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _minutes_since_last_publish_v2():
    try:
        r = requests.get(
            f"{MAKE_BASE}/api/v2/scenarios/{SCENARIO_ID}/logs",
            params={"pg[limit]": 30}, headers=UA, timeout=20,
        )
        r.raise_for_status()
        logs = r.json().get("scenarioLogs", [])
        for l in logs:
            ops = l.get("operations")
            if ops and ops >= 4:
                dt = _parse_iso(l.get("timestamp", ""))
                if not dt:
                    continue
                ts_epoch = dt.timestamp()
                mins = (datetime.now(timezone.utc).timestamp() - ts_epoch) / 60.0
                return round(mins, 1), ts_epoch
        return None, None
    except Exception as e:
        _log(f"error consultando Make logs: {e}")
        return None, None


# ---- Consultar feed: cuantas notas que pasan el filtro hay despues de ts ----
def _new_passing_items_after(ts_epoch):
    """Cuenta notas del feed que pasan el filtro y son mas nuevas que ts_epoch."""
    try:
        r = requests.get(FEED_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        xml = r.text
        items = re.findall(r"<item>(.*?)</item>", xml, re.S)
        count = 0
        newest_title = None
        for it in items:
            cat_m = re.search(r"<category><!\[CDATA\[(.*?)\]\]>", it)
            date_m = re.search(r"<pubDate>(.*?)</pubDate>", it)
            cat = cat_m.group(1) if cat_m else ""
            if cat not in ALLOWED_CATS:
                continue
            if date_m:
                try:
                    item_ts = parsedate_to_datetime(date_m.group(1)).timestamp()
                except Exception:
                    continue
                if ts_epoch is None or item_ts > ts_epoch + 30:  # 30s de margen
                    count += 1
                    if newest_title is None:
                        t_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]>", it)
                        newest_title = t_m.group(1) if t_m else None
        return count, newest_title
    except Exception as e:
        _log(f"error consultando feed: {e}")
        return 0, None


# ---- Auto-recuperar: forzar run ----
def _force_run():
    try:
        r = requests.post(
            f"{MAKE_BASE}/api/v2/scenarios/{SCENARIO_ID}/run",
            headers=UA, timeout=30,
        )
        _log(f"force_run status={r.status_code}")
        return r.status_code in (200, 201, 202)
    except Exception as e:
        _log(f"error force_run: {e}")
        return False


# ---- Enviar correo ----
def _send_alert_email(mins_ago, pending, newest_title, recovered):
    if not (SMTP_USER and SMTP_PASS and ALERT_TO):
        _log("SMTP no configurado, no se envia correo")
        return False
    estado = "se intentó forzar un run pero SIGUE sin publicar" if not recovered else "recuperado tras forzar run"
    body = f"""ALERTA - La Red RSS a Facebook (Make)

El scenario dejo de publicar en Facebook.

- Ultima publicacion hace: {mins_ago} minutos (umbral: {ALERT_THRESHOLD_MIN} min)
- Notas nuevas esperando en el feed: {pending}
- Nota mas reciente sin publicar: {newest_title or '(desconocida)'}
- Accion automatica: {estado}

Scenario: {SCENARIO_LINK}

Este es un aviso automatico del watchdog del servicio lared-imagen-api.
"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[ALERTA] La Red FB sin publicar hace {int(mins_ago)} min"
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [ALERT_TO], msg.as_string())
        _log(f"correo de alerta enviado a {ALERT_TO}")
        return True
    except Exception as e:
        _log(f"error enviando correo: {e}")
        return False


# ---- Ciclo principal ----
def _check_once():
    st = _load_state()
    mins, ts_epoch = _minutes_since_last_publish_v2()
    st["last_check"] = datetime.now(timezone.utc).isoformat()
    st["last_post_min_ago"] = mins

    if mins is None:
        _log("no se pudo determinar ultima publicacion; se omite ciclo")
        _save_state(st)
        return

    pending, newest_title = _new_passing_items_after(ts_epoch)
    st["pending_new"] = pending

    # Condicion de falla: pasaron +umbral min Y hay notas nuevas que pasan el filtro
    is_failing = (mins >= ALERT_THRESHOLD_MIN) and (pending > 0)

    if is_failing:
        if st.get("alerted"):
            _log(f"ya alertado este incidente (mins={mins}, pending={pending}); sin reenvio")
            _save_state(st)
            return
        _log(f"FALLA detectada: mins={mins}, pending={pending}. Intentando auto-recuperar...")
        _force_run()
        time.sleep(RECOVER_WAIT_SEC)
        mins2, ts2 = _minutes_since_last_publish_v2()
        recovered = (mins2 is not None and mins2 < ALERT_THRESHOLD_MIN)
        if recovered:
            _log(f"recuperado tras force_run (mins ahora={mins2}); sin correo")
            st["alerted"] = False
        else:
            _send_alert_email(mins, pending, newest_title, recovered=False)
            st["alerted"] = True
            st["last_alert_ts"] = time.time()
    else:
        if st.get("alerted"):
            _log(f"normalizado (mins={mins}, pending={pending}); reset de alerta")
        st["alerted"] = False

    _save_state(st)


def _loop():
    _log(f"watchdog iniciado (scenario={SCENARIO_ID}, umbral={ALERT_THRESHOLD_MIN}min, cada {CHECK_EVERY_SEC}s)")
    # arranque escalonado para que no choquen los 2 workers al mismo tiempo
    time.sleep(20)
    while True:
        try:
            if _acquire_lock():
                _check_once()
        except Exception as e:
            _log(f"error en ciclo: {e}")
        time.sleep(CHECK_EVERY_SEC)


_started = False


def start_watchdog():
    global _started
    if _started:
        return
    if not ENABLED:
        _log("watchdog DESACTIVADO (WATCHDOG_ENABLED!=1)")
        return
    if not MAKE_TOKEN:
        _log("watchdog NO inicia: falta MAKE_API_TOKEN")
        return
    _started = True
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def get_status():
    """Para el endpoint /watchdog-status."""
    st = _load_state()
    mins, ts_epoch = _minutes_since_last_publish_v2()
    pending, newest = _new_passing_items_after(ts_epoch) if ts_epoch else (None, None)
    return {
        "enabled": ENABLED,
        "scenario_id": SCENARIO_ID,
        "threshold_min": ALERT_THRESHOLD_MIN,
        "last_publish_min_ago": mins,
        "pending_new_items": pending,
        "newest_pending_title": newest,
        "alerted_active": st.get("alerted", False),
        "last_check": st.get("last_check"),
        "smtp_configured": bool(SMTP_USER and SMTP_PASS),
        "status": "OK" if (mins is None or mins < ALERT_THRESHOLD_MIN or not pending) else "FALLA",
    }
