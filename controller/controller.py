"""Multi-room control service: per-room PID/on-off control, setpoints, mode and safety logic.

Topics (BASE_TOPIC = building/f1; rooms are discovered from their sensor messages):
  subscribe:  <BASE>/<room>/sensor                  measurements from the simulator
  subscribe:  <BASE>/<room>/setpoint/temperature    temperature setpoint (C)
  subscribe:  <BASE>/<room>/setpoint/co2            CO2 setpoint (ppm), default 1000
  subscribe:  <BASE>/<room>/setpoint/fan_on_temp    temperature at which the fan turns on (C), default 24
  subscribe:  <BASE>/<room>/control/mode            "pid" | "onoff"
  publish:    <BASE>/<room>/actuator/valve|fan      0-100
  publish:    <BASE>/<room>/control/status          JSON (mode, setpoints, outputs, overheat, failsafe)

Fail-safe: if a room's sensor data stops for SENSOR_TIMEOUT seconds, that room goes to valve closed, fan 50 %.
"""
import json
import os
import threading
import time

import paho.mqtt.client as mqtt

from control import OverheatGuard, make_controllers

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
BASE = os.getenv("BASE_TOPIC", "building/f1")
SPEED = float(os.getenv("SPEED", "60"))                     # must match the simulator (used for dt)
SENSOR_TIMEOUT = float(os.getenv("SENSOR_TIMEOUT", "10"))   # real seconds
MODE = os.getenv("MODE", "pid")
DEFAULT_SP_TEMP = float(os.getenv("SP_TEMP", "21"))
DEFAULT_SP_CO2 = float(os.getenv("SP_CO2", "1000"))
FAN_ON_TEMP = float(os.getenv("FAN_ON_TEMP", "24"))
FAN_ON_HYST = float(os.getenv("FAN_ON_HYST", "0.5"))
DEFAULT_SAMPLE = 60.0                                       # dt for the first sample (simulated seconds)
SAFE_VALVE, SAFE_FAN = 0.0, 50.0
ROOM_SP_TEMP = {"living_room": 21.0, "kitchen": 20.0, "bedroom": 18.5, "study": 21.0}  # per-room default setpoints

lock = threading.Lock()
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="house-controller")


class RoomCtl:
    def __init__(self, name):
        self.name = name
        self.mode = MODE
        self.sp_temp = ROOM_SP_TEMP.get(name, DEFAULT_SP_TEMP)
        self.sp_co2 = DEFAULT_SP_CO2
        self.c_temp, self.c_co2 = make_controllers(self.mode)
        self.guard = OverheatGuard(on_temp=FAN_ON_TEMP, hysteresis=FAN_ON_HYST)
        self.valve = self.fan = 0.0
        self.failsafe = False
        self.last = None     # monotonic time of the last valid sensor message

    def topic(self, suffix):
        return f"{BASE}/{self.name}/{suffix}"

    def publish_outputs(self, valve, fan):
        client.publish(self.topic("actuator/valve"), f"{valve:.1f}")
        client.publish(self.topic("actuator/fan"), f"{fan:.1f}")
        self.valve, self.fan = valve, fan

    def publish_status(self):
        client.publish(self.topic("control/status"), json.dumps({
            "mode": self.mode, "sp_temp": self.sp_temp, "sp_co2": self.sp_co2,
            "valve": round(self.valve, 1), "fan": round(self.fan, 1),
            "fan_on_temp": self.guard.on_temp, "overheat": self.guard.active, "failsafe": self.failsafe}))

    def set_mode(self, mode):
        self.mode = mode
        self.c_temp, self.c_co2 = make_controllers(mode)
        print(f"[{self.name}] mode: {mode}")

    def on_sensor(self, s):
        now = time.monotonic()
        if self.last is None or self.failsafe:
            dt = DEFAULT_SAMPLE
            self.c_temp.reset()
            self.c_co2.reset()
        else:
            dt = min(600.0, max(1.0, (now - self.last) * SPEED))
        self.last = now
        self.failsafe = False
        valve = self.c_temp.update(self.sp_temp, s["temperature"], dt)
        fan = self.c_co2.update(self.sp_co2, s["co2"], dt)
        valve, fan = self.guard.update(s["temperature"], valve, fan)   # above the threshold: fan on, valve closed
        self.publish_outputs(valve, fan)
        self.publish_status()


rooms = {}


def valid(s) -> bool:
    try:
        return -20 <= s["temperature"] <= 60 and 0 <= s["humidity"] <= 100 and 300 <= s["co2"] <= 10000
    except (KeyError, TypeError):
        return False


def clamp_float(payload, lo, hi):
    try:
        return min(hi, max(lo, float(payload)))
    except ValueError:
        print(f"Invalid value: {payload!r}")
        return None


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"MQTT connected: {reason_code}")
    client.subscribe([(f"{BASE}/+/sensor", 0), (f"{BASE}/+/setpoint/#", 0), (f"{BASE}/+/control/mode", 0)])


def on_message(client, userdata, msg):
    rel = msg.topic[len(BASE) + 1:].split("/", 1)          # [room, 'sensor' | 'setpoint/co2' | ...]
    if len(rel) != 2:
        return
    name, kind = rel
    payload = msg.payload.decode(errors="replace").strip()
    with lock:
        rc = rooms.get(name)
        if rc is None:
            rc = rooms[name] = RoomCtl(name)
            print(f"New room: {name} (setpoint {rc.sp_temp} C, CO2 {rc.sp_co2} ppm)")
        if kind == "setpoint/temperature":
            v = clamp_float(payload, 10.0, 30.0)
            rc.sp_temp = rc.sp_temp if v is None else v
        elif kind == "setpoint/co2":
            v = clamp_float(payload, 400.0, 2000.0)
            rc.sp_co2 = rc.sp_co2 if v is None else v
        elif kind == "setpoint/fan_on_temp":
            v = clamp_float(payload, 20.0, 35.0)
            rc.guard.on_temp = rc.guard.on_temp if v is None else v
        elif kind == "control/mode":
            if payload in ("pid", "onoff"):
                rc.set_mode(payload)
            else:
                print(f"Invalid mode: {payload!r}")
        elif kind == "sensor":
            try:
                s = json.loads(payload)
            except json.JSONDecodeError:
                print(f"[{name}] invalid JSON: {payload!r}")
                return
            if not valid(s):
                print(f"[{name}] invalid sensor data, ignored: {s}")
                return
            rc.on_sensor(s)


def watchdog_loop():
    """If one room's sensor data stops, only that room switches to the safe state."""
    while True:
        time.sleep(1.0)
        with lock:
            for rc in rooms.values():
                if rc.last is not None and not rc.failsafe and time.monotonic() - rc.last > SENSOR_TIMEOUT:
                    rc.failsafe = True
                    print(f"WARNING [{rc.name}]: no sensor data, switching to the safe state")
                    rc.publish_outputs(SAFE_VALVE, SAFE_FAN)
                    rc.publish_status()


def main():
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            break
        except OSError as exc:
            print(f"Broker not ready ({exc}), retrying in 2 s...")
            time.sleep(2)
    client.loop_start()
    print(f"Controller started: mode={MODE}, rooms are discovered from {BASE}/<room>/sensor")
    watchdog_loop()


if __name__ == "__main__":
    main()