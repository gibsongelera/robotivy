#!/usr/bin/env python3
"""
TempBot - Temperature Checking Humanoid Robot Controller
=========================================================

Main controller for a Raspberry Pi 3 Model B based temperature-checking
humanoid robot. Detects people with an ultrasonic sensor, measures body
temperature with a non-contact IR sensor, and provides visual/audio feedback.

Hardware:
    - HC-SR04 Ultrasonic Sensor (TRIG=GPIO23, ECHO=GPIO24)
    - MLX90614 Non-Contact IR Temperature Sensor (I2C 0x5A)
    - I2C LCD 16x2 Display (I2C 0x27)
    - SG90 Servo Motor (GPIO18 PWM) - head movement
    - Green LED (GPIO17) - normal temperature indicator
    - Red LED (GPIO27) - fever indicator
    - Buzzer (GPIO22) - fever alert

SD Card Safety Features:
    - Atomic file writes via SafeFileWriter (write → fsync → rename)
    - All temporary/log data stored on tmpfs (/tmp/tempbot/)
    - Write-ahead logging for persistent state
    - Graceful shutdown signal handlers (SIGTERM, SIGINT)
    - Watchdog monitoring system health
    - JSON event logging with rotation

Author: TempBot Project
License: MIT
"""

import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
import queue
import shutil
import glob
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

# ---------------------------------------------------------------------------
# Conditional hardware imports – fall back to stubs when not on a Raspberry Pi
# ---------------------------------------------------------------------------
_SIMULATION_MODE = False

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):
    _SIMULATION_MODE = True
    GPIO = None

try:
    import smbus2
except ImportError:
    smbus2 = None

try:
    import board
    import busio
    import adafruit_mlx90614
except (ImportError, NotImplementedError, RuntimeError):
    board = None
    busio = None
    adafruit_mlx90614 = None

try:
    from RPLCD.i2c import CharLCD
except ImportError:
    CharLCD = None

try:
    import psutil
except ImportError:
    psutil = None


# ===========================================================================
# Configuration Loader
# ===========================================================================

def load_config(config_path: Optional[str] = None) -> dict:
    """Load configuration from JSON file with fallback defaults.

    Args:
        config_path: Absolute or relative path to config.json.
                     Defaults to ``config.json`` in the same directory as
                     this script.

    Returns:
        dict: Merged configuration dictionary.
    """
    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

    defaults: Dict[str, Any] = {
        "pins": {"trig": 23, "echo": 24, "servo": 18, "green_led": 17, "red_led": 27, "buzzer": 22},
        "i2c": {"lcd_address": "0x27", "mlx_address": "0x5A", "bus_number": 1},
        "thresholds": {
            "fever_temp": 37.5, "detection_distance": 50,
            "min_valid_temp": 30.0, "max_valid_temp": 45.0,
            "min_valid_distance": 2, "max_valid_distance": 400,
        },
        "safety": {
            "use_tmpfs": True, "tmpfs_path": "/tmp/tempbot",
            "persistent_path": os.path.expanduser("~/tempbot_data"),
            "sync_interval": 300, "max_log_size_mb": 10, "max_log_files": 5,
            "watchdog_interval": 30, "max_events_in_memory": 1000,
        },
        "servo": {
            "min_angle": 0, "max_angle": 180, "center_angle": 90,
            "scan_speed": 2, "scan_step_delay": 0.05,
            "pwm_frequency": 50, "min_duty": 2.5, "max_duty": 12.5,
        },
        "timing": {
            "measurement_interval": 0.5, "lcd_update_interval": 0.3,
            "idle_scan_delay": 0.05, "buzzer_beep_duration": 0.2,
            "buzzer_beep_pause": 0.3, "buzzer_beep_count": 3,
            "person_hold_time": 5.0, "ultrasonic_timeout": 0.04,
        },
        "lcd": {"columns": 16, "rows": 2, "backlight_enabled": True},
    }

    try:
        with open(config_path, "r") as fh:
            user_cfg = json.load(fh)
        # Shallow-merge each section
        for section, values in user_cfg.items():
            if isinstance(values, dict) and section in defaults:
                defaults[section].update(values)
            else:
                defaults[section] = values
    except FileNotFoundError:
        logging.warning("Config file %s not found – using defaults.", config_path)
    except json.JSONDecodeError as exc:
        logging.error("Malformed config file %s: %s – using defaults.", config_path, exc)

    return defaults


# ===========================================================================
# SafeFileWriter – atomic writes to prevent SD-card corruption
# ===========================================================================

