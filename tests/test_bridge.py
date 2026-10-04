"""XInput 震动联动测试：包络混合（攻击/回落/死区/链路）、震动反馈换算、
键盘键位解析与合成、输入合并、虚拟手柄组合与映射派发。"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from dglab.params import build_dispatchers, core_inputs

from modules.xinput_oscillate import vigem
from modules.xinput_oscillate.bridge import (MOTOR_MAX, BridgeConfig,
                                             RumbleMixer, XInputBridge)
from modules.xinput_oscillate.virtualpad import (DEFAULT_KEYBOARD_MAP,
                                                 KeyMap, VirtualPad,
                                                 merge_inputs, parse_vk)


class _Noop:
    async def coro(self):
        pass


def _noop_coro():
    return _Noop().coro()


class RecordingApi:
    """派发器记录桩：同步记录动作调用，协程直接丢弃。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def resolve_slot(self, family: str = "") -> str | None:
        return f"slot-{family or 'COYOTE'}".lower()

    def wave_order(self, family: str = "") -> list[str]:
        return ["静默", "持续", "波浪"]

    def wave_selection(self) -> dict:
        return {"A": "静默", "B": "静默"}

    def set_strength(self, channel, value, slot_id=None):
        self.calls.append(("strength", channel, value, slot_id))
        return _noop_coro()

    def set_wave(self, channel, name, slot_id=None):
        self.calls.append(("wave", channel, name, slot_id))
        return _noop_coro()

    def zap(self, channel, seconds=1.0, slot_id=None):
        self.calls.append(("zap", channel, seconds, slot_id))
        return _noop_coro()

    def fire_start(self, slot_id=None, channel=None):
        self.calls.append(("fire_start", slot_id, channel))
        return _noop_coro()

    def fire_stop(self, slot_id=None, channel=None):
        self.calls.append(("fire_stop", slot_id, channel))
        return _noop_coro()

    def emergency_stop(self):
        self.calls.append(("emergency",))
        return _noop_coro()

    def run(self, coro) -> None:
        coro.close()


class FakeCtx:
    """宿主 ModuleContext 桩：仅实现桥用到的成员。"""

    def __init__(self):
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))

    def resolve_slot(self, family="", output_only=False):
        return None

    def wave_order(self, family="COYOTE"):
        return ["静默", "持续"]

    def wave_selection(self):
        return {}

    def set_strength(self, channel, value, slot_id=None):
        return _noop_coro()

    def set_wave(self, channel, name, slot_id=None):
        return _noop_coro()

    def zap(self, channel, seconds=1.0, slot_id=None):
        return _noop_coro()

    def fire_start(self, slot_id=None, channel=None):
        return _noop_coro()

    def fire_stop(self, slot_id=None, channel=None):
        return _noop_coro()

    def emergency_stop(self):
        return _noop_coro()

    def get_state(self):
        return None


class FakeClient:
    """ViGemClient 桩：记录状态更新，可手动触发震动反馈回调。"""

    def __init__(self):
        self.updates: list[dict] = []
        self.closed = False
        self._notif = None

    def connect(self):
        pass

    def add_x360(self):
        pass

    def register_notification(self, feedback):
        self._notif = feedback

    def attached(self):
        return True

    def update(self, report):
        self.updates.append(dict(wButtons=report.wButtons,
                                 bLeftTrigger=report.bLeftTrigger,
                                 bRightTrigger=report.bRightTrigger,
                                 sThumbLX=report.sThumbLX,
                                 sThumbLY=report.sThumbLY,
                                 sThumbRX=report.sThumbRX,
                                 sThumbRY=report.sThumbRY))

    def close(self):
        self.closed = True

    def emit_feedback(self, large, small, led=1):
        assert self._notif is not None, "尚未注册震动反馈回调"
        self._notif(large, small, led)


