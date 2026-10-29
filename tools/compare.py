"""Offline comparison: PID vs. on/off control with hysteresis on the same single-room scenario.

Usage:  python tools/compare.py [--log]
No MQTT or Docker needed; it uses room_model.py and control.py directly.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "simulator"))
sys.path.insert(0, os.path.join(ROOT, "controller"))

from room_model import RoomModel, occupancy  # noqa: E402
from control import make_controllers  # noqa: E402

SP_TEMP, SP_CO2 = 21.0, 1000.0
SAMPLE = 60.0  # control period, simulated seconds


def run(mode, hours=24, seed=1, log=False):
    room = RoomModel(start_hour=7, seed=seed)
    c_temp, c_co2 = make_controllers(mode)
    stats = dict(n=0, t_err=0.0, co2_sum=0.0, co2_max=0.0, over=0, fan=0.0, valve=0.0)
    steps = int(hours * 3600)
    for i in range(steps):
        if i % int(SAMPLE) == 0:
            s = room.read_sensors()
            room.set_valve(c_temp.update(SP_TEMP, s["temperature"], SAMPLE))
            room.set_fan(c_co2.update(SP_CO2, s["co2"], SAMPLE))
            if occupancy(room.hour) > 0:
                stats["n"] += 1
                stats["t_err"] += abs(room.temp - SP_TEMP)
                stats["co2_sum"] += room.co2
                stats["co2_max"] = max(stats["co2_max"], room.co2)
                stats["over"] += room.co2 > SP_CO2 + 100
            stats["fan"] += room.fan
            stats["valve"] += room.valve
            if log and (i // int(SAMPLE)) % 60 == 0:
                print(f"{room.state()['sim_time']} T={room.temp:5.2f} CO2={room.co2:6.0f} fan={room.fan:5.1f} valve={room.valve:5.1f}")
        room.step(1.0)
    samples = steps / SAMPLE
    n = max(stats["n"], 1)
    return {
        "Mean temperature error while occupied (K)": stats["t_err"] / n,
        "Mean CO2 while occupied (ppm)": stats["co2_sum"] / n,
        "Peak CO2 (ppm)": stats["co2_max"],
        "Time with CO2 > 1100 ppm (% of occupied)": 100 * stats["over"] / n,
        "Mean fan (%)": stats["fan"] / samples,
        "Mean valve (%)": stats["valve"] / samples,
    }


if __name__ == "__main__":
    results = {m: run(m) for m in ("onoff", "pid")}
    print(f"{'Metric':<46}{'On/off':>10}{'PID':>10}")
    for k in results["pid"]:
        print(f"{k:<46}{results['onoff'][k]:>10.2f}{results['pid'][k]:>10.2f}")
    if "--log" in sys.argv:
        run("pid", log=True)