# dell-ipmi-fan-control

Smart IPMI fan control for **Dell PowerEdge** servers. Drives chassis fans from
inlet and CPU temperatures with optional **drive-temperature awareness**, plus
hysteresis, smooth ramping, safety overrides, and a systemd watchdog.

It exists because the stock BMC fan policy on these servers tends to be either
loud (aggressive default curve) or, with third-party PCIe cards installed, stuck
at high RPM. This gives you a quiet, sensible curve while still protecting the
hardware - and unlike most fan-control scripts, it can factor in **disk
temperatures**, which are the real heat source in a storage box on a hot day.

## Supported hardware

- **Dell PowerEdge 12th/13th generation** (R620, R720, R730, R630, and similar)
  that accept the Dell OEM IPMI raw fan command (`0x30 0x30 ...`) via a local
  BMC at `/dev/ipmi0`.
- Linux host with `ipmitool`. Drive-temperature support additionally needs
  `smartmontools` (`smartctl`) and `lsblk`.

Other server brands (HP iLO, Supermicro, Lenovo) use different raw commands and
are **not supported out of the box**. The vendor-specific bits are isolated in a
clearly marked `HARDWARE ADAPTER` block at the top of the script - porting means
replacing two functions and the sensor names. See [Adapting](#adapting-to-other-hardware).

> **Safety:** this program takes **manual control** of your fans. A bad
> configuration can run them too low and cook hardware. It has safety-temperature
> overrides and a watchdog, but **you** are responsible for validating the curve
> on your hardware before relying on it. Start with conservative thresholds.

## How it works

Every 15 seconds it reads inlet + CPU temps via `ipmitool sensor`, computes a fan
target from a piecewise-linear curve (with a CPU bump), and applies it with
hysteresis and ramp-down limiting (no fan down-spikes). If any temperature
crosses the safety threshold it jumps straight to the safety fan speed.

If drive-temperature support is enabled, every 120 seconds it samples disk temps
via `smartctl` (without waking standby disks), classifies each device as HDD /
SSD / NVMe, and feeds a per-class curve into the target via `max()`. Drives are
**not** lumped into the CPU safety threshold - HDDs act earlier, NVMe runs hotter.
Read failures never report 0C and never force max fan.

The systemd watchdog (`WATCHDOG_USEC` from the service environment) is pinged each
cycle and during the drive sweep, so a hung controller is restarted automatically.

## Install

```bash
sudo install -d /opt/ipmi-fan-control
sudo install -m 0755 ipmi_fan_control.py /opt/ipmi-fan-control/ipmi_fan_control.py

# Edit the unit's ExecStart path if you installed elsewhere, then:
sudo cp ipmi-fan-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ipmi-fan-control
journalctl -u ipmi-fan-control -f
```

Confirm it survives past the watchdog window (no restarts after ~2 minutes):

```bash
systemctl show ipmi-fan-control -p NRestarts -p ActiveState
```

## Configuration

All tunables are constants near the top of `ipmi_fan_control.py`, grouped by
section. The defaults are tuned for one specific server in one environment -
**re-tune them for yours**:

| Group | Keys |
|-------|------|
| Fan curve | `FAN_FLOOR`, `FAN_CEILING`, `INLET_LOW`, `INLET_HIGH`, `CPU_BUMP_*` |
| Safety | `SAFETY_TEMP`, `SAFETY_FAN` |
| Smoothing | `HYSTERESIS`, `RAMP_DOWN_MAX`, `CYCLE_SECONDS` |
| Drives | `ENABLE_DRIVE_TEMPS`, `DRIVE_PROFILES`, `DRIVE_SAMPLE_SECONDS`, `DRIVE_CACHE_STALE_SECONDS` |

Set `ENABLE_DRIVE_TEMPS = False` to run purely on inlet/CPU temps.

## Adapting to other hardware

The `HARDWARE ADAPTER` block at the top of the script holds everything
vendor-specific:

- `fan_enable_manual()` / `fan_set_percent()` - the Dell OEM raw commands.
  Replace with your platform's mechanism. **Ensure your platform falls back to
  automatic fan control if this process dies** (Dell does).
- `SENSOR_INLET` / `SENSOR_EXHAUST` / `SENSOR_CPU` - the sensor names as they
  appear in your `ipmitool sensor` output.

The fan curve, drive sampling, and control logic are vendor-neutral.

## License

MIT - see [LICENSE](LICENSE).
