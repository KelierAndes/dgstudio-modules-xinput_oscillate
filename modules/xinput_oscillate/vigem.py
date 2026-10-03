"""ViGEmClient.dll 的 ctypes 薄封装：虚拟手柄创建与震动反馈接收。

通过本机已安装的 ViGEmBus 驱动创建虚拟 Xbox 360 手柄。游戏（无需任何
注入）照常经 XInput 把震动派发给这只虚拟手柄——即游戏本身的震动派发
通道；派发结果经 ``register_notification`` 的回调回传，``LargeMotor`` /
``SmallMotor`` 即左右马达强度 0-255。

随模块分发 ``bin/ViGEmClient.dll``（x64，源自官方 MIT 实现，
源码 https://github.com/nefarius/ViGEmClient ，许可全文见
``bin/LICENSE.ViGEmClient.txt``）。ViGEmBus 内核驱动需用户自行安装
（官方 https://github.com/nefarius/ViGEmBus/releases ）；本模块提供
:func:`bus_available` 探测，缺失时降级为不可用并在日志中说明。
"""

from __future__ import annotations

import ctypes
import os
import sys
import winreg

__all__ = ["VIGEM_ERROR_NONE", "VIGEM_ERROR_BUS_NOT_FOUND", "ViGemError",
           "ViGemClient", "bus_available", "client_dll_path", "XUSBReport",
           "NOTIFICATION", "XUSB_BUTTONS", "MOTOR_SCALE"]

# ViGEm 错误码基值（VIGEM_ERROR_NONE = 0x20000000，见官方 Common.h）
VIGEM_ERROR_NONE = 0x20000000
VIGEM_ERROR_BUS_NOT_FOUND = 0x20000001

# XUSB 马达 0-255 → XInput 马达满量程 0-65535（255*257=65535）
MOTOR_SCALE = 257

# 与 XInput 等价的按钮旗标（XUSB_GAMEPAD_*）
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
    """ViGEm 调用失败（error 为 VIGEM_ERROR_* 十六进制码）。"""


def bus_available() -> bool:
    """ViGEmBus 驱动是否已安装（读服务注册表键，无需管理员）。"""
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Services\ViGEmBus")
        return True
    except OSError:
        return False


def client_dll_path() -> str | None:
    """随模块分发的 ViGEmClient.dll 路径（与当前解释器位数匹配）。"""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    if sys.maxsize > 2 ** 32:
        name = "ViGEmClient.dll"
    else:
        return None        # 未随包分发 32 位客户端
    path = os.path.join(base, name)
    return path if os.path.isfile(path) else None


class ViGemClient:
    """单个 ViGEm 连接 + 一只虚拟 Xbox 360 手柄。"""

    def __init__(self, dll_path: str | None = None):
        path = dll_path or client_dll_path()
        if not path:
            raise ViGemError("未找到 ViGEmClient.dll（模块 bin/ 目录缺失或"
                             "32 位解释器）")
        self._dll = ctypes.WinDLL(path)
        self._proto()
        self._client = None
        self._target = None
        self._notif_ref = None               # 保住回调对象不被 GC
        self.feedback = None                 # fn(large, small, led) 由上层注入

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

    # ---- 生命周期 -------------------------------------------------------
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
        """feedback(large:int, small:int, led:int)，由 ViGEm 工作线程调用。"""
        def trampoline(_client, _ctx, large, small, led):
            try:
                feedback(large, small, led)
            except Exception:
                pass                    # 反馈回调绝不上抛拖垮驱动线程
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

    # ---- 手柄控制 ---------------------------------------------------------
    @property
    def index(self) -> int:
        """总线序号（1 起；与 XInput 序号不一定相同，仅供日志）。"""
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
