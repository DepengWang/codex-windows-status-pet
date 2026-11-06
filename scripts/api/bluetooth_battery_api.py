"""Read the connected DJI Mic Mini 2 battery from Windows PnP properties."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


_POWERSHELL_QUERY = r"""
$ErrorActionPreference = 'Stop'
try {
    $deviceLabels = @{
        '6F775D' = 'GPT'
        '6F3291' = 'CC'
    }
    $audioEndpoints = @(Get-PnpDevice -Class AudioEndpoint -PresentOnly |
        Where-Object { $_.FriendlyName -like '*DJI Mic Mini 2*Hands-Free*' })
    # Keep a row for each known transmitter so the overlay can distinguish an
    # offline device from a missing reading without showing cached battery data.
    $readings = @(
        [pscustomobject]@{ label = 'CC'; connected = $false; level = $null },
        [pscustomobject]@{ label = 'GPT'; connected = $false; level = $null }
    )
    $batteryNodes = @(Get-PnpDevice -Class System -PresentOnly |
        Where-Object { $_.FriendlyName -match '^DJI Mic Mini 2-[0-9A-F]{6} Hands-Free AG$' })

    foreach ($node in $batteryNodes) {
        $match = [regex]::Match($node.FriendlyName, '^DJI Mic Mini 2-([0-9A-F]{6}) Hands-Free AG$')
        $suffix = $match.Groups[1].Value.ToUpperInvariant()
        $label = $deviceLabels[$suffix]
        if (-not $label) {
            continue
        }
        $deviceName = $node.FriendlyName -replace ' Hands-Free AG$', ''
        $endpoint = $audioEndpoints | Where-Object {
            $_.FriendlyName -like "*($deviceName Hands-Free)*"
        } | Select-Object -First 1
        if (-not $endpoint) {
            continue
        }

        $battery = Get-PnpDeviceProperty -InstanceId $node.InstanceId `
            -KeyName 'DEVPKEY_Device_BatteryLevel' -ErrorAction SilentlyContinue
        $level = $null
        if ($battery -and $null -ne $battery.Data) {
            $candidate = [int]$battery.Data
            if ($candidate -ge 0 -and $candidate -le 100) {
                $level = $candidate
            }
        }
        $reading = $readings | Where-Object { $_.label -eq $label } | Select-Object -First 1
        if ($reading) {
            $reading.connected = $true
            $reading.level = $level
        }
    }

    [pscustomobject]@{
        connected = (@($readings | Where-Object { $_.connected }).Count -gt 0)
        devices = @($readings)
    } | ConvertTo-Json -Depth 3 -Compress
} catch {
    exit 1
}
"""


def _validated_devices(payload):
    devices = payload.get("devices")
    if isinstance(devices, dict):
        devices = [devices]
    if not isinstance(devices, list):
        return []

    validated = []
    for device in devices:
        if not isinstance(device, dict) or device.get("label") not in {"GPT", "CC"}:
            continue
        level = device.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
            level = None
        connected = device.get("connected") is True
        if not connected:
            level = None
        validated.append({"label": device["label"], "connected": connected, "level": level})
    return validated


def _display_choice(devices):
    connected_devices = [device for device in devices if device["connected"]]
    if not connected_devices:
        return "MIC", None, None
    if len(connected_devices) == 1:
        device = connected_devices[0]
        return device["label"], device["level"], device["label"]

    if any(device["level"] is None for device in connected_devices):
        return "MIC", None, None
    lowest = min(connected_devices, key=lambda device: (device["level"], device["label"]))
    return "MIC", lowest["level"], lowest["label"]


def _reading_from_payload(payload):
    connected = payload.get("connected") is True
    devices = _validated_devices(payload)
    label, level, selected = _display_choice(devices)
    return {
        "connected": any(device["connected"] for device in devices),
        "label": label,
        "level": level,
        "selected": selected,
        "devices": devices,
    }


def read_connected_dji_mic_mini_2_battery():
    """Return live GPT/CC HFP readings, marking paired but inactive devices offline.

    Matching an active Windows audio endpoint prevents a cached battery level
    from a paired but disconnected transmitter from being shown as current.
    Both known transmitters are included so the UI can display an offline row.
    """
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        candidate = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if candidate.is_file():
            powershell = str(candidate)
        else:
            raise RuntimeError("Windows PowerShell is unavailable")

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _POWERSHELL_QUERY],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        startupinfo=startupinfo,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Windows did not return the DJI Mic Mini 2 battery level")

    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError as error:
        raise RuntimeError("Windows returned an unreadable battery result") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Windows returned an invalid battery result")
    return _reading_from_payload(payload)
