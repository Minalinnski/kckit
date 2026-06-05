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
1. screen spy（DOM MutationObserver，confidence=1.0）← 主要来源
   - 插件启动时自动注入到KC webview
   - 不依赖API事件；KC缓存API响应时也能正常工作
   - 通过 poi_client.current_screen 读取
   - broadcast type=screen_change 事件

2. API事件推断（confidence=0.9）← 仅作兜底
   - 仅当spy未注入成功时使用
   - KC大量缓存api_get_member/* 响应，可靠性差

3. 游戏状态启发式（confidence≤0.6）← 最后手段
```

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
