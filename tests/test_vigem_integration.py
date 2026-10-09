from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from modules.xinput_oscillate import vigem
from modules.xinput_oscillate.bridge import BridgeConfig, XInputBridge
from modules.xinput_oscillate.virtualpad import RealPadReader

from test_bridge import FakeCtx


def _real_env() -> bool:
    return bool(vigem.bus_available() and vigem.client_dll_path())


@unittest.skipUnless(_real_env(), "本机未安装 ViGEmBus 驱动或缺少客户端库")
class RealViGemIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_full_rumble_loop_publishes_signals(self):
        cfg = BridgeConfig({"vigem_enabled": True, "keyboard_enabled": False,
                            "forward_pad": False})
        bridge = XInputBridge(cfg, FakeCtx())
        await bridge.start()
        try:
            pad = bridge.pad
            assert pad is not None and pad.client.attached()
            index = pad.xinput_index
            assert index is not None, "虚拟手柄的 XInput 序号探测失败"

            # 模拟游戏：对虚拟手柄按 XInput 原生通道派发 194/255 ≈ 76% 左马达
            assert pad.send_vibration(194, 78)
            peak = 0.0
            for _ in range(100):
                await asyncio.sleep(0.02)
                peak = max(peak, bridge.engine.signals.get("xvib_max", 0.0))
                if peak >= 75.9:
                    break
            # 攻击峰值 ≈76.1（驱动偶发杂散脉冲与回落中间值不作顺序断言）
            assert 75.5 <= peak <= 76.6, peak
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
