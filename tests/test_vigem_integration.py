from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from dglab.params import build_dispatchers, core_inputs

from modules.xinput_oscillate import vigem
from modules.xinput_oscillate.bridge import BridgeConfig, XInputBridge
from modules.xinput_oscillate.virtualpad import RealPadReader

from test_bridge import RecordingApi, FakeCtx


def _real_env() -> bool:
    return bool(vigem.bus_available() and vigem.client_dll_path())


@unittest.skipUnless(_real_env(), "本机未安装 ViGEmBus 驱动或缺少客户端库")
class RealViGemIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_full_rumble_loop_via_native_channel(self):
        cfg = BridgeConfig({
            "vigem_enabled": True, "keyboard_enabled": False,
            "forward_pad": False,
            "mappings": [{"param": "in_ovc_strength_a", "expr": "{xvib_max}"}]})
        bridge = XInputBridge(cfg, FakeCtx())
        bridge._api = RecordingApi()
        bridge.actions = build_dispatchers(bridge._api, core_inputs())
        await bridge.start()
        try:
            pad = bridge.pad
            assert pad is not None and pad.client.attached()
            index = pad.xinput_index
            assert index is not None, "虚拟手柄的 XInput 序号探测失败"

            assert pad.send_vibration(194, 78)
            for _ in range(100):
                if any(c[0] == "strength" for c in bridge._api.calls):
                    break
                await asyncio.sleep(0.02)
            values = [c[2] for c in bridge._api.calls if c[0] == "strength"]
            assert values and 76 in values, bridge._api.calls
            assert max(values) == 76
            assert bridge.rx_count >= 1
        finally:
            await bridge.stop()
        assert bridge.pad is None

    async def test_real_pad_reader_finds_devices(self):
        reader = RealPadReader()
        assert reader.available
        for index in range(4):
            reader.read(index)


if __name__ == "__main__":
    unittest.main()
