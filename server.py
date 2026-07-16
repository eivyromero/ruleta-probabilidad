"""
Ruleta de la Probabilidad — servidor multijugador
==================================================
Proyecto de Estadística y Probabilidad — Casa Abierta (ULEAM)

IMPORTANTE: el dinero de este juego es VIRTUAL. Nadie paga por jugar; el saldo
inicial se otorga gratis y los premios son simbólicos. La app solo sirve para
demostrar conceptos de probabilidad con datos generados en vivo.

Arquitectura:
  - Flask sirve tres páginas: TABLERO (proyectado), JUEGO (celulares) y ADMIN.
  - Base de datos DUAL:  local -> SQLite ;  Render -> Postgres (DATABASE_URL)

Variables de entorno (Render):
  DATABASE_URL, ADMIN_PASSWORD, PUBLIC_URL, PYTHON_VERSION
"""

import os
import random
import secrets
import socket
import threading
import time
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps

from flask import (Flask, jsonify, render_template, request, g, send_file,
                   session, redirect, url_for)

APP_DIR = Path(__file__).parent

# ------------------------------------------------------------- configuración
STARTING_BALANCE = 5.00       # saldo virtual de bienvenida (dólares de juego)
PRIZE_THRESHOLD = 10.00       # a partir de aquí el jugador gana un premio simbólico

# --- Ciclo automático (lo manda el reloj del servidor, no un botón) ---
#     [ giro 25 s ] -> resultado -> [ apuestas 15 s ] -> giro -> ...
SPIN_DURATION = 25.0          # seg que la rueda gira antes de revelar el número
BETTING_WINDOW = 15.0         # seg con las apuestas abiertas entre giro y giro
CYCLE = SPIN_DURATION + BETTING_WINDOW
REVEAL_DELAY = SPIN_DURATION  # el celular no ve el resultado antes que el tablero
GLOW_TIME = 3.0               # seg que el número ganador brilla en el tablero de apuestas
CHI2_CRITICAL = 5.991         # gl=2, alfa=0.05

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Mendoza")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
IS_PG = DATABASE_URL.startswith("postgresql://")
DB_PATH = APP_DIR / "ruleta.db"

ORDER = [0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26]
RED_NUMS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ruleta-casa-abierta-uleam")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def money(v):
    """Redondea a 2 decimales y evita -0.0."""
    return round(float(v or 0) + 0.0, 2)


# ------------------------------------------------------------------ database
def get_db():
    if "db" not in g:
        if IS_PG:
            import psycopg2
            import psycopg2.extras
            g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            import sqlite3
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _sql(sql):
    return sql.replace("?", "%s") if IS_PG else sql


def query(sql, params=(), one=False):
    db = get_db()
    cur = db.cursor()
    cur.execute(_sql(sql), params)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=(), commit=True):
    db = get_db()
    cur = db.cursor()
    cur.execute(_sql(sql), params)
    cur.close()
    if commit:
        db.commit()


def insert_returning_id(sql, params):
    db = get_db()
    cur = db.cursor()
    if IS_PG:
        cur.execute(_sql(sql + " RETURNING id"), params)
        new_id = cur.fetchone()["id"]
    else:
        cur.execute(_sql(sql), params)
        new_id = cur.lastrowid
    cur.close()
    db.commit()
    return new_id


