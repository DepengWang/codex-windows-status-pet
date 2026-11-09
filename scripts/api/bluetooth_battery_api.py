"""Read configured Bluetooth device batteries from Windows PnP properties.

Uses CfgMgr32 in-process (no PowerShell child process); one read takes
milliseconds instead of seconds of process start-up and PnP enumeration.
"""

from __future__ import annotations

import ctypes
import re
import sys
import uuid
from ctypes import wintypes


# Last six hex digits of the transmitter's Bluetooth address -> display label.
DEVICE_LABELS = {"6F775D": "GPT", "6F3291": "CC"}
MIC_LABELS = frozenset(DEVICE_LABELS.values())
DEVICE_NAME_PATTERNS = {"KEY": re.compile(r"^IINE_KeyBoard$", re.IGNORECASE)}
_NODE_NAME = re.compile(r"^DJI Mic Mini 2-([0-9A-F]{6}) Hands-Free AG$")
_ENDPOINT_NAME = re.compile(r"\(DJI Mic Mini 2-([0-9A-F]{6}) Hands-Free\)")

_CLASS_SYSTEM = "{4D36E97D-E325-11CE-BFC1-08002BE10318}"
_CLASS_AUDIO_ENDPOINT = "{C166523C-FE0C-4A94-A586-F1A80CFBBF3E}"
_CM_GETIDLIST_FILTER_PRESENT = 0x00000100
_CM_GETIDLIST_FILTER_CLASS = 0x00000200
_CR_SUCCESS = 0
_DEVPROP_TYPE_BYTE = 0x03
_DEVPROP_TYPE_STRING = 0x12


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", wintypes.ULONG)]


def _devpropkey(guid, pid):
    raw = uuid.UUID(guid)
    fmtid = _Guid(
        raw.time_low, raw.time_mid, raw.time_hi_version, (ctypes.c_ubyte * 8)(*raw.bytes[8:])
    )
    return _DevPropKey(fmtid, pid)


_cfgmgr = None
_KEY_FRIENDLY_NAME = None
_KEY_BATTERY_LEVEL = None


def _api():
    global _cfgmgr, _KEY_FRIENDLY_NAME, _KEY_BATTERY_LEVEL
    if _cfgmgr is None:
        if sys.platform != "win32":
            raise RuntimeError("Windows PnP properties are unavailable on this platform")
        lib = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        lib.CM_Get_Device_ID_List_SizeW.argtypes = [
            ctypes.POINTER(wintypes.ULONG), wintypes.LPCWSTR, wintypes.ULONG,
        ]
        lib.CM_Get_Device_ID_List_SizeW.restype = wintypes.DWORD
        lib.CM_Get_Device_ID_ListW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
        ]
        lib.CM_Get_Device_ID_ListW.restype = wintypes.DWORD
        lib.CM_Locate_DevNodeW.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG,
        ]
        lib.CM_Locate_DevNodeW.restype = wintypes.DWORD
        lib.CM_Get_DevNode_PropertyW.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(_DevPropKey),
            ctypes.POINTER(wintypes.ULONG),
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.ULONG),
            wintypes.ULONG,
        ]
        lib.CM_Get_DevNode_PropertyW.restype = wintypes.DWORD
        _KEY_FRIENDLY_NAME = _devpropkey("A45C254E-DF1C-4EFD-8020-67D146A850E0", 14)
        _KEY_BATTERY_LEVEL = _devpropkey("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)
        _cfgmgr = lib
    return _cfgmgr


def _present_instance_ids(class_guid=None):
    lib = _api()
    flags = _CM_GETIDLIST_FILTER_PRESENT
    if class_guid is not None:
        flags |= _CM_GETIDLIST_FILTER_CLASS
    for _ in range(3):  # the list can grow between the size and read calls
        size = wintypes.ULONG(0)
        if lib.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), class_guid, flags) != _CR_SUCCESS:
            raise RuntimeError("Windows could not size the device list")
        buffer = ctypes.create_unicode_buffer(size.value + 2)
        if lib.CM_Get_Device_ID_ListW(class_guid, buffer, size.value, flags) == _CR_SUCCESS:
            return [item for item in ctypes.wstring_at(buffer, size.value).split("\0") if item]
    raise RuntimeError("Windows could not enumerate devices")

