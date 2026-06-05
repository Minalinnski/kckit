# kckit

舰队 Collection 自动化工具套件。通过 poi 浏览器代理操控游戏，绝不直接调 `/kcsapi`。

---

## 目标

- 自动管理远征、入渠、补给等日常任务
- 根据攻略 YAML 自动配装、编队、出击
- 任意关卡可写 NGA 攻略导入脚本，生成标准化策略配置
- 反检测：随机延迟 + 贝塞尔曲线鼠标 + 每 24h 强制休息 ≥4h

**硬约束**：所有操作通过 poi 浏览器正常渲染流程（虚拟鼠标点击 canvas），不伪造 HTTP 请求。

---

## 架构

```
KC 游戏 canvas (PIXI.js)
  ↕ xhr-hack.js 拦截 XHR
poi Electron 浏览器
  ↕ WebSocket :23456
poi-plugin-kckit-bridge          ← 数据桥插件
  ↕ WebSocket
kckit Python core                ← 自动化逻辑
  ↕ HTTP + WebSocket
Simulator UI :8765               ← 调试界面
```

---

## 调试工具

### Simulator UI

```bash
cd kckit
python -m tools.simulator.server
# → http://localhost:8765
```

提供：
- 实时截图 + UI 元素坐标叠加（SVG overlay）
- 当前屏幕状态、资源、舰队信息
- 手动注入 spy、探查 KC 内部全局变量

### poi 插件

插件安装后在 poi 后台运行，通过 WebSocket :23456 向外推送游戏数据。

```bash
# 安装插件
bash tools/sync_plugin.sh

# 检查桥接状态
curl http://localhost:8765/api/bridge/status
```

### KC 内部探查

```bash
python tools/probe_kc.py globals      # 导出 KC 全局变量 → temp/kc_globals_dump.json
python tools/probe_kc.py monitor      # 实时监控画面切换事件
python tools/probe_kc.py inject-spy   # 手动注入 DOM 观察器
```

---

## 快速开始

```bash
# 依赖
pip install -r requirements.txt

# 首次标定游戏画布位置（poi 需已打开到游戏）
python tools/calibrate.py

# 构建装备/舰娘知识库
python tools/build_master_db.py

# 查看当前状态
python main.py status
```

---

## 项目结构

```
core/               自动化核心（编队/配装/出击/入渠/远征/安全检查）
config/
  screen_layout.yaml    各界面 UI 元素坐标（889×533 canvas 相对坐标）
  poi_window.yaml       游戏画布在屏幕上的像素位置
data/               装备/舰娘数据库、攻略原始数据
strategies/         出击/任务策略 YAML（NGA 攻略导入生成）
poi-plugin/         poi 数据桥插件（JS）
tools/
  simulator/        调试 UI（FastAPI + WebSocket）
  import_nga.py     NGA 攻略批量导入
  probe_kc.py       KC 内部状态探查
```

---

## 端口

| 端口 | 用途 |
|------|------|
| 23456 | poi 插件 WebSocket（游戏数据推送） |
| 23457 | poi 插件 HTTP（截图 `GET /screenshot`） |
| 8765 | Simulator 调试 UI |
