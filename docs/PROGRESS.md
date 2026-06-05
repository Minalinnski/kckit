# kckit 项目进度与设计文档

> 最后更新：2026-05-24

---

## 一、系统定位

**目标**：不修改 KC 客户端、不直接调 KC API，通过 poi 浏览器的正常渲染流程实现远征/入渠/出击的全自动循环调度。

**硬约束（不可绕过）**：
- 大破保护：`safety.check_taiha()` 是红线，任何大破 → 强制撤退
- 不直接调 API：所有操作经过 poi 浏览器渲染层
- 反检测：随机延迟 + 贝塞尔鼠标曲线 + 每 24h 休息 ≥4h

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────┐
│  poi 浏览器（Electron）                                   │
│  ┌──────────────────┐   ┌──────────────────────────┐    │
│  │  KC 游戏画布      │   │  poi React 导航栏         │    │
│  │  (800×480 canvas)│   │  (poi 注入, y=0.06..0.15)│    │
│  └──────────────────┘   └──────────────────────────┘    │
│            ↕ KC API 流量                                  │
│  ┌──────────────────────────────────┐                    │
│  │  poi-plugin/index.js (kckit-bridge)                   │
│  │  订阅 Redux store → WebSocket → 127.0.0.1:12450      │
│  └──────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────┘
                     ↕ WebSocket
