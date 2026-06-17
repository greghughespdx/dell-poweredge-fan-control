#!/usr/bin/env python3
"""
Smart IPMI fan control for Dell PowerEdge servers.

Inlet/CPU temperature-driven fan curve with drive-temperature awareness,
hysteresis, smooth ramping, safety overrides, and a systemd watchdog.

Supported hardware
------------------
Dell PowerEdge 12th/13th generation (R620/R720/R730/R630 and similar) that
accept the Dell OEM IPMI raw fan-control commands via a local BMC (/dev/ipmi0).
Other vendors (HP iLO, Supermicro, Lenovo) use different raw commands and are
NOT supported out of the box -- see the HARDWARE ADAPTER section below.

Safety
------
This program takes MANUAL control of chassis fans. A misconfiguration can leave
fans too low. It includes safety-temperature overrides and a systemd watchdog,
but you are responsible for validating the curve on YOUR hardware in YOUR
environment before relying on it. Test with conservative thresholds first.

Tuning
------
The constants in the CONFIG section are tuned for one specific server in one
environment. Re-tune FAN CURVE, SAFETY, and DRIVE PROFILE values for your own
hardware and ambient conditions.
"""

import subprocess
import time
import sys
import os
import glob
import json
from datetime import datetime

# =============================================================================
# HARDWARE ADAPTER -- Dell PowerEdge (edit this block for other vendors)
# =============================================================================
# `ipmitool` talks to the local BMC. On Dell PowerEdge 12G/13G the OEM raw
# command 0x30 0x30 controls fan mode and duty cycle:
#   - 0x30 0x30 0x01 0x00            -> disable automatic fan control (manual)
#   - 0x30 0x30 0x02 0xff 0x<pct>    -> set all fans to <pct> percent
# To port to another vendor, replace fan_enable_manual() and fan_set_percent()
# with that vendor's mechanism (and verify there is a safe fallback to automatic
# control if this process dies).

IPMI_CMD = ["ipmitool"]

# Sensor names exactly as they appear in `ipmitool sensor` output on this BMC.
SENSOR_INLET = "Inlet Temp"
SENSOR_EXHAUST = "Exhaust Temp"
SENSOR_CPU = "Temp"          # generic per-socket CPU temperature rows


def fan_enable_manual():
    """Dell PowerEdge: switch the BMC out of automatic fan control."""
    subprocess.run(IPMI_CMD + ["raw", "0x30", "0x30", "0x01", "0x00"],
                   capture_output=True, check=True, timeout=5)


def fan_set_percent(percent):
    """Dell PowerEdge: set all fans to a duty-cycle percentage."""
    hexval = format(int(percent), "02x")
    subprocess.run(IPMI_CMD + ["raw", "0x30", "0x30", "0x02", "0xff", f"0x{hexval}"],
                   capture_output=True, check=True, timeout=5)


# =============================================================================
# CONFIG -- fan curve (tune to your hardware + environment)
# =============================================================================
FAN_FLOOR = 10               # minimum fan duty %
FAN_CEILING = 90             # maximum fan duty % under the normal curve
SAFETY_TEMP = 65             # any inlet/CPU temp >= this forces SAFETY_FAN
SAFETY_FAN = 80              # fan % when a safety temperature is reached

INLET_LOW = 18               # inlet temp mapped to FAN_FLOOR
INLET_HIGH = 40              # inlet temp mapped to FAN_CEILING

CPU_BUMP_THRESHOLD = 50      # CPU temp above which an extra fan bump is added
CPU_BUMP_MAX = 70            # CPU temp mapped to the full CPU bump
CPU_BUMP_AMOUNT = 20         # max extra fan % from the CPU bump

HYSTERESIS = 3               # ignore target changes smaller than this (%)
RAMP_DOWN_MAX = 5            # max fan % decrease per cycle (no down-spikes)
CYCLE_SECONDS = 15           # main loop cadence

# =============================================================================
# CONFIG -- drive temperatures (optional; set ENABLE_DRIVE_TEMPS = False to skip)
# =============================================================================
ENABLE_DRIVE_TEMPS = True
DRIVE_SAMPLE_SECONDS = 120        # SMART sweep cadence (not every cycle)
DRIVE_CACHE_STALE_SECONDS = 600   # reuse last-good temp up to this long
DRIVE_SMART_TIMEOUT = 5           # per-device smartctl timeout (seconds)
DRIVE_UNKNOWN_MIN_FAN = 35        # modest floor if a drive errors with no fresh cache

