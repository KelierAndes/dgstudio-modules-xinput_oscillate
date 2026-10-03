"""虚拟手柄：键盘映射 / 实体手柄透传 / 游戏震动反馈接收。

围绕 :class:`vigem.ViGemClient` 组合三件事：

* **键盘 → 虚拟手柄**：``KeyMap`` 把可配置的键盘键位（VK）合成为
  XUSB 手柄状态，无实体手柄也能驱动游戏（调试场景）；
* **实体手柄透传**（可选）：读取实体 XInput 手柄状态合并进虚拟手柄——
  游戏改为使用虚拟手柄后，其原生震动派发才会回流到反馈通道；
* **震动反馈**：游戏对虚拟手柄的 ``XInputSetState`` 由 ViGEm 回调
  （驱动工作线程）通知，本层做线程安全暂存，引擎循环按拍取走。

虚拟手柄的 XInput 序号用「十字键上+下同时按」探测——真实手柄物理上
无法同按这两个方向，按此标记轮询 XInput 即可无歧义定位。
"""

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

MARKER_BUTTONS = 0x0003            # dpad_up | dpad_down（探测标记）

# 键盘键名 → Windows VK 码（其余支持 "0x…" 十六进制与单字符）
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

# 键盘方向槽位 → (轴字段, 方向)
_STICK_SLOTS = {
    "ls_up": ("sThumbLY", STICK_MAX), "ls_down": ("sThumbLY", -STICK_MAX),
    "ls_left": ("sThumbLX", -STICK_MAX), "ls_right": ("sThumbLX", STICK_MAX),
    "rs_up": ("sThumbRY", STICK_MAX), "rs_down": ("sThumbRY", -STICK_MAX),
    "rs_left": ("sThumbRX", -STICK_MAX), "rs_right": ("sThumbRX", STICK_MAX),
}

_EMPTY_FIELDS = {"wButtons": 0, "bLeftTrigger": 0, "bRightTrigger": 0,
                 "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0}


def parse_vk(name: str) -> int | None:
    """键盘键名 → VK 码；支持 0x 十六进制、单字符与常用键名；非法返回 None。"""
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
    """键位表：虚拟手柄槽位名 → 键盘 VK（纯逻辑，可独立单测）。"""

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
        """按下的 VK 集合 → XUSB 状态字段。"""
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
    """键盘字段与实体手柄字段合并：按钮取并集、扳机取较大值、
    摇杆轴手柄非零优先。"""
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
    """ctypes 直读 XInput（轮询输入 / 回环测试用，不依赖任何 pip 包）。"""

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
        """手柄状态字段；未连接返回 None。"""
        if self._dll is None:
            return None
        state = _XInputState()
        rc = self._dll.XInputGetState(index, ctypes.byref(state))
        if rc != 0:                       # ERROR_SUCCESS
            return None
        pad = state.pad
        return {"wButtons": pad.wButtons, "bLeftTrigger": pad.bLeftTrigger,
                "bRightTrigger": pad.bRightTrigger, "sThumbLX": pad.sThumbLX,
                "sThumbLY": pad.sThumbLY, "sThumbRX": pad.sThumbRX,
                "sThumbRY": pad.sThumbRY}

    def set_vibration(self, index: int, left: int, right: int) -> bool:
        """XInputSetState（回环测试：对虚拟手柄派发震动）。"""
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
    """虚拟 Xbox 360 手柄：键盘 + 实体手柄 → 输入合成；震动反馈暂存。"""

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
        self.xinput_index: int | None = None   # 探测后填入

    # ---- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        self.client.connect()
        self.client.add_x360()
        self.client.register_notification(self._on_notification)

    def stop(self) -> None:
        try:
            if self.client.attached():
                self.client.update(XUSBReport())   # 输入归零再摘除
        except Exception:
            pass
        if self._own_client:
            self.client.close()

    # ---- 震动反馈（ViGEm 工作线程 → 引擎循环） ----------------------------
    def _on_notification(self, large: int, small: int, led: int) -> None:
        """回调线程只做入队（高频派发逐条保留，不丢中间值）。"""
        with self._lock:
            self._feedback.append((large, small, time.perf_counter()))

    def drain_feedback(self) -> list[tuple[int, int, float]]:
        """取走全部暂存反馈 ``(LargeMotor, SmallMotor, 时刻)``，按到达序。"""
        with self._lock:
            pending = list(self._feedback)
            self._feedback.clear()
        return pending

    # ---- 输入合成 -----------------------------------------------------------
    def poll(self) -> bool:
        """采样键盘与实体手柄，状态有变化时更新虚拟手柄。返回是否更新。"""
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
        """当前按下的键盘 VK 集合（GetAsyncKeyState）。"""
        out = set()
        for vk in self.keymap.table.values():
            if ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
                out.add(vk)
        return out

    def _real_index(self) -> int | None:
        """实体手柄的 XInput 序号（排除虚拟手柄自身；断开时重探）。"""
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

    # ---- XInput 序号探测 ----------------------------------------------------
    def probe_xinput_index(self, timeout: float = 1.0) -> int | None:
        """用「十字键上+下」标记探测虚拟手柄占用的 XInput 序号。"""
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
        """对虚拟手柄按 XInput 原生通道派发震动（回环测试）。"""
        index = self.xinput_index
        if index is None:
            return False
        return self.pad_reader.set_vibration(
            index, int(left * MOTOR_SCALE), int(right * MOTOR_SCALE))
