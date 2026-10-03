"""手柄震动联动核心：虚拟手柄震动反馈 → 包络混合 → 核心参数映射。

数据来源（无任何游戏注入，全部走游戏原生的震动派发通道）：

* **虚拟手柄反馈**：ViGEm 虚拟 Xbox 360 手柄接收游戏的
  ``XInputSetState`` 派发（见 :mod:`virtualpad`），左右马达 0-255
  回流为本模块数据源；
* **测试注入**：按键动作向链路注入合成震动（无游戏/手柄自测）；
* **回环测试**：对虚拟手柄按 XInput 原生通道派发一次震动，
  验证「游戏 → 手柄 → 联动」完整链路。

马达值经增益/死区/回落包络平滑后，以模块参数（``xvib_l`` / ``xvib_r`` /
``xvib_max`` / ``xvib_active`` / ``xvib_link`` / ``xvib_pad``）喂进共享
:class:`dglab.mapping.MappingEngine` 信号空间，联动页输入映射表把它们
组合后驱动设备。纯逻辑（包络）与桥（ViGEm、引擎）分离，便于单测。
"""

from __future__ import annotations

import asyncio
import copy
import time

from dglab.mapping import MappingEngine, signal_specs
from dglab.params import (build_dispatchers, core_alias_values, core_inputs,
                          device_state_values)

from modules.xinput_oscillate import vigem
from modules.xinput_oscillate.vigem import MOTOR_SCALE
from modules.xinput_oscillate.virtualpad import KeyMap, VirtualPad

__all__ = ["BridgeConfig", "RumbleMixer", "XInputBridge", "MOTOR_MAX",
           "PARAM_KEYS"]

# XINPUT_VIBRATION 马达满值
MOTOR_MAX = 65535

# 模块参数（包络输出；xvib_pad 由虚拟手柄就绪状态单独喂入）
PARAM_KEYS = ("xvib_l", "xvib_r", "xvib_max", "xvib_active", "xvib_link")

DEFAULTS = {
    "vigem_enabled": True,     # 创建虚拟手柄（游戏的原生震动派发通道）
    "keyboard_enabled": True,  # 键盘键位映射驱动虚拟手柄（无手柄调试）
    "forward_pad": False,      # 实体手柄输入透传到虚拟手柄
    "keyboard_map": {},        # 键位表（缺省见 virtualpad.DEFAULT_KEYBOARD_MAP）
    "gain": 1.0,               # 百分比增益（0.1-3）
    "deadband_pct": 2.0,       # 死区：低于该百分比视为 0
    "active_pct": 15.0,        # 激活阈值：包络峰值超过视为震动激活
    "release_ms": 300,         # 回落时间：目标归零后包络线性衰减时长
    "idle_ms": 1000,           # 链路超时：超时未收到反馈视为断开
    "refresh_s": 0.5,          # 设备状态变量参与表达式时的重算间隔
    "mappings": [],            # [{"param": 核心输入参数 id, "expr": 表达式}]
    "outputs": [],             # [{"param": 核心输出参数 id, "name": 字段名,
                               #   "expr": 表达式, "type": "Int"}]
}