# Per-class curves. Drives are deliberately NOT folded into SAFETY_TEMP: HDDs
# need action well before a CPU-class 65C, and NVMe legitimately runs hotter.
# Each band is (low_C, high_C, fan_low_%, fan_high_%); >= safety_temp -> safety_fan.
DRIVE_PROFILES = {
    "hdd": {"safety_temp": 55, "safety_fan": 80, "bands": [(45, 50, 45, 65), (50, 55, 65, 80)]},
    "ssd": {"safety_temp": 75, "safety_fan": 80, "bands": [(55, 65, 45, 65), (65, 75, 65, 80)]},
    "nvme": {"safety_temp": 80, "safety_fan": 80, "bands": [(60, 70, 45, 65), (70, 80, 65, 80)]},
}

# =============================================================================
# Runtime state
# =============================================================================
DRIVE_DEVICES = []
DRIVE_CACHE = {}
LAST_DRIVE_SAMPLE = 0


def _resolve_watchdog_usec():
    # systemd sets WATCHDOG_USEC in the service env when WatchdogSec= is configured.
    # Prefer that over a legacy argv[1] contract; fall back to argv; else disabled.
    env_val = os.environ.get("WATCHDOG_USEC")
    if env_val and env_val.isdigit():
        return int(env_val)
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        return int(sys.argv[1])
    return 0


WATCHDOG_USEC = _resolve_watchdog_usec()


