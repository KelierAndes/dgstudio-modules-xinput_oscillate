
from __future__ import annotations

import collections
import ctypes
import ctypes.wintypes
import threading
import time

from modules.xinput_oscillate.vigem import (MOTOR_SCALE, XUSB_BUTTONS,
                                            XUSBReport, ViGemClient)

__all__ = ["KeyMap", "DEFAULT_KEYBOARD_MAP", "merge_inputs", "RealPadReader",
           "VirtualPad", "TRIGGER_MAX", "STICK_MAX"]

TRIGGER_MAX = 255
STICK_MAX = 32767

MARKER_BUTTONS = 0x0003

_VK_NAMES = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "up": 0x26,
    "down": 0x28, "left": 0x25, "right": 0x27, "shift": 0x10,
    "lshift": 0xA0, "rshift": 0xA1, "ctrl": 0x11, "lctrl": 0xA2,
    "rctrl": 0xA3, "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "capslock": 0x14, "numlock": 0x90, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{f"num{i}": 0x60 + i for i in range(10)},
    "nummul": 0x6A, "numadd": 0x6B, "numsub": 0x6D, "numdec": 0x6E,
    "numdiv": 0x6F, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD,
    ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC,
    "]": 0xDD, "'": 0xDE,
}

DEFAULT_KEYBOARD_MAP = {
    "a": "Space", "b": "Q", "x": "E", "y": "R",
    "lb": "1", "rb": "2", "lt": "3", "rt": "4",
    "back": "Backspace", "start": "Enter",
    "ls_click": "C", "rs_click": "V",
    "dpad_up": "Up", "dpad_down": "Down",
    "dpad_left": "Left", "dpad_right": "Right",
    "ls_up": "W", "ls_down": "S", "ls_left": "A", "ls_right": "D",
    "rs_up": "I", "rs_down": "K", "rs_left": "J", "rs_right": "L",
}

_STICK_SLOTS = {
    "ls_up": ("sThumbLY", STICK_MAX), "ls_down": ("sThumbLY", -STICK_MAX),
    "ls_left": ("sThumbLX", -STICK_MAX), "ls_right": ("sThumbLX", STICK_MAX),
    "rs_up": ("sThumbRY", STICK_MAX), "rs_down": ("sThumbRY", -STICK_MAX),
    "rs_left": ("sThumbRX", -STICK_MAX), "rs_right": ("sThumbRX", STICK_MAX),
}

