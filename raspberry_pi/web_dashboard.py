#!/usr/bin/env python3
"""
TempBot Web Dashboard Server
=============================
Flask + Flask-SocketIO web server for the TempBot temperature-checking robot.
Serves a real-time dashboard accessible via the Raspberry Pi's WiFi hotspot
at 192.168.4.1:5000.

Reads robot state from shared tmpfs files written by tempbot.py:
  /tmp/tempbot/state.json   - current robot state
  /tmp/tempbot/history.json - temperature reading history
  /tmp/tempbot/events.log   - event log for serial monitor
  /tmp/tempbot/system.json  - system health info

SD Card Safety: This module ONLY reads from /tmp/tempbot/ (tmpfs).
It never writes to the SD card to preserve its lifespan.
"""

import os
import sys
import json
import time
import logging
import subprocess
import threading
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

try:
    from flask_cors import CORS
except ImportError:
    CORS = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

if os.name == "nt":
    TMPFS_DIR = os.environ.get("TEMPBOT_TMPFS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_tempbot"))
else:
    TMPFS_DIR = os.environ.get("TEMPBOT_TMPFS", "/tmp/tempbot")
STATE_FILE = os.path.join(TMPFS_DIR, "state.json")
HISTORY_FILE = os.path.join(TMPFS_DIR, "history.json")
EVENTS_LOG = os.path.join(TMPFS_DIR, "events.log")
SYSTEM_FILE = os.path.join(TMPFS_DIR, "system.json")
SERVO_CMD_FILE = os.path.join(TMPFS_DIR, "servo_cmd.json")
THRESHOLD_CMD_FILE = os.path.join(TMPFS_DIR, "threshold_cmd.json")
AUTOSCAN_CMD_FILE = os.path.join(TMPFS_DIR, "autoscan_cmd.json")

BROADCAST_INTERVAL_MS = 500  # WebSocket broadcast interval in milliseconds
MAX_SERIAL_LINES = 500       # Maximum serial monitor lines to keep
MAX_HISTORY_ITEMS = 100      # Maximum history items to return via API
MAX_LOG_LINES = 200          # Maximum log lines to return via API

# ---------------------------------------------------------------------------
# Logging — write to tmpfs only
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s.%(msecs)03d] %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("web_dashboard")

# ---------------------------------------------------------------------------
# Flask App Setup
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
)
app.config["SECRET_KEY"] = "tempbot-dashboard-secret-key"

if CORS:
    CORS(app, resources={r"/api/*": {"origins": "*"}})
else:
    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,DELETE,OPTIONS"
        return response

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    ping_timeout=10,
    ping_interval=5,
)

# ---------------------------------------------------------------------------
# Serial Monitor Buffer (in-memory ring buffer)
# ---------------------------------------------------------------------------

serial_buffer = deque(maxlen=MAX_SERIAL_LINES)
serial_lock = threading.Lock()
_boot_time = time.time()


def _timestamp() -> str:
    """Return a formatted timestamp string [HH:MM:SS.mmm]."""
    now = datetime.now()
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def serial_add(message: str, level: str = "INFO") -> None:
    """
    Add a line to the serial monitor buffer.

    Parameters
    ----------
    message : str
        The message text.
    level : str
        One of INFO, WARN, ERROR, DATA, SYSTEM.
    """
    entry = {
        "timestamp": _timestamp(),
        "level": level.upper(),
        "message": message,
    }
    with serial_lock:
        serial_buffer.append(entry)


# Seed buffer with boot message
serial_add("TempBot Web Dashboard initialised", "SYSTEM")

# ---------------------------------------------------------------------------
# Helper: Safe file readers
# ---------------------------------------------------------------------------


def _read_json(filepath: str, default=None):
    """
    Safely read and parse a JSON file from tmpfs.

    Returns *default* (or ``{}``) on any failure so the dashboard
    never crashes from a missing or corrupt state file.
    """
    if default is None:
        default = {}
    try:
        with open(filepath, "r") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        logger.warning("Corrupt JSON in %s: %s", filepath, exc)
        return default
    except OSError as exc:
        logger.error("Cannot read %s: %s", filepath, exc)
        return default


