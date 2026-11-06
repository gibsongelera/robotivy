# 🔧 TempBot Troubleshooting & Diagnostic Manual

> Complete step-by-step diagnostic procedures, common error resolutions, and hardware testing instructions for TempBot on Raspberry Pi 3 Model B.

---

## 📑 Diagnostic Checklist

Before troubleshooting individual components, run this system-level diagnostic command:

```bash
# Check running TempBot services
sudo systemctl status tempbot
sudo systemctl status tempbot-web

# View real-time application logs
tail -f /tmp/tempbot/events.log

# Check I2C bus detection
sudo i2cdetect -y 1
```

---

## 1. 🌡️ MLX90614 Temperature Sensor Issues

### Symptom: `Err` or `Sensor not found` / No I2C response
* **Cause 1: Incorrect Wiring**
  * Check pin connections:
    * `VIN` → **3.3V** (Pin 1 or 17). *Do NOT connect MLX90614 to 5V!*
    * `GND` → Raspberry Pi **GND** (Pin 6, 9, 14, 20, 25, 30, 34, or 39)
    * `SDA` → GPIO2 (Pin 3)
    * `SCL` → GPIO3 (Pin 5)
* **Cause 2: I2C Interface Not Enabled**
  * Run `sudo raspi-config` → `Interface Options` → `I2C` → `Enable` → `Reboot`.
* **Cause 3: Missing Bus Detection**
  * Run:
    ```bash
    sudo i2cdetect -y 1
    ```
  * You should see **`5a`** displayed in the matrix. If it is blank, check for loose jumper wires or cold solder joints.

### Symptom: Temperature reading is too low (e.g., 32°C - 34°C)
* **Explanation:** Infrared sensors measure surface skin temperature, which is naturally 2°C to 4°C cooler than internal core body temperature.
* **Solution:**
  1. Measure distance must be **2 cm to 5 cm** away from the forehead or palm for an accurate reading.
  2. If measuring at 10–20 cm, the sensor reads a blend of room air and forehead temperature.
  3. You can configure an offset in `config.json` under `calibration_offset_c: 1.5` to compensate for ambient skin cooling.

---

## 2. 📺 I2C LCD 16×2 Display Issues

### Symptom: LCD backlight is ON, but no characters or solid black rectangles
* **Cause 1: Contrast Potentiometer Out of Adjustment**
  * Locate the small blue trim potentiometer with a brass screw on the back of the black I2C backpack board.
  * Use a small flat-head screwdriver to slowly rotate the screw clockwise or counter-clockwise while looking at the screen until text becomes crisp and sharp.
* **Cause 2: Incorrect I2C Address**
  * Some LCD backpacks use address `0x27`, while others (e.g. PCF8574A chip) use `0x3F`.
  * Run:
    ```bash
    sudo i2cdetect -y 1
    ```
  * Note the address that appears (usually `27` or `3f`).
  * If it is `0x3F`, update `config.json`:
    ```json
    "i2c": {
        "lcd_address": "0x3f"
    }
    ```

### Symptom: Garbled characters or flickering screen
* **Cause:** Voltage drop or interference on the I2C bus.
  * Ensure LCD `VCC` is connected to a stable **5V pin** (Pin 2 or Pin 4). LCD logic works with 3.3V I2C signals, but the display panel itself requires 5V.
  * Ensure jumper wires do not run parallel or touch the high-current servo power cables.

---

## 3. 📏 HC-SR04 Ultrasonic Distance Sensor Issues

### Symptom: Distance reads constant 0 cm, -1 cm, or 400 cm
* **Cause 1: ECHO Pin Voltage Divider Not Connected Properly**
  * The HC-SR04 produces a **5V ECHO output signal**, which will damage the 3.3V Raspberry Pi GPIO pin if connected directly!
  * Verify the voltage divider:
    ```
    HC-SR04 ECHO ──── 1kΩ Resistor ──── GPIO24 (Pin 18)
                                    │
                                  2kΩ Resistor
                                    │
                                   GND
    ```
* **Cause 2: Sensor Wiring Swapped**
  * Check that `TRIG` is on **GPIO23** (Pin 16) and `ECHO` is on **GPIO24** (Pin 18).
