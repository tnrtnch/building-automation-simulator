"""House simulator: advances all four rooms together, publishes each room's sensors over MQTT and listens for actuator commands.

Topics (BASE_TOPIC = building/f1, room = living_room | kitchen | bedroom | study):
  publish:    <BASE>/<room>/sensor           {"temperature":..,"humidity":..,"co2":..,"ts":..}
  publish:    <BASE>/<room>/status           {"sim_time":..,"occupancy":..,"outdoor_temp":..,"fan":..,"valve":..,...}
  subscribe:  <BASE>/<room>/actuator/fan     0-100 (plain number)
  subscribe:  <BASE>/<room>/actuator/valve   0-100 (plain number)
"""
import json
import os
import time

import paho.mqtt.client as mqtt

from house_model import House

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
BASE = os.getenv("BASE_TOPIC", "building/f1")
SPEED = float(os.getenv("SPEED", "60"))                  # 1 real second = SPEED simulated seconds
START_HOUR = float(os.getenv("START_HOUR", "17"))
PUBLISH_EVERY = float(os.getenv("PUBLISH_EVERY", "1.0"))  # real seconds

house = House(start_hour=START_HOUR)


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"MQTT connected: {reason_code}")
    client.subscribe(f"{BASE}/+/actuator/#")


def on_message(client, userdata, msg):
    parts = msg.topic[len(BASE) + 1:].split("/")          # [room, 'actuator', 'fan'|'valve']
    if len(parts) != 3 or parts[1] != "actuator" or parts[0] not in house.rooms:
        return
    try:
        value = float(msg.payload.decode().strip())
    except ValueError:
        print(f"Invalid payload ({msg.topic}): {msg.payload!r}")
        return
    room = house.rooms[parts[0]]
    if parts[2] == "fan":
        room.set_fan(value)
    elif parts[2] == "valve":
        room.set_valve(value)


def connect() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="house-simulator")
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            break
        except OSError as exc:
            print(f"Broker not ready ({exc}), retrying in 2 s...")
            time.sleep(2)
    client.loop_start()   # paho handles reconnects
    return client


def main():
    client = connect()
    sim_steps = max(1, int(round(SPEED * PUBLISH_EVERY)))
    print(f"Simulation started: {', '.join(house.rooms)} | speed x{SPEED}, start {START_HOUR:.0f}:00")
    while True:
        started = time.monotonic()
        for _ in range(sim_steps):
            house.step(1.0)
        ts = int(time.time())
        for name in house.rooms:
            sensors = house.read_sensors(name)
            sensors["ts"] = ts
            client.publish(f"{BASE}/{name}/sensor", json.dumps(sensors))
            client.publish(f"{BASE}/{name}/status", json.dumps(house.state(name)))
        time.sleep(max(0.0, PUBLISH_EVERY - (time.monotonic() - started)))


if __name__ == "__main__":
    main()