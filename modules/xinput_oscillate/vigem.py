
from __future__ import annotations

import ctypes
import os
import sys
import winreg

__all__ = ["VIGEM_ERROR_NONE", "VIGEM_ERROR_BUS_NOT_FOUND", "ViGemError",
           "ViGemClient", "bus_available", "client_dll_path", "XUSBReport",
           "NOTIFICATION", "XUSB_BUTTONS", "MOTOR_SCALE"]

VIGEM_ERROR_NONE = 0x20000000
VIGEM_ERROR_BUS_NOT_FOUND = 0x20000001

MOTOR_SCALE = 257

XUSB_BUTTONS = {
    "dpad_up": 0x0001, "dpad_down": 0x0002, "dpad_left": 0x0004,
    "dpad_right": 0x0008, "start": 0x0010, "back": 0x0020,
    "ls_click": 0x0040, "rs_click": 0x0080,
    "lb": 0x0100, "rb": 0x0200,
    "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
}


class XUSBReport(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_uint16),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_int16),
                ("sThumbLY", ctypes.c_int16),
                ("sThumbRX", ctypes.c_int16),
                ("sThumbRY", ctypes.c_int16)]


NOTIFICATION = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte)


class ViGemError(RuntimeError):
    pass


def bus_available() -> bool:
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Services\ViGEmBus")
        return True
    except OSError:
        return False


def client_dll_path() -> str | None:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    if sys.maxsize > 2 ** 32:
        name = "ViGEmClient.dll"
    else:
        return None
    path = os.path.join(base, name)
    return path if os.path.isfile(path) else None


class ViGemClient:

    def __init__(self, dll_path: str | None = None):
        path = dll_path or client_dll_path()
        if not path:
            raise ViGemError("未找到 ViGEmClient.dll（模块 bin/ 目录缺失或"
                             "32 位解释器）")
        self._dll = ctypes.WinDLL(path)
        self._proto()
        self._client = None
        self._target = None
        self._notif_ref = None
        self.feedback = None

    def _proto(self) -> None:
        d = self._dll
        c_void_p, c_uint32, c_ulong = (ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.c_ulong)
        d.vigem_alloc.restype = c_void_p
        d.vigem_free.argtypes = [c_void_p]
        d.vigem_connect.argtypes = [c_void_p]
        d.vigem_connect.restype = c_uint32
        d.vigem_disconnect.argtypes = [c_void_p]
        d.vigem_disconnect.restype = c_uint32
        d.vigem_target_x360_alloc.restype = c_void_p
        d.vigem_target_free.argtypes = [c_void_p]
        d.vigem_target_add.argtypes = [c_void_p, c_void_p]
        d.vigem_target_add.restype = c_uint32
        d.vigem_target_remove.argtypes = [c_void_p, c_void_p]
        d.vigem_target_remove.restype = c_uint32
        d.vigem_target_get_index.argtypes = [c_void_p]
        d.vigem_target_get_index.restype = c_ulong
        d.vigem_target_is_attached.argtypes = [c_void_p]
        d.vigem_target_is_attached.restype = ctypes.c_bool
        d.vigem_target_x360_update.argtypes = [c_void_p, c_void_p, XUSBReport]
        d.vigem_target_x360_update.restype = c_uint32
        d.vigem_target_x360_register_notification.argtypes = [
            c_void_p, c_void_p, NOTIFICATION]
        d.vigem_target_x360_register_notification.restype = c_uint32

    def connect(self) -> None:
        self._client = self._dll.vigem_alloc()
        rc = self._dll.vigem_connect(self._client)
        if rc == VIGEM_ERROR_BUS_NOT_FOUND:
            raise ViGemError("未找到 ViGEmBus 驱动（0x20000001）——"
                             "请先安装：github.com/nefarius/ViGEmBus/releases")
        if rc != VIGEM_ERROR_NONE:
            raise ViGemError(f"ViGEm 连接失败（{rc:#x}）")

    def add_x360(self) -> None:
        self._target = self._dll.vigem_target_x360_alloc()
        rc = self._dll.vigem_target_add(self._client, self._target)
        if rc != VIGEM_ERROR_NONE:
            self._target = None
            raise ViGemError(f"创建虚拟手柄失败（{rc:#x}）")

    def register_notification(self, feedback) -> None:
        def trampoline(_client, _ctx, large, small, led):
            try:
                feedback(large, small, led)
            except Exception:
                pass
        self.feedback = feedback
        self._notif_ref = NOTIFICATION(trampoline)
        rc = self._dll.vigem_target_x360_register_notification(
            self._client, self._target, self._notif_ref)
        if rc != VIGEM_ERROR_NONE:
            self._notif_ref = None
            raise ViGemError(f"注册震动反馈失败（{rc:#x}）")

    def close(self) -> None:
        if self._target is not None and self._client is not None:
            try:
                self._dll.vigem_target_remove(self._client, self._target)
            except Exception:
                pass
            self._dll.vigem_target_free(self._target)
            self._target = None
        if self._client is not None:
            try:
                self._dll.vigem_disconnect(self._client)
            except Exception:
                pass
            self._dll.vigem_free(self._client)
            self._client = None
        self._notif_ref = None

    @property
    def index(self) -> int:
        return int(self._dll.vigem_target_get_index(self._target)) \
            if self._target else 0

    def attached(self) -> bool:
        return bool(self._dll.vigem_target_is_attached(self._target)) \
            if self._target else False

    def update(self, report: XUSBReport) -> None:
        rc = self._dll.vigem_target_x360_update(self._client, self._target,
                                                report)
        if rc != VIGEM_ERROR_NONE:
            raise ViGemError(f"虚拟手柄状态更新失败（{rc:#x}）")
