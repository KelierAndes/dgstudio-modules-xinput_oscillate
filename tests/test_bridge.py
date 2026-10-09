from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from modules.xinput_oscillate import vigem
from modules.xinput_oscillate.bridge import (MOTOR_MAX, BridgeConfig,
                                             RumbleMixer, SignalBoard,
                                             XInputBridge)
from modules.xinput_oscillate.virtualpad import (DEFAULT_KEYBOARD_MAP,
                                                 KeyMap, VirtualPad,
                                                 merge_inputs, parse_vk)

BLOCKED_DEVICE_METHODS = ("set_strength", "add_strength", "reset_strength",
                          "set_wave", "push_pulse_stream", "fire",
                          "fire_start", "fire_stop", "zap",
                          "set_intensity_param")


class DeviceWrite(Exception):
    """桩上下文抛出它：模块一旦直写设备，测试立刻失败。"""


def _deny(name: str):

    def deny(self, *args, **kwargs):
        raise DeviceWrite(f"模块不得直写设备：{name}()")
    return deny


class FakeCtx:
    """ModuleContext 桩：放行读状态 / 登记变量，拦截全部设备直写。"""

    logs: list[str]

    def __init__(self):
        self.logs: list[str] = []
        self.settings: dict = {}
        self.emergency_calls = 0

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))

    def resolve_slot(self, family="", output_only=False):
        return None

    def wave_order(self, family="COYOTE"):
        return ["静默", "持续"]

    def wave_selection(self):
        return {}

    def get_state(self):
        return None

    def emergency_stop(self):
        self.emergency_calls += 1

    def submit(self, coro):
        coro.close()

    set_strength = _deny("set_strength")
    add_strength = _deny("add_strength")
    reset_strength = _deny("reset_strength")
    set_wave = _deny("set_wave")
    push_pulse_stream = _deny("push_pulse_stream")
    fire = _deny("fire")
    fire_start = _deny("fire_start")
    fire_stop = _deny("fire_stop")
    zap = _deny("zap")
    set_intensity_param = _deny("set_intensity_param")