def _property(instance_id, key, expected_type):
    """Return the raw property bytes, or None when the device lacks the property."""
    lib = _api()
    devinst = wintypes.DWORD()
    if lib.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id, 0) != _CR_SUCCESS:
        return None
    prop_type = wintypes.ULONG()
    size = wintypes.ULONG(0)
    lib.CM_Get_DevNode_PropertyW(
        devinst, ctypes.byref(key), ctypes.byref(prop_type), None, ctypes.byref(size), 0
    )
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    status = lib.CM_Get_DevNode_PropertyW(
        devinst, ctypes.byref(key), ctypes.byref(prop_type), buffer, ctypes.byref(size), 0
    )
    if status != _CR_SUCCESS or prop_type.value != expected_type:
        return None
    return buffer.raw[: size.value]


def _friendly_name(instance_id):
    raw = _property(instance_id, _KEY_FRIENDLY_NAME, _DEVPROP_TYPE_STRING)
    return raw.decode("utf-16-le").rstrip("\0") if raw else ""


def _payload_from_windows():
    # Mic connectivity is confirmed by an active audio endpoint. Generic
    # Bluetooth devices use Windows' present-device list and the same battery
    # property reader, matched by configured friendly name.
    readings = {
        label: {"label": label, "connected": False, "level": None}
        for label in ("GPT", "CC", *DEVICE_NAME_PATTERNS)
    }
    active = set()
    for instance_id in _present_instance_ids(_CLASS_AUDIO_ENDPOINT):
        match = _ENDPOINT_NAME.search(_friendly_name(instance_id))
        if match:
            active.add(match.group(1).upper())
    for instance_id in _present_instance_ids(_CLASS_SYSTEM):
        if "BTHENUM" not in instance_id.upper():
            continue
        match = _NODE_NAME.match(_friendly_name(instance_id))
        if not match:
            continue
        suffix = match.group(1).upper()
        label = DEVICE_LABELS.get(suffix)
        if label is None or suffix not in active:
            continue
        raw = _property(instance_id, _KEY_BATTERY_LEVEL, _DEVPROP_TYPE_BYTE)
        level = raw[0] if raw and 0 <= raw[0] <= 100 else None
        current = readings[label]
        current["connected"] = True
        if level is not None or current["level"] is None:
            current["level"] = level
    for instance_id in _present_instance_ids():
        friendly_name = _friendly_name(instance_id).strip()
        label = next(
            (name for name, pattern in DEVICE_NAME_PATTERNS.items()
             if pattern.fullmatch(friendly_name)),
            None,
        )
        if label is None:
            continue
        raw = _property(instance_id, _KEY_BATTERY_LEVEL, _DEVPROP_TYPE_BYTE)
        level = raw[0] if raw and 0 <= raw[0] <= 100 else None
        current = readings[label]
        current["connected"] = True
        if level is not None or current["level"] is None:
            current["level"] = level
    devices = list(readings.values())
    return {"connected": any(item["connected"] for item in devices), "devices": devices}

def _validated_devices(payload):
    devices = payload.get("devices")
    if isinstance(devices, dict):
        devices = [devices]
    if not isinstance(devices, list):
        return []

    validated = []
    for device in devices:
        if not isinstance(device, dict) or device.get("label") not in {"GPT", "CC", "KEY"}:
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
    connected_devices = [
        device for device in devices
        if device["connected"] and device["label"] in MIC_LABELS
    ]
    if not connected_devices:
        return "MIC", None, None
    if len(connected_devices) == 1:
        device = connected_devices[0]
        return device["label"], device["level"], device["label"]
    if any(device["level"] is None for device in connected_devices):
        return "MIC", None, None
    lowest = min(connected_devices, key=lambda device: (device["level"], device["label"]))
    return "MIC", lowest["level"], lowest["label"]

def _reading_from_payload(payload, allowed_labels=None):
    devices = _validated_devices(payload)
    if allowed_labels is not None:
        devices = [device for device in devices if device["label"] in allowed_labels]
    label, level, selected = _display_choice(devices)
    mic_devices = [device for device in devices if device["label"] in MIC_LABELS]
    return {
        "connected": any(device["connected"] for device in mic_devices),
        "label": label,
        "level": level,
        "selected": selected,
        "devices": devices,
    }
def read_configured_device_batteries():
    """Return Mic and configured generic-device readings from Windows."""
    return _reading_from_payload(_payload_from_windows())
def read_connected_dji_mic_mini_2_battery():
    """Compatibility API returning only the two DJI Mic Mini 2 readings."""
    return _reading_from_payload(_payload_from_windows(), MIC_LABELS)