* **Cause 3: Echo Timeout**
  * Soft surfaces (fabrics, wool sweaters) absorb ultrasonic sound waves. Test with a flat book or cardboard piece held in front of the sensor.

---

## 4. 🦾 SG90 Servo Motor Jitter or Raspberry Pi Freezing

### Symptom: Servo chatters/jitters continuously or Raspberry Pi reboots when servo moves
* **Cause 1: Current Surge (Brownout)**
  * A moving servo draws up to 500mA–800mA stall current. When powered from the Raspberry Pi's 5V pin, it can drop the Pi's internal rail voltage below 4.63V, triggering an emergency CPU reset (brownout).
  * **Fix:** Connect the servo's power (Red wire) and Ground (Brown wire) to an **external 5V 1A battery or adapter**. Connect the external power ground to the Raspberry Pi ground (Common Ground).
* **Cause 2: Software PWM Jitter**
  * Software PWM suffers from microsecond timing fluctuations caused by Linux background tasks.
  * TempBot's `tempbot.py` handles this automatically by setting PWM duty cycle to `0` once movement finishes to immediately release hold torque.

---

## 5. 🌐 Web Dashboard & WiFi Hotspot Issues

### Symptom: Cannot connect to WiFi network `TempBot-Monitor`
* **Cause 1: WiFi blocked by RF-Kill**
  * Run:
    ```bash
    sudo rfkill unblock wlan
    ```
* **Cause 2: Service conflict with dhcpcd / NetworkManager**
  * Check hotspot service logs:
    ```bash
    sudo systemctl status hostapd
    sudo systemctl status dnsmasq
    ```
  * Verify `wlan0` has static IP `192.168.4.1`:
    ```bash
    ip addr show wlan0
    ```

### Symptom: Connected to WiFi, but `http://192.168.4.1:5000` does not load
* **Cause 1: Flask Web Dashboard not running**
  * Check if web dashboard process is active:
    ```bash
    sudo systemctl status tempbot-web
    ```
  * Start it manually to inspect output:
    ```bash
    python3 /home/pi/robotivy/raspberry_pi/web_dashboard.py
    ```
* **Cause 2: Port 5000 blocked or already in use**
  * Check listening ports:
    ```bash
    sudo netstat -tulpn | grep 5000
    ```

---

## 6. ⚡ Raspberry Pi Power & Undervoltage

### Symptom: Yellow lightning bolt icon on screen or Pi reboots randomly
* **Diagnostic Command:**
  ```bash
  vcgencmd get_throttled
  ```
  * Output `0x0`: Power is optimal.
  * Output `0x50000` or `0x50005`: Currently undervolted (voltage dropped below 4.63V).
* **Fix:** Use a certified **5V 2.5A or 3.0A power supply** with a thick 18AWG–20AWG USB cable. Cheap thin cables cause significant voltage drops under load.

---

## 7. 💾 MicroSD Card Recovery & Health Check

### Step 1: Check File System Integrity
If an abrupt power loss occurred before applying safety measures:
```bash
sudo touch /forcefsck
sudo reboot
```
The Raspberry Pi will run a thorough file check and repair inconsistent blocks on the next startup.

### Step 2: Test RAM tmpfs directory
Verify that TempBot's temporary files are properly residing in RAM:
```bash
df -h /tmp/tempbot
```
The Filesystem column should display `tmpfs`.

---

## 📋 Comprehensive Hardware Diagnostic Script

You can run this quick Python one-liner to verify all connected hardware simultaneously:

```python
# test_hardware.py
import time
import RPi.GPIO as GPIO
import smbus2

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# Test LEDs & Buzzer
pins = [17, 27, 22]
for p in pins:
    GPIO.setup(p, GPIO.OUT)
    GPIO.output(p, True)
    time.sleep(0.3)
    GPIO.output(p, False)

print("LEDs and Buzzer OK!")

# Test I2C Bus
bus = smbus2.SMBus(1)
try:
    bus.read_byte(0x5A)
    print("MLX90614 (0x5A) detected!")
except Exception:
    print("MLX90614 NOT responding on 0x5A")

try:
    bus.read_byte(0x27)
    print("LCD (0x27) detected!")
except Exception:
    print("LCD NOT responding on 0x27 (check if 0x3F)")

GPIO.cleanup()
```
Run with: `sudo python3 test_hardware.py`