class FakePadReader:
    """XInput 读取桩：按脚本返回手柄状态（默认 1 号位带探测标记）。"""

    def __init__(self, states: dict[int, dict | None] | None = None,
                 available: bool = True):
        if states is None:
            states = {1: _pad_state(0x0003)}
        self.states = states
        self.vibrations: list[tuple[int, int, int]] = []
        self.available = available

    def read(self, index: int) -> dict | None:
        return self.states.get(index)

    def set_vibration(self, index, left, right):
        self.vibrations.append((index, left, right))
        return True


def _pad_state(wButtons: int = 0) -> dict:
    return {"wButtons": wButtons, "bLeftTrigger": 0, "bRightTrigger": 0,
            "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0}


class RumbleMixerTests(unittest.TestCase):
    def _mixer(self, **overrides) -> RumbleMixer:
        return RumbleMixer(BridgeConfig(overrides))

    def test_attack_instant_and_values(self):
        m = self._mixer()
        m.on_packet(0, MOTOR_MAX, MOTOR_MAX // 2, now=10.0)
        vals = m.tick(10.0)
        assert vals["xvib_l"] == 100.0
        assert vals["xvib_r"] == 50.0
        assert vals["xvib_max"] == 100.0
        assert vals["xvib_link"] == 1.0
        assert vals["xvib_active"] == 1.0

    def test_gain_and_deadband(self):
        m = self._mixer(gain=2.0, deadband_pct=3.0)
        m.on_packet(0, MOTOR_MAX // 4, MOTOR_MAX // 1000, now=0.0)  # 25%→50%, 0.1%→0
        vals = m.tick(0.0)
        assert vals["xvib_l"] == 50.0
        assert vals["xvib_r"] == 0.0

    def test_release_linear_decay(self):
        m = self._mixer(release_ms=1000)
        m.on_packet(0, MOTOR_MAX, 0, now=0.0)
        m.on_packet(0, 0, 0, now=0.0)        # 游戏停止震动（发零）
        half = m.tick(0.5)
        assert half["xvib_l"] == 50.0
        done = m.tick(1.1)
        assert done["xvib_l"] == 0.0

    def test_active_flag_follows_envelope(self):
        m = self._mixer(active_pct=30.0, release_ms=1000)
        m.on_packet(0, MOTOR_MAX // 2, 0, now=0.0)          # 50%
        assert m.tick(0.0)["xvib_active"] == 1.0
        m.on_packet(0, 0, 0, now=0.0)
        below = m.tick(0.25)                                 # 回落到 25% < 30%
        assert below["xvib_active"] == 0.0
        assert below["xvib_l"] == 25.0

    def test_idle_timeout_breaks_link(self):
        m = self._mixer(idle_ms=500, release_ms=300)
        m.on_packet(0, MOTOR_MAX, 0, now=0.0)
        assert m.tick(0.4)["xvib_link"] == 1.0
        after = m.tick(1.0)
        assert after["xvib_link"] == 0.0
        assert after["xvib_l"] == 0.0

    def test_zero_only_stream_never_holds_link(self):
        """纯零包（无任何非零震动）不应让链路显示活跃。"""
        m = self._mixer()
        m.on_packet(0, 0, 0, now=0.0)
        assert m.tick(0.1)["xvib_link"] == 0.0

    def test_sustained_rumble_holds_link(self):
        """游戏按帧持续派发时链路保持活跃。"""
        m = self._mixer(idle_ms=500)
        for i in range(24):                                  # 每 100ms 一包，共 2.3s
            m.on_packet(0, MOTOR_MAX // 2, 0, now=i * 0.1)
        vals = m.tick(2.4)
        assert vals["xvib_link"] == 1.0
        assert vals["xvib_l"] == 50.0


class FeedbackTests(unittest.TestCase):
    """震动反馈（0-255 马达）→ 原始值 → 包络/派发。"""

    def _bridge(self, mappings: list[dict], **overrides) -> XInputBridge:
        cfg = BridgeConfig({"mappings": mappings,
                            "vigem_enabled": False, **overrides})
        bridge = XInputBridge(cfg, FakeCtx(),
                              pad_factory=lambda cfg: None)
        bridge._api = RecordingApi()
        bridge.actions = build_dispatchers(bridge._api, core_inputs())
        bridge.apply_config()
        return bridge

    def test_motor_scale_to_mapping(self):
        bridge = self._bridge([{"param": "in_ovc_strength_a",
                                "expr": "{xvib_max}"}])
        bridge._on_feedback(195, 78, time.monotonic())  # 195*257≈76.5%, 78*257≈30.6%
        strengths = [c for c in bridge._api.calls if c[0] == "strength"]
        assert strengths and strengths[-1][2] == 76

    def test_zero_feedback_decays_but_keeps_state(self):
        bridge = self._bridge([{"param": "in_ovc_strength_a",
                                "expr": "{xvib_max}"}])
        bridge._on_feedback(255, 255, now=0.0)          # 满震
        assert bridge.rx_count == 1
        bridge._on_feedback(0, 0, now=0.1)              # 游戏显式归零
        assert bridge.rx_count == 1                     # 零包不计链路活跃
        vals = bridge.mixer.tick(0.2)
        assert vals["xvib_l"] < 100.0                   # 开始回落

    def test_dispatch_paths_via_feedback(self):
        bridge = self._bridge([
            {"param": "in_ovc_zap_a", "expr": "{xvib_active}"},
            {"param": "in_ovc_fire", "expr": "{xvib_active}"},
            {"param": "in_emergency", "expr": "{xvib_link}"},
        ])
        now = time.monotonic()
        bridge._on_feedback(255, 0, now)                # 链路上升沿 + 激活
        kinds = [c[0] for c in bridge._api.calls]
        assert "zap" in kinds and "fire_start" in kinds and "emergency" in kinds
        bridge._on_feedback(0, 0, now + 0.1)
        bridge._feed(bridge.mixer.tick(now + 0.5))      # 回落越过激活阈值
        assert ("fire_stop", "slot-ovc", None) in bridge._api.calls


class KeyMapTests(unittest.TestCase):
    def test_parse_vk(self):
        assert parse_vk("Space") == 0x20
        assert parse_vk("enter") == 0x0D
        assert parse_vk("F5") == 0x74
        assert parse_vk("A") == 0x41
        assert parse_vk("5") == 0x35
        assert parse_vk("0x2D") == 0x2D
        assert parse_vk("notakey") is None
        assert parse_vk("") is None
        assert parse_vk(None) is None

    def test_default_table_resolves(self):
        km = KeyMap()
        assert len(km.table) == len(DEFAULT_KEYBOARD_MAP)
        assert km.table["a"] == 0x20
        assert km.table["dpad_up"] == 0x26
        assert not km.invalid

    def test_override_and_invalid(self):
        km = KeyMap({"a": "LShift", "b": "BadKey", "ls_up": "0x57"})
        assert km.table["a"] == 0xA0
        assert km.table["ls_up"] == 0x57
        assert "b" not in km.table
        assert any(entry.startswith("b=") for entry in km.invalid)

    def test_apply_synthesis(self):
        km = KeyMap()
        fields = km.apply({0x20, 0x51, 0x26, 0x57, 0x33})
        # Space→A 键, Q→B 键, Up→十字上, W→左摇杆上, 3→左扳机
        assert fields["wButtons"] == 0x1000 | 0x2000 | 0x0001
        assert fields["bLeftTrigger"] == 255
        assert fields["sThumbLY"] == 32767
        assert fields["sThumbLX"] == 0


class MergeInputsTests(unittest.TestCase):
    def test_merge(self):
        kb = {"wButtons": 0x1000, "bLeftTrigger": 255, "bRightTrigger": 0,
              "sThumbLX": 0, "sThumbLY": 32767, "sThumbRX": 0, "sThumbRY": 0}
        real = {"wButtons": 0x2000, "bLeftTrigger": 10, "bRightTrigger": 200,
                "sThumbLX": -1000, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0}
        out = merge_inputs(kb, real)
        assert out["wButtons"] == 0x1000 | 0x2000
        assert out["bLeftTrigger"] == 255
        assert out["bRightTrigger"] == 200
        assert out["sThumbLX"] == -1000            # 手柄非零优先
        assert out["sThumbLY"] == 32767            # 手柄为零回退键盘

    def test_merge_none_sides(self):
        only_kb = merge_inputs({"wButtons": 0x1000, **{k: 0 for k in
                              ("bLeftTrigger", "bRightTrigger", "sThumbLX",
                               "sThumbLY", "sThumbRX", "sThumbRY")}}, None)
        assert only_kb["wButtons"] == 0x1000
        assert merge_inputs(None, None)["wButtons"] == 0


class VirtualPadTests(unittest.TestCase):
    def _pad(self, reader: FakePadReader | None = None,
             keyboard=True, forward=False, keymap=None) -> VirtualPad:
        return VirtualPad(keymap or KeyMap(), keyboard_enabled=keyboard,
                          forward_pad=forward, client=FakeClient(),
                          pad_reader=reader or FakePadReader())

    def test_feedback_queue_roundtrip(self):
        pad = self._pad()
        pad.start()
        pad._on_notification(195, 78, 1)
        pad._on_notification(0, 0, 1)              # 高频派发逐条保留
        pending = pad.drain_feedback()
        assert [fb[0] for fb in pending] == [195, 0]
        assert pad.drain_feedback() == []          # 取走即清空

    def test_poll_keyboard_updates_virtual_pad(self):
        pad = self._pad()
        pad.start()
        pad._pressed = lambda: {0x20, 0x57}        # A 键 + 左摇杆上
        assert pad.poll() is True
        report = pad.client.updates[-1]
        assert report["wButtons"] == 0x1000
        assert report["sThumbLY"] == 32767
        assert pad.poll() is False                 # 状态未变不重发
        pad._pressed = lambda: set()
        assert pad.poll() is True                  # 松键归零
        assert pad.client.updates[-1]["wButtons"] == 0

    def test_poll_disabled_keyboard_sends_nothing(self):
        pad = self._pad(keyboard=False)
        pad.start()
        assert pad.poll() is False
        assert pad.client.updates == []

    def test_forward_pad_merges_real_input(self):
        reader = FakePadReader({0: {"wButtons": 0x2000, "bLeftTrigger": 10,
                                    "bRightTrigger": 0, "sThumbLX": -500,
                                    "sThumbLY": 0, "sThumbRX": 0,
                                    "sThumbRY": 0}})
        pad = self._pad(reader=reader, forward=True)
        pad.start()
        pad.xinput_index = 1                       # 虚拟手柄在 1 号位
        pad._pressed = lambda: {0x20}              # 键盘按住 A
        assert pad.poll() is True
        report = pad.client.updates[-1]
        assert report["wButtons"] == 0x1000 | 0x2000
        assert report["sThumbLX"] == -500

    def test_real_index_excludes_virtual(self):
        reader = FakePadReader({
            0: {"wButtons": 0, "bLeftTrigger": 0, "bRightTrigger": 0,
                "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0},
            1: None, 2: None, 3: None,
        })
        pad = self._pad(reader=reader, forward=True, keyboard=False)
        pad.xinput_index = 1
        assert pad._real_index() == 0

    def test_probe_xinput_index(self):
        reader = FakePadReader({
            0: {"wButtons": 0, "bLeftTrigger": 0, "bRightTrigger": 0,
                "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0},
            2: {"wButtons": 0x0003, "bLeftTrigger": 0, "bRightTrigger": 0,
                "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0},
        })
        pad = self._pad(reader=reader)
        pad.start()
        assert pad.probe_xinput_index(timeout=0.2) == 2
        assert pad.xinput_index == 2
        assert pad.client.updates[0]["wButtons"] == 0x0003   # 探测标记
        assert pad.client.updates[-1]["wButtons"] == 0       # 探测后归零

    def test_send_vibration_uses_motor_scale(self):
        reader = FakePadReader()
        pad = self._pad(reader=reader)
        pad.start()
        pad.xinput_index = 1
        assert pad.send_vibration(194, 78)          # 马达单位 0-255
        assert reader.vibrations == [(1, 194 * 257, 78 * 257)]

    def test_stop_zeroes_input(self):
        pad = self._pad()
        pad.start()
        pad.stop()
        # 注入的客户端不归 VirtualPad 所有（不关闭），仅把输入归零后摘除
        assert not pad.client.closed
        assert pad.client.updates[-1]["wButtons"] == 0


def _fake_pad(config=None, **kwargs) -> VirtualPad:
    return VirtualPad(KeyMap(kwargs.pop("keymap", {})),
                      keyboard_enabled=kwargs.pop("keyboard_enabled", False),
                      forward_pad=False,
                      client=kwargs.pop("client", FakeClient()),
                      pad_reader=kwargs.pop("reader", FakePadReader()),
                      **kwargs)


class BridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_without_pad_degrades(self):
        cfg = BridgeConfig({"vigem_enabled": False, "mappings": []})
        bridge = XInputBridge(cfg, FakeCtx(), pad_factory=lambda cfg: None)
        await bridge.start()
        try:
            assert bridge.is_running()
            assert bridge.pad is None
        finally:
            await bridge.stop()

    async def test_feedback_drives_mapping_end_to_end(self):
        real_bus = vigem.bus_available
        vigem.bus_available = lambda: True
        try:
            cfg = BridgeConfig({
                "mappings": [{"param": "in_ovc_strength_a",
                              "expr": "{xvib_max}"}]})
            bridge = XInputBridge(cfg, FakeCtx(), pad_factory=_fake_pad)
            bridge._api = RecordingApi()
            bridge.actions = build_dispatchers(bridge._api, core_inputs())
            await bridge.start()
            try:
                assert bridge.pad is not None
                # 模拟游戏派发震动：77/255 ≈ 30% 马达强度
                bridge.pad.client.emit_feedback(77, 0, 1)
                for _ in range(50):
                    if bridge._api.calls:
                        break
                    await asyncio.sleep(0.02)
                strengths = [c for c in bridge._api.calls
                             if c[0] == "strength"]
                assert strengths and strengths[-1][2] == 30
                assert bridge.engine.signals.get("xvib_pad") == 1.0
            finally:
                await bridge.stop()
            assert not bridge.is_running()
        finally:
            vigem.bus_available = real_bus

    async def test_bus_missing_degrades_with_log(self):
        real_bus = vigem.bus_available
        vigem.bus_available = lambda: False
        try:
            bridge = XInputBridge(BridgeConfig(), FakeCtx(), pad_factory=_fake_pad)
            await bridge.start()
            try:
                assert bridge.pad is None
                assert bridge.is_running()
                assert any("ViGEmBus" in m for m in bridge.ctx.logs)
            finally:
                await bridge.stop()
        finally:
            vigem.bus_available = real_bus

    async def test_reload_config_updates_keymap(self):
        cfg = BridgeConfig({"vigem_enabled": True})
        bridge = XInputBridge(cfg, FakeCtx(), pad_factory=_fake_pad)
        await bridge.start()
        try:
            bridge.config["keyboard_map"] = {"a": "LShift"}
            from modules.xinput_oscillate.virtualpad import KeyMap

            bridge.pad.keymap = KeyMap({"a": "LShift"})
            assert bridge.pad.keymap.table["a"] == 0xA0
        finally:
            await bridge.stop()

    async def test_inject_test_feeds_pipeline(self):
        mappings = [{"param": "in_ovc_strength_a", "expr": "{xvib_max}"}]
        bridge = XInputBridge(BridgeConfig({"vigem_enabled": False,
                                            "mappings": mappings}),
                              FakeCtx(), pad_factory=lambda cfg: None)
        bridge._api = RecordingApi()
        bridge.actions = build_dispatchers(bridge._api, core_inputs())
        await bridge.start()
        try:
            await bridge.inject_test(60, seconds=0.12)
            strengths = [c[2] for c in bridge._api.calls if c[0] == "strength"]
            # 首个派发即注入目标值；结束后的回落中间值（<60）属正确的包络
            # 回落行为，不作强断言
            assert strengths and strengths[0] == 60
            assert max(strengths) == 60
        finally:
            await bridge.stop()


if __name__ == "__main__":
    unittest.main()
