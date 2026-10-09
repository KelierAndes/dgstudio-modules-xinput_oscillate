
from __future__ import annotations

import asyncio
import copy
import time

from modules.xinput_oscillate import vigem
from modules.xinput_oscillate.vigem import MOTOR_SCALE
from modules.xinput_oscillate.virtualpad import KeyMap, VirtualPad

__all__ = ["BridgeConfig", "RumbleMixer", "SignalBoard", "XInputBridge",
           "MOTOR_MAX", "PARAM_KEYS"]

MOTOR_MAX = 65535

PARAM_KEYS = ("xvib_l", "xvib_r", "xvib_max", "xvib_active", "xvib_link")

DEFAULTS = {
    "vigem_enabled": True,
    "keyboard_enabled": True,
    "forward_pad": False,
    "keyboard_map": {},
    "gain": 1.0,
    "deadband_pct": 2.0,
    "active_pct": 15.0,
    "release_ms": 300,
    "idle_ms": 1000,
}


def _num(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _as_number(value) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class SignalBoard:
    """模块只登记变量：实时值落在 signals，设备动作由事件流的写入卡片驱动。

    核心按 ``inst.bridge.engine`` 取 ``signals`` / ``errors`` 渲染变量表与实时值，
    并会对引擎调用 ``pump()``——本模块没有表达式表，pump 是空实现，签名保留。
    """

    def __init__(self) -> None:
        self.signals: dict[str, float] = {}
        self.errors: dict[str, str] = {}

    def signal(self, name: str, value) -> None:
        num = _as_number(value)
        if num is None or self.signals.get(name) == num:
            return
        self.signals[name] = num

    def pump(self) -> None:
        return None

    def reset(self) -> None:
        self.signals.clear()
        self.errors.clear()


class BridgeConfig(dict):

    def __init__(self, data: dict | None = None, defaults: dict | None = None):
        super().__init__(copy.deepcopy(defaults or DEFAULTS))
        if data:
            self.update({k: v for k, v in data.items() if v is not None})


class RumbleMixer:

    def __init__(self, config: dict):
        self.config = config
        self._targets: dict[int, tuple[float, float]] = {}
        self._last_rx: dict[int, float] = {}
        self._focus: int = 0
        self._env = [0.0, 0.0]
        self._last_tick: float | None = None

    def on_packet(self, u: int, left: float, right: float, now: float) -> None:
        gain = max(0.0, _num(self.config.get("gain"), 1.0))
        deadband = min(100.0, max(0.0, _num(self.config.get("deadband_pct"), 0.0)))
        target = tuple(self._scale(v, gain, deadband) for v in (left, right))
        self._targets[u] = target
        if left > 0 or right > 0:
            self._last_rx[u] = now
            self._focus = u
        self._last_tick = now if self._last_tick is None else self._last_tick
        for i in (0, 1):
            if target[i] > self._env[i]:
                self._env[i] = target[i]

    @staticmethod
    def _scale(raw: float, gain: float, deadband: float) -> float:
        pct = (raw / MOTOR_MAX) * 100.0 * gain
        return 0.0 if pct < deadband else min(100.0, pct)

    def tick(self, now: float) -> dict[str, float]:
        idle_s = max(0.0, _num(self.config.get("idle_ms"), 1000.0)) / 1000.0
        release_s = max(0.01, _num(self.config.get("release_ms"), 300.0)) / 1000.0
        active_pct = max(0.0, _num(self.config.get("active_pct"), 0.0))

        last = self._last_tick
        dt = 0.0 if last is None else max(0.0, min(1.0, now - last))
        self._last_tick = now

        target = self._targets.get(self._focus, (0.0, 0.0))
        link = 1.0 if (self._focus in self._last_rx
                       and now - self._last_rx[self._focus] <= idle_s) else 0.0
        for i in (0, 1):
            goal = target[i] if link else 0.0
            if goal >= self._env[i]:
                self._env[i] = goal
            else:
                self._env[i] = max(goal, self._env[i] - 100.0 / release_s * dt)

        peak = max(self._env)
        return {
            "xvib_l": round(self._env[0], 1),
            "xvib_r": round(self._env[1], 1),
            "xvib_max": round(peak, 1),
            "xvib_active": 1.0 if peak > active_pct else 0.0,
            "xvib_link": link,
        }

    def reset(self) -> None:
        self._targets.clear()
        self._last_rx.clear()
        self._env = [0.0, 0.0]
        self._last_tick = None


def _default_pad_factory(config: dict) -> VirtualPad | None:
    if not config.get("vigem_enabled", True):
        return None
    keymap = KeyMap(config.get("keyboard_map") or {})
    return VirtualPad(
        keymap,
        keyboard_enabled=bool(config.get("keyboard_enabled", True)),
        forward_pad=bool(config.get("forward_pad", False)))


class XInputBridge:

    def __init__(self, config: BridgeConfig, ctx, pad_factory=None):
        self.config = config
        self.ctx = ctx
        self.mixer = RumbleMixer(config)
        self.engine = SignalBoard()
        self._pad_factory = pad_factory or _default_pad_factory

        self.pad: VirtualPad | None = None
        self._tick_task: asyncio.Task | None = None
        self._inputs_task: asyncio.Task | None = None
        self._running = False
        self._last_fed: dict[str, float] = {}
        self._link_seen: bool | None = None
        self.rx_count = 0
        self.last_rx: float | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._start_pad()
        self._tick_task = asyncio.ensure_future(self._tick_loop())
        self._inputs_task = asyncio.ensure_future(self._inputs_loop())
        self.ctx.log("震动桥已启动" + ("，虚拟手柄就绪" if self.pad else
                     "（无虚拟手柄，仅测试注入可用）"))

    async def _start_pad(self) -> None:
        if not self.config.get("vigem_enabled", True):
            self.ctx.log("虚拟手柄已禁用（模块设置 vigem_enabled）")
            return
        if not vigem.bus_available():
            self.ctx.log("未检测到 ViGEmBus 驱动，虚拟手柄不可用——"
                         "请安装 ViGEmBus（github.com/nefarius/ViGEmBus/"
                         "releases），安装后重新开关本模块")
            return
        try:
            pad = self._pad_factory(self.config)
            if pad is None:
                return
            await asyncio.to_thread(pad.start)
            self.pad = pad
            index = await asyncio.to_thread(pad.probe_xinput_index)
        except Exception as exc:
            self.pad = None
            self.ctx.log(f"虚拟手柄创建失败，已降级为仅测试注入: {exc!r}")
            return
        self.ctx.log(f"虚拟手柄已接入（XInput 序号 {index if index is not None else '未知'}；"
                     f"键盘映射{'开' if pad.keyboard_enabled else '关'}，"
                     f"实体手柄透传{'开' if pad.forward_pad else '关'}）")
        self._last_fed["xvib_pad"] = 1.0
        self.engine.signal("xvib_pad", 1.0)

    async def stop(self) -> None:
        self._running = False
        for task in (self._tick_task, self._inputs_task):
            if task is not None:
                task.cancel()
        self._tick_task = self._inputs_task = None
        pad, self.pad = self.pad, None
        if pad is not None:
            try:
                await asyncio.to_thread(pad.stop)
            except Exception:
                pass
            self._last_fed["xvib_pad"] = 0.0
            self.engine.signal("xvib_pad", 0.0)
        self.mixer.reset()
        self.engine.reset()
        self.ctx.log("震动桥已停止")

    def is_running(self) -> bool:
        return self._running

    def _consume_feedback(self) -> None:
        pad = self.pad
        if pad is None:
            return
        for large, small, ts in pad.drain_feedback():
            self._on_feedback(large, small, ts)

    def _on_feedback(self, large: int, small: int, now: float) -> None:
        left = float(large) * MOTOR_SCALE
        right = float(small) * MOTOR_SCALE
        if left > 0 or right > 0:
            self.rx_count += 1
            self.last_rx = now
        self.mixer.on_packet(0, left, right, now)
        self._feed(self.mixer.tick(now))

    def _feed(self, values: dict[str, float]) -> None:
        for key, value in values.items():
            if self._last_fed.get(key) != value:
                self._last_fed[key] = value
                self.engine.signal(key, value)
        link = values.get("xvib_link")
        if link is not None and bool(link) != self._link_seen:
            self._link_seen = bool(link)
            self.ctx.log("游戏震动链路：" + ("有数据" if link else "已断开（超时）"))

    async def _tick_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.04)
                self._consume_feedback()
                self._feed(self.mixer.tick(time.perf_counter()))
        except asyncio.CancelledError:
            pass

    async def _inputs_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.016)
                if self.pad is not None:
                    self.pad.poll()
        except asyncio.CancelledError:
            pass

    async def inject_test(self, pct: float, seconds: float = 0.8) -> None:
        pct = min(100.0, max(1.0, float(pct or 60)))
        raw = pct / 100.0 * MOTOR_MAX
        deadline = time.perf_counter() + max(0.1, seconds)
        while time.perf_counter() < deadline:
            now = time.perf_counter()
            self.mixer.on_packet(0, raw, raw, now)
            self._feed(self.mixer.tick(now))
            await asyncio.sleep(0.05)
        now = time.perf_counter()
        self.mixer.on_packet(0, 0.0, 0.0, now)
        self._feed(self.mixer.tick(now))
        self.ctx.log(f"测试震动脉冲已注入（{pct:.0f}%，{seconds:.1f}s）")

    async def pad_loopback(self, pct: float, seconds: float = 0.6) -> None:
        pad = self.pad
        if pad is None:
            self.ctx.log("虚拟手柄未就绪，回环测试不可用")
            return
        pct = min(100.0, max(1.0, float(pct or 60)))
        raw = int(pct / 100.0 * MOTOR_MAX)
        if not pad.send_vibration(raw, raw):
            self.ctx.log("回环测试失败：虚拟手柄的 XInput 序号未知，"
                         "请重新开关模块重试")
            return
        await asyncio.sleep(max(0.1, seconds))
        pad.send_vibration(0, 0)
        self.ctx.log(f"回环测试完成（XInput 序号 {pad.xinput_index}，"
                     f"{pct:.0f}%；反馈经游戏原生震动通道回流）")
