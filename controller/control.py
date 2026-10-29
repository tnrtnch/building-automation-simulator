"""Control algorithms (independent of MQTT, testable on their own)."""


class PID:
    """PI(D) controller: derivative on measurement, filtered input, conditional-integration anti-windup.

    reverse=False: error = setpoint - measurement (heating: low temperature raises the output)
    reverse=True : error = measurement - setpoint (ventilation: high CO2 raises the output)
    """

    def __init__(self, kp, ki, kd=0.0, out_min=0.0, out_max=100.0, reverse=False, filter_tau=0.0):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.out_min, self.out_max = out_min, out_max
        self.reverse = reverse
        self.filter_tau = filter_tau
        self.integral = 0.0
        self._filtered = None
        self._prev_filtered = None

    def reset(self):
        self.integral = 0.0
        self._filtered = None
        self._prev_filtered = None

    def update(self, setpoint, measurement, dt):
        # First-order low-pass on the measurement to tame sensor noise
        if self._filtered is None or self.filter_tau <= 0:
            self._filtered = measurement
        else:
            a = dt / (self.filter_tau + dt)
            self._filtered += a * (measurement - self._filtered)
        meas = self._filtered

        error = (meas - setpoint) if self.reverse else (setpoint - meas)

        # Derivative on measurement (no derivative kick when the setpoint changes)
        d_term = 0.0
        if self.kd and self._prev_filtered is not None and dt > 0:
            d_meas = (meas - self._prev_filtered) / dt
            d_term = -self.kd * (-d_meas if self.reverse else d_meas)
        self._prev_filtered = meas

        p_term = self.kp * error
        unsat = p_term + self.integral + d_term
        out = min(self.out_max, max(self.out_min, unsat))

        # Anti-windup: while saturated, the integral only moves in the direction that reduces saturation
        if out == unsat or (unsat > self.out_max and error < 0) or (unsat < self.out_min and error > 0):
            self.integral += self.ki * error * dt
            self.integral = min(self.out_max, max(self.out_min, self.integral))

        return min(self.out_max, max(self.out_min, p_term + self.integral + d_term))


class Hysteresis:
    """On/off control with hysteresis. Reference for comparison against PID."""

    def __init__(self, band, reverse=False, out_on=100.0, out_off=0.0):
        self.band = band
        self.reverse = reverse
        self.out_on, self.out_off = out_on, out_off
        self.on = False

    def reset(self):
        self.on = False

    def update(self, setpoint, measurement, dt=0.0):
        error = (measurement - setpoint) if self.reverse else (setpoint - measurement)
        if error > self.band:
            self.on = True
        elif error < -self.band:
            self.on = False
        return self.out_on if self.on else self.out_off


# Tuned gains (offline, with tools/compare.py); time unit: simulated seconds
DEFAULT_GAINS = dict(
    temp=dict(kp=60.0, ki=0.02, filter_tau=120.0),   # temperature -> heating valve (%/K)
    co2=dict(kp=0.4, ki=0.001, filter_tau=120.0),    # CO2 -> fan (%/ppm)
)


def make_controllers(mode):
    """Return (temperature_controller, co2_controller). mode: 'pid' | 'onoff'."""
    if mode == "pid":
        return PID(**DEFAULT_GAINS["temp"]), PID(reverse=True, **DEFAULT_GAINS["co2"])
    return Hysteresis(band=0.5), Hysteresis(band=100.0, reverse=True)


class OverheatGuard:
    """Turns the ventilation fan on when the temperature reaches a threshold (with hysteresis).

    While active: the fan runs at least fan_pct and the heating valve is closed, so heater and fan never
    fight each other. Switches on at on_temp and off again at on_temp - hysteresis.
    """

    def __init__(self, on_temp=24.0, hysteresis=0.5, fan_pct=100.0):
        self.on_temp = on_temp
        self.hysteresis = hysteresis
        self.fan_pct = fan_pct
        self.active = False

    def update(self, temp, valve, fan):
        if temp >= self.on_temp:
            self.active = True
        elif temp <= self.on_temp - self.hysteresis:
            self.active = False
        if self.active:
            return 0.0, max(fan, self.fan_pct)
        return valve, fan