"""Four-room house model: per-room parameters, heat flow between neighbouring rooms, per-room occupancy.

The physics of one room is the same as in room_model.py (thermal model + CO2 mass balance + simple humidity).
Differences: each room has its own volume, wall loss, heater power and fan flow, plus heat flow through partition walls.
"""
import random
from dataclasses import dataclass

from room_model import (CO2_OUTDOOR, CO2_PER_PERSON, Q_PERSON, Q_WINDOW, RH_OUTDOOR,
                        RH_PER_PERSON, RHO_CP, outdoor_temp)

UA_PARTITION = 30.0  # W/K, partition wall between neighbouring rooms


@dataclass(frozen=True)
class RoomSpec:
    name: str
    volume: float      # m3
    ua: float          # W/K, heat loss to the outside
    c_thermal: float   # J/K
    q_heater: float    # W at valve 100 %
    q_fan_max: float   # m3/s at fan 100 %
    schedule: tuple    # (start_hour, end_hour, people, extra_heat_W)


SPECS = (
    RoomSpec("living_room", 75, 110, 1.6e6, 2500, 0.06, ((13, 17, 1, 0), (17, 23, 3, 0))),
    RoomSpec("kitchen", 35, 60, 0.8e6, 1200, 0.05, ((7, 8, 2, 300), (12, 13, 1, 300), (18, 19.5, 2, 900))),
    RoomSpec("bedroom", 40, 70, 0.9e6, 1500, 0.04, ((0, 7, 2, 0), (23, 24, 2, 0))),
    RoomSpec("study", 30, 55, 0.7e6, 1000, 0.04, ((9, 17, 1, 0),)),
)
# Layout (2x2):  living_room | kitchen
#                bedroom     | study
NEIGHBORS = (("living_room", "kitchen"), ("living_room", "bedroom"), ("kitchen", "study"), ("bedroom", "study"))


def clamp(v, lo, hi):
    return min(hi, max(lo, v))


class Room:
    def __init__(self, spec: RoomSpec, temp: float = 18.0):
        self.spec = spec
        self.temp, self.co2, self.rh = temp, 450.0, 45.0
        self.fan = self.valve = 0.0
        self.window_until = 0.0

    def set_fan(self, pct):
        self.fan = clamp(pct, 0.0, 100.0)

    def set_valve(self, pct):
        self.valve = clamp(pct, 0.0, 100.0)


class House:
    def __init__(self, start_hour=17.0, seed=None, specs=SPECS, neighbors=NEIGHBORS):
        self.t = start_hour * 3600.0
        self.rng = random.Random(seed)
        self.rooms = {s.name: Room(s) for s in specs}
        self.neighbors = neighbors

    @property
    def hour(self):
        return (self.t / 3600.0) % 24.0

    def occupancy(self, name):
        """(people, extra_heat_W) for the current hour."""
        h = self.hour
        for start, end, people, extra in self.rooms[name].spec.schedule:
            if start <= h < end:
                return people, extra
        return 0, 0.0

    def step(self, dt=1.0):
        t_out = outdoor_temp(self.hour)
        flows = {n: 0.0 for n in self.rooms}   # net heat flow through partition walls (W)
        for a, b in self.neighbors:
            q = UA_PARTITION * (self.rooms[a].temp - self.rooms[b].temp)
            flows[a] -= q
            flows[b] += q

        for name, r in self.rooms.items():
            s = r.spec
            people, extra = self.occupancy(name)
            if self.t >= r.window_until and people > 0 and self.rng.random() < dt / (6 * 3600.0):
                r.window_until = self.t + 600.0
            scale = s.volume / 60.0
            q_air = 0.005 * scale + s.q_fan_max * r.fan / 100.0 + (Q_WINDOW * scale if self.t < r.window_until else 0.0)

            q_heat = ((s.ua + q_air * RHO_CP) * (t_out - r.temp) + people * Q_PERSON + extra
                      + s.q_heater * r.valve / 100.0 + flows[name])
            r.temp += q_heat / s.c_thermal * dt
            r.co2 += (people * CO2_PER_PERSON * 1e6 + q_air * (CO2_OUTDOOR - r.co2)) / s.volume * dt
            r.rh = clamp(r.rh + (people * RH_PER_PERSON + q_air / s.volume * (RH_OUTDOOR - r.rh)) * dt, 0.0, 100.0)
        self.t += dt

    def read_sensors(self, name):
        r = self.rooms[name]
        return {"temperature": round(r.temp + self.rng.gauss(0, 0.1), 2),
                "humidity": round(r.rh + self.rng.gauss(0, 0.5), 1),
                "co2": int(round(r.co2 + self.rng.gauss(0, 10)))}

    def state(self, name):
        r, h = self.rooms[name], self.hour
        return {"sim_time": f"{int(h):02d}:{int((h % 1) * 60):02d}", "occupancy": self.occupancy(name)[0],
                "outdoor_temp": round(outdoor_temp(h), 2), "window_open": self.t < r.window_until,
                "fan": round(r.fan, 1), "valve": round(r.valve, 1)}