def _read_log_tail(filepath: str, max_lines: int = MAX_LOG_LINES) -> list:
    """Read the last *max_lines* lines from a log file."""
    try:
        with open(filepath, "r") as fh:
            lines = fh.readlines()
        return [line.rstrip("\n") for line in lines[-max_lines:]]
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.error("Cannot read log %s: %s", filepath, exc)
        return []


def _get_system_health() -> dict:
    """
    Gather system health information.

    Tries the shared system.json first (written by tempbot.py).
    Falls back to reading /proc directly if the file is absent.
    """
    data = _read_json(SYSTEM_FILE)
    if data:
        return data

    # Fallback: gather basic info directly
    health = {
        "cpu_percent": 0.0,
        "ram_percent": 0.0,
        "ram_used_mb": 0,
        "ram_total_mb": 0,
        "cpu_temp": 0.0,
        "disk_percent": 0.0,
        "disk_used_gb": 0.0,
        "disk_total_gb": 0.0,
        "uptime_seconds": 0,
        "uptime_human": "unknown",
    }

    # CPU usage from /proc/stat (simple 1-sample estimate)
    try:
        with open("/proc/loadavg", "r") as fh:
            parts = fh.read().split()
        load1 = float(parts[0])
        cpu_count = os.cpu_count() or 1
        health["cpu_percent"] = round(min(load1 / cpu_count * 100, 100), 1)
    except Exception:
        pass

    # RAM from /proc/meminfo
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    meminfo[key] = int(val)
        total = meminfo.get("MemTotal", 1)
        available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        used = total - available
        health["ram_total_mb"] = round(total / 1024)
        health["ram_used_mb"] = round(used / 1024)
        health["ram_percent"] = round(used / total * 100, 1) if total else 0
    except Exception:
        pass

    # CPU temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
            health["cpu_temp"] = round(int(fh.read().strip()) / 1000, 1)
    except Exception:
        pass

    # Disk usage
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        health["disk_total_gb"] = round(total / (1024 ** 3), 2)
        health["disk_used_gb"] = round(used / (1024 ** 3), 2)
        health["disk_percent"] = round(used / total * 100, 1) if total else 0
    except Exception:
        pass

    # Uptime
    try:
        with open("/proc/uptime", "r") as fh:
            uptime_sec = float(fh.read().split()[0])
        health["uptime_seconds"] = int(uptime_sec)
        td = timedelta(seconds=int(uptime_sec))
        days = td.days
        hours, rem = divmod(td.seconds, 3600)
        mins, secs = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        parts.append(f"{secs}s")
        health["uptime_human"] = " ".join(parts)
    except Exception:
        pass

    return health


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the main dashboard page."""
    return render_template("dashboard.html")


# ---------------------------------------------------------------------------
# REST API Endpoints
# ---------------------------------------------------------------------------


@app.route("/api/status", methods=["GET"])
def api_status():
    """Return current robot state, system health, and history from tmpfs."""
    try:
        state = _read_json(STATE_FILE, default={
            "temperature": None,
            "distance_cm": None,
            "person_detected": False,
            "servo_angle": 90,
            "fever_detected": False,
            "fever_threshold": 37.5,
            "auto_scan": True,
            "state": "idle",
            "last_reading_time": None,
            "error": None,
        })
        history = _read_json(HISTORY_FILE, default=[])
        if isinstance(history, dict):
            history = history.get("readings", [])
        system = _get_system_health()
        return jsonify({
            "ok": True,
            "data": state,
            "state": state,
            "system": system,
            "history": history[-50:] if isinstance(history, list) else [],
            "timestamp": time.time(),
        })
    except Exception as exc:
        logger.exception("Error in /api/status")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/history", methods=["GET"])
def api_history():
    """Return temperature reading history (last 100 entries)."""
    try:
        history = _read_json(HISTORY_FILE, default=[])
        if isinstance(history, list):
            history = history[-MAX_HISTORY_ITEMS:]
        else:
            # If the file stores a dict with a "readings" key
            readings = history.get("readings", [])
            history = readings[-MAX_HISTORY_ITEMS:]
        return jsonify({"ok": True, "data": history, "count": len(history)})
    except Exception as exc:
        logger.exception("Error in /api/history")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/system", methods=["GET"])
def api_system():
    """Return system health information."""
    try:
        health = _get_system_health()
        return jsonify({"ok": True, "data": health})
    except Exception as exc:
        logger.exception("Error in /api/system")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/servo", methods=["POST"])
def api_servo():
    """
    Send a manual servo control command.

    Expects JSON: {"angle": <int 0-180>}
    Writes to servo_cmd.json which tempbot.py watches.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        angle = data.get("angle")
        if angle is None:
            return jsonify({"ok": False, "error": "Missing 'angle' parameter"}), 400

        try:
            angle = int(angle)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "'angle' must be an integer"}), 400

        if not 0 <= angle <= 180:
            return jsonify({"ok": False, "error": "'angle' must be 0-180"}), 400

        # Write command to tmpfs for tempbot.py to pick up
        cmd = {"angle": angle, "timestamp": time.time()}
        try:
            with open(SERVO_CMD_FILE, "w") as fh:
                json.dump(cmd, fh)
        except OSError as exc:
            logger.error("Cannot write servo command: %s", exc)
            return jsonify({"ok": False, "error": f"File write error: {exc}"}), 500

        serial_add(f"Servo command: angle={angle}°", "INFO")
        return jsonify({"ok": True, "angle": angle})
    except Exception as exc:
        logger.exception("Error in /api/servo")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/threshold", methods=["POST"])