def _num(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default     # NaN → default


class BridgeConfig(dict):
    """模块配置：缺省值优先取模块声明（defaults 参数），DEFAULTS 为兜底。"""

    def __init__(self, data: dict | None = None, defaults: dict | None = None):
        super().__init__(copy.deepcopy(defaults or DEFAULTS))
        if data:
            self.update({k: v for k, v in data.items() if v is not None})


class RumbleMixer:
    """纯逻辑包络混合器：最近的震动数据 → 平滑包络与状态参数。

    * 攻击即时：新目标高于包络时立刻抬升（跟手，不丢突发）；
    * 回落线性：目标归零后按 ``release_ms`` 线性衰减到 0（设备强度不跳变）；
    * 链路：仅**非零**震动数据维持 ``idle_ms`` 内的链路活跃——游戏显式
      归零或暂停派发不会把链路"续命"，超时即判定断开。

    配置键按 tick 实时读取（``gain`` / ``deadband_pct`` / ``active_pct`` /
    ``release_ms`` / ``idle_ms``），联动页改设置即生效。
    """

    def __init__(self, config: dict):
        self.config = config
        self._targets: dict[int, tuple[float, float]] = {}   # u -> (左%, 右%)
        self._last_rx: dict[int, float] = {}                 # u -> 上次非零收包
        self._focus: int = 0                                 # 最近活跃手柄
        self._env = [0.0, 0.0]                               # 左右包络 0-100
        self._last_tick: float | None = None

    # -------------------------------------------------------------- 数据面
    def on_packet(self, u: int, left: float, right: float, now: float) -> None:
        """记录一包震动数据（马达原始值 0-65535），即时抬升包络。"""
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

    # -------------------------------------------------------------- 时间面
    def tick(self, now: float) -> dict[str, float]:
        """推进包络（按时间衰减）并输出参数快照。"""
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
    """按配置构造虚拟手柄；前置条件不满足返回 None（原因由调用方记录）。"""
    if not config.get("vigem_enabled", True):
        return None
    keymap = KeyMap(config.get("keyboard_map") or {})
    return VirtualPad(
        keymap,
        keyboard_enabled=bool(config.get("keyboard_enabled", True)),
        forward_pad=bool(config.get("forward_pad", False)))


class XInputBridge:
    """虚拟手柄震动反馈 + 包络混合 + 共享映射引擎。"""

    def __init__(self, config: BridgeConfig, ctx, pad_factory=None):
        self.config = config
        self.ctx = ctx
        self.mixer = RumbleMixer(config)
        self.engine = MappingEngine(self._dispatch,
                                    device_vars=self.device_vars)
        self.engine.set_ranges(signal_specs())
        self._api = self._DeviceApi(self)
        self.actions = build_dispatchers(self._api, core_inputs())
        self._pad_factory = pad_factory or _default_pad_factory

        self.pad: VirtualPad | None = None
        self._tasks: set[asyncio.Task] = set()
        self._tick_task: asyncio.Task | None = None
        self._inputs_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._running = False
        self._primed = False
        self._last_fed: dict[str, float] = {}
        self._link_seen: bool | None = None      # 链路状态日志去重
        self.rx_count = 0
        self.last_rx: float | None = None

    class _DeviceApi:
        """把宿主 ModuleContext 适配成核心参数派发器需要的接口。"""

        def __init__(self, bridge: "XInputBridge"):
            self._bridge = bridge

        @property
        def _ctx(self):
            return self._bridge.ctx

        def resolve_slot(self, family: str = "") -> str | None:
            return self._ctx.resolve_slot(
                family=str(family or "COYOTE").upper(), output_only=True)

        def wave_order(self, family: str = "") -> list[str]:
            return self._ctx.wave_order(str(family or "COYOTE").upper())

        def wave_selection(self) -> dict:
            return self._ctx.wave_selection() or {}

        def set_strength(self, channel, value, slot_id=None):
            return self._ctx.set_strength(channel, value, slot_id=slot_id)

        def set_wave(self, channel, name, slot_id=None):
            return self._ctx.set_wave(channel, name, slot_id=slot_id)

        def zap(self, channel, seconds=1.0, slot_id=None):
            return self._ctx.zap(channel, seconds, slot_id=slot_id)

        def fire_start(self, slot_id=None):
            return self._ctx.fire_start(slot_id=slot_id)

        def fire_stop(self, slot_id=None):
            return self._ctx.fire_stop(slot_id=slot_id)

        def emergency_stop(self):
            return self._ctx.emergency_stop()

        def run(self, coro) -> None:
            self._bridge._spawn(coro)

    # ---- 生命周期 -------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.apply_config()
        await self._start_pad()
        self._tick_task = asyncio.ensure_future(self._tick_loop())
        self._inputs_task = asyncio.ensure_future(self._inputs_loop())
        self._pump_task = asyncio.ensure_future(self._pump_loop())
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
        for task in (self._tick_task, self._inputs_task, self._pump_task):
            if task is not None:
                task.cancel()
        self._tick_task = self._inputs_task = self._pump_task = None
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

    def apply_config(self) -> None:
        """装载两张映射表（首轮静默求值，避免启动即把设备写成 0）。"""
        first = not self._primed
        if first:
            self.engine.armed = False
        self.engine.set_mappings(self.config.get("mappings") or [])
        self.engine.set_outputs(self.config.get("outputs") or [])
        if first:
            self.engine.armed = True
            self._primed = True

    # ---- 震动反馈（ViGEm 回调线程 → 引擎循环） ----------------------------
    def _consume_feedback(self) -> None:
        """取走虚拟手柄暂存的全部反馈（按到达序）并逐条送入包络。

        不按时间戳去重：队列已保序，且 Windows 的 time.monotonic 精度
        只有约 15.6ms，同刻度内的两条真实反馈会被误判为重复而丢失。
        """
        pad = self.pad
        if pad is None:
            return
        for large, small, ts in pad.drain_feedback():
            self._on_feedback(large, small, ts)

    def _on_feedback(self, large: int, small: int, now: float) -> None:
        """一包震动反馈（马达 0-255）→ 原始值 → 包络。"""
        left = float(large) * MOTOR_SCALE
        right = float(small) * MOTOR_SCALE
        if left > 0 or right > 0:
            self.rx_count += 1
            self.last_rx = now
        self.mixer.on_packet(0, left, right, now)
        self._feed(self.mixer.tick(now))        # 攻击即时生效，不等 tick

    def _feed(self, values: dict[str, float]) -> None:
        """参数变化才进引擎（engine.signal 内部同样去重，双保险）。"""
        for key, value in values.items():
            if self._last_fed.get(key) != value:
                self._last_fed[key] = value
                self.engine.signal(key, value)
        link = values.get("xvib_link")
        if link is not None and bool(link) != self._link_seen:
            self._link_seen = bool(link)
            self.ctx.log("游戏震动链路：" + ("有数据" if link else "已断开（超时）"))

    # ---- 循环 -----------------------------------------------------------
    async def _tick_loop(self) -> None:
        """40ms 心跳：取反馈、推进包络回落并喂参数（回落不依赖新反馈）。"""
        try:
            while self._running:
                await asyncio.sleep(0.04)
                self._consume_feedback()
                self._feed(self.mixer.tick(time.perf_counter()))
        except asyncio.CancelledError:
            pass

    async def _inputs_loop(self) -> None:
        """60Hz：键盘 / 实体手柄 → 虚拟手柄输入合成。"""
        try:
            while self._running:
                await asyncio.sleep(0.016)
                if self.pad is not None:
                    self.pad.poll()
        except asyncio.CancelledError:
            pass

    async def _pump_loop(self) -> None:
        """设备状态变量随时间变化：定期重算两张映射表。"""
        try:
            while self._running:
                interval = max(0.05, _num(self.config.get("refresh_s"), 0.5))
                await asyncio.sleep(interval)
                self.engine.pump()
        except asyncio.CancelledError:
            pass

    # ---- 测试注入 ---------------------------------------------------------
    async def inject_test(self, pct: float, seconds: float = 0.8) -> None:
        """注入一串合成震动数据（联动链路自测，无需游戏与手柄）。"""
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
        """回环测试：对虚拟手柄派发真实震动（走游戏原生通道 → 反馈 → 映射）。"""
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

    # ---- 值空间 / 派发 ----------------------------------------------------
    def device_vars(self) -> dict[str, float]:
        """核心输出参数实时值（家族.信号）+ 全家族短名别名。"""
        try:
            state = self.ctx.get_state()
        except Exception:
            return {}
        vals = device_state_values(state)
        vals.update(core_alias_values(vals))
        return vals

    def _dispatch(self, target: str, value: int) -> None:
        action = self.actions.get(target)
        if action is None:
            return
        try:
            action(value)
        except Exception as exc:
            self.ctx.log(f"映射派发 {target}={value} 失败: {exc!r}")

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:      # 无运行中的事件循环（测试/停机）
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
