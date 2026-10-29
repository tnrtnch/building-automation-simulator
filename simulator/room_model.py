"""Single-room physics model (thermal, CO2, humidity) and the constants shared with the house model.

States:       temperature (C), CO2 (ppm), relative humidity (%)
Inputs:       fan (0-100 %), valve (0-100 %, heating coil)
Disturbances: outdoor temperature, occupancy, window
"""
import math
import random

# --- Room ---
VOLUME = 60.0            # m3 (20 m2 x 3 m)
UA = 100.0               # W/K, heat loss through walls and windows
C_THERMAL = 1.0e6        # J/K, thermal capacity (air + furniture + wall surfaces)
RHO_CP = 1.2 * 1005.0    # J/(m3*K), volumetric heat capacity of air

# --- Ventilation ---
Q_INFILTRATION = 0.005   # m3/s, leakage with the fan off (~0.3 ACH)
Q_FAN_MAX = 0.05         # m3/s, fan at 100 % (~3 ACH)
Q_WINDOW = 0.04          # m3/s, extra flow when a window is open

# --- Heat sources ---
Q_PERSON = 100.0         # W per person
Q_COIL_MAX = 2000.0      # W at valve 100 %

# --- CO2 / humidity ---
CO2_PER_PERSON = 5.0e-6  # m3/s per person (~0.005 L/s)
CO2_OUTDOOR = 420.0      # ppm
RH_PER_PERSON = 0.0008   # %/s per person (simplified)
RH_OUTDOOR = 60.0        # %


def outdoor_temp(hour: float) -> float:
    """Outdoor temperature: max 16 C at 15:00, min 4 C at 03:00."""
    return 10.0 + 6.0 * math.sin(2 * math.pi * (hour - 9.0) / 24.0)


def occupancy(hour: float) -> int:
    """Occupancy schedule of an office/classroom day."""
    if 8 <= hour < 12 or 13 <= hour < 17:
        return 4
    if 12 <= hour < 13:
        return 1
    return 0


class RoomModel:
    def __init__(self, start_hour: float = 7.0, seed: int | None = None):
        self.t_sim = start_hour * 3600.0   # simulated time of day, seconds
        self.temp = 18.0
        self.co2 = 450.0
        self.rh = 45.0
        self.fan = 0.0     # %
        self.valve = 0.0   # %
        self.window_open_until = 0.0
        self.rng = random.Random(seed)

    @property
    def hour(self) -> float:
        return (self.t_sim / 3600.0) % 24.0

    def set_fan(self, pct: float):
        self.fan = min(100.0, max(0.0, pct))

    def set_valve(self, pct: float):
        self.valve = min(100.0, max(0.0, pct))

    def _maybe_toggle_window(self, dt: float):
        # Occasionally a window is opened for 10 minutes while the room is occupied
        if self.t_sim >= self.window_open_until and occupancy(self.hour) > 0:
            if self.rng.random() < dt / (6 * 3600.0):
                self.window_open_until = self.t_sim + 600.0

    def step(self, dt: float = 1.0):
        """Advance the simulation by dt seconds (explicit Euler)."""
        self._maybe_toggle_window(dt)
        h = self.hour
        n = occupancy(h)
        t_out = outdoor_temp(h)

        q_air = Q_INFILTRATION + Q_FAN_MAX * self.fan / 100.0
        if self.t_sim < self.window_open_until:
            q_air += Q_WINDOW

        # Temperature: C dT/dt = (UA + q_air*rho*cp)(Tout - T) + people + heating coil
        q_total = ((UA + q_air * RHO_CP) * (t_out - self.temp)
                   + n * Q_PERSON + Q_COIL_MAX * self.valve / 100.0)
        self.temp += q_total / C_THERMAL * dt

        # CO2 mass balance: V dC/dt = N*G*1e6 + Q*(Cout - C)
        d_co2 = (n * CO2_PER_PERSON * 1e6 + q_air * (CO2_OUTDOOR - self.co2)) / VOLUME
        self.co2 += d_co2 * dt

        # Humidity (simplified): people add moisture, ventilation pulls toward the outdoor value
        d_rh = n * RH_PER_PERSON + q_air / VOLUME * (RH_OUTDOOR - self.rh)
        self.rh = min(100.0, max(0.0, self.rh + d_rh * dt))

        self.t_sim += dt

    def read_sensors(self) -> dict:
        """Noisy sensor readings (SCD41-like)."""
        return {"temperature": round(self.temp + self.rng.gauss(0, 0.1), 2),
                "humidity": round(self.rh + self.rng.gauss(0, 0.5), 1),
                "co2": int(round(self.co2 + self.rng.gauss(0, 10)))}

    def state(self) -> dict:
        h = self.hour
        return {"sim_time": f"{int(h):02d}:{int((h % 1) * 60):02d}", "occupancy": occupancy(h),
                "outdoor_temp": round(outdoor_temp(h), 2), "window_open": self.t_sim < self.window_open_until,
                "fan": round(self.fan, 1), "valve": round(self.valve, 1)}