class SafeFileWriter:
    """Writes files atomically to prevent corruption on sudden power loss.

    Strategy:
        1. Write data to a temporary ``.tmp`` sibling file.
        2. ``fsync`` the file descriptor to flush OS buffers to storage.
        3. ``os.replace`` (atomic rename) the temp file over the target.
        4. ``fsync`` the containing directory to persist the rename.

    This guarantees that the target file is either the *old* complete version
    or the *new* complete version – never a partially-written mix.
    """

    @staticmethod
    def write(filepath: str, data: str) -> None:
        """Atomically write *data* to *filepath*.

        Args:
            filepath: Destination file path.
            data: String content to write.

        Raises:
            OSError: If the write, fsync, or rename fails.
        """
        filepath = os.path.abspath(filepath)
        dir_path = os.path.dirname(filepath)
        os.makedirs(dir_path, exist_ok=True)

        tmp_path = filepath + ".tmp"

        try:
            # 1. Write to temporary file
            with open(tmp_path, "w") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())

            # 2. Atomic rename (overwrites target on POSIX)
            os.replace(tmp_path, filepath)

            # 3. Fsync the directory so the rename is persisted (POSIX ext4)
            if hasattr(os, "O_DIRECTORY") and os.name != "nt":
                try:
                    dir_fd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except (OSError, PermissionError):
                    pass

        except OSError:
            # Best-effort cleanup of the temp file
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def write_json(filepath: str, obj: Any, indent: int = 2) -> None:
        """Atomically write a JSON-serialisable *obj* to *filepath*.

        Args:
            filepath: Destination file path.
            obj: JSON-serialisable Python object.
            indent: JSON indentation level.
        """
        SafeFileWriter.write(filepath, json.dumps(obj, indent=indent, default=str))

    @staticmethod
    def check_recovery(filepath: str) -> bool:
        """Check for and recover from an incomplete atomic write.

        If a ``.tmp`` sibling exists but the target does not, the previous
        write was interrupted *after* flushing the temp file but *before*
        the rename completed.  We complete the rename now.

        Args:
            filepath: The target file path.

        Returns:
            True if recovery was performed, False otherwise.
        """
        tmp_path = filepath + ".tmp"
        if os.path.exists(tmp_path):
            if not os.path.exists(filepath):
                # The rename never happened – complete it now
                try:
                    os.replace(tmp_path, filepath)
                    logging.info("Recovered incomplete write: %s", filepath)
                    return True
                except OSError as exc:
                    logging.error("Recovery failed for %s: %s", filepath, exc)
            else:
                # Both exist – the rename succeeded but cleanup didn't;
                # discard the stale temp file.
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return False


# ===========================================================================
# Write-Ahead Logger (WAL) for critical persistent data
# ===========================================================================

