
META = {
    "id": "xinput_oscillate",
    "name": "手柄震动联动（XInput）",
    "version": "0.3.0",
    "description": "经 ViGEm 虚拟手柄接收游戏原生 XInput 震动派发（不注入游戏），"
                   "把震动强度与状态登记为只读变量；设备动作请在「事件流」页用"
                   "写入卡片按这些变量编排。支持键盘键位映射（无手柄调试）、"
                   "实体手柄透传与回环自测。",
    "settings_key": "xinput",
    "actions": ["xinput_test", "xinput_loopback"],
    "params": {
        "xvib_l": {"label": "左马达震动", "type": "Float",
                   "desc": "XInput 左马达包络强度 0-100"},
        "xvib_r": {"label": "右马达震动", "type": "Float",
                   "desc": "XInput 右马达包络强度 0-100"},
        "xvib_max": {"label": "震动峰值", "type": "Float",
                     "desc": "左右马达包络较大值 0-100"},
        "xvib_active": {"label": "震动激活", "type": "Bool",
                        "desc": "包络峰值超过激活阈值时为 1，回落后归 0"},
        "xvib_link": {"label": "震动链路", "type": "Bool",
                      "desc": "链路超时窗口内收到游戏震动派发为 1，超时为 0"},
        "xvib_pad": {"label": "虚拟手柄就绪", "type": "Bool",
                     "desc": "虚拟手柄已接入（游戏可见手柄）为 1"},
    },
    "config": {
        "vigem_enabled": {
            "label": "创建虚拟手柄", "type": "bool", "default": True,
            "group": "pad", "desc": "经 ViGEmBus 建虚拟 Xbox 360 手柄接收游戏震动派发；"
                                    "需先安装 ViGEmBus 驱动",
        },
        "keyboard_enabled": {
            "label": "键盘映射", "type": "bool", "default": True,
            "group": "pad", "desc": "按下方键位表把键盘输入合成到虚拟手柄"
                                    "（无实体手柄时的调试通道）",
        },
        "forward_pad": {
            "label": "实体手柄透传", "type": "bool", "default": False,
            "group": "pad", "desc": "实体手柄输入合并进虚拟手柄：游戏改用虚拟手柄后"
                                    "震动才能回流（建议同时屏蔽系统可见的实体手柄）",
        },
        "keyboard_map": {
            "label": "键盘键位表", "type": "map",
            "default": {
                "a": "Space", "b": "Q", "x": "E", "y": "R",
                "lb": "1", "rb": "2", "lt": "3", "rt": "4",
                "back": "Backspace", "start": "Enter",
                "ls_click": "C", "rs_click": "V",
                "dpad_up": "Up", "dpad_down": "Down",
                "dpad_left": "Left", "dpad_right": "Right",
                "ls_up": "W", "ls_down": "S", "ls_left": "A", "ls_right": "D",
                "rs_up": "I", "rs_down": "K", "rs_left": "J", "rs_right": "L",
            },
            "group": "pad",
            "desc": "槽位 → 键盘键名（支持字母/数字、Space/Enter/F1 等、0x 十六进制）；"
                    "槽位：a/b/x/y、lb/rb、lt/rt、back/start、ls_click/rs_click、"
                    "dpad_*、ls_up/down/left/right、rs_*",
        },
        "gain": {
            "label": "强度增益", "type": "float", "default": 1.0,
            "min": 0.1, "max": 3.0, "step": 0.05, "unit": "×",
            "group": "settings", "desc": "震动百分比的放大倍数（结果仍钳制 0-100）",
        },
        "deadband_pct": {
            "label": "死区", "type": "float", "default": 2.0,
            "min": 0.0, "max": 20.0, "step": 0.5, "unit": "%",
            "group": "settings", "desc": "低于该百分比的震动视为 0（过滤游戏微颤）",
        },
        "active_pct": {
            "label": "激活阈值", "type": "float", "default": 15.0,
            "min": 0.0, "max": 50.0, "step": 1.0, "unit": "%",
            "group": "settings", "desc": "包络峰值超过该值时 {xvib_active} 为 1",
        },
        "release_ms": {
            "label": "回落时间", "type": "int", "default": 300,
            "min": 50, "max": 2000, "unit": "ms",
            "group": "settings", "desc": "震动归零后包络线性衰减到 0 的时长（设备强度不跳变）",
        },
        "idle_ms": {
            "label": "链路超时", "type": "int", "default": 1000,
            "min": 200, "max": 5000, "unit": "ms",
            "group": "settings",
            "desc": "超时未收到游戏震动派发即视为链路断开（{xvib_link} 归 0）",
        },
    },
}

