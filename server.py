"""
Ruleta de la Probabilidad — servidor multijugador
==================================================
Proyecto de Estadística y Probabilidad — Casa Abierta (ULEAM)

Arquitectura:
  - Flask sirve tres páginas: TABLERO (proyectado), JUEGO (celulares) y ADMIN.
  - Base de datos DUAL:
      * Local  -> SQLite  (ruleta.db)
      * Render -> Postgres (usa la variable de entorno DATABASE_URL)
    Esto es clave: el disco de Render (plan free) es efímero y borraría el
    SQLite en cada reinicio. Postgres persiste de verdad.
  - Los celulares entran por internet (Render), no por IP local.

Cómo correrlo local:
  1. pip install -r requirements.txt
  2. python server.py
  3. Tablero: http://localhost:5000/   ·  Admin: http://localhost:5000/admin

Variables de entorno (Render):
  DATABASE_URL    -> la da Render al crear el Postgres
  ADMIN_PASSWORD  -> contraseña del panel admin (por defecto: Mendoza)
  PUBLIC_URL      -> ej. https://ruleta.onrender.com  (para que el QR apunte ahí)
"""

import os
import random
import socket
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps

from flask import (Flask, jsonify, render_template, request, g, send_file,
                   session, redirect, url_for)

APP_DIR = Path(__file__).parent

# ------------------------------------------------------------- configuración
STARTING_COINS = 10           # el ING pidió bajar el límite inicial a 10
REVEAL_DELAY = 5.5            # seg: los celulares no ven el resultado antes que el tablero
CHI2_CRITICAL = 5.991         # gl=2, alfa=0.05

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Mendoza")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):          # Render entrega el esquema viejo
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
IS_PG = DATABASE_URL.startswith("postgresql://")
DB_PATH = APP_DIR / "ruleta.db"

ORDER = [0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26]
RED_NUMS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ruleta-casa-abierta-uleam")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
    """SQLite usa ? como placeholder; Postgres usa %s. Escribimos siempre con ?."""
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
    """INSERT que devuelve el id nuevo, funcionando en ambos motores."""
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


SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    coins INTEGER NOT NULL DEFAULT 10,
    active INTEGER NOT NULL DEFAULT 1,
    coins_added INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number INTEGER NOT NULL,
    color TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    spin_id INTEGER REFERENCES spins(id),
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    payout INTEGER NOT NULL,
    stake INTEGER NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    won INTEGER NOT NULL DEFAULT 0,
    return_amount INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS players (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    coins INTEGER NOT NULL DEFAULT 10,
    active INTEGER NOT NULL DEFAULT 1,
    coins_added INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spins (
    id SERIAL PRIMARY KEY,
    number INTEGER NOT NULL,
    color TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bets (
    id SERIAL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id),
    spin_id INTEGER REFERENCES spins(id),
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    payout INTEGER NOT NULL,
    stake INTEGER NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    won INTEGER NOT NULL DEFAULT 0,
    return_amount INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def init_db():
    if IS_PG:
        import psycopg2
        db = psycopg2.connect(DATABASE_URL)
        cur = db.cursor()
        cur.execute(SCHEMA_PG)
        db.commit()
        cur.close()
        db.close()
    else:
        import sqlite3
        db = sqlite3.connect(DB_PATH)
        db.executescript(SCHEMA_SQLITE)
        db.commit()
        db.close()


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
    """URL que va en el QR: la pública de Render si existe, si no la IP local."""
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/jugar"
    return f"http://{local_ip()}:5000/jugar"


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
    name = (request.json or {}).get("name", "").strip()[:24]
    if not name:
        return jsonify(error="Nombre requerido"), 400
    row = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if row is None:
        insert_returning_id(
            "INSERT INTO players (name, coins, active, created_at) VALUES (?, ?, 1, ?)",
            (name, STARTING_COINS, now_iso()),
        )
        row = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    elif not row["active"]:
        # jugador de una ronda anterior que vuelve: se reactiva con saldo fresco
        execute("UPDATE players SET active = 1, coins = ? WHERE id = ?", (STARTING_COINS, row["id"]))
        row = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    return jsonify(id=row["id"], name=row["name"], coins=row["coins"])


@app.route("/api/bets", methods=["POST"])
def api_place_bets():
    """Recibe TODAS las apuestas confirmadas de un jugador en un solo envío."""
    data = request.json or {}
    name = data.get("name", "").strip()
    bets = data.get("bets") or []

    player = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if player is None:
        return jsonify(error="Jugador no encontrado"), 404

    already = query(
        "SELECT COUNT(*) AS c FROM bets WHERE player_id = ? AND resolved = 0",
        (player["id"],), one=True,
    )["c"]
    if already:
        return jsonify(error="Ya confirmaste tus apuestas para esta ronda"), 400

    clean, total = [], 0
    for b in bets:
        try:
            stake = int(b.get("stake", 0))
            payout = int(b.get("payout", 1))
        except (TypeError, ValueError):
            continue
        if stake < 1 or b.get("type") not in ("number", "color", "parity", "half", "dozen"):
            continue
        clean.append((b["type"], str(b.get("value")), payout, stake))
        total += stake

    if not clean:
        return jsonify(error="No hay apuestas válidas"), 400
    if total > player["coins"]:
        return jsonify(error="Monedas insuficientes"), 400

    now = now_iso()
    for btype, value, payout, stake in clean:
        execute(
            """INSERT INTO bets (player_id, spin_id, type, value, payout, stake, created_at)
               VALUES (?, NULL, ?, ?, ?, ?, ?)""",
            (player["id"], btype, value, payout, stake, now),
            commit=False,
        )
    execute("UPDATE players SET coins = coins - ? WHERE id = ?", (total, player["id"]))
    new_coins = query("SELECT coins FROM players WHERE id = ?", (player["id"],), one=True)["coins"]
    return jsonify(coins=new_coins, placed=len(clean), total=total)


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


@app.route("/api/spin", methods=["POST"])
def api_spin():
    number = random.choice(ORDER)
    color = color_of(number)
    spin_id = insert_returning_id(
        "INSERT INTO spins (number, color, created_at) VALUES (?, ?, ?)",
        (number, color, now_iso()),
    )

    pending = query("SELECT * FROM bets WHERE resolved = 0")
    for bet in pending:
        won = _bet_matches(bet["type"], bet["value"], number)
        ret = bet["stake"] * (bet["payout"] + 1) if won else 0
        execute(
            "UPDATE bets SET resolved = 1, won = ?, return_amount = ?, spin_id = ? WHERE id = ?",
            (1 if won else 0, ret, spin_id, bet["id"]),
            commit=False,
        )
        if ret:
            execute("UPDATE players SET coins = coins + ? WHERE id = ?",
                    (ret, bet["player_id"]), commit=False)
    get_db().commit()

    return jsonify(number=number, color=color, spin_id=spin_id, resolved_bets=len(pending))


@app.route("/api/state")
def api_state():
    """Lo consultan los celulares. Los resultados se revelan solo después de
    REVEAL_DELAY, para que nadie vea si ganó antes que el tablero proyectado."""
    name = request.args.get("name", "").strip()
    since_spin = int(request.args.get("since_spin", 0))
    player = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if player is None:
        return jsonify(error="Jugador no encontrado"), 404

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=REVEAL_DELAY)).isoformat()

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

    # historial visible para el jugador (lo pidió el ING: ver qué fichas han salido)
    hist_rows = query(
        "SELECT number, color FROM spins WHERE created_at <= ? ORDER BY id DESC LIMIT 15", (cutoff,)
    )
    history = [dict(number=r["number"], color=r["color"]) for r in hist_rows]

    return jsonify(
        coins=player["coins"] - int(hidden_gain),
        pending=[dict(r) for r in pending],
        resolved=[dict(r) for r in resolved],
        history=history,
        latest_spin_id=latest["id"] if latest else 0,
        latest_spin=(dict(number=latest["number"], color=latest["color"]) if latest else None),
    )


@app.route("/api/stats")
def api_stats():
    """Estadística descriptiva + inferencial + series para los gráficos."""
    spins = query("SELECT id, number, color, created_at FROM spins ORDER BY id")
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

    # --- serie de convergencia (ley de los grandes números): gráfico de línea
    convergence = []
    r = b = 0
    for i, s in enumerate(spins, start=1):
        if s["color"] == "rojo":
            r += 1
        elif s["color"] == "negro":
            b += 1
        convergence.append(
            dict(spin=i, red_pct=round(r / i * 100, 2), black_pct=round(b / i * 100, 2))
        )

    # --- tabla de resultados: últimos giros con hora
    results_table = [
        dict(spin=s["id"], number=s["number"], color=s["color"], time=str(s["created_at"])[11:19])
        for s in spins[-15:]
    ][::-1]

    active_players = query("SELECT COUNT(*) AS c FROM players WHERE active = 1", one=True)["c"]
    history = [dict(number=s["number"], color=s["color"]) for s in spins[-20:]][::-1]

    return jsonify(
        total=total,
        red=red, black=black, green=green,
        red_pct=round(red / total * 100, 1) if total else 0,
        black_pct=round(black / total * 100, 1) if total else 0,
        green_pct=round(green / total * 100, 1) if total else 0,
        dozens=dozens,
        chi2=round(chi2, 3) if chi2 is not None else None,
        chi2_critical=CHI2_CRITICAL,
        verdict=verdict,
        active_players=active_players,
        history=history,
        convergence=convergence,
        results_table=results_table,
    )