_EMPTY_FIELDS = {"wButtons": 0, "bLeftTrigger": 0, "bRightTrigger": 0,
                 "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0}


def parse_vk(name: str) -> int | None:
    text = str(name or "").strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith("0x"):
        try:
            return int(low, 16)
        except ValueError:
            return None
    if len(text) == 1:
        ch = text
        if ch.isdigit():
            return ord(ch)
        if ch.isascii() and ch.isalpha():
            return ord(ch.upper())
    return _VK_NAMES.get(low)


class KeyMap:

    def __init__(self, mapping: dict | None = None):
        merged = dict(DEFAULT_KEYBOARD_MAP)
        merged.update({str(k): v for k, v in (mapping or {}).items()
                       if str(v or "").strip()})
        self.table: dict[str, int] = {}
        self.invalid: list[str] = []
        for slot, name in merged.items():
            vk = parse_vk(name)
            if vk is None:
                if slot in merged and slot not in DEFAULT_KEYBOARD_MAP:
                    self.invalid.append(f"{slot}={name}")
                elif str(name or "").strip():
                    self.invalid.append(f"{slot}={name}")
                continue
            self.table[slot] = vk

    def slots(self) -> list[str]:
        return sorted(self.table)

    def apply(self, pressed: set[int]) -> dict:
        fields = dict(_EMPTY_FIELDS)
        for slot, vk in self.table.items():
            if vk not in pressed:
                continue
            flag = XUSB_BUTTONS.get(slot)
            if flag:
                fields["wButtons"] |= flag
            elif slot == "lt":
                fields["bLeftTrigger"] = TRIGGER_MAX
            elif slot == "rt":
                fields["bRightTrigger"] = TRIGGER_MAX
            elif slot in _STICK_SLOTS:
                name, value = _STICK_SLOTS[slot]
                fields[name] = value
        return fields


def merge_inputs(keyboard: dict | None, pad: dict | None) -> dict:
    kb = keyboard or dict(_EMPTY_FIELDS)
    real = pad or dict(_EMPTY_FIELDS)
    out = dict(_EMPTY_FIELDS)
    out["wButtons"] = kb.get("wButtons", 0) | real.get("wButtons", 0)
    for name in ("bLeftTrigger", "bRightTrigger"):
        out[name] = max(kb.get(name, 0), real.get(name, 0))
    for name in ("sThumbLX", "sThumbLY", "sThumbRX", "sThumbRY"):
        out[name] = real.get(name, 0) or kb.get(name, 0)
    return out


class RealPadReader:

    def __init__(self, dll_names: tuple[str, ...] = ("xinput1_4",
                                                     "xinput1_3",
                                                     "xinput9_1_0")):
        self._dll = None
        for name in dll_names:
            try:
                self._dll = ctypes.WinDLL(name)
                break
            except OSError:
                continue

    @property
    def available(self) -> bool:
        return self._dll is not None

    def read(self, index: int) -> dict | None:
        if self._dll is None:
            return None
        state = _XInputState()
        rc = self._dll.XInputGetState(index, ctypes.byref(state))
        if rc != 0:
            return None
        pad = state.pad
        return {"wButtons": pad.wButtons, "bLeftTrigger": pad.bLeftTrigger,
                "bRightTrigger": pad.bRightTrigger, "sThumbLX": pad.sThumbLX,
                "sThumbLY": pad.sThumbLY, "sThumbRX": pad.sThumbRX,
                "sThumbRY": pad.sThumbRY}

    def set_vibration(self, index: int, left: int, right: int) -> bool:
        if self._dll is None or not hasattr(self._dll, "XInputSetState"):
            return False
        vib = _XInputVibration(min(65535, max(0, int(left))),
                               min(65535, max(0, int(right))))
        return self._dll.XInputSetState(index, ctypes.byref(vib)) == 0


class _XInputState(ctypes.Structure):
    class _Pad(ctypes.Structure):
        _fields_ = [("wButtons", ctypes.c_uint16),
                    ("bLeftTrigger", ctypes.c_ubyte),
                    ("bRightTrigger", ctypes.c_ubyte),
                    ("sThumbLX", ctypes.c_int16),
                    ("sThumbLY", ctypes.c_int16),
                    ("sThumbRX", ctypes.c_int16),
                    ("sThumbRY", ctypes.c_int16)]

    _fields_ = [("packet", ctypes.c_uint32), ("pad", _Pad)]


class _XInputVibration(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", ctypes.c_uint16),
                ("wRightMotorSpeed", ctypes.c_uint16)]


class VirtualPad:

    def __init__(self, keymap: KeyMap, *, keyboard_enabled: bool = True,
                 forward_pad: bool = False,
                 client: ViGemClient | None = None,
                 pad_reader: RealPadReader | None = None):
        self.keymap = keymap
        self.keyboard_enabled = keyboard_enabled
        self.forward_pad = forward_pad
        self._own_client = client is None
        self.client = client or ViGemClient()
        self.pad_reader = pad_reader or RealPadReader()
        self._lock = threading.Lock()
        self._feedback: collections.deque = collections.deque(maxlen=64)
        self._last_fields: dict | None = None
        self._real_idx: int | None = None
        self.xinput_index: int | None = None

    def start(self) -> None:
        self.client.connect()
        self.client.add_x360()
        self.client.register_notification(self._on_notification)

    def stop(self) -> None:
        try:
            if self.client.attached():
                self.client.update(XUSBReport())
        except Exception:
            pass
        if self._own_client:
            self.client.close()

    def _on_notification(self, large: int, small: int, led: int) -> None:
        with self._lock:
            self._feedback.append((large, small, time.perf_counter()))

    def drain_feedback(self) -> list[tuple[int, int, float]]:
        with self._lock:
            pending = list(self._feedback)
            self._feedback.clear()
        return pending

    def poll(self) -> bool:
        fields = None
        if self.keyboard_enabled:
            pressed = {vk for vk in self.keymap.table.values()
                       if vk in self._pressed()}
            fields = self.keymap.apply(pressed)
        if self.forward_pad:
            raw = None
            index = self._real_index()
            if index is not None:
                raw = self.pad_reader.read(index)
            if raw is not None:
                fields = merge_inputs(fields, raw)
        if fields == self._last_fields:
            return False
        self._last_fields = fields
        self.client.update(XUSBReport(**(fields or dict(_EMPTY_FIELDS))))
        return True

    def _pressed(self) -> set[int]:
        out = set()
        for vk in self.keymap.table.values():
            if ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
                out.add(vk)
        return out

    def _real_index(self) -> int | None:
        if self._real_idx is not None:
            if self.pad_reader.read(self._real_idx) is not None:
                return self._real_idx
            self._real_idx = None
        if not self.pad_reader.available:
            return None
        for index in range(4):
            if index == self.xinput_index:
                continue
            if self.pad_reader.read(index) is not None:
                self._real_idx = index
                return index
        return None

    def probe_xinput_index(self, timeout: float = 1.0) -> int | None:
        if self.xinput_index is not None:
            return self.xinput_index
        if not self.pad_reader.available:
            return None
        marker = XUSBReport(wButtons=MARKER_BUTTONS)
        try:
            self.client.update(marker)
        except Exception:
            return None
        deadline = time.monotonic() + timeout
        found = None
        while time.monotonic() < deadline and found is None:
            for index in range(4):
                raw = self.pad_reader.read(index)
                if raw is not None and raw["wButtons"] == MARKER_BUTTONS:
                    found = index
                    break
            if found is None:
                time.sleep(0.05)
        try:
            self.client.update(XUSBReport())
        except Exception:
            pass
        if found is not None:
            self.xinput_index = found
        return found

    def send_vibration(self, left: int, right: int) -> bool:
        index = self.xinput_index
        if index is None:
            return False
        return self.pad_reader.set_vibration(
            index, int(left * MOTOR_SCALE), int(right * MOTOR_SCALE))