MONEY_T = "DOUBLE PRECISION" if IS_PG else "REAL"
SERIAL_T = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS players (
    id {SERIAL_T},
    name TEXT UNIQUE NOT NULL,
    coins {MONEY_T} NOT NULL DEFAULT 5,
    active INTEGER NOT NULL DEFAULT 1,
    coins_added {MONEY_T} NOT NULL DEFAULT 0,
    token TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spins (
    id {SERIAL_T},
    number INTEGER NOT NULL,
    color TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bets (
    id {SERIAL_T},
    player_id INTEGER NOT NULL REFERENCES players(id),
    spin_id INTEGER REFERENCES spins(id),
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    payout INTEGER NOT NULL,
    stake {MONEY_T} NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    won INTEGER NOT NULL DEFAULT 0,
    return_amount {MONEY_T} NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Migraciones: la versión anterior guardaba las columnas de dinero como INTEGER.
# Al pasar a dólares con decimales hay que convertirlas. SQLite es de tipado
# dinámico y no lo necesita; Postgres sí.
PG_MIGRATIONS = [
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS token TEXT",
    "ALTER TABLE players ALTER COLUMN coins TYPE DOUBLE PRECISION USING coins::double precision",
    "ALTER TABLE players ALTER COLUMN coins_added TYPE DOUBLE PRECISION USING coins_added::double precision",
    "ALTER TABLE bets ALTER COLUMN stake TYPE DOUBLE PRECISION USING stake::double precision",
    "ALTER TABLE bets ALTER COLUMN return_amount TYPE DOUBLE PRECISION USING return_amount::double precision",
    "ALTER TABLE players ALTER COLUMN coins SET DEFAULT 5",
]


def init_db():
    if IS_PG:
        import psycopg2
        db = psycopg2.connect(DATABASE_URL)
        cur = db.cursor()
        cur.execute(SCHEMA)
        db.commit()
        for stmt in PG_MIGRATIONS:
            try:
                cur.execute(stmt)
                db.commit()
            except Exception:
                db.rollback()      # ya estaba migrada: seguimos
        cur.close()
        db.close()
    else:
        import sqlite3
        db = sqlite3.connect(DB_PATH)
        db.executescript(SCHEMA)
        db.commit()
        try:                                   # base creada antes de los tokens
            db.execute("ALTER TABLE players ADD COLUMN token TEXT")
            db.commit()
        except sqlite3.OperationalError:
            pass                               # ya existía
        db.close()


def get_setting(key, default=""):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    if query("SELECT 1 FROM settings WHERE key = ?", (key,), one=True):
        execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), key))
    else:
        execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


def is_unlimited():
    return get_setting("unlimited", "0") == "1"


def color_of(num: int) -> str:
    if num == 0:
        return "verde"
    return "rojo" if num in RED_NUMS else "negro"


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def join_url() -> str:
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/jugar"
    return f"http://{local_ip()}:5000/jugar"


def cycle_info():
    """El reloj del ciclo vive en el SERVIDOR, para que el tablero proyectado y
    todos los celulares vean exactamente el mismo contador.

        [ girando 25 s ] --> resultado --> [ apuestas 15 s ] --> girando ...

    Se apoya en dos datos: cuándo fue el último giro y cuándo toca el siguiente.
    """
    now = datetime.now(timezone.utc)

    raw = get_setting("next_spin_at", "")
    nsa = None
    if raw:
        try:
            nsa = datetime.fromisoformat(raw)
            if nsa.tzinfo is None:
                nsa = nsa.replace(tzinfo=timezone.utc)
        except ValueError:
            nsa = None
    if nsa is None:                       # arranque en frío o tras un reinicio
        nsa = now + timedelta(seconds=BETTING_WINDOW)
        set_setting("next_spin_at", nsa.isoformat())
    elif (now - nsa).total_seconds() > 90:
        # El plan gratuito de Render duerme el servidor tras un rato sin visitas.
        # Al despertar no tiene sentido girar de golpe: damos ventana de apuestas.
        nsa = now + timedelta(seconds=BETTING_WINDOW)
        set_setting("next_spin_at", nsa.isoformat())

    last = query("SELECT id, number, color, created_at FROM spins ORDER BY id DESC LIMIT 1", one=True)
    spin_elapsed = None
    if last:
        try:
            t = datetime.fromisoformat(str(last["created_at"]))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            spin_elapsed = (now - t).total_seconds()
        except ValueError:
            spin_elapsed = None

    # Fase 1: la rueda está girando y el número todavía no se revela.
    # Al tablero SÍ le decimos el número: es quien tiene que animar la rueda hasta
    # esa casilla. A los celulares no (usan /api/state, que respeta REVEAL_DELAY).
    if spin_elapsed is not None and spin_elapsed < SPIN_DURATION:
        return dict(phase="spinning", remaining=round(SPIN_DURATION - spin_elapsed, 1),
                    betting_open=False, due=False,
                    current_spin=dict(id=last["id"], number=last["number"],
                                      color=last["color"], elapsed=round(spin_elapsed, 2)))

    # Fase 2: resultado en pantalla y ventana de apuestas abierta
    remaining = (nsa - now).total_seconds()
    return dict(phase="betting", remaining=round(max(remaining, 0.0), 1),
                betting_open=remaining > 0, due=remaining <= 0)