@app.route("/api/players")
def api_players():
    """Jugadores ACTIVOS de la ronda actual — alimenta el gráfico de barras."""
    rows = query(
        """SELECT p.id, p.name, p.coins, p.coins_added,
                  COALESCE(SUM(b.stake), 0) AS staked,
                  COALESCE(SUM(b.return_amount), 0) AS returned,
                  COUNT(b.id) AS bets_count,
                  COALESCE(SUM(b.won), 0) AS bets_won
           FROM players p
           LEFT JOIN bets b ON b.player_id = p.id AND b.resolved = 1
           WHERE p.active = 1
           GROUP BY p.id, p.name, p.coins, p.coins_added
           ORDER BY p.coins DESC, p.name ASC"""
    )
    players = []
    for r in rows:
        staked, returned = int(r["staked"]), int(r["returned"])
        players.append(dict(
            id=r["id"], name=r["name"], coins=r["coins"],
            staked=staked, returned=returned,
            net=returned - staked,                     # ganancia/pérdida neta
            bets_count=int(r["bets_count"]), bets_won=int(r["bets_won"]),
            coins_added=int(r["coins_added"]),
        ))
    return jsonify(players=players)


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
        dict(name=r["name"], coins=r["coins"], active=bool(r["active"]),
             joined=str(r["created_at"])[:19].replace("T", " "),
             coins_added=int(r["coins_added"]),
             bets_count=int(r["bets_count"]), bets_won=int(r["bets_won"]))
        for r in rows
    ], total=len(rows))


@app.route("/api/leaderboard")
def api_leaderboard():
    rows = query("SELECT name, coins FROM players WHERE active = 1 ORDER BY coins DESC, name ASC LIMIT 10")
    return jsonify(leaders=[dict(name=r["name"], coins=r["coins"]) for r in rows])


# ------------------------------------------------------------- admin actions
@app.route("/api/admin/add_coins", methods=["POST"])
@admin_required
def api_add_coins():
    """Recarga GRATUITA de monedas. El admin decide la cantidad, sin tope.
    Se permiten cantidades negativas para corregir un error de tipeo,
    pero el saldo nunca baja de 0."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify(error="Cantidad inválida"), 400
    if amount == 0:
        return jsonify(error="La cantidad no puede ser 0"), 400

    player = query("SELECT * FROM players WHERE name = ?", (name,), one=True)
    if player is None:
        return jsonify(error="Jugador no encontrado"), 404

    # si es un descuento, no dejamos el saldo por debajo de 0
    delta = amount
    if delta < 0:
        delta = max(delta, -player["coins"])

    execute(
        "UPDATE players SET coins = coins + ?, coins_added = coins_added + ? WHERE id = ?",
        (delta, delta, player["id"]),
    )
    new_coins = query("SELECT coins FROM players WHERE id = ?", (player["id"],), one=True)["coins"]
    return jsonify(ok=True, name=name, coins=new_coins, added=delta)


@app.route("/api/admin/reset", methods=["POST"])
@admin_required
def api_reset():
    """Nueva ronda: limpia giros y apuestas (tabla + gráficos vuelven a cero)
    y desactiva a los jugadores actuales. NO borra el registro histórico."""
    execute("DELETE FROM bets", commit=False)
    execute("DELETE FROM spins", commit=False)
    execute("UPDATE players SET active = 0, coins = ?", (STARTING_COINS,))
    return jsonify(ok=True)


@app.route("/api/admin/wipe", methods=["POST"])
@admin_required
def api_wipe():
    """Borrado TOTAL, incluido el registro histórico. Usar con cuidado."""
    execute("DELETE FROM bets", commit=False)
    execute("DELETE FROM spins", commit=False)
    execute("DELETE FROM players")
    return jsonify(ok=True)


init_db()   # se ejecuta también bajo gunicorn (Render), no solo en __main__

if __name__ == "__main__":
    ip = local_ip()
    print("\n" + "=" * 60)
    print("  RULETA DE LA PROBABILIDAD — servidor iniciado")
    print("=" * 60)
    print(f"  Motor de BD:   {'Postgres (persistente)' if IS_PG else 'SQLite local'}")
    print(f"  Tablero:       http://{ip}:5000/")
    print(f"  Juego:         {join_url()}")
    print(f"  Admin:         http://{ip}:5000/admin   (clave: {ADMIN_PASSWORD})")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
