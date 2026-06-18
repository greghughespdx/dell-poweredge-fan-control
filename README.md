# dell-ipmi-fan-control

Smart IPMI fan control for **Dell PowerEdge** servers (see generations supported, 
below). Drives chassis fans from inlet and CPU temperatures with optional 
**drive-temperature awareness**, plus hysteresis, smooth ramping, safety overrides, 
and a systemd watchdog.

**Why use it?** Because the stock BMC fan policy on these servers tends to be either
loud (aggressive default curve) or, when third-party PCIe cards are installed, stuck
at high RPM. This control gives you a quiet, sensible curve while still protecting the
hardware - and unlike most fan-control scripts, it can factor in **disk
temperatures**, which are a real heat source in a storage box on a hot day.

## Supported hardware

- **Built and tested on a Dell PowerEdge R730XD.** Expected to work on many Dell
  PowerEdge 12th/13th generation systems (R620/R720/R730/R630-class) that accept the
  **undocumented** Dell OEM IPMI raw fan command (`0x30 0x30 ...`) via a local
  BMC at `/dev/ipmi0`. This command is community-known, but is not an officially
  supported Dell interface, and **some newer iDRAC firmware revisions reject it**
  (reported on 14G with iDRAC 3.34+). Verify on your exact model and iDRAC
  firmware before enabling. If you find it works on other server models, please
  drop a comment.
- Linux host with `ipmitool`. Drive-temperature support additionally needs
  `smartmontools` (`smartctl`) and `lsblk`.

Two independent data paths: fan control and air/CPU temps go through **IPMI**
(the BMC); drive temps come from **`smartctl`** over the storage interface, not
the BMC. The only thing this program actuates is fan duty cycle, via IPMI.

Other server brands (HP iLO, Supermicro, Lenovo) use different raw commands and
are **not supported out of the box**. The vendor-specific bits are isolated in a
clearly marked `HARDWARE ADAPTER` block at the top of the script - porting means
replacing two functions and the sensor names. See [Adapting](#adapting-to-other-hardware).

> **IMPORTANT:** this program takes **manual control** of your fans. While it runs,
> the BMC's automatic fan control is **disabled**, and Dell does **not**
> automatically take fans back if the process dies - they stay at the last-set
> duty cycle. The systemd unit mitigates that (watchdog + `Restart=always`
> for hangs/crashes, and `ExecStopPost` restores automatic mode on a clean stop);
> to restore by hand: `ipmitool raw 0x30 0x30 0x01 0x01`. A bad configuration can
> run fans too low and cook hardware. **You** are responsible for validating the
> curve on your hardware before relying on it. Start with conservative thresholds.

## How it works

Every 15 seconds it reads inlet + CPU temps via `ipmitool sensor`, computes a fan
target from a piecewise-linear curve (with a CPU bump), and applies it with
hysteresis and ramp-down limiting (no fan down-spikes). If any temperature
crosses the safety threshold, it jumps straight to the safety fan speed.

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
**re-tune them for your system!**:

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
  Replace with your platform's mechanism, and provide an equivalent
  **restore-to-automatic** command. The Dell restore is
  `ipmitool raw 0x30 0x30 0x01 0x01` (wired into the unit's `ExecStopPost`).
- `SENSOR_INLET` / `SENSOR_EXHAUST` / `SENSOR_CPU` - the sensor names as they
  appear in your `ipmitool sensor` output.

The fan curve and control logic are vendor-neutral. The drive-temperature module
is portable across Linux + `smartmontools`, with one caveat: `discover_drive_devices()`
uses `smartctl -d scsi` for all non-NVMe disks (correct for SAS/HBA topologies).
Direct-attach SATA or USB/JBOD may need `-d ata` / `-d sat`; if a non-NVMe disk
reports no temperature, check `smartctl --scan` and adjust `smart_type`.

## License

MIT - see [LICENSE](LICENSE).
