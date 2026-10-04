"""插件层测试：META 字面量、配置声明一致性、模块类协议与按键动作。"""
from __future__ import annotations

import ast
import os
import sys
import unittest

from unittest import IsolatedAsyncioTestCase


def _noop_coro():
    """占位协程（桩方法返回值，调用方可能 close 它）。"""
    async def _noop():
        pass
    return _noop()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from plugins import ModuleBase, spec_defaults

from modules.xinput_oscillate import plugin as plugin_mod
from modules.xinput_oscillate.bridge import DEFAULTS, PARAM_KEYS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PY = os.path.join(HERE, "modules", "xinput_oscillate", "plugin.py")


class MetaLiteralTests(unittest.TestCase):
    def test_meta_is_pure_literal(self):
        """宿主以 AST 不执行代码读取 META——必须是纯字面量字典。"""
        with open(PLUGIN_PY, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        metas = [node for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(getattr(t, "id", None) == "META"
                         for t in node.targets)]
        assert len(metas) == 1
        value = ast.literal_eval(metas[0].value)
        assert value["id"] == "xinput_oscillate"
        assert value["settings_key"] == "xinput"
        assert isinstance(value["config"], dict)
        assert isinstance(value["params"], dict)
        # 架构契约：不携带游戏模组、不做游戏注入，数据仅来自
        # 虚拟手柄的震动反馈（游戏原生震动派发通道）
        assert "mods" not in value
        assert not any(k.startswith("host") or k.startswith("port")
                       for k in value["config"])
        # 纯输入设计：无输出映射表、无可读参数声明
        assert "outputs" not in value["config"]
        assert "reads" not in value

    def test_config_decl_matches_bridge_defaults(self):
        """META["config"] 与 bridge.DEFAULTS 键集合一致、数值类型一致。"""
        declared = spec_defaults(plugin_mod.META["config"])
        assert set(declared) == set(DEFAULTS), (set(declared) ^ set(DEFAULTS))
        for key, default in DEFAULTS.items():
            got = declared[key]
            assert type(got) is type(default), (key, got, default)

    def test_params_decl_covers_envelope_keys(self):
        declared = set(plugin_mod.META["params"])
        assert set(PARAM_KEYS) <= declared


class ModuleClassTests(unittest.TestCase):
    def test_class_protocol(self):
        module = plugin_mod.XInputOscillateModule()
        assert isinstance(module, ModuleBase)
        assert module.id == "xinput_oscillate"
        assert module.config_spec() is plugin_mod.META["config"]
        names = [name for name, _ in module.link_params()]
        assert set(PARAM_KEYS) <= set(names)
        assert "xvib_pad" in names

    def test_button_actions_declared(self):
        actions = plugin_mod.XInputOscillateModule().button_actions()
        keys = [a.key for a in actions]
        assert keys == plugin_mod.META["actions"]
        assert all(a.on_press is not None for a in actions)

    def test_pct_argument_parsing(self):
        assert plugin_mod._pct_of(None) == 60.0
        assert plugin_mod._pct_of("35") == 35.0
        assert plugin_mod._pct_of("999") == 100.0
        assert plugin_mod._pct_of("abc") == 60.0

    def test_module_press_without_bridge_logs(self):
        module = plugin_mod.XInputOscillateModule()
        module.ctx = _LogCtx()
        module.on_load(module.ctx)
        module.on_unload()
        module.ctx = _LogCtx()
        module._inject_press(None, "50")
        module._loopback_press(None, "")
        assert any("未运行" in m for m in module.ctx.logs)


class _LogCtx:
    def __init__(self):
        self.logs: list[str] = []
        self.settings: dict = {}

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))

    def submit(self, coro):
        coro.close()
        return None


class ModuleLifecycleTests(IsolatedAsyncioTestCase):
    """模块级装配冒烟：start → reload_config → stop（宿主 ModuleContext 桩）。"""

    async def test_start_reload_stop(self):
        from modules.xinput_oscillate.plugin import _CONFIG_DEFAULTS

        class Ctx:
            def __init__(self):
                self.logs: list[str] = []
                self.settings = dict(_CONFIG_DEFAULTS)
                # 密闭测试：禁用虚拟手柄（不依赖 ViGEmBus / 不产生系统副作用）
                self.settings["vigem_enabled"] = False

            def log(self, msg):
                self.logs.append(str(msg))

            def resolve_slot(self, slot_id=None, family=None, output_only=False):
                return None

            def wave_order(self, family="COYOTE"):
                return ["静默", "持续"]

            def wave_selection(self):
                return {}

            def get_state(self):
                return None

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

            def submit(self, coro):
                coro.close()

        module = plugin_mod.XInputOscillateModule()
        module.ctx = Ctx()
        module.on_load(module.ctx)
        try:
            await module.start()
            assert module.is_running()
            assert module.bridge is not None
            # 热更新映射表：不重启桥
            module.ctx.settings["mappings"] = [
                {"param": "in_ovc_strength_a", "expr": "{xvib_max}"}]
            await module.reload_config()
            assert "in_ovc_strength_a" in module.bridge.engine.mappings
            assert module.is_running()
        finally:
            await module.stop()
            assert not module.is_running()
            assert module.bridge is None


if __name__ == "__main__":
    unittest.main()
