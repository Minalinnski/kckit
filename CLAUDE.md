# kckit

KanColle 舰队自动化工具套件。poi 浏览器 + 虚拟鼠标方案，绝不直接调 API。

## 项目结构

```
kckit/
├── core/
│   ├── schema.py           # Strategy YAML 数据契约（pydantic v2）
│   ├── models.py           # 游戏数据模型 — Ship/Equipment/Fleet/Quest/Construction/RepairDock
│   ├── knowledge.py        # 装备知识库 + 特殊攻击规则（KnowledgeBase）
│   ├── poi_client.py       # WebSocket 客户端，接收 poi 数据
│   ├── optimizer.py        # 装备优化（制空/索敌算法，Phase 0-3）
│   ├── composer.py         # 编队算法
│   ├── screen_detector.py  # 界面状态推断（API事件→当前界面）
│   ├── action_log.py       # 操作结构化日志（JSON lines + 截图路径）
│   ├── expedition_manager.py  # 远征状态评估与行动建议
│   ├── repair_manager.py   # 入渠状态评估与舰娘排队
│   ├── safety.py           # 大破保护 + 反检测（红线，不可绕过）
│   ├── confirm.py          # CLI 人工确认界面
│   ├── executor.py         # 出击执行器（虚拟鼠标，dry_run 模式）
│   └── scheduler.py        # 任务调度器
├── tools/
│   ├── import_nga.py       # NGA 攻略导入（独立工具，用 Claude API）
│   ├── build_master_db.py  # 从 poi navy-album 构建装备/舰娘知识库
│   ├── inspect.py          # 可视化状态检查（截图+叠加注释→PNG）
│   └── calibrate.py        # 坐标标定（点击游戏画布角点→config/poi_window.yaml）
├── config/
│   ├── screen_layout.yaml  # 各界面 UI 元素坐标定义（相对坐标 0-1）
│   └── poi_window.yaml     # 游戏画布在屏幕上的像素坐标（由 calibrate.py 生成）
├── data/
│   ├── equip_db.json       # 装备知识库（725件，来自 navy-album/master.json）
│   ├── ship_db.json        # 舰娘数据库（1677艘）
│   └── equip_subs.json     # 装备平替分级（26个角色）
├── poi-plugin/     # JS poi 数据桥插件
│   ├── index.js
│   └── package.json
├── strategies/     # NGA 导入生成的 YAML 攻略配置（按海域分文件）
├── temp/           # 运行时产生的日志、截图（.gitignore）
├── tests/
└── main.py         # 主入口（status/plan/compose/sortie/schedule）
```

## 安装

```bash
pip install -r requirements.txt

# poi 插件安装
cp -r poi-plugin ~/Library/Application\ Support/poi/plugins/node_modules/poi-plugin-kckit-bridge
cd ~/Library/Application\ Support/poi/plugins/node_modules/poi-plugin-kckit-bridge
npm install
# 然后在 poi 插件管理界面启用 kckit-bridge
```

## 工作流

```
# 首次使用
python tools/calibrate.py              # 标定游戏画布坐标 → config/poi_window.yaml
python tools/build_master_db.py        # 构建装备/舰娘知识库 → data/*.json

# 日常使用
python main.py status                  # 查看当前状态（资源/远征/入渠/任务）
python tools/inspect.py                # 截图 + 状态叠加注释 → temp/inspect_*.png
python main.py plan 5-5 --preset 武大  # 推演配装方案（不需要 poi 在线）
python main.py compose 5-5             # 编队+配装，人工确认后应用
python main.py sortie --map 5-5        # 出击循环（需先 compose）
python main.py schedule                # 全自动（远征+出击，7×24h）

# 攻略导入
python tools/import_nga.py batch       # 批量导入 NGA 攻略 → strategies/*.yaml
```

## 关键约束

- **大破保护**：`safety.py` 的 `check_taiha()` 是硬红线，任何大破 → 强制撤退
- **绝不直接调 API**：所有操作通过 poi 浏览器的正常渲染流程
- **反检测**：所有操作加随机延迟，鼠标走贝塞尔曲线，每 24h 休息 ≥4h

## 界面状态检测（优先级顺序）

```
1. PIXI 场景树（confidence=0.95）← 主要来源（感知层 v2，2026-06-09）
   - tools/scene_dump.py 捕获 stage 根并遍历：纹理 frame 名 + 实时 bounds + Text 内容
   - core/scene_perception.py classify_screen()：可见图集前缀直方图 → 画面名
   - find_element()：语义元素 → 实时点击坐标（取代手工 screen_layout.yaml）
   - 注意指纹优先级：battle 排在 sally_jin 之前（战斗 HUD 含阵形图标）
   - 待办：接入 screen_detector / executor、插件内定时广播

2. screen spy（PIXI hook + hash，poi-plugin 内置）← 兜底
3. API事件推断（confidence=0.9）← 兜底（KC缓存api_get_member/*，可靠性差）
4. 游戏状态启发式（confidence≤0.6）← 最后手段
```

**感知层 v2 工作流**：
```bash
python tools/scene_dump.py                  # 当前画面场景树（--textures/--json）
python tools/harvest_atlases.py             # 增量采集 UI 图集（路过新画面后重跑）
python tools/atlas_sheet.py sally_sortie    # 生成标注拼图 → VLM 认图 → semantics.yaml
```