def api_threshold():
    """
    Update the fever threshold temperature.

    Expects JSON: {"threshold": <float>}
    Writes to threshold_cmd.json which tempbot.py watches.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        threshold = data.get("threshold")
        if threshold is None:
            return jsonify({"ok": False, "error": "Missing 'threshold' parameter"}), 400

        try:
            threshold = float(threshold)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "'threshold' must be a number"}), 400

        if not 30.0 <= threshold <= 45.0:
            return jsonify({"ok": False, "error": "'threshold' must be 30.0-45.0 °C"}), 400

        cmd = {"threshold": threshold, "timestamp": time.time()}
        try:
            with open(THRESHOLD_CMD_FILE, "w") as fh:
                json.dump(cmd, fh)
        except OSError as exc:
            logger.error("Cannot write threshold command: %s", exc)
            return jsonify({"ok": False, "error": f"File write error: {exc}"}), 500

        serial_add(f"Fever threshold updated: {threshold:.1f}°C", "INFO")
        return jsonify({"ok": True, "threshold": threshold})
    except Exception as exc:
        logger.exception("Error in /api/threshold")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/autoscan", methods=["POST"])
def api_autoscan():
    """
    Enable or disable automatic servo scanning.

    Expects JSON: {"auto_scan": <bool>}
    Writes to autoscan_cmd.json which tempbot.py watches.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        enabled = bool(data.get("auto_scan", True))
        cmd = {"auto_scan": enabled, "timestamp": time.time()}
        try:
            with open(AUTOSCAN_CMD_FILE, "w") as fh:
                json.dump(cmd, fh)
        except OSError as exc:
            logger.error("Cannot write autoscan command: %s", exc)
            return jsonify({"ok": False, "error": f"File write error: {exc}"}), 500

        serial_add(f"Auto-scan toggled: {'ON' if enabled else 'OFF'}", "INFO")
        return jsonify({"ok": True, "auto_scan": enabled})
    except Exception as exc:
        logger.exception("Error in /api/autoscan")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Return recent log / event entries."""
    try:
        lines = _read_log_tail(EVENTS_LOG, MAX_LOG_LINES)
        return jsonify({"ok": True, "data": lines, "count": len(lines)})
    except Exception as exc:
        logger.exception("Error in /api/logs")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/serial", methods=["GET"])
def api_serial():
    """Return the serial monitor buffer contents."""
    try:
        # Merge in-memory buffer with events log for a unified serial view
        with serial_lock:
            buffer_lines = list(serial_buffer)

        # Also read fresh lines from the events log
        log_lines = _read_log_tail(EVENTS_LOG, MAX_SERIAL_LINES)
        parsed_log = []
        for line in log_lines:
            # Expected format: [HH:MM:SS.mmm] LEVEL message
            level = "INFO"
            ts = _timestamp()
            msg = line
            if line.startswith("[") and "]" in line:
                bracket_end = line.index("]")
                ts = line[1:bracket_end]
                rest = line[bracket_end + 2:]  # skip "] "
                parts = rest.split(" ", 1)
                if len(parts) == 2 and parts[0] in ("INFO", "WARN", "ERROR", "DATA", "SYSTEM"):
                    level = parts[0]
                    msg = parts[1]
                else:
                    msg = rest
            parsed_log.append({"timestamp": ts, "level": level, "message": msg})

        # Combine and deduplicate (prefer log file entries, then buffer)
        combined = parsed_log + buffer_lines
        # Keep last MAX_SERIAL_LINES
        combined = combined[-MAX_SERIAL_LINES:]

        return jsonify({"ok": True, "data": combined, "count": len(combined)})
    except Exception as exc:
        logger.exception("Error in /api/serial")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    """
    Initiate a safe reboot.

    Syncs filesystem buffers before issuing the reboot command.
    """
    try:
        serial_add("REBOOT command received — syncing data...", "SYSTEM")

        # Sync filesystem buffers
        try:
            subprocess.run(["sync"], timeout=5, check=False)
        except Exception:
            pass

        serial_add("Data synced. Rebooting in 2 seconds...", "SYSTEM")

        def _do_reboot():
            time.sleep(2)
            try:
                subprocess.run(["sudo", "reboot"], timeout=5, check=False)
            except Exception as exc:
                logger.error("Reboot failed: %s", exc)

        threading.Thread(target=_do_reboot, daemon=True).start()
        return jsonify({"ok": True, "message": "Rebooting in 2 seconds..."})
    except Exception as exc:
        logger.exception("Error in /api/reboot")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """
    Initiate a safe shutdown.

    Syncs filesystem buffers before issuing the shutdown command.
    """
    try:
        serial_add("SHUTDOWN command received — syncing data...", "SYSTEM")

        try:
            subprocess.run(["sync"], timeout=5, check=False)
        except Exception:
            pass

        serial_add("Data synced. Shutting down in 2 seconds...", "SYSTEM")

        def _do_shutdown():
            time.sleep(2)
            try:
                subprocess.run(["sudo", "shutdown", "-h", "now"], timeout=5, check=False)
            except Exception as exc:
                logger.error("Shutdown failed: %s", exc)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        return jsonify({"ok": True, "message": "Shutting down in 2 seconds..."})
    except Exception as exc:
        logger.exception("Error in /api/shutdown")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# WebSocket Events
# ---------------------------------------------------------------------------


@socketio.on("connect")
def handle_connect():
    """Handle new WebSocket client connection."""
    logger.info("Client connected: %s", request.sid)
    serial_add(f"Dashboard client connected (sid={request.sid[:8]}…)", "SYSTEM")


@socketio.on("disconnect")
def handle_disconnect():
    """Handle WebSocket client disconnection."""
    logger.info("Client disconnected: %s", request.sid)
    serial_add(f"Dashboard client disconnected (sid={request.sid[:8]}…)", "SYSTEM")


@socketio.on("request_serial")
def handle_request_serial():
    """Client explicitly requests full serial buffer."""
    with serial_lock:
        data = list(serial_buffer)
    socketio.emit("serial_data", {"lines": data}, room=request.sid)


@socketio.on("servo_command")
def handle_servo_command(data):
    """Handle servo command via WebSocket."""
    angle = data.get("angle")
    if angle is not None:
        try:
            angle = int(angle)
            if 0 <= angle <= 180:
                cmd = {"angle": angle, "timestamp": time.time()}
                with open(SERVO_CMD_FILE, "w") as fh:
                    json.dump(cmd, fh)
                serial_add(f"Servo command (WS): angle={angle}°", "INFO")
                socketio.emit("servo_ack", {"ok": True, "angle": angle})
            else:
                socketio.emit("servo_ack", {"ok": False, "error": "Angle out of range"})
        except (ValueError, TypeError, OSError) as exc:
            socketio.emit("servo_ack", {"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Background Broadcast Thread
# ---------------------------------------------------------------------------

_previous_state_hash = None
_last_processed_log_count = 0


def _broadcast_loop():
    """
    Periodically read state files and broadcast updates to all connected
    WebSocket clients. Runs every BROADCAST_INTERVAL_MS milliseconds.
    """
    global _previous_state_hash, _last_processed_log_count

    while True:
        try:
            socketio.sleep(BROADCAST_INTERVAL_MS / 1000.0)

            # Read current state
            state = _read_json(STATE_FILE, default={})
            history = _read_json(HISTORY_FILE, default=[])
            system = _get_system_health()

            # Read latest event-log lines for serial feed
            log_lines = _read_log_tail(EVENTS_LOG, 50)

            if isinstance(history, dict):
                history = history.get("readings", [])

            # Build payload
            payload = {
                "state": state,
                "history": history[-50:] if isinstance(history, list) else [],
                "system": system,
                "timestamp": time.time(),
            }

            socketio.emit("update", payload)

            # Only append truly new lines to serial buffer
            if len(log_lines) > _last_processed_log_count:
                new_lines = log_lines[_last_processed_log_count:]
                for line in new_lines:
                    if line:
                        serial_add(line, "DATA")
                _last_processed_log_count = len(log_lines)
            elif len(log_lines) < _last_processed_log_count:
                # Log was rotated or reset
                _last_processed_log_count = len(log_lines)

        except Exception as exc:
            logger.error("Broadcast error: %s", exc)
            socketio.sleep(2)


# ---------------------------------------------------------------------------
# Stdout/Stderr Capture for Serial Monitor
# ---------------------------------------------------------------------------


class _SerialCapture:
    """
    Wraps sys.stdout / sys.stderr so that print() calls are captured into
    the serial monitor buffer while still being written to the real stream.
    """

    def __init__(self, original_stream, level="INFO"):
        self._original = original_stream
        self._level = level

    def write(self, text):
        if self._original:
            self._original.write(text)
        text = text.strip()
        if text:
            serial_add(text, self._level)

    def flush(self):
        if self._original:
            self._original.flush()

    def fileno(self):
        if self._original:
            return self._original.fileno()
        raise AttributeError("no fileno")

    def isatty(self):
        return False


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def _free_port(port: int = 5000):
    """Release port if held by a stale background process on Linux."""
    if os.name != "nt":
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                in_use = (s.connect_ex(("127.0.0.1", port)) == 0)
            if in_use:
                logger.warning("Port %d is occupied. Terminating stale process...", port)
                subprocess.run(["fuser", "-k", f"{port}/tcp"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.0)
        except Exception as e:
            logger.debug("Port check note: %s", e)


def start_dashboard(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """
    Start the TempBot web dashboard server.

    Parameters
    ----------
    host : str
        Bind address. Default ``0.0.0.0`` (all interfaces).
    port : int
        Listen port. Default ``5000``.
    debug : bool
        Enable Flask debug mode.
    """
    # Free port if a zombie process is still running
    _free_port(port)

    # Ensure tmpfs directory exists
    os.makedirs(TMPFS_DIR, exist_ok=True)

    # Start background broadcast thread
    socketio.start_background_task(_broadcast_loop)

    logger.info("TempBot Dashboard starting on http://%s:%d", host, port)
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    start_dashboard(debug=False)