class WriteAheadLog:
    """Simple write-ahead log that journals mutations before applying them.

    Each entry is fsynced to the WAL file before the actual data file is
    updated, ensuring crash-safe persistence.
    """

    def __init__(self, wal_path: str):
        self._wal_path = wal_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(wal_path), exist_ok=True)
        self._replay_pending()

    def _replay_pending(self) -> None:
        """Replay any pending WAL entries that were not committed."""
        if not os.path.exists(self._wal_path):
            return
        try:
            with open(self._wal_path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    target = entry.get("target")
                    payload = entry.get("data")
                    if target and payload is not None:
                        SafeFileWriter.write_json(target, payload)
                        logging.info("WAL replay: wrote pending data to %s", target)
            os.remove(self._wal_path)
        except Exception as exc:
            logging.error("WAL replay error: %s", exc)

    def log_and_write(self, target_path: str, data: Any) -> None:
        """Journal *data* in the WAL, then atomically write to *target_path*.

        Args:
            target_path: Final destination file.
            data: JSON-serialisable data.
        """
        with self._lock:
            entry = json.dumps({"target": target_path, "data": data, "ts": time.time()}, default=str)
            try:
                with open(self._wal_path, "a") as fh:
                    fh.write(entry + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                logging.error("WAL write failed: %s", exc)
                return

            SafeFileWriter.write_json(target_path, data)

            # Truncate WAL after successful commit
            try:
                with open(self._wal_path, "w") as fh:
                    fh.truncate(0)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass


# ===========================================================================
# JSON Event Logger with rotation
# ===========================================================================

class EventLogger:
    """Thread-safe JSON event logger that writes to tmpfs and rotates files.

    Events are kept in an in-memory ring buffer for the web dashboard and
    simultaneously appended to a JSONL file on tmpfs.  Periodic sync copies
    the current log to persistent storage.
    """

    def __init__(self, log_dir: str, persistent_dir: str,
                 max_size_mb: float = 10, max_files: int = 5,
                 max_memory_events: int = 1000):
        self._log_dir = log_dir
        self._persistent_dir = persistent_dir
        self._max_bytes = int(max_size_mb * 1024 * 1024)
        self._max_files = max_files
        self._max_memory = max_memory_events
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._current_file: Optional[str] = None
        self._current_size = 0

        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(persistent_dir, exist_ok=True)
        self._rotate_if_needed()

    def _current_log_path(self) -> str:
        if self._current_file is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._current_file = os.path.join(self._log_dir, f"events_{ts}.jsonl")
            self._current_size = 0
        return self._current_file

    def _rotate_if_needed(self) -> None:
        """Rotate the log file if it exceeds the maximum size."""
        if self._current_size >= self._max_bytes:
            self._current_file = None  # force new file on next write
            # Prune old log files
            logs = sorted(glob.glob(os.path.join(self._log_dir, "events_*.jsonl")))
            while len(logs) > self._max_files:
                try:
                    os.remove(logs.pop(0))
                except OSError:
                    pass

    def log(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Append an event to the log.

        Args:
            event_type: Short label for the event (e.g. ``"reading"``,
                ``"fever_alert"``).
            data: Arbitrary data payload.

        Returns:
            The event dict that was logged.
        """
        event: Dict[str, Any] = {
            "ts": datetime.now().isoformat(),
            "type": event_type,
            "data": data or {},
        }
        with self._lock:
            # In-memory ring buffer
            self._events.append(event)
            if len(self._events) > self._max_memory:
                self._events = self._events[-self._max_memory:]

            # Append to JSONL file on tmpfs
            try:
                line = json.dumps(event, default=str) + "\n"
                path = self._current_log_path()
                with open(path, "a") as fh:
                    fh.write(line)
                    # No fsync – tmpfs doesn't need it and we want speed
                self._current_size += len(line.encode("utf-8"))
                self._rotate_if_needed()
            except OSError as exc:
                logging.error("Event log write failed: %s", exc)

        return event

    def get_recent(self, count: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent *count* events from memory."""
        with self._lock:
            return list(self._events[-count:])

    def sync_to_persistent(self) -> None:
        """Copy current log files to persistent storage (called periodically)."""
        try:
            for src in glob.glob(os.path.join(self._log_dir, "events_*.jsonl")):
                dst = os.path.join(self._persistent_dir, os.path.basename(src))
                shutil.copy2(src, dst)
            logging.debug("Event logs synced to persistent storage.")
        except OSError as exc:
            logging.error("Event log sync failed: %s", exc)


# ===========================================================================
# TempBotState – thread-safe shared state
# ===========================================================================

@dataclass
class TempBotState:
    """Thread-safe shared state between the robot controller and web dashboard.

    All attribute access should go through :meth:`update` and :meth:`snapshot`
    to ensure consistency under concurrent access.
    """

    temperature: float = 0.0
    distance: float = 0.0
    distance_cm: float = 0.0
    person_detected: bool = False
    fever_detected: bool = False
    fever_threshold: float = 37.5
    auto_scan: bool = True
    status: str = "Initializing..."
    last_reading_time: str = ""
    total_readings: int = 0
    fever_count: int = 0
    normal_count: int = 0
    uptime_start: float = field(default_factory=time.time)
    servo_angle: int = 90
    system_health: dict = field(default_factory=dict)

    def __post_init__(self):
        self._lock = threading.Lock()

    def update(self, **kwargs) -> None:
        """Update one or more fields atomically.

        Example::

            state.update(temperature=36.7, person_detected=True)
        """
        if "distance" in kwargs and "distance_cm" not in kwargs:
            kwargs["distance_cm"] = kwargs["distance"]
        elif "distance_cm" in kwargs and "distance" not in kwargs:
            kwargs["distance"] = kwargs["distance_cm"]
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key) and not key.startswith("_"):
                    setattr(self, key, value)

    def snapshot(self) -> Dict[str, Any]:
        """Return a consistent, JSON-friendly snapshot of the current state."""
        with self._lock:
            data = asdict(self)
        # Compute derived fields
        data["uptime_seconds"] = round(time.time() - data["uptime_start"], 1)
        data["distance_cm"] = data.get("distance", 0.0)
        return data


# ===========================================================================
# Hardware Abstraction Layer
# ===========================================================================

class UltrasonicSensor:
    """HC-SR04 ultrasonic distance sensor driver.

    Uses GPIO BCM pin numbering.

    Args:
        trig_pin: BCM pin number for TRIG.
        echo_pin: BCM pin number for ECHO.
        timeout: Maximum wait time (seconds) for echo pulse.
    """

    def __init__(self, trig_pin: int, echo_pin: int, timeout: float = 0.04):
        self._trig = trig_pin
        self._echo = echo_pin
        self._timeout = timeout
        self._lock = threading.Lock()

        if GPIO:
            GPIO.setup(self._trig, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(self._echo, GPIO.IN)

    def measure(self) -> float:
        """Take a single distance measurement.

        Returns:
            Distance in centimetres, or -1.0 on timeout / error.
        """
        if _SIMULATION_MODE:
            return 100.0  # Simulated distance

        with self._lock:
            try:
                # Ensure trigger is low
                GPIO.output(self._trig, GPIO.LOW)
                time.sleep(0.00002)

                # Send 10µs trigger pulse
                GPIO.output(self._trig, GPIO.HIGH)
                time.sleep(0.00001)
                GPIO.output(self._trig, GPIO.LOW)

                # Wait for echo to go HIGH
                start = time.time()
                deadline = start + self._timeout
                while GPIO.input(self._echo) == GPIO.LOW:
                    start = time.time()
                    if start > deadline:
                        return -1.0

                # Wait for echo to go LOW
                end = start
                deadline = start + self._timeout
                while GPIO.input(self._echo) == GPIO.HIGH:
                    end = time.time()
                    if end > deadline:
                        return -1.0

                # Distance = (time × speed_of_sound) / 2
                distance = (end - start) * 34300.0 / 2.0
                return round(distance, 2)

            except Exception as exc:
                logging.error("Ultrasonic measurement error: %s", exc)
                return -1.0


class TemperatureSensor:
    """MLX90614 non-contact infrared temperature sensor driver (I2C).

    Args:
        bus_number: I2C bus number (usually 1 on RPi 3).
    """

    def __init__(self, bus_number: int = 1):
        self._sensor = None
        self._lock = threading.Lock()

        if adafruit_mlx90614 and board and busio:
            try:
                i2c = busio.I2C(board.SCL, board.SDA)
                self._sensor = adafruit_mlx90614.MLX90614(i2c)
                logging.info("MLX90614 sensor initialized on I2C bus %d.", bus_number)
            except Exception as exc:
                logging.error("MLX90614 initialization failed: %s", exc)
        else:
            logging.warning("MLX90614 libraries not available – running in simulation mode.")

    def read_object_temp(self) -> float:
        """Read the object (body) temperature in °C.

        Returns:
            Temperature in Celsius, or -1.0 on error.
        """
        with self._lock:
            if self._sensor is None:
                return 36.5 + (time.time() % 3) * 0.3  # Simulated

            try:
                temp = self._sensor.object_temperature
                return round(temp, 2)
            except Exception as exc:
                logging.error("Temperature read error: %s", exc)
                return -1.0

    def read_ambient_temp(self) -> float:
        """Read the ambient temperature in °C.

        Returns:
            Temperature in Celsius, or -1.0 on error.
        """
        with self._lock:
            if self._sensor is None:
                return 25.0  # Simulated

            try:
                return round(self._sensor.ambient_temperature, 2)
            except Exception as exc:
                logging.error("Ambient temp read error: %s", exc)
                return -1.0


class LCDDisplay:
    """I2C 16×2 character LCD driver (HD44780 compatible via PCF8574).

    Args:
        address: I2C address (e.g. 0x27).
        bus_number: I2C bus number.
        cols: Number of columns.
        rows: Number of rows.
        backlight: Enable backlight on init.
    """

    def __init__(self, address: int = 0x27, bus_number: int = 1,
                 cols: int = 16, rows: int = 2, backlight: bool = True):
        self._lcd = None
        self._cols = cols
        self._rows = rows
        self._lock = threading.Lock()

        if CharLCD:
            try:
                self._lcd = CharLCD(
                    i2c_expander="PCF8574",
                    address=address,
                    port=bus_number,
                    cols=cols,
                    rows=rows,
                    backlight_enabled=backlight,
                )
                logging.info("LCD initialized at address 0x%02X.", address)
            except Exception as exc:
                logging.error("LCD initialization failed: %s", exc)
        else:
            logging.warning("RPLCD library not available – LCD in simulation mode.")

    def write(self, line1: str, line2: str = "") -> None:
        """Write up to two lines to the display.

        Lines are truncated / padded to fit the display width.

        Args:
            line1: First line text.
            line2: Second line text.
        """
        l1 = line1[:self._cols].ljust(self._cols)
        l2 = line2[:self._cols].ljust(self._cols)

        with self._lock:
            if self._lcd:
                try:
                    self._lcd.cursor_pos = (0, 0)
                    self._lcd.write_string(l1)
                    self._lcd.cursor_pos = (1, 0)
                    self._lcd.write_string(l2)
                except Exception as exc:
                    logging.error("LCD write error: %s", exc)
            else:
                logging.debug("LCD >>> %s | %s", l1.strip(), l2.strip())

    def clear(self) -> None:
        """Clear the display."""
        with self._lock:
            if self._lcd:
                try:
                    self._lcd.clear()
                except Exception as exc:
                    logging.error("LCD clear error: %s", exc)

    def set_backlight(self, enabled: bool) -> None:
        """Turn the backlight on or off."""
        with self._lock:
            if self._lcd:
                try:
                    self._lcd.backlight_enabled = enabled
                except Exception as exc:
                    logging.error("LCD backlight error: %s", exc)


class ServoMotor:
    """SG90 micro servo motor driver using hardware PWM.

    Args:
        pin: BCM GPIO pin number.
        frequency: PWM frequency in Hz (50 Hz for SG90).
        min_duty: Duty cycle percentage for 0°.
        max_duty: Duty cycle percentage for 180°.
        min_angle: Minimum allowed angle.
        max_angle: Maximum allowed angle.
    """

    def __init__(self, pin: int = 18, frequency: int = 50,
                 min_duty: float = 2.5, max_duty: float = 12.5,
                 min_angle: int = 0, max_angle: int = 180):
        self._pin = pin
        self._min_duty = min_duty
        self._max_duty = max_duty
        self._min_angle = min_angle
        self._max_angle = max_angle
        self._current_angle = 90
        self._pwm = None
        self._lock = threading.Lock()

        if GPIO:
            try:
                GPIO.setup(pin, GPIO.OUT)
                self._pwm = GPIO.PWM(pin, frequency)
                self._pwm.start(0)
                logging.info("Servo initialized on GPIO%d.", pin)
            except Exception as exc:
                logging.error("Servo initialization failed: %s", exc)

    def _angle_to_duty(self, angle: int) -> float:
        """Convert angle (degrees) to duty cycle percentage."""
        angle = max(self._min_angle, min(self._max_angle, angle))
        return self._min_duty + (angle / 180.0) * (self._max_duty - self._min_duty)

    def set_angle(self, angle: int) -> None:
        """Move the servo to the specified angle.

        Args:
            angle: Target angle in degrees (clamped to configured range).
        """
        angle = max(self._min_angle, min(self._max_angle, angle))
        with self._lock:
            self._current_angle = angle
            if self._pwm:
                try:
                    duty = self._angle_to_duty(angle)
                    self._pwm.ChangeDutyCycle(duty)
                    time.sleep(0.02)
                    # Stop sending PWM to prevent servo jitter
                    self._pwm.ChangeDutyCycle(0)
                except Exception as exc:
                    logging.error("Servo set_angle error: %s", exc)
            else:
                logging.debug("Servo >>> angle=%d°", angle)

    @property
    def angle(self) -> int:
        """Current servo angle."""
        with self._lock:
            return self._current_angle

    def stop(self) -> None:
        """Stop PWM output."""
        with self._lock:
            if self._pwm:
                try:
                    self._pwm.ChangeDutyCycle(0)
                    self._pwm.stop()
                except Exception:
                    pass
                self._pwm = None


class LEDController:
    """Simple GPIO-based LED controller.

    Args:
        green_pin: BCM pin for the green LED.
        red_pin: BCM pin for the red LED.
    """

    def __init__(self, green_pin: int = 17, red_pin: int = 27):
        self._green = green_pin
        self._red = red_pin

        if GPIO:
            GPIO.setup(green_pin, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(red_pin, GPIO.OUT, initial=GPIO.LOW)

    def green_on(self) -> None:
        self._set(self._green, True)
        self._set(self._red, False)

    def red_on(self) -> None:
        self._set(self._red, True)
        self._set(self._green, False)

    def all_off(self) -> None:
        self._set(self._green, False)
        self._set(self._red, False)

    def _set(self, pin: int, on: bool) -> None:
        if GPIO:
            try:
                GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)
            except Exception as exc:
                logging.error("LED pin %d error: %s", pin, exc)
        else:
            logging.debug("LED pin %d → %s", pin, "ON" if on else "OFF")


class BuzzerController:
    """Active buzzer controller.

    Args:
        pin: BCM GPIO pin connected to the buzzer.
    """

    def __init__(self, pin: int = 22):
        self._pin = pin
        if GPIO:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

    def beep(self, duration: float = 0.2, count: int = 3, pause: float = 0.3) -> None:
        """Emit a series of beeps.

        Args:
            duration: Length of each beep in seconds.
            count: Number of beeps.
            pause: Pause between beeps in seconds.
        """
        for i in range(count):
            if GPIO:
                try:
                    GPIO.output(self._pin, GPIO.HIGH)
                    time.sleep(duration)
                    GPIO.output(self._pin, GPIO.LOW)
                except Exception as exc:
                    logging.error("Buzzer error: %s", exc)
                    return
            else:
                logging.debug("BUZZ [%d/%d]", i + 1, count)
                time.sleep(duration)

            if i < count - 1:
                time.sleep(pause)

    def off(self) -> None:
        """Ensure the buzzer is silent."""
        if GPIO:
            try:
                GPIO.output(self._pin, GPIO.LOW)
            except Exception:
                pass


# ===========================================================================
# Watchdog – monitors system health
# ===========================================================================

class SystemWatchdog:
    """Periodically collects system health metrics.

    Monitors CPU temperature, memory usage, disk usage, and CPU load.
    Results are published to :class:`TempBotState`.
    """

    def __init__(self, state: TempBotState, interval: float = 30):
        self._state = state
        self._interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the watchdog thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="watchdog")
        self._thread.start()
        logging.info("System watchdog started (interval=%ds).", self._interval)

    def stop(self) -> None:
        """Stop the watchdog thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                health = self._collect()
                self._state.update(system_health=health)
            except Exception as exc:
                logging.error("Watchdog error: %s", exc)
            self._stop_event.wait(self._interval)

    def _collect(self) -> Dict[str, Any]:
        """Collect system health metrics."""
        health: Dict[str, Any] = {"timestamp": datetime.now().isoformat()}

        if psutil:
            try:
                health["cpu_percent"] = psutil.cpu_percent(interval=1)
                mem = psutil.virtual_memory()
                health["memory_percent"] = mem.percent
                health["memory_available_mb"] = round(mem.available / (1024 * 1024), 1)
                disk = psutil.disk_usage("/")
                health["disk_percent"] = disk.percent
                health["disk_free_mb"] = round(disk.free / (1024 * 1024), 1)
            except Exception as exc:
                health["psutil_error"] = str(exc)

        # Raspberry Pi CPU temperature
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
                cpu_temp = int(fh.read().strip()) / 1000.0
                health["cpu_temp_c"] = round(cpu_temp, 1)
        except (FileNotFoundError, ValueError):
            health["cpu_temp_c"] = None

        return health


# ===========================================================================
# TempBot – main robot controller
# ===========================================================================

class TempBot:
    """Main TempBot controller orchestrating all hardware and logic.

    This class owns all hardware driver instances, manages the main control
    loop, and exposes state for the web dashboard via :attr:`state` and
    :attr:`event_queue`.

    Usage::

        bot = TempBot()
        bot.start()       # blocking; runs until shutdown signal
    """

    def __init__(self, config_path: Optional[str] = None):
        self.cfg = load_config(config_path)
        self._stop_event = threading.Event()

        # ---- Paths --------------------------------------------------------
        safety = self.cfg["safety"]
        if os.name == "nt":
            self._tmpfs_dir = os.environ.get("TEMPBOT_TMPFS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_tempbot"))
            self._persist_dir = os.environ.get("TEMPBOT_PERSIST", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
        else:
            self._tmpfs_dir = os.environ.get("TEMPBOT_TMPFS", safety.get("tmpfs_path", "/tmp/tempbot"))
            self._persist_dir = os.environ.get("TEMPBOT_PERSIST", safety.get("persistent_path", os.path.expanduser("~/tempbot_data")))

        self._tmpfs_state_file = os.path.join(self._tmpfs_dir, "state.json")
        self._tmpfs_history_file = os.path.join(self._tmpfs_dir, "history.json")
        self._tmpfs_events_log = os.path.join(self._tmpfs_dir, "events.log")
        self._servo_cmd_file = os.path.join(self._tmpfs_dir, "servo_cmd.json")
        self._threshold_cmd_file = os.path.join(self._tmpfs_dir, "threshold_cmd.json")
        self._autoscan_cmd_file = os.path.join(self._tmpfs_dir, "autoscan_cmd.json")
        self._state_file = os.path.join(self._persist_dir, "state.json")
        self._wal_file = os.path.join(self._persist_dir, "wal.jsonl")
        self._history_readings: List[Dict[str, Any]] = []

        os.makedirs(self._tmpfs_dir, exist_ok=True)
        os.makedirs(self._persist_dir, exist_ok=True)

        # ---- Logging (all to tmpfs) ---------------------------------------
        self._setup_logging()

        logging.info("=" * 60)
        logging.info("TempBot starting up...")
        logging.info("Simulation mode: %s", _SIMULATION_MODE)
        logging.info("=" * 60)

        # ---- Auto-recovery -----------------------------------------------
        SafeFileWriter.check_recovery(self._state_file)

        # ---- Shared state & event infrastructure --------------------------
        self.state = TempBotState()
        self.event_queue: queue.Queue = queue.Queue(maxsize=500)
        self._wal = WriteAheadLog(self._wal_file)
        self._event_logger = EventLogger(
            log_dir=os.path.join(self._tmpfs_dir, "events"),
            persistent_dir=os.path.join(self._persist_dir, "events"),
            max_size_mb=safety["max_log_size_mb"],
            max_files=safety["max_log_files"],
            max_memory_events=safety["max_events_in_memory"],
        )

        # ---- GPIO setup --------------------------------------------------
        if GPIO:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

        pins = self.cfg["pins"]

        # ---- Hardware drivers ---------------------------------------------
        self.ultrasonic = UltrasonicSensor(
            trig_pin=pins["trig"],
            echo_pin=pins["echo"],
            timeout=self.cfg["timing"]["ultrasonic_timeout"],
        )
        self.temp_sensor = TemperatureSensor(
            bus_number=self.cfg["i2c"].get("bus_number", 1),
        )

        i2c_cfg = self.cfg["i2c"]
        lcd_cfg = self.cfg["lcd"]
        self.lcd = LCDDisplay(
            address=int(i2c_cfg["lcd_address"], 16),
            bus_number=i2c_cfg.get("bus_number", 1),
            cols=lcd_cfg["columns"],
            rows=lcd_cfg["rows"],
            backlight=lcd_cfg["backlight_enabled"],
        )

        servo_cfg = self.cfg["servo"]
        self.servo = ServoMotor(
            pin=pins["servo"],
            frequency=servo_cfg["pwm_frequency"],
            min_duty=servo_cfg["min_duty"],
            max_duty=servo_cfg["max_duty"],
            min_angle=servo_cfg["min_angle"],
            max_angle=servo_cfg["max_angle"],
        )

        self.leds = LEDController(
            green_pin=pins["green_led"],
            red_pin=pins["red_led"],
        )
        self.buzzer = BuzzerController(pin=pins["buzzer"])

        # ---- Watchdog -----------------------------------------------------
        self._watchdog = SystemWatchdog(
            self.state,
            interval=safety["watchdog_interval"],
        )

        # ---- Worker threads -----------------------------------------------
        self._threads: List[threading.Thread] = []
        self._sync_interval = safety["sync_interval"]

        logging.info("TempBot initialized successfully.")

    # ------------------------------------------------------------------
    # Logging setup (all to tmpfs)
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure logging to tmpfs with rotation."""
        log_dir = os.path.join(self._tmpfs_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, "tempbot.log")

        # Root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        # Console handler
        console_fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(console_fmt)
        root_logger.addHandler(console)

        # File handler (on tmpfs – safe for frequent writes)
        try:
            from logging.handlers import RotatingFileHandler
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3,
            )
            file_handler.setLevel(logging.DEBUG)
            file_fmt = logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
            )
            file_handler.setFormatter(file_fmt)
            root_logger.addHandler(file_handler)
        except Exception as exc:
            logging.warning("Could not set up file logging: %s", exc)

        # File handler for Web Dashboard Serial Monitor (events.log)
        try:
            events_handler = logging.FileHandler(
                self._tmpfs_events_log,
                mode="a",
                encoding="utf-8",
            )
            events_handler.setLevel(logging.INFO)
            events_fmt = logging.Formatter(
                "[%(asctime)s] %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
            events_handler.setFormatter(events_fmt)
            root_logger.addHandler(events_handler)
        except Exception as exc:
            logging.warning("Could not set up events.log: %s", exc)

    # ------------------------------------------------------------------
    # Signal handlers for graceful shutdown
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM and SIGINT handlers for graceful shutdown."""
        def _handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logging.info("Received %s – initiating graceful shutdown...", sig_name)
            self._stop_event.set()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        logging.info("Signal handlers installed (SIGTERM, SIGINT).")

    # ------------------------------------------------------------------
    # Main control loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the TempBot – installs signal handlers, starts threads, and
        blocks until a shutdown signal is received."""
        self._install_signal_handlers()
        self._watchdog.start()

        # Restore persisted state if available
        self._restore_state()

        # Start worker threads
        self._start_thread("sensor_loop", self._sensor_loop)
        self._start_thread("servo_loop", self._servo_loop)
        self._start_thread("sync_loop", self._sync_loop)
        self._start_thread("state_publisher", self._state_publisher_loop)

        self.state.update(status="Ready")
        self.lcd.write("TempBot Ready", "Scanning...")
        self._emit_event("startup", {"mode": "simulation" if _SIMULATION_MODE else "hardware"})

        logging.info("TempBot is running. Press Ctrl+C to stop.")

        # Block until shutdown
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(1.0)
        except KeyboardInterrupt:
            pass

        self.shutdown()

    def shutdown(self) -> None:
        """Perform a graceful shutdown: stop threads, save state, clean up GPIO."""
        logging.info("Shutting down TempBot...")
        self._stop_event.set()

        # Wait for worker threads
        for t in self._threads:
            t.join(timeout=5)
            if t.is_alive():
                logging.warning("Thread %s did not stop in time.", t.name)

        self._watchdog.stop()

        # Persist final state
        self._persist_state()
        self._event_logger.sync_to_persistent()

        # Turn off outputs
        self.leds.all_off()
        self.buzzer.off()
        self.servo.set_angle(self.cfg["servo"]["center_angle"])
        self.servo.stop()

        # LCD goodbye
        self.lcd.write("TempBot", "Shutting down...")
        time.sleep(1)
        self.lcd.clear()
        self.lcd.set_backlight(False)

        # GPIO cleanup
        if GPIO:
            try:
                GPIO.cleanup()
            except Exception:
                pass

        self._emit_event("shutdown", {})
        logging.info("TempBot shutdown complete.")

    # ------------------------------------------------------------------
    # Worker threads
    # ------------------------------------------------------------------

    def _start_thread(self, name: str, target) -> None:
        t = threading.Thread(target=target, daemon=True, name=name)
        t.start()
        self._threads.append(t)
        logging.info("Started thread: %s", name)

    def _state_publisher_loop(self) -> None:
        """Continuously writes state.json to tmpfs and checks commands from web dashboard."""
        while not self._stop_event.is_set():
            try:
                # 1. Publish state snapshot to tmpfs for web dashboard
                snapshot = self.state.snapshot()
                SafeFileWriter.write_json(self._tmpfs_state_file, snapshot)

                # 2. Watch for servo command
                if os.path.exists(self._servo_cmd_file):
                    try:
                        with open(self._servo_cmd_file, "r") as fh:
                            cmd = json.load(fh)
                        os.remove(self._servo_cmd_file)
                        angle = int(cmd.get("angle", 90))
                        angle = max(0, min(180, angle))
                        self.servo.set_angle(angle)
                        self.state.update(servo_angle=angle, auto_scan=False)
                        logging.info("Servo commanded from Web Dashboard: %d°", angle)
                    except Exception as err:
                        logging.warning("Error reading servo_cmd: %s", err)

                # 3. Watch for threshold command
                if os.path.exists(self._threshold_cmd_file):
                    try:
                        with open(self._threshold_cmd_file, "r") as fh:
                            cmd = json.load(fh)
                        os.remove(self._threshold_cmd_file)
                        thresh = float(cmd.get("threshold", 37.5))
                        self.state.update(fever_threshold=thresh)
                        self.cfg["thresholds"]["fever_temp"] = thresh
                        logging.info("Fever threshold updated from Web Dashboard: %.1f°C", thresh)
                    except Exception as err:
                        logging.warning("Error reading threshold_cmd: %s", err)

                # 4. Watch for autoscan command
                if os.path.exists(self._autoscan_cmd_file):
                    try:
                        with open(self._autoscan_cmd_file, "r") as fh:
                            cmd = json.load(fh)
                        os.remove(self._autoscan_cmd_file)
                        auto_val = bool(cmd.get("auto_scan", True))
                        self.state.update(auto_scan=auto_val)
                        logging.info("Auto-scan updated from Web Dashboard: %s", auto_val)
                    except Exception as err:
                        logging.warning("Error reading autoscan_cmd: %s", err)

            except Exception as exc:
                logging.error("State publisher error: %s", exc)

            self._stop_event.wait(0.3)

    def _sensor_loop(self) -> None:
        """Main sensing loop – measures distance and temperature."""
        timing = self.cfg["timing"]
        thresholds = self.cfg["thresholds"]
        interval = timing["measurement_interval"]
        hold_time = timing["person_hold_time"]
        fever_temp = thresholds["fever_temp"]
        detection_dist = thresholds["detection_distance"]
        min_temp = thresholds["min_valid_temp"]
        max_temp = thresholds["max_valid_temp"]

        person_last_seen = 0.0

        while not self._stop_event.is_set():
            try:
                # -- Distance measurement ----------------------------------
                distance = self.ultrasonic.measure()
                if distance < 0:
                    self._stop_event.wait(interval)
                    continue

                self.state.update(distance=distance)

                person_now = (
                    thresholds["min_valid_distance"] <= distance <= detection_dist
                )

                if person_now:
                    person_last_seen = time.time()

                # Keep "person detected" flag high for hold_time after last
                # positive detection to avoid flicker.
                person_detected = (time.time() - person_last_seen) < hold_time

                if person_detected and not self.state.person_detected:
                    # Rising edge – person just arrived
                    logging.info("Person detected at %.1f cm.", distance)
                    self._emit_event("person_detected", {"distance": distance})
                elif not person_detected and self.state.person_detected:
                    # Falling edge – person left
                    logging.info("Person left.")
                    self._emit_event("person_left", {})
                    self.lcd.write("TempBot Ready", "Scanning...")

                self.state.update(person_detected=person_detected)

                # -- Temperature measurement (only when person is close) ----
                if person_detected:
                    temp = self.temp_sensor.read_object_temp()

                    if min_temp <= temp <= max_temp:
                        now_str = datetime.now().strftime("%H:%M:%S")
                        fever = temp >= fever_temp

                        snap_before = self.state.snapshot()
                        self.state.update(
                            temperature=temp,
                            fever_detected=fever,
                            last_reading_time=now_str,
                            total_readings=snap_before["total_readings"] + 1,
                            fever_count=snap_before["fever_count"] + (1 if fever else 0),
                            normal_count=snap_before["normal_count"] + (0 if fever else 1),
                        )

                        if fever:
                            self.state.update(status="FEVER DETECTED!")
                            self.leds.red_on()
                            self.lcd.write(
                                f"TEMP: {temp:.1f}C",
                                "!! FEVER !!",
                            )
                            self.buzzer.beep(
                                duration=timing["buzzer_beep_duration"],
                                count=timing["buzzer_beep_count"],
                                pause=timing["buzzer_beep_pause"],
                            )
                            self._emit_event("fever_alert", {"temp": temp, "distance": distance})
                        else:
                            self.state.update(status="Normal temperature")
                            self.leds.green_on()
                            self.lcd.write(
                                f"TEMP: {temp:.1f}C",
                                "Normal - OK",
                            )
                            self._emit_event("reading", {"temp": temp, "distance": distance, "status": "normal"})

                        # Record in history buffer and write to tmpfs for web dashboard charts
                        reading_entry = {
                            "timestamp": datetime.now().isoformat(),
                            "temperature": temp,
                            "distance_cm": distance,
                            "fever": fever,
                        }
                        self._history_readings.append(reading_entry)
                        if len(self._history_readings) > 100:
                            self._history_readings = self._history_readings[-100:]
                        try:
                            SafeFileWriter.write_json(self._tmpfs_history_file, self._history_readings)
                        except Exception:
                            pass

                    else:
                        logging.debug("Invalid temperature reading: %.2f°C", temp)

                else:
                    self.leds.all_off()
                    self.state.update(
                        fever_detected=False,
                        status="Scanning for people...",
                    )

            except Exception as exc:
                logging.error("Sensor loop error: %s", exc)
                self._emit_event("error", {"source": "sensor_loop", "message": str(exc)})

            self._stop_event.wait(interval)

    def _servo_loop(self) -> None:
        """Servo control loop – scans when idle, centres when person detected."""
        servo_cfg = self.cfg["servo"]
        timing = self.cfg["timing"]
        min_a = servo_cfg["min_angle"]
        max_a = servo_cfg["max_angle"]
        center = servo_cfg["center_angle"]
        step = servo_cfg["scan_speed"]
        delay = servo_cfg["scan_step_delay"]

        angle = center
        direction = 1  # 1 = increasing, -1 = decreasing

        while not self._stop_event.is_set():
            try:
                if self.state.person_detected:
                    # Centre the head when a person is detected
                    if angle != center:
                        angle = center
                        self.servo.set_angle(angle)
                        self.state.update(servo_angle=angle)
                    self._stop_event.wait(timing["idle_scan_delay"])
                elif not self.state.auto_scan:
                    # Manual servo mode via Web Dashboard - hold current angle
                    self._stop_event.wait(0.2)
                else:
                    # Scan left ↔ right
                    angle += step * direction
                    if angle >= max_a:
                        angle = max_a
                        direction = -1
                    elif angle <= min_a:
                        angle = min_a
                        direction = 1

                    self.servo.set_angle(angle)
                    self.state.update(servo_angle=angle)
                    self._stop_event.wait(delay)

            except Exception as exc:
                logging.error("Servo loop error: %s", exc)
                self._stop_event.wait(1.0)

    def _sync_loop(self) -> None:
        """Periodically persist state and sync event logs to SD card."""
        while not self._stop_event.is_set():
            self._stop_event.wait(self._sync_interval)
            if self._stop_event.is_set():
                break
            try:
                self._persist_state()
                self._event_logger.sync_to_persistent()
                logging.debug("Periodic sync complete.")
            except Exception as exc:
                logging.error("Sync loop error: %s", exc)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _persist_state(self) -> None:
        """Persist current state to SD card via WAL."""
        try:
            snapshot = self.state.snapshot()
            self._wal.log_and_write(self._state_file, snapshot)
            logging.debug("State persisted to %s.", self._state_file)
        except Exception as exc:
            logging.error("State persist error: %s", exc)

    def _restore_state(self) -> None:
        """Restore statistics from the last persisted state."""
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file, "r") as fh:
                data = json.load(fh)
            self.state.update(
                total_readings=data.get("total_readings", 0),
                fever_count=data.get("fever_count", 0),
                normal_count=data.get("normal_count", 0),
            )
            logging.info(
                "Restored state: %d readings (%d fever, %d normal).",
                data.get("total_readings", 0),
                data.get("fever_count", 0),
                data.get("normal_count", 0),
            )
        except Exception as exc:
            logging.warning("Could not restore state: %s", exc)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Log an event and push it to the web dashboard queue."""
        event = self._event_logger.log(event_type, data)
        try:
            self.event_queue.put_nowait(event)
        except queue.Full:
            # Drop oldest event to make room
            try:
                self.event_queue.get_nowait()
                self.event_queue.put_nowait(event)
            except queue.Empty:
                pass

    def get_recent_events(self, count: int = 50) -> List[Dict[str, Any]]:
        """Return recent events for the web dashboard.

        Args:
            count: Maximum number of events to return.

        Returns:
            List of event dicts, most-recent last.
        """
        return self._event_logger.get_recent(count)


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    """TempBot entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="TempBot – Temperature Checking Robot")
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to config.json (default: config.json in script directory)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Also launch the web dashboard server in background",
    )
    args = parser.parse_args()

    if args.web:
        try:
            from web_dashboard import start_dashboard
            web_t = threading.Thread(
                target=start_dashboard,
                kwargs={"host": "0.0.0.0", "port": 5000, "debug": False},
                daemon=True,
                name="web_dashboard",
            )
            web_t.start()
            logging.info("Web dashboard started alongside TempBot on port 5000.")
        except Exception as exc:
            logging.warning("Could not launch web dashboard: %s", exc)

    bot = TempBot(config_path=args.config)
    bot.start()


if __name__ == "__main__":
    main()