def notify_watchdog():
    if WATCHDOG_USEC <= 0:
        return
    try:
        import sdnotify
        sdnotify.SystemdNotifier().notify("WATCHDOG=1")
        return
    except ImportError:
        pass
    try:
        r = subprocess.run(["systemd-notify", "WATCHDOG=1"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            print(f"[WARN] systemd-notify WATCHDOG=1 rc={r.returncode} stderr={r.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"[WARN] systemd-notify failed: {e}", flush=True)


def lerp(value, in_low, in_high, out_low, out_high):
    if value <= in_low:
        return out_low
    if value >= in_high:
        return out_high
    ratio = (value - in_low) / (in_high - in_low)
    return out_low + ratio * (out_high - out_low)


def compute_fan_target(inlet, cpu_max):
    base = lerp(inlet, INLET_LOW, INLET_HIGH, FAN_FLOOR, FAN_CEILING)
    bump = 0
    if cpu_max > CPU_BUMP_THRESHOLD:
        bump = lerp(cpu_max, CPU_BUMP_THRESHOLD, CPU_BUMP_MAX, 0, CPU_BUMP_AMOUNT)
    target = base + bump
    return max(FAN_FLOOR, min(FAN_CEILING, target))


def apply_ramping(current, target):
    diff = target - current
    if abs(diff) < HYSTERESIS:
        return current
    if diff > 0:
        return target
    else:
        return max(target, current - RAMP_DOWN_MAX)


def read_all_sensors():
    """Single ipmitool call, parse inlet/exhaust/CPU temps."""
    inlet = 0
    exhaust = 0
    generic_temps = []
    try:
        result = subprocess.run(IPMI_CMD + ["sensor"],
                                capture_output=True, text=True, timeout=30)
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not parts[1].replace('.', '', 1).isdigit():
                continue
            val = int(float(parts[1]))
            if parts[0] == SENSOR_INLET:
                inlet = val
            elif parts[0] == SENSOR_EXHAUST:
                exhaust = val
            elif parts[0] == SENSOR_CPU:
                generic_temps.append(val)
    except Exception as e:
        print(f"[ERROR] IPMI sensor read failed: {e}", flush=True)
    temp1 = generic_temps[0] if len(generic_temps) > 0 else 0
    temp2 = generic_temps[1] if len(generic_temps) > 1 else 0
    return inlet, exhaust, temp1, temp2


def set_fan_speed(percent):
    try:
        fan_enable_manual()
        fan_set_percent(percent)
    except Exception as e:
        print(f"[ERROR] Failed to set fan speed: {e}", flush=True)


# =============================================================================
# Drive temperature sampling (smartctl; portable across Linux + smartmontools)
# =============================================================================

def stable_byid_path(kernel_path):
    prefixes = ("wwn-", "scsi-", "ata-", "nvme-eui.", "nvme-")
    matches = []
    for path in glob.glob("/dev/disk/by-id/*"):
        name = os.path.basename(path)
        if "-part" in name or not name.startswith(prefixes):
            continue
        if os.path.realpath(path) == kernel_path:
            matches.append(path)
    priority = ("wwn-", "scsi-", "nvme-eui.", "ata-", "nvme-")
    matches.sort(key=lambda p: next((i for i, pre in enumerate(priority) if os.path.basename(p).startswith(pre)), 99))
    return matches[0] if matches else kernel_path


def discover_drive_devices():
    try:
        result = subprocess.run(
            ["lsblk", "-d", "-J", "-o", "NAME,TYPE,ROTA,TRAN,MODEL,SERIAL,SIZE,PATH"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        payload = json.loads(result.stdout)
    except Exception as e:
        print(f"[WARN] Drive discovery failed: {e}", flush=True)
        return []

    devices = []
    for dev in payload.get("blockdevices", []):
        name = dev.get("name", "")
        if dev.get("type") != "disk" or name.startswith("loop"):
            continue

        kernel_path = dev.get("path") or f"/dev/{name}"
        if name.startswith("nvme"):
            profile = "nvme"
            smart_type = "nvme"
        elif dev.get("rota"):
            profile = "hdd"
            smart_type = "scsi"
        else:
            profile = "ssd"
            smart_type = "scsi"

        devices.append({
            "path": stable_byid_path(kernel_path),
            "kernel_path": kernel_path,
            "name": name,
            "profile": profile,
            "smart_type": smart_type,
            "model": dev.get("model") or "unknown",
            "serial": dev.get("serial") or "unknown",
        })

    print(f"[INFO] Discovered {len(devices)} drive temperature inputs", flush=True)
    return devices


def extract_drive_temp(payload, profile):
    temps = []
    current = payload.get("temperature", {}).get("current")
    if isinstance(current, (int, float)):
        temps.append(int(current))

    if profile == "nvme":
        log = payload.get("nvme_smart_health_information_log", {})
        nvme_temp = log.get("temperature")
        if isinstance(nvme_temp, (int, float)):
            temps.append(int(nvme_temp))
        for sensor_temp in log.get("temperature_sensors", []) or []:
            if isinstance(sensor_temp, (int, float)) and sensor_temp > 0:
                temps.append(int(sensor_temp))

    attrs = payload.get("ata_smart_attributes", {}).get("table", []) or []
    for attr in attrs:
        if attr.get("name") in ("Temperature_Celsius", "Airflow_Temperature_Cel", "Temperature_Internal"):
            raw = attr.get("raw", {}).get("value")
            if isinstance(raw, (int, float)):
                temps.append(int(raw))

    return max(temps) if temps else None


def read_drive_temperature(device):
    cmd = ["smartctl", "-A", "-j", "-d", device["smart_type"], device["path"]]
    if device["smart_type"] != "nvme":
        cmd[1:1] = ["-n", "standby,3"]   # do not spin up a sleeping disk

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SMART_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"status": "error", "temp": None, "detail": "timeout"}
    except Exception as e:
        return {"status": "error", "temp": None, "detail": str(e)}

    if result.returncode == 3:
        return {"status": "standby", "temp": None, "detail": "standby"}

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {"status": "error", "temp": None, "detail": "invalid-json"}

    temp = extract_drive_temp(payload, device["profile"])
    if temp is None:
        return {"status": "no-temp", "temp": None, "detail": f"smartctl-rc={result.returncode}"}

    status = "ok" if result.returncode == 0 else "ok-with-warning"
    return {"status": status, "temp": temp, "detail": f"smartctl-rc={result.returncode}"}


def fresh_cached_drive_temp(cache_entry, now):
    if not cache_entry:
        return None
    if now - cache_entry.get("ts", 0) > DRIVE_CACHE_STALE_SECONDS:
        return None
    return cache_entry.get("temp")


def sample_drive_temperatures(now=None):
    global DRIVE_DEVICES, LAST_DRIVE_SAMPLE
    now = time.time() if now is None else now
    if not DRIVE_DEVICES:
        DRIVE_DEVICES = discover_drive_devices()

    entries = []
    for device in DRIVE_DEVICES:
        key = device["path"]
        reading = read_drive_temperature(device)
        # Keep the systemd watchdog fed across a worst-case slow sweep
        # (many devices x timeout could otherwise approach WatchdogSec).
        notify_watchdog()
        cache_entry = DRIVE_CACHE.get(key)

        temp = reading["temp"]
        used_cache = False
        if temp is not None:
            DRIVE_CACHE[key] = {
                "temp": temp,
                "ts": now,
                "profile": device["profile"],
                "status": reading["status"],
            }
        else:
            temp = fresh_cached_drive_temp(cache_entry, now)
            used_cache = temp is not None

        entries.append({
            "device": device,
            "status": reading["status"],
            "detail": reading["detail"],
            "temp": temp,
            "used_cache": used_cache,
        })

    LAST_DRIVE_SAMPLE = now
    return summarize_drive_temperatures(entries)


def summarize_drive_temperatures(entries):
    max_by_profile = {}
    unknown_errors = 0
    standby = 0
    no_temp = 0

    for entry in entries:
        profile = entry["device"]["profile"]
        temp = entry["temp"]
        status = entry["status"]

        if temp is not None:
            max_by_profile[profile] = max(max_by_profile.get(profile, temp), temp)
        elif status == "standby":
            standby += 1
        elif status == "no-temp":
            no_temp += 1
        else:
            unknown_errors += 1

    return {
        "max_by_profile": max_by_profile,
        "standby": standby,
        "no_temp": no_temp,
        "unknown_errors": unknown_errors,
        "count": len(entries),
    }


def drive_target_for_temp(profile, temp):
    cfg = DRIVE_PROFILES[profile]
    if temp >= cfg["safety_temp"]:
        return cfg["safety_fan"], True

    for low, high, fan_low, fan_high in cfg["bands"]:
        if low <= temp < high:
            return lerp(temp, low, high, fan_low, fan_high), False

    return None, False


def compute_drive_fan_target(summary):
    target = None
    safety_fan = None
    reasons = []

    for profile, temp in summary.get("max_by_profile", {}).items():
        profile_target, is_safety = drive_target_for_temp(profile, temp)
        if profile_target is not None:
            target = max(target or FAN_FLOOR, profile_target)
            reasons.append(f"{profile}:{temp}C->{int(profile_target)}%")
        if is_safety:
            safety_fan = max(safety_fan or FAN_FLOOR, profile_target)

    if target is None and summary.get("unknown_errors", 0) > 0:
        target = DRIVE_UNKNOWN_MIN_FAN
        reasons.append(f"drive-read-error-floor:{DRIVE_UNKNOWN_MIN_FAN}%")

    return target, safety_fan, ",".join(reasons) if reasons else "drives-ok"


def main():
    print(f"[INFO] IPMI fan control starting (Dell PowerEdge smart curve)", flush=True)
    print(f"[INFO] Fan range: {FAN_FLOOR}%-{FAN_CEILING}%, safety at {SAFETY_TEMP}C->{SAFETY_FAN}%", flush=True)
    print(f"[INFO] Inlet curve: {INLET_LOW}C->{FAN_FLOOR}%, {INLET_HIGH}C->{FAN_CEILING}%", flush=True)
    print(f"[INFO] Drive temps: {'enabled' if ENABLE_DRIVE_TEMPS else 'disabled'}", flush=True)
    print(f"[INFO] Watchdog: {'enabled' if WATCHDOG_USEC > 0 else 'disabled'} (WATCHDOG_USEC={WATCHDOG_USEC})", flush=True)

    current_fan = FAN_FLOOR

    # Initial drive sample so drives are covered from the first cycle (e.g. if
    # they are already hot at service restart), rather than blind for 120s.
    drive_summary = {"max_by_profile": {}, "standby": 0, "no_temp": 0, "unknown_errors": 0, "count": 0}
    if ENABLE_DRIVE_TEMPS:
        drive_summary = sample_drive_temperatures()

    while True:
        loop_start = time.time()
        ts = datetime.now().strftime('%H:%M:%S')

        notify_watchdog()

        inlet, exhaust, temp1, temp2 = read_all_sensors()
        cpu_max = max(exhaust, temp1, temp2)
        all_max = max(inlet, cpu_max)

        target = compute_fan_target(inlet, cpu_max)
        drive_target, drive_safety_fan, drive_reason = compute_drive_fan_target(drive_summary)
        if drive_target is not None:
            target = max(target, drive_target)

        if all_max >= SAFETY_TEMP or drive_safety_fan:
            cpu_safety = SAFETY_FAN if all_max >= SAFETY_TEMP else FAN_FLOOR
            new_fan = max(cpu_safety, drive_safety_fan or FAN_FLOOR)
            print(f"[{ts}] SAFETY: In:{inlet} Ex:{exhaust} T1:{temp1} T2:{temp2} Max:{all_max}C Drive:{drive_reason} -> {int(new_fan)}%", flush=True)
        else:
            new_fan = apply_ramping(current_fan, target)
            print(f"[{ts}] In:{inlet} Ex:{exhaust} T1:{temp1} T2:{temp2} Tgt:{int(target)}% Cur:{int(current_fan)}% -> {int(new_fan)}% Drive:{drive_reason}", flush=True)

        if int(new_fan) != int(current_fan):
            set_fan_speed(int(new_fan))
            current_fan = new_fan

        notify_watchdog()

        # Sample drives after fan action so SMART latency never delays cooling.
        if ENABLE_DRIVE_TEMPS and time.time() - LAST_DRIVE_SAMPLE >= DRIVE_SAMPLE_SECONDS:
            drive_summary = sample_drive_temperatures()

        elapsed = time.time() - loop_start
        sleep_time = max(0, CYCLE_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