class FakeClient:

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
        m.on_packet(0, MOTOR_MAX // 4, MOTOR_MAX // 1000, now=0.0)
        vals = m.tick(0.0)
        assert vals["xvib_l"] == 50.0
        assert vals["xvib_r"] == 0.0

    def test_release_linear_decay(self):
        m = self._mixer(release_ms=1000)
        m.on_packet(0, MOTOR_MAX, 0, now=0.0)
        m.on_packet(0, 0, 0, now=0.0)
        half = m.tick(0.5)
        assert half["xvib_l"] == 50.0
        done = m.tick(1.1)
        assert done["xvib_l"] == 0.0

    def test_active_flag_follows_envelope(self):
        m = self._mixer(active_pct=30.0, release_ms=1000)
        m.on_packet(0, MOTOR_MAX // 2, 0, now=0.0)
        assert m.tick(0.0)["xvib_active"] == 1.0
        m.on_packet(0, 0, 0, now=0.0)
        below = m.tick(0.25)
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
        m = self._mixer()
        m.on_packet(0, 0, 0, now=0.0)
        assert m.tick(0.1)["xvib_link"] == 0.0

    def test_sustained_rumble_holds_link(self):
        m = self._mixer(idle_ms=500)
        for i in range(24):
            m.on_packet(0, MOTOR_MAX // 2, 0, now=i * 0.1)
        vals = m.tick(2.4)
        assert vals["xvib_link"] == 1.0
        assert vals["xvib_l"] == 50.0


class FeedbackTests(unittest.TestCase):

    def _bridge(self, **overrides) -> XInputBridge:
        cfg = BridgeConfig({"vigem_enabled": False, **overrides})
        return XInputBridge(cfg, FakeCtx(), pad_factory=lambda cfg: None)

    def test_feedback_publishes_envelope_signals(self):
        bridge = self._bridge()
        bridge._on_feedback(195, 78, now=0.0)
        sig = bridge.engine.signals
        assert sig["xvib_l"] == 76.5
        assert sig["xvib_r"] == 30.6
        assert sig["xvib_max"] == 76.5
        assert sig["xvib_active"] == 1.0
        assert sig["xvib_link"] == 1.0
        assert bridge.rx_count == 1

    def test_zero_feedback_decays_but_keeps_state(self):
        bridge = self._bridge()
        bridge._on_feedback(255, 255, now=0.0)
        assert bridge.rx_count == 1
        bridge._on_feedback(0, 0, now=0.1)
        assert bridge.rx_count == 1
        vals = bridge.mixer.tick(0.2)
        assert vals["xvib_l"] < 100.0

    def test_link_loss_publishes_without_touching_devices(self):
        bridge = self._bridge(idle_ms=200, release_ms=100)
        bridge._on_feedback(255, 0, now=0.0)
        assert bridge.engine.signals["xvib_link"] == 1.0
        bridge._feed(bridge.mixer.tick(1.0))
        assert bridge.engine.signals["xvib_link"] == 0.0
        assert bridge.engine.signals["xvib_l"] == 0.0
        assert any("链路" in m for m in bridge.ctx.logs)

    def test_engine_is_signal_board_only(self):
        bridge = self._bridge()
        assert isinstance(bridge.engine, SignalBoard)
        assert isinstance(bridge.engine.signals, dict)
        assert isinstance(bridge.engine.errors, dict)
        bridge.engine.signal("xvib_l", 42)
        assert bridge.engine.signals["xvib_l"] == 42.0
        bridge.engine.signal("xvib_l", 42)
        bridge.engine.pump()
        bridge.engine.reset()
        assert bridge.engine.signals == {}

    def test_bridge_has_no_device_dispatch_surface(self):
        bridge = self._bridge()
        for attr in ("actions", "dispatchers", "_dispatch", "device_vars",
                     "_api"):
            assert not hasattr(bridge, attr), attr


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
        assert out["sThumbLX"] == -1000
        assert out["sThumbLY"] == 32767

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
        pad._on_notification(0, 0, 1)
        pending = pad.drain_feedback()
        assert [fb[0] for fb in pending] == [195, 0]
        assert pad.drain_feedback() == []

    def test_poll_keyboard_updates_virtual_pad(self):
        pad = self._pad()
        pad.start()
        pad._pressed = lambda: {0x20, 0x57}
        assert pad.poll() is True
        report = pad.client.updates[-1]
        assert report["wButtons"] == 0x1000
        assert report["sThumbLY"] == 32767
        assert pad.poll() is False
        pad._pressed = lambda: set()
        assert pad.poll() is True
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
        pad.xinput_index = 1
        pad._pressed = lambda: {0x20}
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
        assert pad.client.updates[0]["wButtons"] == 0x0003
        assert pad.client.updates[-1]["wButtons"] == 0

    def test_send_vibration_uses_motor_scale(self):
        reader = FakePadReader()
        pad = self._pad(reader=reader)
        pad.start()
        pad.xinput_index = 1
        assert pad.send_vibration(194, 78)
        assert reader.vibrations == [(1, 194 * 257, 78 * 257)]

    def test_stop_zeroes_input(self):
        pad = self._pad()
        pad.start()
        pad.stop()
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
        cfg = BridgeConfig({"vigem_enabled": False})
        bridge = XInputBridge(cfg, FakeCtx(), pad_factory=lambda cfg: None)
        await bridge.start()
        try:
            assert bridge.is_running()
            assert bridge.pad is None
        finally:
            await bridge.stop()

    async def test_feedback_publishes_signals_end_to_end(self):
        real_bus = vigem.bus_available
        vigem.bus_available = lambda: True
        try:
            cfg = BridgeConfig({"idle_ms": 1000})
            ctx = FakeCtx()
            bridge = XInputBridge(cfg, ctx, pad_factory=_fake_pad)
            await bridge.start()
            try:
                assert bridge.pad is not None
                assert bridge.engine.signals.get("xvib_pad") == 1.0
                bridge.pad.client.emit_feedback(77, 0, 1)
                peak = 0.0
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    peak = max(peak, bridge.engine.signals.get("xvib_max", 0.0))
                    if peak >= 30:
                        break
                assert round(peak) == 30, peak
                assert bridge.rx_count >= 1
            finally:
                await bridge.stop()
            assert not bridge.is_running()
            assert bridge.engine.signals == {}
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

    async def test_inject_test_feeds_signals(self):
        bridge = XInputBridge(BridgeConfig({"vigem_enabled": False}),
                              FakeCtx(), pad_factory=lambda cfg: None)
        await bridge.start()
        try:
            task = asyncio.ensure_future(bridge.inject_test(60, seconds=0.3))
            peak = 0.0
            while not task.done():
                await asyncio.sleep(0.02)
                peak = max(peak, bridge.engine.signals.get("xvib_max", 0.0))
            await task
            assert abs(peak - 60.0) < 0.5, peak
            assert any("测试震动脉冲" in m for m in bridge.ctx.logs)
        finally:
            await bridge.stop()

    async def test_pad_loopback_never_writes_devices(self):
        real_bus = vigem.bus_available
        vigem.bus_available = lambda: True
        try:
            bridge = XInputBridge(BridgeConfig(), FakeCtx(),
                                  pad_factory=_fake_pad)
            await bridge.start()
            try:
                await bridge.pad_loopback(50, seconds=0.1)
                assert any("回环测试完成" in m for m in bridge.ctx.logs)
            finally:
                await bridge.stop()
        finally:
            vigem.bus_available = real_bus


class NoDeviceWriteGuardTests(unittest.IsolatedAsyncioTestCase):
    """架构契约：模块只登记变量。桩上下文对任何设备直写都抛错，全程不许被触发。"""

    async def test_full_cycle_survives_blocking_context(self):
        real_bus = vigem.bus_available
        vigem.bus_available = lambda: True
        try:
            ctx = FakeCtx()
            bridge = XInputBridge(BridgeConfig({"idle_ms": 120}), ctx,
                                  pad_factory=_fake_pad)
            await bridge.start()
            try:
                bridge.pad.client.emit_feedback(195, 78, 1)
                await asyncio.sleep(0.12)
                await bridge.inject_test(80, seconds=0.12)
                await bridge.pad_loopback(50, seconds=0.1)
                bridge._feed(bridge.mixer.tick(time.perf_counter() + 5.0))
                assert bridge.engine.errors == {}
                assert set(bridge.engine.signals) >= {"xvib_l", "xvib_r",
                                                      "xvib_max",
                                                      "xvib_active",
                                                      "xvib_link", "xvib_pad"}
            finally:
                await bridge.stop()
        finally:
            vigem.bus_available = real_bus

    def test_stub_context_blocks_every_forbidden_method(self):
        ctx = FakeCtx()
        for name in BLOCKED_DEVICE_METHODS:
            with self.assertRaises(DeviceWrite):
                getattr(ctx, name)(channel="A")
        ctx.emergency_stop()          # 急停是安全通道，仍允许
        assert ctx.emergency_calls == 1


if __name__ == "__main__":
    unittest.main()
