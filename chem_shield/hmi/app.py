"""Minimal Flask operator panel for the isolated virtual lab."""

import argparse
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.security import check_password_hash, generate_password_hash
from chem_shield.communication.device_connection import DeviceConnection
from chem_shield.data.store import TelemetryStore
from chem_shield.events.abnormal_events import write_event


AUTH_PAGE_DIR = Path(__file__).resolve().parents[2] / "the login"
ADMIN_PAGE_DIR = Path(__file__).resolve().parents[1] / "login page"


def create_app(
    plc_host="127.0.0.1",
    plc_port=5020,
    database="chem_shield/data/telemetry.db",
    auth_database="chem_shield/data/auth.db",
    admin_email=None,
    admin_username=None,
    admin_password=None,
):
    admin_email = (admin_email or os.environ.get("CHEMSHIELD_ADMIN_EMAIL", "")).strip().lower()
    admin_username = (admin_username or os.environ.get("CHEMSHIELD_ADMIN_USERNAME", "")).strip()
    admin_password = admin_password or os.environ.get("CHEMSHIELD_ADMIN_PASSWORD", "")
    if not admin_email or not admin_username or not admin_password:
        raise RuntimeError(
            "Set CHEMSHIELD_ADMIN_EMAIL, CHEMSHIELD_ADMIN_USERNAME, and "
            "CHEMSHIELD_ADMIN_PASSWORD before starting the HMI."
        )
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", admin_email):
        raise ValueError("CHEMSHIELD_ADMIN_EMAIL must be a valid email address")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", admin_username):
        raise ValueError("CHEMSHIELD_ADMIN_USERNAME must be 3-32 valid username characters")
    if len(admin_password) < 12 or len(admin_password) > 256:
        raise ValueError("CHEMSHIELD_ADMIN_PASSWORD must be between 12 and 256 characters")

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("CHEMSHIELD_SESSION_SECRET") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("CHEMSHIELD_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    app.jinja_loader = ChoiceLoader(
        [
            app.jinja_loader,
            FileSystemLoader(str(AUTH_PAGE_DIR)),
            FileSystemLoader(str(ADMIN_PAGE_DIR)),
        ]
    )
    auth_path = Path(auth_database)
    auth_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def auth_connection():
        connection = sqlite3.connect(str(auth_path))
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with auth_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS authorized_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    store = TelemetryStore(database)
    paused = False
    device = DeviceConnection(plc_host, plc_port)
    try:
        device.connect()
    except ConnectionError:
        pass

    def plc_call(action):
        return device.call(action)

    def csrf_token():
        token = session.get("_csrf_token")
        if token is None:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    def valid_csrf_token():
        submitted = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
        expected = session.get("_csrf_token", "")
        return bool(submitted and expected and hmac.compare_digest(submitted, expected))

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def require_authentication():
        if request.endpoint in ("login", "auth_stylesheet", "static") or request.endpoint is None:
            return None

        role = session.get("role")
        if role == "admin":
            if session.get("admin_email") != admin_email:
                session.clear()
                role = None
        elif role == "user":
            with auth_connection() as connection:
                user = connection.execute(
                    "SELECT id FROM authorized_users WHERE id = ?",
                    (session.get("user_id"),),
                ).fetchone()
            if user is None:
                session.clear()
                role = None
        else:
            session.clear()

        if role:
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.endpoint not in (
                "logout", "add_user", "delete_user"
            ) and not valid_csrf_token():
                abort(400, description="Invalid security token")
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("login"))

    @app.get("/login")
    @app.post("/login")
    def login():
        if session.get("role"):
            return redirect(url_for("index"))

        error = None
        username = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not valid_csrf_token():
                error = "Your session expired. Please try signing in again."
            elif not username or not password or len(username) > 32 or len(password) > 256:
                error = "Enter your username and password."
            elif (
                username.casefold() == admin_username.casefold()
                and hmac.compare_digest(password.encode("utf-8"), admin_password.encode("utf-8"))
            ):
                session.clear()
                session.permanent = True
                session["role"] = "admin"
                session["admin_email"] = admin_email
                session["_csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("index"))
            else:
                with auth_connection() as connection:
                    user = connection.execute(
                        """
                        SELECT id, username, password_hash
                        FROM authorized_users
                        WHERE username = ? COLLATE NOCASE
                        """,
                        (username,),
                    ).fetchone()
                if user is not None and check_password_hash(user["password_hash"], password):
                    session.clear()
                    session.permanent = True
                    session["role"] = "user"
                    session["user_id"] = user["id"]
                    session["username"] = user["username"]
                    session["_csrf_token"] = secrets.token_urlsafe(32)
                    return redirect(url_for("index"))
                error = "Username or password is incorrect."

        return render_template("login.html", error=error, username=username)

    @app.get("/login.css")
    def auth_stylesheet():
        return send_from_directory(str(AUTH_PAGE_DIR), "login.css", mimetype="text/css")

    @app.post("/logout")
    def logout():
        if not valid_csrf_token():
            abort(400, description="Invalid security token")
        session.clear()
        return redirect(url_for("login"))

    @app.get("/admin/users")
    def admin_users():
        if session.get("role") != "admin":
            abort(403)
        with auth_connection() as connection:
            users = connection.execute(
                "SELECT id, email, username, created_at FROM authorized_users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        success = "Operator account added." if request.args.get("added") == "1" else (
            "Operator account removed." if request.args.get("removed") == "1" else None
        )
        return render_template("admin.html", users=users, error=None, success=success)

    @app.post("/admin/users")
    def add_user():
        if session.get("role") != "admin":
            abort(403)
        if not valid_csrf_token():
            abort(400, description="Invalid security token")

        email = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        error = None
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Enter a valid email address."
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            error = "Username must be 3-32 characters: letters, numbers, dots, dashes, or underscores."
        elif len(password) < 12 or len(password) > 256:
            error = "Password must be between 12 and 256 characters."
        elif email == admin_email or username.casefold() == admin_username.casefold():
            error = "That email or username is reserved for the administrator."

        if error is None:
            try:
                with auth_connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO authorized_users (email, username, password_hash, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            email,
                            username,
                            generate_password_hash(password, method="pbkdf2:sha256"),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
            except sqlite3.IntegrityError:
                error = "An account already uses that email or username."
            else:
                return redirect(url_for("admin_users", added="1"))

        with auth_connection() as connection:
            users = connection.execute(
                "SELECT id, email, username, created_at FROM authorized_users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return render_template(
            "admin.html", users=users, error=error, success=None, email=email, username=username
        ), 400

    @app.post("/admin/users/<int:user_id>/delete")
    def delete_user(user_id):
        if session.get("role") != "admin":
            abort(403)
        if not valid_csrf_token():
            abort(400, description="Invalid security token")
        with auth_connection() as connection:
            result = connection.execute("DELETE FROM authorized_users WHERE id = ?", (user_id,))
        if result.rowcount == 0:
            abort(404)
        return redirect(url_for("admin_users", removed="1"))

    def read_state(client):
        result = client.read_holding_registers(0, count=8, slave=1)
        if result.isError():
            raise RuntimeError(result)
        values = result.registers
        return {
            "motor_status": "PAUSED" if paused or values[6] == 5 else ("RUNNING" if values[0] and values[2] > 1 else "STOPPED"),
            "target_rpm": values[1], "rpm": values[2],
            "temperature": values[3] / 100, "pressure": values[4] / 100,
            "power": values[5] / 100,
        }

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/admin")
    def admin_redirect():
        if session.get("role") != "admin":
            abort(403)
        return redirect(url_for("admin_users"))

    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/state")
    def state():
        try:
            return jsonify(plc_call(read_state))
        except (ConnectionError, RuntimeError) as error:
            return jsonify({"error": str(error)}), 503

    @app.get("/api/connection")
    def connection_status():
        return jsonify(device.snapshot())

    @app.post("/api/connection")
    def connection_control():
        payload = request.get_json(silent=True) or {}
        action = payload.get("action", "connect")
        try:
            if action == "disconnect":
                return jsonify(device.disconnect())
            if action in ("connect", "reconnect"):
                device.configure(payload.get("host", device.host), payload.get("port", device.port), payload.get("name", device.name))
                return jsonify(device.reconnect() if action == "reconnect" else device.connect())
            raise ValueError("Action must be connect, disconnect, or reconnect")
        except (ConnectionError, ValueError) as error:
            return jsonify({**device.snapshot(), "status": "CONNECTION ERROR", "error": str(error)}), 400

    @app.get("/api/telemetry")
    def telemetry():
        return jsonify(store.recent())

    @app.get("/api/events")
    def event_feed():
        return jsonify(store.recent_events())

    @app.post("/api/control")
    def control():
        nonlocal paused
        payload = request.get_json(silent=True) or {}
        try:
            target = max(0, min(3000, int(payload.get("target_rpm", 1500))))
            current = plc_call(read_state)
            action = payload.get("action")
            if action == "pause":
                paused = True
            elif action == "start":
                paused = False
            elif action == "stop":
                paused = False
            motor = 1 if action == "start" else 0 if action in ("pause", "stop") else (1 if current["motor_status"] == "RUNNING" else 0)
            plc_call(lambda client: client.write_registers(0, [motor, target], slave=1))
            plc_call(lambda client: client.write_register(6, 5 if action == "pause" else 0, slave=1))
            motor_changed = (motor == 1 and current["motor_status"] != "RUNNING") or (motor == 0 and current["motor_status"] != "STOPPED")
            if action in ("start", "pause", "stop") and motor_changed:
                store.add_event({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "HMI", "machine_id": "M-01",
                    "event_type": "MOTOR_COMMAND" if motor else ("MOTOR_PAUSE" if action == "pause" else "MOTOR_STOP"),
                    "parameter": "motor_status", "old_value": current["motor_status"],
                    "new_value": "RUNNING" if motor else ("PAUSED" if action == "pause" else "STOPPED"), "severity": "info",
                })
            if target != current["target_rpm"]:
                store.add_event({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "HMI", "machine_id": "M-01",
                    "event_type": "SETPOINT_CHANGE", "parameter": "target_rpm",
                    "old_value": current["target_rpm"], "new_value": target, "severity": "info",
                })
            return jsonify({"ok": True, "motor_status": "RUNNING" if motor else ("PAUSED" if action == "pause" else "STOPPED"), "target_rpm": target})
        except (ConnectionError, RuntimeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/api/events")
    def events():
        payload = request.get_json(silent=True) or {}
        event_type = payload.get("event_type")
        value = int(payload.get("value", 1500))
        try:
            event = plc_call(lambda client: write_event(client, event_type, value))
            store.add_event(event)
            return jsonify(event), 201
        except (ConnectionError, RuntimeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CHEM-SHIELD Virtual Machine operator HMI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--plc-port", type=int, default=5020)
    parser.add_argument("--database", default="chem_shield/data/telemetry.db")
    args = parser.parse_args()
    app = create_app(plc_port=args.plc_port, database=args.database)
    app.run(host=args.host, port=args.port, debug=False)