┌─────────────────────────────────────────────────────────┐
│  kckit Python 进程                                        │
│                                                           │
│  poi_client.py ──→ GameState (ships/fleets/repairs/…)   │
│       ↓                                                   │
│  screen_detector.py ──→ 当前界面推断                     │
│       ↓                                                   │
│  scheduler.py ──→ 优先级任务队列                         │
│       ├── expedition_manager.py ── 远征轮换               │
│       ├── repair_manager.py    ── 入渠排队                │
│       └── executor.py         ── 出击执行                │
│              ↓                                            │
│       composer.py + optimizer.py ── 编队/配装            │
│       safety.py ── 大破检查（每步必过）                  │
│       confirm.py ── CLI 人工确认                         │
│              ↓                                            │
│       pyautogui ── 虚拟鼠标点击 poi 窗口                 │
│       screen_layout.yaml ── 元素坐标查表                  │
└─────────────────────────────────────────────────────────┘
```

---

## 三、屏幕坐标系

screenshots = KC canvas at ~1.11× 缩放，分辨率 889×533。

坐标用比例（0–1），在 canvas 尺度下与截图比例完全等价。

### header 结构（edge-confirmed，所有界面共用）

| 区域 | y 范围 | px 范围 |
|------|--------|---------|
| KC 资源栏（燃弹钢铝桶材） | 0.000 – 0.059 | 0 – 31 |
| poi React 导航栏（插件快捷键） | 0.059 – 0.146 | 31 – 77 |
| **KC 游戏内容起始** | **0.146** | **77 px** |

| 元素 | 坐标 |
|------|------|
| 母港按钮（茶绿圆形，左上） | cx=0.040, cy=0.073, w=0.080, h=0.146 |
| 左侧 chrome 右边界 | x=0.0799 |
| poi 导航按钮行中心 | cy=0.1032 |
| poi 按钮 1（最左确认） | cx≈0.200 |
| poi 按钮 2 | cx≈0.399 |
| 各界面舰队标签行 | y≈0.175（内容起始 + 约 16px） |

---

## 四、模块实现状态

### core/

| 模块 | 状态 | 说明 |
|------|------|------|
| `schema.py` | ✅ 完成 | Strategy YAML pydantic v2 契约；330 行；全部 tests 通过 |
| `models.py` | ✅ 完成 | Ship / Equipment / Fleet / Quest / Construction / RepairDock |
| `knowledge.py` | ✅ 完成 | 装备知识库（725件）+ 特殊攻击规则（昼连击/夜战CI/AACI/弹着） |
| `poi_client.py` | ✅ 完成 | WebSocket 客户端；GameState 全量更新 |
| `optimizer.py` | ✅ 完成 | noro6 制空公式；索敌分数；三阶段装备优先级 |
| `composer.py` | ✅ 完成 | 编队算法；舰种约束；大破排除 |
| `screen_detector.py` | ✅ 完成 | API 事件 → 界面推断；ScreenLayout 坐标查表 |
| `action_log.py` | ✅ 完成 | JSON lines 操作日志 + 截图路径 |
| `expedition_manager.py` | ✅ 完成 | 远征状态评估；返回时间预测；行动建议 |
| `repair_manager.py` | ✅ 完成 | 入渠排队；高桶修复优先级 |
| `safety.py` | ✅ 完成 | `check_taiha()` 硬红线；贝塞尔曲线；随机延迟；24h 休息调度 |
| `confirm.py` | ✅ 完成 | CLI 人工确认界面（rich 表格） |
| `executor.py` | ✅ 完成（待实测） | 虚拟鼠标出击执行器；dry_run 模式；事件驱动等待 |
| `scheduler.py` | ✅ 完成（待实测） | 任务优先级队列；远征/入渠/出击循环调度 |

### data/

| 文件 | 状态 |
|------|------|
| `equip_db.json` | ✅ 725 件，来自 navy-album/master.json |
| `ship_db.json` | ✅ 1677 艘 |
| `equip_subs.json` | ✅ 26 个角色装备平替分级 |

### poi-plugin/

| 文件 | 状态 |
|------|------|
| `index.js` | ✅ 完成；订阅 Redux store，WebSocket 桥接 |

### tools/

| 工具 | 状态 | 说明 |
|------|------|------|
| `import_nga.py` | ✅ 完成 | Claude API 解析 NGA 攻略 → strategies/*.yaml |
| `build_master_db.py` | ✅ 完成 | 构建 equip_db / ship_db |
| `calibrate.py` | ✅ 完成（待运行） | 点击画布角点 → poi_window.yaml |
| `simulator/` | ✅ 完成 | Web UI 可视化模拟器（server.py + index.html 1379行） |
| `annotate_screenshots.py` | ✅ 完成 | YAML 坐标叠加到截图 |
| `calibrate_chrome.py` | ✅ 完成 | Edge 分析：AND-Canny 提取 header/左侧 chrome 精确边界 |
| `coord_grid.py` | ✅ 完成 | HoughLinesP 检测行列分割线 |
| `edge_calibrate.py` | ✅ 完成 | Canny + 轮廓检测每屏矩形 |
| `find_common_ui.py` | ✅ 完成 | 11张截图 AND 边缘图 → 公共 chrome |

### config/screen_layout.yaml

| 界面 | 坐标质量 | 说明 |
|------|----------|------|
| 公共 header（母港按钮/poi导航） | ✅ edge-confirmed | AND-Canny 精确测量 |
| `port` nav wheel | ⚠️ APPROX | 目测估算；形状合理但需 live 验证 |
| `hensei` 2×3 ship grid | ⚠️ APPROX | HoughLinesP 分区；slot 中心偏差 ≤5% |
| `repair` 4 docks | ✅ HoughLinesP | 行边界精确；实测对齐好 |
| `factory` 4 tabs + 4 docks | ✅ HoughLinesP | 同上 |
| `quest_list` 5 rows | ✅ HoughLinesP | 同上 |
| `equipment` ship list | ⚠️ APPROX | v-line 分割已确认；行距 APPROX |
| `equipment_detail` slots | ⚠️ APPROX | x 位置已修正；y 间距 APPROX |
| `sortie_type` 3 circles | ⚠️ APPROX | 圆心 y 约 0.57–0.63；范围内可点击 |
| `sortie_world` world tiles | ⚠️ APPROX | 2 行 × 2 列可见；卷动后同列 |
| `expedition_select` | ⚠️ APPROX | 区域 tab + 远征列表；相对合理 |

### config/poi_window.yaml

| 状态 | ❌ 未生成 |
|------|-----------|
| 原因 | 需要 poi 开机 + `python tools/calibrate.py` 点击 4 个画布角点 |
| 影响 | executor.py 的 `to_pixel()` 无法将坐标转换为屏幕绝对像素，实际点击无法执行 |

### strategies/

- 目录结构已建立：`maps/world1..7/` + `quests/daily|weekly|monthly|...`
- `example_5_4.yaml`：示例策略文件
- NGA 导入内容：待运行 `import_nga.py batch`

---

## 五、测试

```
64 tests passing (pytest)
核心覆盖：schema / optimizer / composer / safety / poi_client / import_nga
```

executor / scheduler / confirm 无自动化测试（依赖真实 poi 环境）。

---

## 六、剩余工作

### 6.1 必须做（上线前阻断）

| 项目 | 操作 |
|------|------|
| **生成 poi_window.yaml** | poi 开机 → `python tools/calibrate.py` → 点 4 角 |
| **坐标 live 验证** | poi 开机 → simulator 点击标定模式 → 逐界面确认 APPROX 坐标 |
| **executor dry_run 验证** | 启动 poi → `main.py sortie --map 1-1 --dry-run` → 检查日志 |

### 6.2 重要（功能完整性）

| 项目 | 说明 |
|------|------|
| **Simulator 点击标定模式** | 在 Simulator 里点击游戏截图 → 输出 YAML 坐标片段，替代手动测量 |
| **screen_layout 精确化** | live poi 运行后逐界面核对所有 APPROX 坐标 |
| **出击执行完整测试** | 1-1 → 阵形选择 → 昼战/夜战 → 撤退/进击判断 |
| **远征/入渠完整循环** | scheduler 驱动的 24h 自动循环测试 |
| **supply 截图** | 补给界面至今没有截图，坐标全靠估算 |

### 6.3 后续优化

| 项目 | 说明 |
|------|------|
| NGA 攻略批量导入 | `import_nga.py batch` 填充 strategies/maps/ |
| 出击策略精细化 | 节点选择、资源节回避、Boss 优先 |
| 联合舰队支持 | combined_battle 路径已预留接口 |
| 夜战判断逻辑 | 当前只有骨架，MVP 阶段总是撤退 |

---

## 七、关键文件索引

```
main.py                     # 主入口 CLI
core/safety.py              # 大破保护红线
core/executor.py            # 虚拟鼠标出击
core/scheduler.py           # 任务调度主循环
config/screen_layout.yaml   # 屏幕元素坐标（451行）
tools/simulator/            # Web 可视化调试器
tools/calibrate_chrome.py   # Edge 分析工具（产生 temp/chrome_cal/）
temp/annotated/             # YAML 坐标叠加截图（11张，最新版本）
temp/chrome_overlay/        # Chrome edge 分析结果（11张）
```

---

## 八、已知设计决策

- **为什么不用 OCR / 模板匹配**：KC canvas 有动画，截图抖动大；API 事件更可靠，作为 screen detect 主要手段
- **为什么 screen_layout.yaml 用比例而非像素**：poi 窗口大小可变；比例在任意缩放下不变
- **坐标比例基准**：截图分辨率 889×533 = KC canvas 1.11× 缩放；比例值 scale-invariant
- **poi React 导航栏**：poi 注入在 y=0.059–0.146，不是 KC 原生导航；KC 游戏内容从 y=0.146 开始
- **母港按钮**：茶绿圆形 crest（x=0..0.08，y=0..0.146），AND-Canny 在所有 11 张截图中均检出