from plugins import ButtonAction, ModuleBase, spec_defaults

from modules.xinput_oscillate.bridge import BridgeConfig, XInputBridge

_CONFIG_DEFAULTS = spec_defaults(META["config"])

_LEGACY_TABLE_KEYS = ("mappings", "outputs")


def drop_legacy_tables(settings, log=None) -> bool:
    """清掉映射表时代留在设置里的行：设备动作已迁到「事件流」的写入卡片。"""
    removed = [key for key in _LEGACY_TABLE_KEYS if key in settings]
    if not removed:
        return False
    for key in removed:
        settings.pop(key, None)
    if hasattr(settings, "save"):
        settings.save()
    if log is not None:
        log("已清除旧版映射表设置（" + "、".join(removed) + "）："
            "本模块只登记震动变量，设备动作请在「事件流」页用写入卡片按这些变量编排")
    return True


def var_rows() -> list[dict]:
    """META 声明的震动读数 → 变量表登记行（全部只读：模块只发布，不接收写入）。"""
    return [{"name": name, "label": str(item.get("label") or name),
             "dir": "in", "type": str(item.get("type") or "Float"),
             "desc": str(item.get("desc") or "")}
            for name, item in META["params"].items()]


class XInputOscillateModule(ModuleBase):
    id = META["id"]
    name = META["name"]
    version = META["version"]
    description = META["description"]
    settings_key = META["settings_key"]

    def __init__(self):
        self.bridge: XInputBridge | None = None
        self.ctx = None

    def config_spec(self) -> dict:
        return META["config"]

    def link_params(self) -> list[dict]:
        return var_rows()

    def temp_specs(self) -> list[dict]:
        """同一批读数再以 key 形式登记：核心变量表据此判定方向与类型。"""
        return [{"key": row["name"],
                 **{k: v for k, v in row.items() if k != "name"}}
                for row in var_rows()]

    def on_load(self, ctx) -> None:
        self.ctx = ctx
        drop_legacy_tables(ctx.settings, ctx.log)

    def on_unload(self) -> None:
        self.bridge = None
        self.ctx = None

    async def start(self) -> None:
        if self.bridge is not None and self.bridge.is_running():
            return
        await self.stop()
        cfg = BridgeConfig({k: self.ctx.settings.get(k, default)
                            for k, default in _CONFIG_DEFAULTS.items()})
        self.bridge = XInputBridge(cfg, self.ctx)
        await self.bridge.start()

    async def reload_config(self) -> None:
        if self.bridge is None:
            return
        for key, default in _CONFIG_DEFAULTS.items():
            if key == "keyboard_map":
                continue
            self.bridge.config[key] = self.ctx.settings.get(key, default)
        want_map = self.ctx.settings.get(
            "keyboard_map", _CONFIG_DEFAULTS["keyboard_map"])
        self.bridge.config["keyboard_map"] = want_map
        if self.bridge.pad is not None:
            from modules.xinput_oscillate.virtualpad import KeyMap

            self.bridge.pad.keymap = KeyMap(want_map)
        for key in ("vigem_enabled",):
            want = self.ctx.settings.get(key, _CONFIG_DEFAULTS[key])
            if bool(self.bridge.config.get(key)) != bool(want):
                self.ctx.log("虚拟手柄开关已修改，需重新开关模块后生效")
        drop_legacy_tables(self.ctx.settings, self.ctx.log)

    async def stop(self) -> None:
        if self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None

    def is_running(self) -> bool:
        return self.bridge is not None and self.bridge.is_running()

    def button_actions(self) -> list:
        return [
            ButtonAction(
                key="xinput_test",
                label="手柄震动测试脉冲…",
                argument_placeholder="强度% 1-100（默认 60）",
                on_press=self._inject_press,
            ),
            ButtonAction(
                key="xinput_loopback",
                label="虚拟手柄回环测试…",
                argument_placeholder="强度% 1-100（默认 60）",
                on_press=self._loopback_press,
            ),
        ]

    def _inject_press(self, slot_id, argument) -> None:
        if self.bridge is None or not self.bridge.is_running():
            self.ctx.log("震动桥未运行，无法注入测试脉冲")
            return
        self.ctx.submit(self.bridge.inject_test(_pct_of(argument)))

    def _loopback_press(self, slot_id, argument) -> None:
        if self.bridge is None or not self.bridge.is_running():
            self.ctx.log("震动桥未运行，无法回环测试")
            return
        self.ctx.submit(self.bridge.pad_loopback(_pct_of(argument)))


def _pct_of(argument) -> float:
    try:
        return min(100.0, max(1.0, float(str(argument or "").strip() or 60)))
    except (TypeError, ValueError):
        return 60.0