**交互层发现（2026-06-09，关键）**：KC2 用 **alpha=0 的透明精灵（interactive+buttonMode）定义真实点击区**
——可见精灵只是装饰（如入渠：整条渠是一张 1005×123 烘焙贴图，真点击区是其上的透明
`repair_main_1` 精灵 252×72，与 Figma 手工标定一致）。walker v3 起保留 alpha=0 的
interactive 节点（`i:1/btn:1/a:0`），前端 overlay 青色层显示。**点击坐标的运行时真值
= interactive 节点 bounds**；手工 screen_layout.yaml 保留为对照/兜底，逐画面验证后切换。

**已知坑：@electron/remote 注册表溢出（2026-06-09 实际发生）**：插件经 remote 跨进程调用
（executeJavaScript/capturePage），每次调用的返回对象按字段注册进 remote 内部 Map；
长会话+大对象返回（场景树几百节点）会把它撑到上限 →「Map maximum size exceeded」→
**所有跨进程调用全挂（截图/exec 全失效），唯一恢复手段是重启 poi**。
缓解（已实施）：所有大返回值在 frame 内 `JSON.stringify` 成单字符串再跨进程（千倍减漏）。
若再次出现该报错 → 重启 poi 即可，不是游戏/账号问题。

**海图数据（不需要逐图游玩）**：`python tools/harvest_maps.py` 直接从游戏服务器批量下载
全部海域的 `_info.json`（**节点 cell 坐标+航线向量**）+ 图集 → `data/maps/raw/`，
合并节点表 → `data/maps/spots.json`。静态资源、无凭据、0.5s 节流。

**实时数据流（v6 推送架构，2026-06-11）**：KC2 frame 内注入的**推送代理**自己连
`ws://127.0.0.1:23456`（localhost 豁免混合内容，实测无 CSP 拦截），本地检测场景变化
（scene_ts/c1_stable/click/10s 空闲）→ 页面内遍历 → 直接推送 `kc2_scene_tree`/`kc2_click`。
**数据面零 remote 流量**（根治注册表泄露）；remote 仅剩低频控制面（点击注入/截图/自愈）。
插件收到推送转广播 `scene_tree`；旧的 500ms remote poll 自动静默（push 新鲜时跳过），
push 断 15s 后 poll 复活作为兜底+自愈（重注入 setup 即恢复 push 代理）。
触发延迟实测 ~0.3s（旧 remote 路径 0.8-1.3s）。
→ simulator 附加 `classify_screen` 结果转发，并据此发 `screen_change`（source=scene_tree）；
未识别画面（夜战弹窗等指纹缺口）自动以 `unknown_<主图集>` 存档到 temp/captures/，
已知画面每 6h 重采一次（脏样本自愈）。
WS 命令 `get_scene_tree` / HTTP `POST /api/scene_tree` 按需拉取；
`GET /api/atlas[/{name}]`、`GET /api/semantics` 供前端重建。
前端（:8765）三视图：実拍｜重建（图集+场景树数字孪生，截图降级为 30s 兜底）｜叠加
（绿框=已标语义、灰=未标、橙=图集未采集）；顶部感知条显示分类+置信度+图集直方图，状态条显示转移轨迹。
- `data/ui_atlas/raw/` — 图集 json+png（已采 35 个，含完整出击链路 sally_*）
- `data/ui_atlas/semantics.yaml` — frame→语义词典（已标 sally_top/sally_jin）
- `backup/figma_era_20260609/` — Figma 手工标定时代的成果备份

**发现工具**（首次使用时运行）：
```bash
# 在不同界面下运行，建立screen→DOM变量映射
python tools/probe_kc.py globals          # 导出KC全局变量 + DOM结构
python tools/probe_kc.py monitor          # 实时监控界面变化（在游戏里点点点）
python tools/probe_kc.py inject-spy       # 手动注入spy（插件通常自动完成）
python tools/probe_kc.py cache-path       # 查看本地KC资源缓存路径
```

**Plugin WebSocket 命令（新增）**：
```
probe_kc_globals  → type: kc_globals    — 导出KC全局变量+DOM结构
inject_screen_spy → type: spy_result    — 注入DOM观察器
get_spy_screen    → type: spy_screen    — 轻量轮询当前screen名
get_resource_path → type: resource_path — KC本地资源缓存路径
```

**本地KC资源缓存**（`resource-hack.js` 重定向写入）：
```
~/Library/Application Support/poi/MyCache/KanColle/
  kcs2/js/   — 游戏JavaScript（可分析场景管理器）
  kcs2/img/  — 精灵图（可做pixel-perfect模板匹配，替代截图）
  kcs/       — 旧版资源
```

## poi 数据路径（来自 Redux store）

```
info.ships          → 全舰娘（api_nowhp/api_maxhp/api_slot/api_slot_ex）
info.fleets         → 舰队编成（api_ship[]/api_mission[]）
info.equips         → 装备实例
info.repairs        → 入渠状态
const.$ships        → 舰娘模板（名称/舰种/槽数）
const.$equips       → 装备模板（制空/索敌/对潜等属性）
```

## 游戏数据关键字段

```
Ship:  api_nowhp, api_maxhp, api_cond, api_slot[6], api_slot_ex,
       api_locked, api_lv, api_soku（速力）, api_karyoku, api_taisen,
       api_sakuteki, api_ndock_time
Fleet: api_ship[6]（-1=空槽）, api_mission[0]（0=可用）, api_mission[1]（远征ID）
Equip: api_id, api_slotitem_id, api_level, api_alv（熟练度）
$Equip:api_name, api_type[2]（分类）, api_tyku（对空）, api_sakuteki（索敌）,
       api_taisen（対潜）, api_houg（火力）, api_raig（雷装）
```