def schedule_next_spin():
    set_setting("next_spin_at",
                (datetime.now(timezone.utc) + timedelta(seconds=CYCLE)).isoformat())


def betting_state():
    c = cycle_info()
    return c["betting_open"], c["remaining"]


# --------------------------------------------------------------------- pages
@app.route("/")
def dashboard():
    return render_template("dashboard.html", join_url=join_url())


@app.route("/jugar")
def jugar():
    return render_template("jugar.html")


@app.route("/qr.png")
def qr_png():
    import qrcode
    from io import BytesIO

    img = qrcode.make(join_url(), box_size=10, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ---------------------------------------------------------------------- auth
def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify(error="No autorizado"), 401
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_panel"))
        return render_template("admin_login.html", error="Contraseña incorrecta")
    if session.get("is_admin"):
        return redirect(url_for("admin_panel"))
    return render_template("admin_login.html", error=None)


@app.route("/admin/panel")
@admin_required
def admin_panel():
    return render_template("admin.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


# ----------------------------------------------------------------------- api
@app.route("/api/join", methods=["POST"])
def api_join():
    """Cada jugador recibe un token privado. Sirve para que pueda recargar su
    página sin perder la sesión, y a la vez para que NADIE más pueda entrar con
    un nombre que ya está ocupado."""
    data = request.json or {}
    name = data.get("name", "").strip()[:24]
    token = (data.get("token") or "").strip()
    if not name:
        return jsonify(error="Nombre requerido"), 400

    row = query("SELECT * FROM players WHERE name = ?", (name,), one=True)

    if row is None:                       # nombre libre: bono de bienvenida
        new_token = secrets.token_hex(8)
        insert_returning_id(
            "INSERT INTO players (name, coins, active, token, created_at) VALUES (?, ?, 1, ?, ?)",
            (name, STARTING_BALANCE, new_token, now_iso()),
        )
        row = query("SELECT * FROM players WHERE name = ?", (name,), one=True)

    elif not row["active"]:               # volvió tras un reinicio de ronda
        new_token = secrets.token_hex(8)
        execute("UPDATE players SET active = 1, coins = ?, token = ? WHERE id = ?",
                (STARTING_BALANCE, new_token, row["id"]))
        row = query("SELECT * FROM players WHERE name = ?", (name,), one=True)

    else:                                 # el nombre está en uso ahora mismo
        if not token or token != (row["token"] or ""):
            return jsonify(error="Nombre de usuario en uso"), 409

    return jsonify(id=row["id"], name=row["name"], balance=money(row["coins"]),
                   token=row["token"], unlimited=is_unlimited())


@app.route("/api/bets", methods=["POST"])
def api_place_bets():
    """Recibe TODAS las apuestas confirmadas de un jugador en un solo envío."""
    data = request.json or {}
    name = data.get("name", "").strip()
    bets = data.get("bets") or []

    open_now, remaining = betting_state()
    if not open_now:
        return jsonify(error=f"La ruleta está girando — espera el resultado ({remaining:.0f}s)"), 409

    player = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if player is None:
        return jsonify(error="Jugador no encontrado"), 404

    already = query(
        "SELECT COUNT(*) AS c FROM bets WHERE player_id = ? AND resolved = 0",
        (player["id"],), one=True,
    )["c"]
    if already:
        return jsonify(error="Ya confirmaste tus apuestas para esta ronda"), 400

    clean, total = [], 0.0
    for b in bets:
        try:
            stake = money(b.get("stake", 0))
            payout = int(b.get("payout", 1))
        except (TypeError, ValueError):
            continue
        if stake < 0.01 or b.get("type") not in ("number", "color", "parity", "half", "dozen"):
            continue
        clean.append((b["type"], str(b.get("value")), payout, stake))
        total += stake
    total = money(total)

    if not clean:
        return jsonify(error="No hay apuestas válidas"), 400

    unlimited = is_unlimited()
    balance = money(player["coins"])
    if total > balance:
        if not unlimited:
            return jsonify(error="Saldo insuficiente"), 400
        # Modo ilimitado: la casa cubre la diferencia para que nadie se quede fuera.
        gap = money(total - balance)
        execute("UPDATE players SET coins = coins + ?, coins_added = coins_added + ? WHERE id = ?",
                (gap, gap, player["id"]))

    now = now_iso()
    for btype, value, payout, stake in clean:
        execute(
            """INSERT INTO bets (player_id, spin_id, type, value, payout, stake, created_at)
               VALUES (?, NULL, ?, ?, ?, ?, ?)""",
            (player["id"], btype, value, payout, stake, now),
            commit=False,
        )
    execute("UPDATE players SET coins = coins - ? WHERE id = ?", (total, player["id"]))
    new_balance = query("SELECT coins FROM players WHERE id = ?", (player["id"],), one=True)["coins"]
    return jsonify(balance=money(new_balance), placed=len(clean), total=total)


def _bet_matches(bet_type, value, number):
    c = color_of(number)
    if bet_type == "number":
        return int(value) == number
    if bet_type == "color":
        return c == value
    if bet_type == "parity":
        return number != 0 and (("par" if number % 2 == 0 else "impar") == value)
    if bet_type == "half":
        return number != 0 and (("bajo" if number <= 18 else "alto") == value)
    if bet_type == "dozen":
        if number == 0:
            return False
        d = math.ceil(number / 12)
        return {"d1": 1, "d2": 2, "d3": 3}.get(value) == d
    return False


def perform_spin():
    """Ejecuta un giro: saca el número, resuelve todas las apuestas pendientes y
    programa el siguiente giro."""
    number = random.choice(ORDER)
    color = color_of(number)
    spin_id = insert_returning_id(
        "INSERT INTO spins (number, color, created_at) VALUES (?, ?, ?)",
        (number, color, now_iso()),
    )

    pending = query("SELECT * FROM bets WHERE resolved = 0")
    for bet in pending:
        won = _bet_matches(bet["type"], bet["value"], number)
        ret = money(bet["stake"] * (bet["payout"] + 1)) if won else 0.0
        execute(
            "UPDATE bets SET resolved = 1, won = ?, return_amount = ?, spin_id = ? WHERE id = ?",
            (1 if won else 0, ret, spin_id, bet["id"]),
            commit=False,
        )
        if ret:
            execute("UPDATE players SET coins = coins + ? WHERE id = ?",
                    (ret, bet["player_id"]), commit=False)
    get_db().commit()
    schedule_next_spin()

    return dict(number=number, color=color, spin_id=spin_id, resolved_bets=len(pending))


def _auto_spin_loop():
    """EL CORAZÓN DEL JUEGO. La ruleta gira sola desde el servidor, no desde el
    navegador. Así el juego sigue vivo aunque el tablero se recargue, se cierre
    o la laptop se duerma: al volver, el tablero se re-sincroniza solo."""
    while True:
        try:
            with app.app_context():
                if cycle_info()["due"]:
                    perform_spin()
        except Exception as e:
            print(f"[auto-spin] {e}")
        time.sleep(0.4)


@app.route("/api/spin", methods=["POST"])
def api_spin():
    """Giro manual de emergencia. Normalmente NO se usa: el ciclo automático del
    servidor se encarga. Queda por si hay que forzar un giro."""
    c = cycle_info()
    if not c["due"]:
        return jsonify(error=f"Aún no toca girar ({c['phase']}, faltan {c['remaining']}s)"), 409
    res = perform_spin()
    res.update(spin_duration=SPIN_DURATION, betting_window=BETTING_WINDOW)
    return jsonify(res)


def _reveal_cutoff():
    return (datetime.now(timezone.utc) - timedelta(seconds=REVEAL_DELAY)).isoformat()


@app.route("/api/state")
def api_state():
    """Lo consultan los celulares. Los resultados se revelan solo después de
    REVEAL_DELAY, para que nadie vea si ganó antes que el tablero proyectado."""
    name = request.args.get("name", "").strip()
    since_spin = int(request.args.get("since_spin", 0))
    player = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if player is None:
        return jsonify(error="Jugador no encontrado"), 404

    cutoff = _reveal_cutoff()
    cyc = cycle_info()

    pending = query(
        """SELECT b.id, b.type, b.value, b.stake FROM bets b
           LEFT JOIN spins s ON b.spin_id = s.id
           WHERE b.player_id = ? AND (b.resolved = 0 OR s.created_at > ?)""",
        (player["id"], cutoff),
    )
    resolved = query(
        """SELECT b.type, b.value, b.won, b.return_amount, b.stake, b.spin_id FROM bets b
           JOIN spins s ON b.spin_id = s.id
           WHERE b.player_id = ? AND b.resolved = 1 AND s.created_at <= ? AND b.spin_id > ?
           ORDER BY b.spin_id DESC""",
        (player["id"], cutoff, since_spin),
    )
    hidden_gain = query(
        """SELECT COALESCE(SUM(b.return_amount), 0) AS g FROM bets b
           JOIN spins s ON b.spin_id = s.id
           WHERE b.player_id = ? AND b.resolved = 1 AND s.created_at > ?""",
        (player["id"], cutoff), one=True,
    )["g"]

    latest = query("SELECT * FROM spins WHERE created_at <= ? ORDER BY id DESC LIMIT 1",
                   (cutoff,), one=True)
    hist_rows = query(
        "SELECT number, color FROM spins WHERE created_at <= ? ORDER BY id DESC LIMIT 15", (cutoff,)
    )
    balance = money(player["coins"] - float(hidden_gain or 0))

    return jsonify(
        balance=balance,
        pending=[dict(r) for r in pending],
        resolved=[dict(r) for r in resolved],
        history=[dict(number=r["number"], color=r["color"]) for r in hist_rows],
        latest_spin_id=latest["id"] if latest else 0,
        latest_spin=(dict(number=latest["number"], color=latest["color"]) if latest else None),
        betting_open=cyc["betting_open"],
        phase=cyc["phase"],
        remaining=cyc["remaining"],
        unlimited=is_unlimited(),
        prize=balance >= PRIZE_THRESHOLD,
        prize_threshold=PRIZE_THRESHOLD,
        winners=_winners_payload(cutoff),
    )


def _winners_payload(cutoff=None):
    """Ganadores del último giro ya revelado: nombre y cuánto ganó.
    Es lo que se muestra a los costados para motivar a la gente."""
    if cutoff is None:
        cutoff = _reveal_cutoff()
    last = query("SELECT id, number, color FROM spins WHERE created_at <= ? ORDER BY id DESC LIMIT 1",
                 (cutoff,), one=True)
    if not last:
        return dict(spin_id=0, count=0, total=0.0, list=[], number=None)
    rows = query(
        """SELECT p.name,
                  COALESCE(SUM(b.return_amount), 0) AS won_amount,
                  MAX(CASE WHEN b.type = 'number' AND b.value = '0' THEN 1 ELSE 0 END) AS jackpot,
                  MAX(p.coins) AS balance
           FROM bets b JOIN players p ON p.id = b.player_id
           WHERE b.spin_id = ? AND b.won = 1
           GROUP BY p.name
           ORDER BY won_amount DESC
           LIMIT 12""",
        (last["id"],),
    )
    lst = [dict(name=r["name"], amount=money(r["won_amount"]),
                jackpot=bool(r["jackpot"]), prize=money(r["balance"]) >= PRIZE_THRESHOLD)
           for r in rows]
    return dict(spin_id=last["id"], number=last["number"], color=last["color"],
                count=len(lst), total=money(sum(x["amount"] for x in lst)), list=lst)


@app.route("/api/winners")
def api_winners():
    return jsonify(_winners_payload())


@app.route("/api/cycle")
def api_cycle():
    """Lo consultan el tablero y los celulares para ir todos al mismo compás."""
    c = cycle_info()
    c.update(spin_duration=SPIN_DURATION, betting_window=BETTING_WINDOW,
             unlimited=is_unlimited())
    return jsonify(c)


@app.route("/api/settings")
def api_settings():
    c = cycle_info()
    return jsonify(unlimited=is_unlimited(), betting_open=c["betting_open"],
                   phase=c["phase"], remaining=c["remaining"],
                   spin_duration=SPIN_DURATION, betting_window=BETTING_WINDOW,
                   starting_balance=STARTING_BALANCE, prize_threshold=PRIZE_THRESHOLD)


@app.route("/api/stats")
def api_stats():
    """Estadística descriptiva + inferencial + series para los gráficos.

    IMPORTANTE: solo cuenta los giros YA REVELADOS. Si contara el giro en curso,
    los porcentajes cambiarían antes de que la rueda se detenga y delatarían el
    color que va a salir."""
    cutoff = _reveal_cutoff()
    spins = query("SELECT id, number, color, created_at FROM spins WHERE created_at <= ? ORDER BY id",
                  (cutoff,))
    total = len(spins)
    red = sum(1 for s in spins if s["color"] == "rojo")
    black = sum(1 for s in spins if s["color"] == "negro")
    green = sum(1 for s in spins if s["color"] == "verde")

    dozens = {"d1": 0, "d2": 0, "d3": 0, "zero": 0}
    for s in spins:
        n = s["number"]
        if n == 0:
            dozens["zero"] += 1
        elif n <= 12:
            dozens["d1"] += 1
        elif n <= 24:
            dozens["d2"] += 1
        else:
            dozens["d3"] += 1

    chi2 = verdict = None
    if total >= 5:
        expected = {"rojo": total * 18 / 37, "negro": total * 18 / 37, "verde": total * 1 / 37}
        observed = {"rojo": red, "negro": black, "verde": green}
        chi2 = sum((observed[k] - expected[k]) ** 2 / expected[k] for k in expected)
        verdict = "normal" if chi2 < CHI2_CRITICAL else "sesgo"

    convergence = []
    r = b = 0
    for i, s in enumerate(spins, start=1):
        if s["color"] == "rojo":
            r += 1
        elif s["color"] == "negro":
            b += 1
        convergence.append(dict(spin=i, red_pct=round(r / i * 100, 2), black_pct=round(b / i * 100, 2)))

    freq_numbers = [0] * 37
    for s in spins:
        freq_numbers[s["number"]] += 1
    exp_mean = exp_std = None
    if total > 0:
        exp_mean = sum(x * freq_numbers[x] for x in range(37)) / total
        e_x2 = sum(x * x * freq_numbers[x] for x in range(37)) / total
        exp_std = math.sqrt(max(e_x2 - exp_mean ** 2, 0))

    active_players = query("SELECT COUNT(*) AS c FROM players WHERE active = 1", one=True)["c"]
    resolved_bets = query(
        """SELECT COUNT(*) AS c FROM bets b JOIN spins s ON b.spin_id = s.id
           WHERE b.resolved = 1 AND s.created_at <= ?""", (cutoff,), one=True)["c"]

    return jsonify(
        total=total, red=red, black=black, green=green,
        red_pct=round(red / total * 100, 1) if total else 0,
        black_pct=round(black / total * 100, 1) if total else 0,
        green_pct=round(green / total * 100, 1) if total else 0,
        dozens=dozens,
        chi2=round(chi2, 3) if chi2 is not None else None,
        chi2_critical=CHI2_CRITICAL, verdict=verdict,
        active_players=active_players,
        resolved_bets=resolved_bets,
        convergence=convergence,
        freq_numbers=freq_numbers,
        exp_mean=round(exp_mean, 2) if exp_mean is not None else None,
        exp_std=round(exp_std, 2) if exp_std is not None else None,
    )


def _hidden_gains(cutoff):
    """Cuánto ha ganado cada jugador en giros que la rueda todavía no termina de
    mostrar. Se resta del saldo para que ni la tabla de líderes ni el gráfico
    revelen el resultado antes de tiempo."""
    rows = query(
        """SELECT b.player_id AS pid, COALESCE(SUM(b.return_amount), 0) AS g
           FROM bets b JOIN spins s ON b.spin_id = s.id
           WHERE b.resolved = 1 AND s.created_at > ?
           GROUP BY b.player_id""", (cutoff,))
    return {r["pid"]: float(r["g"] or 0) for r in rows}


@app.route("/api/players")
def api_players():
    """Jugadores ACTIVOS de la ronda actual — alimenta el gráfico de barras."""
    cutoff = _reveal_cutoff()
    hidden = _hidden_gains(cutoff)
    rows = query(
        """SELECT p.id, p.name, p.coins, p.coins_added,
                  COALESCE(SUM(b.stake), 0) AS staked,
                  COALESCE(SUM(b.return_amount), 0) AS returned,
                  COUNT(b.id) AS bets_count,
                  COALESCE(SUM(b.won), 0) AS bets_won
           FROM players p
           LEFT JOIN bets b ON b.player_id = p.id AND b.resolved = 1
                AND b.spin_id IN (SELECT id FROM spins WHERE created_at <= ?)
           WHERE p.active = 1
           GROUP BY p.id, p.name, p.coins, p.coins_added
           ORDER BY p.coins DESC, p.name ASC""", (cutoff,)
    )
    players = []
    for r in rows:
        staked, returned = money(r["staked"]), money(r["returned"])
        visible = money(float(r["coins"]) - hidden.get(r["id"], 0))
        players.append(dict(
            id=r["id"], name=r["name"], balance=visible,
            staked=staked, returned=returned, net=money(returned - staked),
            bets_count=int(r["bets_count"]), bets_won=int(r["bets_won"]),
            funds_added=money(r["coins_added"]),
            prize=visible >= PRIZE_THRESHOLD,
        ))
    players.sort(key=lambda x: (-x["balance"], x["name"]))
    return jsonify(players=players, prize_threshold=PRIZE_THRESHOLD)


@app.route("/api/registry")
def api_registry():
    """Registro histórico: TODOS los que han participado, incluso rondas pasadas."""
    rows = query(
        """SELECT p.id, p.name, p.coins, p.active, p.created_at, p.coins_added,
                  COUNT(b.id) AS bets_count,
                  COALESCE(SUM(b.won), 0) AS bets_won
           FROM players p
           LEFT JOIN bets b ON b.player_id = p.id
           GROUP BY p.id, p.name, p.coins, p.active, p.created_at, p.coins_added
           ORDER BY p.created_at DESC"""
    )
    return jsonify(registry=[
        dict(name=r["name"], balance=money(r["coins"]), active=bool(r["active"]),
             joined=str(r["created_at"])[:19].replace("T", " "),
             funds_added=money(r["coins_added"]),
             bets_count=int(r["bets_count"]), bets_won=int(r["bets_won"]))
        for r in rows
    ], total=len(rows))


@app.route("/api/leaderboard")
def api_leaderboard():
    cutoff = _reveal_cutoff()
    hidden = _hidden_gains(cutoff)
    rows = query("SELECT id, name, coins FROM players WHERE active = 1")
    leaders = []
    for r in rows:
        visible = money(float(r["coins"]) - hidden.get(r["id"], 0))
        leaders.append(dict(name=r["name"], balance=visible, prize=visible >= PRIZE_THRESHOLD))
    leaders.sort(key=lambda x: (-x["balance"], x["name"]))
    return jsonify(leaders=leaders[:10], prize_threshold=PRIZE_THRESHOLD)


# ------------------------------------------------------------- admin actions
@app.route("/api/admin/add_funds", methods=["POST"])
@admin_required
def api_add_funds():
    """Recarga GRATUITA de saldo. El admin decide el monto, sin tope.
    Acepta negativos para corregir; el saldo nunca baja de 0."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    try:
        amount = money(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify(error="Monto inválido"), 400
    if amount == 0:
        return jsonify(error="El monto no puede ser 0"), 400

    player = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if player is None:
        return jsonify(error="Jugador no encontrado"), 404

    delta = amount
    if delta < 0:
        delta = money(max(delta, -money(player["coins"])))

    execute("UPDATE players SET coins = coins + ?, coins_added = coins_added + ? WHERE id = ?",
            (delta, delta, player["id"]))
    new_balance = query("SELECT coins FROM players WHERE id = ?", (player["id"],), one=True)["coins"]
    return jsonify(ok=True, name=name, balance=money(new_balance), added=delta)


@app.route("/api/admin/unlimited", methods=["POST"])
@admin_required
def api_toggle_unlimited():
    """Modo juego ilimitado: los jugadores pueden apostar el monto que quieran;
    si supera su saldo, la casa cubre la diferencia automáticamente."""
    data = request.json or {}
    enabled = bool(data.get("enabled"))
    set_setting("unlimited", "1" if enabled else "0")
    return jsonify(ok=True, unlimited=enabled)


@app.route("/api/admin/delete_player", methods=["POST"])
@admin_required
def api_delete_player():
    """Elimina al jugador por completo. Si vuelve a entrar con el mismo nombre
    se le trata como alguien nuevo: recibe otra vez el bono de bienvenida."""
    name = (request.json or {}).get("name", "").strip()
    player = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if player is None:
        return jsonify(error="Jugador no encontrado"), 404
    execute("DELETE FROM bets WHERE player_id = ?", (player["id"],), commit=False)
    execute("DELETE FROM players WHERE id = ?", (player["id"],))
    return jsonify(ok=True, name=name)


@app.route("/api/admin/reset", methods=["POST"])
@admin_required
def api_reset():
    """Nueva ronda: limpia giros y apuestas y desactiva a los jugadores actuales.
    NO borra el registro histórico."""
    execute("DELETE FROM bets", commit=False)
    execute("DELETE FROM spins", commit=False)
    execute("DELETE FROM settings WHERE key = 'next_spin_at'", commit=False)
    execute("UPDATE players SET active = 0, coins = ?", (STARTING_BALANCE,))
    return jsonify(ok=True)


@app.route("/api/admin/wipe", methods=["POST"])
@admin_required
def api_wipe():
    """Borrado TOTAL, incluido el registro histórico."""
    execute("DELETE FROM bets", commit=False)
    execute("DELETE FROM spins", commit=False)
    execute("DELETE FROM settings WHERE key = 'next_spin_at'", commit=False)
    execute("DELETE FROM players")
    return jsonify(ok=True)


init_db()   # se ejecuta también bajo gunicorn (Render), no solo en __main__

# El hilo del ciclo arranca junto con la app (también bajo gunicorn, que usa
# 1 worker por defecto: un solo hilo girando, sin giros duplicados).
threading.Thread(target=_auto_spin_loop, daemon=True).start()

if __name__ == "__main__":
    ip = local_ip()
    print("\n" + "=" * 60)
    print("  RULETA DE LA PROBABILIDAD — servidor iniciado")
    print("=" * 60)
    print(f"  Motor de BD:   {'Postgres (persistente)' if IS_PG else 'SQLite local'}")
    print(f"  Tablero:       http://{ip}:5000/")
    print(f"  Juego:         {join_url()}")
    print(f"  Admin:         http://{ip}:5000/admin   (clave: {ADMIN_PASSWORD})")
    print(f"  Ciclo:         {SPIN_DURATION:.0f}s girando + {BETTING_WINDOW:.0f}s de apuestas (automático)")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)