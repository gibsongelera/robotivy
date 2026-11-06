# 🤖🌡️ TempBot — Raspberry Pi Humanoid Temperature-Checking Robot

<div align="center">

**An intelligent, contactless temperature screening robot built with Raspberry Pi 3 Model B**

[![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)](https://www.python.org/)
[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-3B-red.svg)](https://www.raspberrypi.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#license)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen.svg)]()

*Affordable · Contactless · Open Source · Beginner-Friendly*

</div>

---

## 📖 About

**TempBot** is a humanoid-shaped, contactless temperature screening robot designed for schools, offices, and public spaces. Built on a **Raspberry Pi 3 Model B**, it uses an **MLX90614 infrared sensor** to measure body temperature without physical contact, displays results on a **16×2 LCD**, and provides visual/audio feedback through LEDs and a buzzer.

The robot automatically detects when a person approaches using an **ultrasonic sensor**, waves its arm via a **servo motor**, takes a temperature reading, and indicates whether the temperature is normal (green LEDs) or elevated (red LEDs + buzzer alarm).

A built-in **Flask web dashboard** allows real-time monitoring and configuration over WiFi.

---

## ✨ Features

| Feature | Description |
|---|---|
| 🌡️ **Contactless Temperature** | MLX90614 IR sensor — no physical contact needed |
| 📏 **Auto-Detection** | HC-SR04 ultrasonic sensor detects approaching people |
| 📺 **LCD Display** | 16×2 I2C LCD shows temperature and status messages |
| 🦾 **Servo Arm Wave** | SG90 servo motor waves the robot's arm as a greeting |
| 🟢🔴 **LED Indicators** | Green = Normal temp, Red = Elevated temp |
| 🔊 **Buzzer Alert** | Audible alarm when elevated temperature is detected |
| 🌐 **Web Dashboard** | Real-time monitoring via Flask + WebSocket |
| 📡 **WiFi Hotspot** | Built-in AP mode — no existing network required |
| 💾 **SD Card Safety** | tmpfs overlay + atomic writes prevent card corruption |
| 📊 **Data Logging** | Persistent CSV logging of all temperature readings |
| 🔧 **Serial Monitor** | Debug and configure via USB serial connection |
| 🔌 **Graceful Shutdown** | Safe power-down procedures to protect hardware |

---

## 🧩 Components

| Component | Quantity | Purpose | Approx. Cost (₱) |
|---|:---:|---|---:|
| [Raspberry Pi 3 Model B](https://www.raspberrypi.org/products/raspberry-pi-3-model-b/) | 1 | Main controller | ₱400–600 |
| [MLX90614 IR Temperature Sensor](https://www.melexis.com/en/product/MLX90614/) | 1 | Contactless temperature measurement (I2C) | ₱150–250 |
| [I2C LCD 16×2 Display](https://www.sunfounder.com/) | 1 | Status and temperature display (I2C) | ₱80–120 |
| [HC-SR04 Ultrasonic Sensor](https://www.sparkfun.com/products/15569) | 1 | Distance/proximity detection | ₱30–50 |
| [SG90 Micro Servo Motor](https://www.towerpro.com.tw/) | 1 | Arm wave mechanism (PWM) | ₱40–60 |
| Passive Buzzer | 1 | Audio alerts | ₱10–20 |
| Green LED (5mm) | 2 | Normal temperature indicator | ₱5 |
| Red LED (5mm) | 2 | Elevated temperature indicator | ₱5 |
| 220Ω Resistor | 4 | Current limiting for LEDs | ₱5 |
| 1kΩ Resistor | 1 | HC-SR04 ECHO voltage divider | ₱2 |
| 2kΩ Resistor | 1 | HC-SR04 ECHO voltage divider | ₱2 |
| Breadboard + Jumper Wires | 1 set | Prototyping connections | ₱50–80 |
| MicroSD Card (16GB+) | 1 | OS and storage | ₱100–150 |
| 5V 2.5A Power Supply | 1 | Raspberry Pi power | ₱100–150 |

> **💰 Estimated Total Cost: ₱1,000 – ₱1,500**

---

## 🚀 Quick Start

### Step 1: Prepare the Hardware

1. Wire all components according to the [Wiring Diagram](WIRING_DIAGRAM.md)
2. Insert a flashed MicroSD card with Raspberry Pi OS Lite
3. Connect a 5V 2.5A power supply

### Step 2: Enable I2C on the Raspberry Pi

```bash
sudo raspi-config
# Navigate to: Interfacing Options → I2C → Enable
sudo reboot
```

### Step 3: Clone the Project

```bash
cd /home/pi
git clone https://github.com/your-username/robotivy.git
cd robotivy
```

### Step 4: Install Dependencies

```bash
sudo apt-get update
sudo apt-get install -y python3-pip python3-smbus i2c-tools
pip3 install -r raspberry_pi/requirements.txt
```

### Step 5: Verify I2C Devices

```bash
sudo i2cdetect -y 1
```

You should see devices at addresses **0x27** (LCD) and **0x5A** (MLX90614).

### Step 6: Configure SD Card Safety & Hotspot

```bash
sudo bash raspberry_pi/sd_safety_setup.sh
sudo bash raspberry_pi/hotspot_setup.sh
```

### Step 7: Run TempBot

```bash
# Run standalone robot controller:
python3 main.py

# Or run with integrated Web Dashboard:
python3 main.py --web
```

### Step 8: Access the Web Dashboard

Connect to the **TempBot-Monitor** WiFi hotspot (password: `tempbot123`), then open in your browser:

```
http://192.168.4.1:5000
# or
http://tempbot.local:5000
```

---

## 📦 Installation

### Prerequisites

- Raspberry Pi 3 Model B with Raspberry Pi OS (Bullseye or later)
- Python 3.7 or higher
- I2C enabled via `raspi-config`
- Internet connection (for initial package installation)

### System Dependencies

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y \
    python3-pip \
    python3-smbus \
    python3-dev \
    i2c-tools \
    libatlas-base-dev \
    hostapd \
    dnsmasq
```

### Python Dependencies

```bash
pip3 install flask flask-socketio smbus2 RPi.GPIO Adafruit-MLX90614 gevent
```

Or install from the requirements file:

```bash
pip3 install -r raspberry_pi/requirements.txt
```

### Auto-Start on Boot (Systemd Services)

TempBot includes pre-configured systemd service units with root filesystem write protections:

```bash
# Copy service files
sudo cp systemd/*.service /etc/systemd/system/

# Reload systemd and enable services
sudo systemctl daemon-reload
sudo systemctl enable --now tempbot.service
sudo systemctl enable --now tempbot-web.service

# Check status
sudo systemctl status tempbot.service
sudo systemctl status tempbot-web.service
```

---

## 💾 SD Card Safety Features

TempBot includes multiple layers of protection to prevent MicroSD card corruption — the #1 killer of Raspberry Pi projects.

| Safety Layer | What It Does |
|---|---|
| **tmpfs Overlay** | Logs and temp files write to RAM, not the SD card |
| **Atomic Writes** | Data files are written atomically (write-to-temp → rename) |
| **Read-Only Root** | The root filesystem can be mounted read-only |
| **log2ram** | Redirects `/var/log` to a RAM disk |
| **Volatile Journald** | systemd journal stays in RAM |
| **Graceful Shutdown** | Signal handlers ensure clean unmount on power-off |

> 📄 Full details: [SD_CARD_SAFETY.md](SD_CARD_SAFETY.md)

---

## 📡 WiFi Hotspot Setup

TempBot can create its own WiFi access point so you can connect to the web dashboard without any existing network infrastructure.

### Automatic Setup

```bash
sudo bash raspberry_pi/hotspot_setup.sh
```

### Manual Setup

1. **Configure hostapd** (`/etc/hostapd/hostapd.conf`):

```conf
interface=wlan0
driver=nl80211
ssid=TempBot
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=tempbot123
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
```

2. **Configure dnsmasq** (`/etc/dnsmasq.conf`):

```conf
interface=wlan0
dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h
address=/#/192.168.4.1
```

3. **Set static IP** for wlan0:

```bash
sudo ip addr add 192.168.4.1/24 dev wlan0
sudo systemctl start hostapd
sudo systemctl start dnsmasq
```

### Connection Details

| Setting | Value |
|---|---|
| SSID | `TempBot` |
| Password | `tempbot123` |
| Dashboard URL | `http://192.168.4.1:5000` |

---

## 🌐 Web Dashboard

The Flask-based web dashboard provides real-time monitoring and control.

### Features

- 📊 **Live temperature readings** with auto-refresh via WebSocket
- 📈 **Temperature history** graph (last 50 readings)
- ⚙️ **Configuration panel** — adjust thresholds, sensor settings
- 📋 **Log viewer** — view recent scan logs
- 🔄 **System status** — CPU temp, uptime, memory usage
- 📥 **Data export** — download readings as CSV

### Accessing the Dashboard

1. Connect to the TempBot WiFi (or same LAN if Ethernet is used)
2. Open a browser and navigate to `http://<pi-ip>:5000`
3. Default credentials: **admin / tempbot** (change on first login)

---

## 🔌 Serial Monitor

For debugging without WiFi, connect via USB serial:

```bash
# On your computer (Linux/macOS)
screen /dev/ttyUSB0 115200

# On your computer (Windows) — use PuTTY or
# Device Manager → COM port → connect at 115200 baud
```

The serial monitor outputs:
- Temperature readings in real time
- Sensor status and error messages
- Distance measurements
- System health metrics

---

## 📁 Project Structure

```
robotivy/
├── main.py                        # Root convenience entry point (supports --web flag)
├── README.md                      # Comprehensive project documentation
├── WIRING_DIAGRAM.md              # Complete hardware wiring diagrams & pinout
├── TROUBLESHOOTING.md             # Diagnostic procedures & testing guides
├── SD_CARD_SAFETY.md              # Deep dive into SD card corruption prevention
│
├── raspberry_pi/
│   ├── tempbot.py                 # Main humanoid robot controller (sensors, LCD, servo, alerts)
│   ├── web_dashboard.py           # Flask + SocketIO web server & Serial Monitor
│   ├── config.json                # Central hardware & network configuration
│   ├── requirements.txt           # Python library dependencies
│   ├── sd_safety_setup.sh         # Shell script: tmpfs, disable swap, log2ram
│   ├── hotspot_setup.sh           # Shell script: hostapd & dnsmasq WiFi hotspot
│   └── templates/
│       └── dashboard.html         # Rich single-page web dashboard & terminal console
│
└── systemd/
    ├── tempbot.service            # Systemd service for auto-starting robot
    └── tempbot-web.service        # Systemd service for web dashboard
```

---

## ⚙️ Configuration

All configuration is centralized in [`raspberry_pi/config.json`](raspberry_pi/config.json):

```python
# Temperature thresholds (°C)
TEMP_THRESHOLD_NORMAL = 37.5      # Below this = normal (green)
TEMP_THRESHOLD_ELEVATED = 38.0    # Above this = elevated (red + buzzer)

# Distance threshold (cm)
DETECTION_DISTANCE = 30           # Person must be within 30cm

# Timing
READING_COOLDOWN = 3              # Seconds between readings
SERVO_WAVE_DELAY = 0.5            # Seconds for arm wave animation

# GPIO Pins (BCM numbering)
PIN_GREEN_LED = 17
PIN_RED_LED = 27
PIN_BUZZER = 22
PIN_TRIG = 23
PIN_ECHO = 24
PIN_SERVO = 18

# I2C Addresses
I2C_LCD_ADDRESS = 0x27
I2C_MLX_ADDRESS = 0x5A

# Web Server
WEB_HOST = '0.0.0.0'
WEB_PORT = 5000
```

---

## 📡 API Documentation

The web dashboard exposes a RESTful API for integration with other systems.

### Endpoints

#### `GET /api/status`

Returns current system status.

```json
{
  "status": "running",
  "uptime": "2h 15m",
  "cpu_temp": 42.3,
  "memory_usage": 34.5,
  "total_scans": 127
}
```

#### `GET /api/temperature`

Returns the latest temperature reading.

```json
{
  "temperature": 36.4,
  "unit": "celsius",
  "status": "normal",
  "timestamp": "2025-01-15T14:30:22Z",
  "ambient_temp": 25.1
}
```

#### `GET /api/readings`

Returns temperature reading history.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 50 | Max number of readings |
| `offset` | int | 0 | Pagination offset |

```json
{
  "readings": [
    {
      "id": 1,
      "temperature": 36.4,
      "status": "normal",
      "distance": 15.2,
      "timestamp": "2025-01-15T14:30:22Z"
    }
  ],
  "total": 127,
  "limit": 50,
  "offset": 0
}
```

#### `GET /api/config`

Returns current configuration values.

```json
{
  "temp_threshold_normal": 37.5,
  "temp_threshold_elevated": 38.0,
  "detection_distance": 30,
  "reading_cooldown": 3
}
```

#### `POST /api/config`

Updates configuration values. Requires JSON body.

```json
{
  "temp_threshold_normal": 37.5,
  "temp_threshold_elevated": 38.0,
  "detection_distance": 25
}
```

**Response:**
```json
{
  "success": true,
  "message": "Configuration updated"
}
```

#### `GET /api/readings/export`

Downloads all readings as a CSV file.

#### `POST /api/system/restart`

Restarts the TempBot service.

#### `POST /api/system/shutdown`

Initiates a graceful shutdown.

#### WebSocket Events

| Event | Direction | Description |
|---|---|---|
| `new_reading` | Server → Client | New temperature reading available |
| `status_update` | Server → Client | System status changed |
| `alert` | Server → Client | Elevated temperature detected |
| `config_changed` | Server → Client | Configuration was updated |

---

## 🔄 How It Works

TempBot operates in a continuous 4-step loop:

```
┌─────────────────────────────────────────────────────┐
│                  TempBot Main Loop                   │
├─────────────────────────────────────────────────────┤
│                                                     │
│   Step 1: DETECT 📏                                 │
│   ├─ HC-SR04 sends ultrasonic pulse                 │
│   ├─ Measures echo return time                      │
│   ├─ Calculates distance in cm                      │
│   └─ If person within 30cm → proceed                │
│                                                     │
│   Step 2: GREET 🦾                                  │
│   ├─ Servo motor waves the arm (0° → 90° → 0°)     │
│   ├─ LCD displays "Please wait..."                  │
│   └─ Brief pause for positioning                    │
│                                                     │
│   Step 3: MEASURE 🌡️                                │
│   ├─ MLX90614 reads object temperature (IR)         │
│   ├─ Also reads ambient temperature                 │
│   ├─ Temperature displayed on LCD                   │
│   └─ Reading logged to CSV via atomic write         │
│                                                     │
│   Step 4: INDICATE ✅❌                              │
│   ├─ If temp < 37.5°C:                              │
│   │   ├─ Green LEDs ON                              │
│   │   ├─ LCD shows "Normal"                         │
│   │   └─ Short beep                                 │
│   ├─ If temp ≥ 37.5°C:                              │
│   │   ├─ Red LEDs ON                                │
│   │   ├─ LCD shows "ELEVATED!"                      │
│   │   └─ Buzzer alarm (3 long beeps)                │
│   └─ Results pushed to Web Dashboard via WebSocket  │
│                                                     │
│   ↻ Cooldown (3 seconds) → Return to Step 1        │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

## 🤝 Contributing

Contributions are welcome! Here's how you can help:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a Pull Request

### Ideas for Contributions

- 🎨 Improved web dashboard UI
- 📱 Mobile app companion
- 🗣️ Text-to-speech announcements
- 📸 Camera integration for face detection
- 📊 Advanced analytics and reporting
- 🌍 Multi-language LCD messages

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

```
MIT License

Copyright (c) 2025 TempBot Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 🙏 Credits & Acknowledgments

- **[Raspberry Pi Foundation](https://www.raspberrypi.org/)** — For the amazing Raspberry Pi platform
- **[Melexis](https://www.melexis.com/)** — MLX90614 infrared temperature sensor
- **[Adafruit](https://www.adafruit.com/)** — Python libraries and learning resources
- **[Flask](https://flask.palletsprojects.com/)** — Lightweight Python web framework
- **[Socket.IO](https://socket.io/)** — Real-time WebSocket communication
- **Open source community** — For inspiration and shared knowledge

---

<div align="center">

**Built with ❤️ for safer, healthier spaces**

🤖🌡️ *TempBot — Because health screening should be smart, simple, and affordable.*

</div>
