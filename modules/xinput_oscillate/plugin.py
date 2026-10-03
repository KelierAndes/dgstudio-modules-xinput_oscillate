"""XInput 手柄震动联动模块：虚拟手柄接收游戏原生震动派发 → 核心参数映射。

不注入游戏：模块经 ViGEm 创建一只虚拟 Xbox 360 手柄，游戏照常通过
自己的 XInput 通道把震动派发给它（无手柄时游戏本就没有震动数据来源）；
派发结果由虚拟手柄的**震动反馈通道**回流，左右马达 0-255 经包络平滑成
参数（``xvib_l`` / ``xvib_r`` / ``xvib_max`` / ``xvib_active`` /
``xvib_link``，``xvib_pad`` 表示虚拟手柄就绪），联动页输入映射表把它们
组合后驱动郊狼/负鼠（如 负鼠通道 A 强度 ← ``{xvib_max}``）。

* **键盘映射**：键盘键位 → 虚拟手柄按键/摇杆，无实体手柄也能驱动游戏
  与验证震动联动（调试场景，键位表可配置）；
* **实体手柄透传**（可选）：把实体手柄输入合并进虚拟手柄，游戏改用
  虚拟手柄后震动可观测。

映射关系全部落在两张映射表上（``mappings`` / ``outputs``），核心参数名
固定不可改；META["config"] 声明全部配置项，宿主自动装载 config/xinput.json，
联动页据此渲染映射表与模块设置。为安全起见默认映射表为空——不装任何
映射就不会驱动任何设备。ViGEmBus 驱动需用户自行安装（模块启动时探测
并降级），ViGEmClient.dll 随模块 bin/ 分发（官方 MIT 实现）。
"""

META = {
    "id": "xinput_oscillate",
    "name": "手柄震动联动（XInput）",
    "version": "0.2.0",
    "description": "经 ViGEm 虚拟手柄接收游戏原生 XInput 震动派发（不注入游戏），"
                   "震动强度/状态经映射表达式驱动郊狼与负鼠；支持键盘键位映射"
                   "（无手柄调试）、实体手柄透传与回环自测。",
    "settings_key": "xinput",
    "actions": ["xinput_test", "xinput_loopback"],
    # 模块自定义参数：震动包络与状态，输入表达式以 {名称} 引用
    "params": {
        "xvib_l": {"label": "左马达震动", "desc": "XInput 左马达包络强度 0-100"},
        "xvib_r": {"label": "右马达震动", "desc": "XInput 右马达包络强度 0-100"},
        "xvib_max": {"label": "震动峰值", "desc": "左右马达包络较大值 0-100"},
        "xvib_active": {"label": "震动激活", "desc": "包络峰值超过激活阈值时为 1，回落后归 0"},
        "xvib_link": {"label": "震动链路", "desc": "链路超时窗口内收到游戏震动派发为 1，超时为 0"},
        "xvib_pad": {"label": "虚拟手柄就绪", "desc": "虚拟手柄已接入（游戏可见手柄）为 1"},
    },
    "config": {
        # ---- 虚拟手柄 ----
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
        # ---- 包络整形 ----
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
        "refresh_s": {
            "label": "状态重算间隔", "type": "float", "default": 0.5,
            "min": 0.1, "max": 5.0, "step": 0.1, "unit": "s",
            "group": "settings", "desc": "设备状态变量参与表达式运算时的重算节流",
        },
        # ---- 两张映射表（配置文件只写这些） ----
        "mappings": {
            "label": "输入映射表", "type": "list", "default": [],
            "group": "map", "rows": "in",
            "desc": "行 {param: 核心输入参数, expr: 表达式}，以 {xvib_l}/{xvib_r}/"
                    "{xvib_max}/{xvib_active} 等组合驱动设备，可混合核心输出参数；"
                    "默认为空——不配置映射就不会驱动任何设备",
        },
        "outputs": {
            "label": "输出映射表", "type": "list", "default": [],
            "group": "map", "rows": "out",
            "desc": "行 {param: 核心输出参数, name: 字段名, expr: 表达式}，"
                    "本模块暂无回传消费端，可留空",
        },
    },
}

from plugins import ButtonAction, ModuleBase, spec_defaults

from modules.xinput_oscillate.bridge import BridgeConfig, XInputBridge

# 配置缺省值唯一来源 = META["config"] 声明，BridgeConfig.DEFAULTS 仅做兜底
_CONFIG_DEFAULTS = spec_defaults(META["config"])


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

    def link_params(self) -> list[tuple[str, str]]:
        """模块可写参数表（震动包络与状态，输入表达式变量池）。"""
        return [(name, str(item.get("label") or name))
                for name, item in META["params"].items()]

    def on_load(self, ctx) -> None:
        self.ctx = ctx

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
        """设置变更后把映射表与包络参数热载进运行中的桥（虚拟手柄开关除外）。"""
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
        self.bridge.apply_config()

    async def stop(self) -> None:
        if self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None

    def is_running(self) -> bool:
        return self.bridge is not None and self.bridge.is_running()

    def button_actions(self) -> list:
        """负鼠按键动作：合成测试脉冲（链路自测）与虚拟手柄回环测试。"""
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
