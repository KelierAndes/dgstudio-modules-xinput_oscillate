from __future__ import annotations

import ast
import asyncio
import os
import sys
import unittest

from unittest import IsolatedAsyncioTestCase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  定位 DGStudio 核心仓库

from plugins import ModuleBase, spec_defaults

from modules.xinput_oscillate import plugin as plugin_mod
from modules.xinput_oscillate.bridge import DEFAULTS, PARAM_KEYS, SignalBoard

from test_bridge import BLOCKED_DEVICE_METHODS, DeviceWrite, FakeCtx

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PY = os.path.join(HERE, "modules", "xinput_oscillate", "plugin.py")


class MetaLiteralTests(unittest.TestCase):
    def test_meta_is_pure_literal(self):
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
        assert value["version"] == "0.3.0"
        assert isinstance(value["config"], dict)
        assert isinstance(value["params"], dict)
        assert "mods" not in value
        assert not any(k.startswith("host") or k.startswith("port")
                       for k in value["config"])
        assert "outputs" not in value["config"]
        assert "mappings" not in value["config"]
        assert "reads" not in value

    def test_meta_declares_variables_only(self):
        """设备动作已迁到事件流：META 的说明必须讲清「只登记变量」。"""
        assert "事件流" in plugin_mod.META["description"]
        assert "映射表" not in plugin_mod.META["description"]
        assert set(plugin_mod.META["config"]) == set(DEFAULTS)

    def test_config_decl_matches_bridge_defaults(self):
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
        names = [row["name"] for row in module.link_params()]
        assert set(PARAM_KEYS) <= set(names)
        assert "xvib_pad" in names

    def test_registered_vars_are_read_only(self):
        """登记行显式写方向与类型：读数一律 dir in，核心据此判定只读。"""
        module = plugin_mod.XInputOscillateModule()
        rows = module.link_params()
        assert rows and all(isinstance(row, dict) for row in rows)
        assert all(row["dir"] == "in" for row in rows)
        assert all(row.get("type") in ("Bool", "Float", "Int")
                   for row in rows)
        specs = module.temp_specs()
        assert {row["key"] for row in specs} == {row["name"] for row in rows}
        assert all(row["dir"] == "in" for row in specs)
        by_type = {row["key"]: row["type"] for row in specs}
        assert by_type["xvib_l"] == "Float"
        assert by_type["xvib_max"] == "Float"
        assert by_type["xvib_active"] == "Bool"
        assert by_type["xvib_pad"] == "Bool"

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


class _Settings(dict):
    def __init__(self, data=None):
        super().__init__(data or {})
        self.saved = 0

    def save(self) -> None:
        self.saved += 1


class LegacyMappingTableTests(unittest.TestCase):
    def test_on_load_drops_tables_with_one_chinese_hint(self):
        ctx = FakeCtx()
        ctx.settings = _Settings({"mappings": [{"param": "in_ovc_strength_a",
                                                "expr": "{xvib_max}"}],
                                  "outputs": [{"name": "x", "expr": "{y}"}],
                                  "gain": 1.5})
        plugin_mod.XInputOscillateModule().on_load(ctx)
        assert "mappings" not in ctx.settings
        assert "outputs" not in ctx.settings
        assert ctx.settings["gain"] == 1.5
        assert ctx.settings.saved >= 1
        assert len(ctx.logs) == 1
        assert "事件流" in ctx.logs[0]
        assert "写入卡片" in ctx.logs[0]

    def test_clean_settings_stay_silent(self):
        ctx = FakeCtx()
        ctx.settings = _Settings({"gain": 1.0})
        plugin_mod.XInputOscillateModule().on_load(ctx)
        assert ctx.logs == []
        assert ctx.settings.saved == 0

    def test_reload_config_purges_tables_written_by_hand(self):
        ctx = FakeCtx()
        ctx.settings = _Settings(dict(plugin_mod._CONFIG_DEFAULTS))
        ctx.settings["vigem_enabled"] = False
        module = plugin_mod.XInputOscillateModule()
        module.on_load(ctx)
        asyncio.run(module.start())
        try:
            ctx.settings["mappings"] = [{"param": "in_fire",
                                         "expr": "{xvib_max}"}]
            asyncio.run(module.reload_config())
            assert "mappings" not in ctx.settings
            assert not hasattr(module.bridge.engine, "mappings")
            assert module.bridge.config["vigem_enabled"] is False
        finally:
            asyncio.run(module.stop())


class ModuleLifecycleTests(IsolatedAsyncioTestCase):
    """start → reload_config → stop 全程跑在拦截设备直写的桩上下文上。"""

    async def test_start_reload_stop(self):
        ctx = FakeCtx()
        ctx.settings = _Settings(dict(plugin_mod._CONFIG_DEFAULTS))
        ctx.settings["vigem_enabled"] = False
        ctx.settings["mappings"] = [{"param": "in_ovc_strength_a",
                                     "expr": "{xvib_max}"}]
        module = plugin_mod.XInputOscillateModule()
        module.on_load(ctx)
        assert "mappings" not in ctx.settings
        try:
            await module.start()
            assert module.is_running()
            assert module.bridge is not None
            assert isinstance(module.bridge.engine, SignalBoard)
            assert not hasattr(module.bridge.engine, "set_mappings")
            assert not hasattr(module.bridge, "actions")
            ctx.settings["idle_ms"] = 900
            await module.reload_config()
            assert module.bridge.config["idle_ms"] == 900
            await module.bridge.inject_test(50, seconds=0.12)
            assert module.bridge.engine.signals["xvib_max"] > 0.0
            assert module.is_running()
        finally:
            await module.stop()
            assert not module.is_running()
            assert module.bridge is None

    def test_stub_context_blocks_every_forbidden_method(self):
        for name in BLOCKED_DEVICE_METHODS:
            with self.assertRaises(DeviceWrite):
                getattr(FakeCtx(), name)("A")


if __name__ == "__main__":
    unittest.main()
