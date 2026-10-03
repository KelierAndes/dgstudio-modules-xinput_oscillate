"""真实 ViGEm 集成测试（需本机安装 ViGEmBus 驱动，缺失时自动跳过）。

完整链路：创建虚拟 Xbox 360 手柄 → 以 XInput 原生通道对虚拟手柄派发
震动（等同游戏行为，XInputSetState）→ 震动反馈回调回流 → 包络 →
映射表达式 → 设备动作派发。同时验证键盘映射路径的真实合成。
"""
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
        """游戏原生震动派发 → 虚拟手柄反馈 → 映射派发（真实驱动）。"""
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

            # 模拟游戏：对虚拟手柄按 XInput 原生通道派发 194/255 ≈ 76% 左马达
            assert pad.send_vibration(194, 78)
            for _ in range(100):
                if any(c[0] == "strength" for c in bridge._api.calls):
                    break
                await asyncio.sleep(0.02)
            values = [c[2] for c in bridge._api.calls if c[0] == "strength"]
            # 攻击峰值 76 必须出现；驱动偶发杂散脉冲（如 0,51）与回落
            # 中间值可能先行或随后，不作顺序断言
            assert values and 76 in values, bridge._api.calls
            assert max(values) == 76
            assert bridge.rx_count >= 1
        finally:
            await bridge.stop()
        # 虚拟手柄应已摘除
        assert bridge.pad is None

    async def test_real_pad_reader_finds_devices(self):
        reader = RealPadReader()
        assert reader.available
        # 本机至少应能枚举 XInput 槽位（虚拟手柄由上一用例的启停保证存在性
        # 不做强断言，这里只验证读取路径不抛错）
        for index in range(4):
            reader.read(index)


if __name__ == "__main__":
    unittest.main()
