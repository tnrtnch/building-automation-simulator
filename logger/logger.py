"""Logger service: listens to all room topics on MQTT and writes wide rows to PostgreSQL.

Topics:  <BASE_TOPIC>/<floor>/<room>/{sensor,status,control/status}
On every 'sensor' message, the latest known values of that room are inserted as one row into `telemetry`.
Rows are buffered and written in batches every FLUSH_EVERY seconds; if the database is down they wait in the
buffer (up to MAX_BUFFER rows) and are written once it is back.
"""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import psycopg

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
BASE_TOPIC = os.getenv("BASE_TOPIC", "building")
PG_DSN = os.getenv("PG_DSN", "host=localhost dbname=climate user=climate password=climate")
FLUSH_EVERY = float(os.getenv("FLUSH_EVERY", "2.0"))
MAX_BUFFER = int(os.getenv("MAX_BUFFER", "10000"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    ts            timestamptz NOT NULL,
    floor         text NOT NULL,
    room          text NOT NULL,
    temperature   real,
    humidity      real,
    co2           integer,
    fan           real,
    valve         real,
    sp_temp       real,
    sp_co2        real,
    mode          text,
    failsafe      boolean,
    occupancy     integer,
    outdoor_temp  real,
    window_open   boolean,
    sim_time      text
);
CREATE INDEX IF NOT EXISTS telemetry_room_ts_idx ON telemetry (floor, room, ts DESC);
"""

COLUMNS = ["ts", "floor", "room", "temperature", "humidity", "co2", "fan", "valve",
           "sp_temp", "sp_co2", "mode", "failsafe", "occupancy", "outdoor_temp",
           "window_open", "sim_time"]
INSERT = f"INSERT INTO telemetry ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})"

buffer = deque(maxlen=MAX_BUFFER)
buffer_lock = threading.Lock()
latest = {}  # (floor, room) -> last known fields


def parse_topic(topic: str):
    """building/f1/kitchen/sensor -> ('f1', 'kitchen', 'sensor'); .../control/status -> (..., 'control/status')"""
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != BASE_TOPIC:
        return None
    return parts[1], parts[2], "/".join(parts[3:])


def handle_message(topic: str, payload: bytes, now=None):
    """Process one message; a 'sensor' message appends a row to the buffer. Kept separate from MQTT for testing."""
    parsed = parse_topic(topic)
    if parsed is None:
        return
    floor, room, kind = parsed
    if kind not in ("sensor", "status", "control/status"):
        return
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (ValueError, UnicodeDecodeError):
        print(f"Invalid JSON ({topic}): {payload[:80]!r}")
        return

    cur = latest.setdefault((floor, room), {})
    if kind == "status":            # values actually applied by the simulator
        cur.update(fan=data.get("fan"), valve=data.get("valve"), occupancy=data.get("occupancy"),
                   outdoor_temp=data.get("outdoor_temp"), window_open=data.get("window_open"),
                   sim_time=data.get("sim_time"))
    elif kind == "control/status":  # controller state
        cur.update(sp_temp=data.get("sp_temp"), sp_co2=data.get("sp_co2"),
                   mode=data.get("mode"), failsafe=data.get("failsafe"))
    else:                           # sensor: emit a row
        row = dict(cur)
        row.update(temperature=data.get("temperature"), humidity=data.get("humidity"),
                   co2=data.get("co2"))
        row.update(ts=now or datetime.now(timezone.utc), floor=floor, room=room)
        with buffer_lock:
            buffer.append(tuple(row.get(c) for c in COLUMNS))


def connect_db() -> psycopg.Connection:
    while True:
        try:
            conn = psycopg.connect(PG_DSN, autocommit=True)
            conn.execute(SCHEMA)
            print("PostgreSQL connected, table ready")
            return conn
        except psycopg.OperationalError as exc:
            print(f"PostgreSQL not ready ({str(exc).strip()[:80]}), retrying in 2 s...")
            time.sleep(2)


def flush_loop():
    conn = connect_db()
    while True:
        time.sleep(FLUSH_EVERY)
        with buffer_lock:
            rows = list(buffer)
            buffer.clear()
        if not rows:
            continue
        try:
            with conn.cursor() as cur:
                cur.executemany(INSERT, rows)
        except psycopg.Error as exc:
            print(f"Write failed, putting {len(rows)} rows back into the buffer: {str(exc).strip()[:80]}")
            with buffer_lock:
                buffer.extendleft(reversed(rows))
            try:
                conn.close()
            except psycopg.Error:
                pass
            conn = connect_db()


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"MQTT connected: {reason_code}")
    client.subscribe(f"{BASE_TOPIC}/+/+/sensor")
    client.subscribe(f"{BASE_TOPIC}/+/+/status")
    client.subscribe(f"{BASE_TOPIC}/+/+/control/status")


def on_message(client, userdata, msg):
    handle_message(msg.topic, msg.payload)


def main():
    threading.Thread(target=flush_loop, daemon=True).start()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="climate-logger")
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            break
        except OSError as exc:
            print(f"Broker not ready ({exc}), retrying in 2 s...")
            time.sleep(2)
    print("Logger started")
    client.loop_forever()


if __name__ == "__main__":
    main()