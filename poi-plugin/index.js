/**
 * poi-plugin-kckit-bridge
 * Exposes game state via WebSocket on port 23456 for the kckit Python client.
 *
 * Server → Client:
 *   { type: "state",  payload: GameState }
 *   { type: "event",  event: "/kcsapi/...", payload: { body, postBody } }
 *
 * Client → Server:
 *   { cmd: "get_state" }
 */

/* global window */
const fs = require('fs')
const http = require('http')
const os = require('os')
const nodePath = require('path')
const WebSocket = require('ws')

const SNAPSHOT_PATH = nodePath.join(os.homedir(), '.kckit', 'box_snapshot.json')

const PORT = 23456
const SCREENSHOT_PORT = 23457
let wss = null
let screenshotServer = null
let gameResponseHandler = null
let _fileWatcher = null

// ── Hot-reload ────────────────────────────────────────────────────────────────
// Reads the plugin's own file, evals it in a fresh module context, then
// cleanly swaps old→new without touching poi's plugin management UI.

function selfReload() {
  try {
    const code = fs.readFileSync(__filename, 'utf8')
    const fresh = { exports: {} }
    // Wrap in CommonJS envelope so require/module/exports work inside
    const fn = new Function('require', 'module', 'exports', '__filename', '__dirname', code)
    fn(require, fresh, fresh.exports, __filename, __dirname)
    if (typeof fresh.exports.pluginDidLoad !== 'function') {
      throw new Error('new code is missing pluginDidLoad')
    }
    // Unload current, load new
    pluginWillUnload()
    fresh.exports.pluginDidLoad()
    console.log('[kckit-bridge] Hot reload OK')
  } catch (e) {
    console.error('[kckit-bridge] Hot reload FAILED (kept running):', e)
  }
}

// ── State snapshot builder ───────────────────────────────────────────────────

function buildState() {
  const state = window.getStore()

  const ships         = (state && state.info && state.info.ships)         || {}
  const equips        = (state && state.info && state.info.equips)        || {}
  const fleets        = (state && state.info && state.info.fleets)        || {}
  const repairs       = (state && state.info && state.info.repairs)       || {}
  const constructionsRaw = (state && state.info && state.info.constructions) || []
  const questsRaw     = (state && state.info && state.info.quests)        || {}
  const $ships        = (state && state.const && state.const.$ships)      || {}
  const $equips       = (state && state.const && state.const.$equips)     || {}
  const basic         = (state && state.info && state.info.basic)         || {}
  const resources     = (state && state.info && state.info.resources)     || {}

  // Merge instance + master for ships
  const shipsFull = {}
  Object.entries(ships).forEach(function([id, ship]) {
    const master = $ships[ship.api_ship_id] || {}
    shipsFull[id] = Object.assign({}, ship, { $master: master })
  })

  // Merge instance + master for equips
  const equipsFull = {}
  Object.entries(equips).forEach(function([id, equip]) {
    const master = $equips[equip.api_slotitem_id] || {}
    equipsFull[id] = Object.assign({}, equip, { $master: master })
  })

  // Normalise fleets: poi may store as array or object — always emit as dict keyed by api_id
  const fleetsArr = Array.isArray(fleets) ? fleets : Object.values(fleets)
  const fleetsDict = {}
  fleetsArr.forEach(function(fleet) {
    if (fleet && fleet.api_id) fleetsDict[fleet.api_id] = fleet
  })

  // Normalise constructions: always emit as array
  const constructionsArr = Array.isArray(constructionsRaw)
    ? constructionsRaw
    : Object.values(constructionsRaw)

  // Normalise quests: poi may store as dict (keyed by quest_id) or array — always emit as dict
  const questsDict = {}
  if (Array.isArray(questsRaw)) {
    questsRaw.forEach(function(q) {
      if (q && q.api_no) questsDict[q.api_no] = q
    })
  } else {
    Object.assign(questsDict, questsRaw)
  }

  // Sortie state — tells us which map/node we're currently on
  const sortieRaw = (state && state.sortie) || {}
  const sortieState = {
    in_sortie:    !!(sortieRaw.mapId && sortieRaw.mapId.length),
    map_id:       sortieRaw.mapId       || [],    // [area_id, map_no] e.g. [5, 5]
    node_id:      sortieRaw.nodeId      || null,  // current node number
    boss_id:      sortieRaw.bossId      || null,  // boss node number
    combined_flag: sortieRaw.combinedFlag || 0,   // 0=normal, 1=carrier, 2=surface, 3=transport
    escaped_pos:  sortieRaw.escapedPos  || [],    // indices of escaped ships (damecon)
    fleet_id:     sortieRaw.sortieFleet || 1,     // which fleet is sortieing
  }

  const resArr = Array.isArray(resources) ? resources : (resources && resources.api_value) || []
  return {
    ships: shipsFull,
    equips: equipsFull,
    fleets: fleetsDict,
    repairs: repairs,
    constructions: constructionsArr,
    quests: questsDict,
    sortie: sortieState,
    resources: {
      fuel:         resArr[0] || 0,
      ammo:         resArr[1] || 0,
      steel:        resArr[2] || 0,
      bauxite:      resArr[3] || 0,
      fast_build:   resArr[4] || 0,
      bucket:       resArr[5] || 0,
      dev_mat:      resArr[6] || 0,
      improve_mat:  resArr[7] || 0,
    },
    hq_level:  basic.api_level || 0,
    timestamp: Date.now(),
    last_event: buildState._lastEvent || '',
  }
}

// ── Broadcast to all connected clients ──────────────────────────────────────

function broadcast(msg) {
  if (!wss) return
  const data = JSON.stringify(msg)
  wss.clients.forEach(function(client) {
    // kc2 push agents are data SOURCES — don't echo broadcasts back at them
    if (client.readyState === WebSocket.OPEN && client._kckitRole !== 'kc2') {
      client.send(data)
    }
  })
}

// ── KC2 in-page push agent state ─────────────────────────────────────────────
var _kc2PushTs = 0          // last message from any kc2 push agent
var _lastPushedTree = null  // most recent pushed scene tree (for get_scene_tree)
var _lastShotHintTs = 0

function pushAgentFresh() {
  return Date.now() - _kc2PushTs < 15000
}

function handleKc2Push(ws, cmd) {
  _kc2PushTs = Date.now()
  if (cmd.cmd === 'kc2_hello') {
    ws._kckitRole = 'kc2'
    console.log('[kckit-bridge] KC2 push agent connected (data plane off remote)')
    broadcast({ type: 'push_status', connected: true })
    return true
  }
  if (cmd.cmd === 'kc2_scene_tree') {
    ws._kckitRole = 'kc2'
    if (cmd.payload && cmd.payload.nodes) {
      _lastPushedTree = cmd.payload
      broadcast({ type: 'scene_tree', payload: cmd.payload })
      // screenshot refresh hint for live view (throttled)
      if (Date.now() - _lastShotHintTs > 800) {
        _lastShotHintTs = Date.now()
        broadcast({ type: 'screenshot_needed', reason: 'kc2_push', ts: Date.now() })
      }
    }
    return true
  }
  if (cmd.cmd === 'kc2_click') {
    ws._kckitRole = 'kc2'
    var rx = Number(cmd.rx) || 0
    var ry = Number(cmd.ry) || 0
    broadcast({ type: 'canvas_click', rx: rx, ry: ry, screen: _lastPolledScreen })
    var navTarget = inferNavClick(rx, ry, _lastPolledScreen)
    if (navTarget && navTarget !== _lastPolledScreen) {
      _lastPolledScreen = navTarget
      broadcast({ type: 'screen_change', screen: navTarget, source: 'click_nav' })
    }
    return true
  }
  return false
}

// ── Handle commands from Python client ──────────────────────────────────────

function getCanvasInfo() {
  // poi renders the KanColle game inside a <webview> element.
  // getBoundingClientRect() gives position within the page;
  // window.screenX/Y gives the browser window's top-left on the physical screen.
  // On Retina displays devicePixelRatio scales logical → physical pixels.
  const dpr = window.devicePixelRatio || 1
  const wx = window.screenX || 0
  const wy = window.screenY || 0

  // Try <webview> first (poi HTML5 mode), then <embed> (legacy Flash/PPAPI)
  const el = document.querySelector('webview') || document.querySelector('embed[src*="kcs"]')
  if (!el) {
    return { found: false, reason: 'no game element found' }
  }
  const rect = el.getBoundingClientRect()
  // rect is in CSS (logical) pixels; multiply by dpr for physical screen pixels
  return {
    found: true,
    x: Math.round((wx + rect.left) * dpr),
    y: Math.round((wy + rect.top)  * dpr),
    w: Math.round(rect.width  * dpr),
    h: Math.round(rect.height * dpr),
    dpr: dpr,
  }
}

// ── KC game frame access ──────────────────────────────────────────────────────
// Architecture:
//   poi webview → DMM portal (https://play.games.dmm.com/game/kancolle)
//                    └── #game_frame iframe → KC2 game (actual game content)
//
// executeJavaScript on the outer webview runs in DMM portal context.
// All probe/spy code must dig through #game_frame.contentWindow to reach KC2.

// Selector to find the KC2 game iframe inside the DMM portal.
// Uses single-quote attribute selectors so it can be safely embedded in double-quoted JS strings.
var _GF = "#game_frame, iframe[src*='kcs2'], iframe[src*='kancolle']"

// ── Canvas click navigation inference ────────────────────────────────────────
// Coordinates are fractions of KC2 canvas — same reference as screen_layout.yaml (889×533).
// Circle entries use r = radius; rect entries use hw/hh = half-width/half-height.
// Figma-calibrated 2026-06-04 (node 1:13 port wheel, 10:2 left nav).
var NAV_MAP = {
  // Port navigation wheel — Figma circles; r = min(w,h)/2 from YAML
  port: [
    { cx:0.246, cy:0.540, r:0.083, to:'sortie_type' },  // 出撃 (large center)
    { cx:0.249, cy:0.281, r:0.058, to:'hensei'      },  // 編成
    { cx:0.098, cy:0.463, r:0.058, to:'supply'      },  // 補給
    { cx:0.396, cy:0.461, r:0.058, to:'equipment'   },  // 改装
    { cx:0.155, cy:0.756, r:0.058, to:'repair'      },  // 入渠
    { cx:0.340, cy:0.756, r:0.058, to:'factory'     },  // 工廠
  ],
  // 出撃種別選択 — 3 large circles (r = min(0.220,0.400)/2 = 0.110)
  sortie_type: [
    { cx:0.225, cy:0.570, r:0.110, to:'sortie_world'      },  // 出撃
    { cx:0.500, cy:0.570, r:0.110, to:'practice'           },  // 演習
    { cx:0.775, cy:0.570, r:0.110, to:'expedition_select'  },  // 遠征
  ],
}

// Left sidebar present on all menu screens.
// 母港 button (right side, larger rect) + 5 nav items (left column, small rects).
var LEFT_NAV_HITS = [
  { cx:0.092, cy:0.539, hw:0.018, hh:0.075, to:'port'      },  // 母港
  { cx:0.027, cy:0.319, hw:0.015, hh:0.040, to:'hensei'    },  // 編成
  { cx:0.027, cy:0.432, hw:0.015, hh:0.040, to:'supply'    },  // 補給
  { cx:0.027, cy:0.544, hw:0.015, hh:0.040, to:'equipment' },  // 改装
  { cx:0.027, cy:0.660, hw:0.015, hh:0.040, to:'repair'    },  // 入渠
  { cx:0.027, cy:0.767, hw:0.015, hh:0.040, to:'factory'   },  // 工廠
]

// Screens that show the left sidebar (all sub-screens too, since sidebar shows through).
var LEFT_NAV_SCREENS = {
  hensei:true, supply:true, equipment:true, equipment_other:true,
  repair:true, factory:true,
  repair_ship_select:true, repair_ship_confirm:true,
  hensei_ship_select:true, hensei_ship_confirm:true,
  equipment_select:true, equipment_confirm:true,
}

function inferNavClick(rx, ry, currentScreen) {
  // Screen-specific buttons (circle or rect)
  var buttons = NAV_MAP[currentScreen] || []
  for (var i = 0; i < buttons.length; i++) {
    var b = buttons[i]
    if (b.r != null) {
      var dx = rx - b.cx, dy = ry - b.cy
      if (dx*dx + dy*dy <= b.r*b.r) return b.to
    } else {
      if (Math.abs(rx - b.cx) <= b.hw && Math.abs(ry - b.cy) <= b.hh) return b.to
    }
  }
  // Left sidebar (present on all menu screens)
  if (LEFT_NAV_SCREENS[currentScreen]) {
    for (var j = 0; j < LEFT_NAV_HITS.length; j++) {
      var n = LEFT_NAV_HITS[j]
      if (Math.abs(rx - n.cx) <= n.hw && Math.abs(ry - n.cy) <= n.hh) return n.to
    }
  }
  return null
}

// ── Click hook injection code (runs inside KC2 frame) ─────────────────────────
var _CLICK_HOOK_CODE = '(function(){'
  + 'if(window._kckit_click_hooked) return {already:true};'
  + 'var cv=document.querySelector("canvas");'
  + 'if(!cv) return {err:"no canvas"};'
  + 'cv.addEventListener("pointerdown",function(e){'
  // Use CSS display size (getBoundingClientRect), NOT cv.width (internal pixel resolution).
  // KC2 canvas is 1200×720 internally but displayed at 889×533 — using cv.width gives
  // systematically wrong fractions (offsetX/1200 instead of offsetX/889).
  + '  var rect=cv.getBoundingClientRect();'
  + '  var w=rect.width||cv.width||1200,h=rect.height||cv.height||720;'
  + '  window._kckit_last_click={rx:e.offsetX/w,ry:e.offsetY/h,ts:Date.now(),cw:Math.round(w),ch:Math.round(h)};'
  + '},{passive:true,capture:true});'
  + 'window._kckit_click_hooked=true;'
  + 'return {ok:true,cvWidth:cv.width,cvHeight:cv.height};'
  + '})()'

// Map KC2 hash fragments → screen names (updated after probe discovers real values)
var KC_HASH_SCREEN = {
  '/port':          'port',
  '/hensei':        'hensei',
  '/supply':        'supply',
  '/nyukyo':        'repair',
  '/kousyou':       'factory',
  '/kaisou':        'equipment',
  '/quest':         'quest_list',
  '/exercise':      'practice',
  '/mission':       'expedition_select',
  '/sortie':        'sortie_world',
  '/formation':     'formation_select',
}

// Map KC2 API path (short, after /kcsapi/) → screen name.
// Used by spy poll to name a canvas transition when a recent API call correlates with it.
var KC2_API_SCREEN = {
  'api_port/port':                          'port',
  'api_get_member/deck':                    'hensei',
  'api_get_member/ship2':                   'supply',
  'api_get_member/ndock':                   'repair',
  'api_get_member/kdock':                   'factory',
  'api_get_member/questlist':               'quest_list',
  'api_get_member/mapinfo':                 'sortie_world',
  'api_get_member/practice':                'practice',
  'api_get_member/mission':                 'expedition_select',
  'api_req_member/get_practice_enemyinfo':  'practice',
  'api_req_practice/battle':                'battle',
  'api_req_practice/midnight_battle':       'night_battle',
  'api_req_practice/battle_result':         'battle_result',
  'api_req_sortie/battle':                  'battle',
  'api_req_sortie/battleresult':            'battle_result',
  'api_req_map/start':                      'formation_select',
  'api_req_map/next':                       'formation_select',
  'api_req_battle_midnight/battle':         'night_battle',
  'api_req_battle_midnight/sp_midnight':    'night_battle',
  'api_req_combined_battle/battle':         'battle',
  'api_req_combined_battle/each_battle':    'battle',
  'api_req_combined_battle/battleresult':   'battle_result',
  'api_req_combined_battle/midnight_battle':'night_battle',
  'api_req_combined_battle/sp_midnight':    'night_battle',
  'api_req_nyukyo/start':                   'repair',
  'api_req_nyukyo/speedchange':             'repair',
  'api_req_hokyu/charge':                   'supply',
  'api_req_kousyou/createship':             'factory',
  'api_req_kousyou/getship':                'construction_result',
  'api_req_kousyou/createitem':             'factory',
  'api_req_kaisou/powerup':                 'modernize_result',
  'api_req_kaisou/remodel_slot':            'equipment',
  'api_req_hensei/change':                  'hensei',
  'api_req_hensei/preset_select':           'hensei',
  'api_req_mission/start':                  'expedition_select',
  'api_req_mission/result':                 'expedition_result',
  'api_req_quest/clearitemget':             'quest_list',
  'api_req_quest/start':                    'quest_list',
  // 編成 preset action (fires on preset selection within 編成 screen)
  'api_get_member/preset_deck':             'hensei',
  'api_req_hensei/preset_select':           'hensei',
  // Excluded: preset_dev_items, can_preset_slot_select — background loads that fire
  // at unpredictable times and cause false equipment/factory misidentification.
}

function getWebContents() {
  var remote = window.remote || (window.require && window.require('@electron/remote'))
  var wv = document.querySelector('webview')
  if (!wv || !remote) return null
  var wcId = wv.getWebContentsId ? wv.getWebContentsId() : null
  return wcId ? remote.webContents.fromId(wcId) : null
}

// Execute JS in the DMM portal context (outer webview)
function execInDMM(code) {
  var wc = getWebContents()
  if (!wc) return Promise.resolve(null)
  return wc.executeJavaScript(code).catch(function(e) {
    console.error('[kckit-bridge] execInDMM error:', e)
    return null
  })
}

// Read KC2's current URL/hash/spy state by digging through game_frame
function getPageState() {
  return execInDMM(
    '(function() {'
    + 'var gf = document.querySelector("' + _GF + '");'
    + 'if (!gf) return { found: false, reason: "no game frame" };'
    + 'var kw; try { kw = gf.contentWindow; } catch(e) { return { found: false, reason: String(e) }; }'
    + 'if (!kw) return { found: false, reason: "contentWindow null" };'
    + 'var hash = "", href = "";'
    + 'try { hash = kw.location.hash; href = kw.location.href; } catch(e) {}'
    + 'var spyScreen = null, spyTs = null, spySrc = null;'
    + 'try { spyScreen = kw._kckit_screen || null; spyTs = kw._kckit_screen_ts || null; spySrc = kw._kckit_screen_source || null; } catch(e) {}'
    + 'return { found: true, hash: hash, href: href, spyScreen: spyScreen, spyTs: spyTs, spySrc: spySrc };'
    + '})()'
  )
}

// ── KC globals discovery ──────────────────────────────────────────────────────
// Reads KC2's globals, visible DOM elements, and all IDs.
// Run this in different screens to build the accurate hash→screen and id→screen maps.

function probeKcGlobals() {
  return execInDMM(
    '(function() {'
    + 'var gf = document.querySelector("' + _GF + '");'
    + 'if (!gf) return { kcFound: false, reason: "no game frame", dmmIds: [].concat.apply([], [document].map(function(d){var a=[];d.querySelectorAll("[id]").forEach(function(e){a.push(e.id)});return a;})) };'
    + 'var kw, kd;'
    + 'try { kw = gf.contentWindow; kd = gf.contentDocument; }'
    + 'catch(e) { return { kcFound: false, reason: "blocked: " + String(e), frameSrc: gf.src }; }'
    + 'if (!kw) return { kcFound: false, reason: "contentWindow null", frameSrc: gf.src };'
    + 'var hash="", href="";'
    + 'try { hash = kw.location.hash; href = kw.location.href; } catch(e) {}'
    // KC2 globals
    + 'var kcGlobals = {};'
    + 'try { Object.keys(kw).forEach(function(k) {'
    + '  if (!/^KC|^kcs|^Kc/i.test(k)) return;'
    + '  try { var v=kw[k], t=typeof v;'
    + '    kcGlobals[k] = (t==="object"&&v) ? {type:t,keys:Object.keys(v).slice(0,20)} : {type:t,value:String(v).slice(0,100)};'
    + '  } catch(e) {}'
    + '}); } catch(e) {}'
    // All elements with id, with visibility
    + 'var visibleDivs = [], allIds = [];'
    + 'try { if (kd) kd.querySelectorAll("[id]").forEach(function(el) {'
    + '  allIds.push(el.id);'
    + '  try { var s=kw.getComputedStyle(el); if(s.display!=="none"&&s.visibility!=="hidden") visibleDivs.push({id:el.id,tag:el.tagName,cls:el.className.slice(0,80)}); } catch(e) {}'
    + '}); } catch(e) {}'
    + 'var spyScreen=null; try{spyScreen=kw._kckit_screen||null;}catch(e){}'
    + 'return { kcFound:true, hash:hash, href:href, kcGlobals:kcGlobals, visibleDivs:visibleDivs, allIds:allIds, spyScreen:spyScreen };'
    + '})()'
  )
}

// ── Screen spy injection ──────────────────────────────────────────────────────
// Injects a <script> tag directly into KC2's document (inside #game_frame).
// Spy watches KC2's hash/DOM → sets kw._kckit_screen.
// Plugin polls kw._kckit_screen every 500ms and broadcasts screen_change events.

var _spyInjected = false
var _spyPollTimer = null
var _lastPolledScreen = null

// Spy code runs INSIDE KC2's JavaScript context via WebFrameMain (no cross-origin issues).
// KC2 is canvas-based (PixiJS), so DOM-based screen detection doesn't apply.
// Instead: hook PIXI.Container add/remove to detect scene transitions (timing),
// and pair that with API events (naming) for complete screen state tracking.
var _SPY_CODE = '(function() {'
  // Allow re-inject to refresh PIXI hooks (don't bail on already-active)
  + 'var _fresh = !window._kckit_spy_active;'

  // ── PIXI Container scene-transition detector ──────────────────────────────
  // Hooks addChild/removeChild to timestamp any canvas scene change.
  // KC2 uses PIXI v4; every screen transition involves adding/removing children.
  + 'if (typeof PIXI !== "undefined" && !PIXI.Container.prototype._kckit_hooked) {'
  + '  var _oA=PIXI.Container.prototype.addChild;'
  + '  var _oR=PIXI.Container.prototype.removeChild;'
  + '  PIXI.Container.prototype.addChild=function(){'
  + '    window._kckit_scene_ts=Date.now();'
  + '    return _oA.apply(this,arguments);'
  + '  };'
  + '  PIXI.Container.prototype.removeChild=function(){'
  + '    window._kckit_scene_ts=Date.now();'
  + '    return _oR.apply(this,arguments);'
  + '  };'
  + '  PIXI.Container.prototype._kckit_hooked=true;'
  + '}'

  // ── Performance API last-kcsapi-call tracker ─────────────────────────────
  // KC2's XHR is already patched by poi's xhr-hack, so API calls DO reach
  // the performance buffer. Reading it here gives us KC2's own view of the
  // last API called, independent of the outer frame's event delivery.
  + 'function _getLastApi() {'
  + '  try {'
  + '    var es=performance.getEntriesByType("resource");'
  + '    for(var i=es.length-1;i>=0;i--){'
  + '      if(es[i].name.indexOf("/kcsapi/")>=0){'
  + '        var p=es[i].name.split("/kcsapi/")[1];'
  + '        return p.split("?")[0];'
  + '      }'
  + '    }'
  + '  }catch(e){}'
  + '  return null;'
  + '}'
  // ── PIXI CanvasRenderer render hook ──────────────────────────────────────
  // KC2 uses Canvas renderer (not WebGL). Hook render() to capture the PIXI
  // stage structure immediately after a Container addChild/removeChild event.
  // This is the ONLY way to fingerprint canvas-internal transitions that have
  // no corresponding API call (e.g. port→sortie world when data is JS-cached).
  // The hook only snapshots the structure when scene_ts changed within the
  // last 800ms, so normal rendering frames are essentially free.
  // ── Hash-based screen detection (SPA / hash-router fallback) ─────────────
  + 'function detectScreen() {'
  + '  var h=location.hash||"";'
  + '  var hm={port:/\\/port/,hensei:/\\/hensei/,supply:/\\/supply/,'
  + '    repair:/\\/nyukyo/,factory:/\\/kousyou/,equipment:/\\/kaisou/,'
  + '    quest_list:/\\/quest/,practice:/\\/exercise/,'
  + '    expedition_select:/\\/mission/,sortie_world:/\\/sortie/,'
  + '    formation_select:/\\/formation/};'
  + '  for (var n in hm) { if(hm[n].test(h)) return n; }'
  + '  return null;'
  + '}'
  + 'function update(src) {'
  + '  var sc=detectScreen();'
  + '  if(sc&&sc!==window._kckit_screen){'
  + '    window._kckit_screen=sc;window._kckit_screen_ts=Date.now();window._kckit_screen_source=src;'
  + '  }'
  + '}'
  + 'window.addEventListener("hashchange",function(){update("hashchange");});'
  + 'if(_fresh) update("init");'
  + 'window._kckit_spy_active=true;'
  + 'window._kckit_scene_ts=window._kckit_scene_ts||Date.now();'
  + 'return { ok:true, already:!_fresh, initialScreen:window._kckit_screen,'
  + '  lastApi:window._kckit_last_api||null,'
  + '  pixi:typeof PIXI!=="undefined",'
  + '  pixiHooked:!!(typeof PIXI!=="undefined"&&PIXI.Container.prototype._kckit_hooked) };'
  + '})()'

// Inject WebGL render hook separately (not bundled in spy code to avoid
// the _fresh guard and ensure it always applies cleanly via direct executeJavaScript).
// Stable vis-pattern render hook.
// KC2 switches screens via container.visible toggling (not addChild/removeChild).
// We read container-1's children vis pattern on every frame, but only commit it
// as the "stable screen pattern" after it has persisted for STABLE_MS unchanged.
// This filters out transient animation frames that cause false positives.
var _WEBGL_HOOK_CODE = '(function(){'
  + 'if(typeof PIXI==="undefined"||!PIXI.WebGLRenderer) return {skip:"no_pixi"};'
  + 'var _o=PIXI.WebGLRenderer.prototype._kckit_orig||PIXI.WebGLRenderer.prototype.render;'
  + 'PIXI.WebGLRenderer.prototype._kckit_orig=_o;'
  + 'var _STABLE_MS=500;'
  + 'var _cand=null,_candTs=0;'
  + 'function c1Pat(root){'
  // Only process main-stage renders (no parent, no render-texture target)
  + '  if(root.parent) return null;'
  + '  var mc=root.children&&root.children[0];'
  + '  if(!mc||!mc.children) return null;'
  + '  var c=mc.children[1];'
  + '  if(!c||!c.children||c.children.length<2) return null;'
  // Level-2: c1 children vis (15 bits) — distinguishes port from menus from sortie
  + '  var p2="";'
  + '  for(var i=0;i<c.children.length&&i<20;i++) p2+=(c.children[i].visible?1:0);'
  // Level-3: visible c1-grandchildren vis — distinguishes supply/repair/equip/hensei
  + '  var p3="";'
  + '  for(var i=0;i<c.children.length&&i<20;i++){'
  + '    var gc=c.children[i];'
  + '    if(gc.visible&&gc.children&&gc.children.length>0){'
  + '      p3+=i+":";'
  + '      for(var j=0;j<gc.children.length&&j<8;j++) p3+=(gc.children[j].visible?1:0);'
  + '      p3+="|";'
  + '    }'
  + '  }'
  + '  return p2+(p3?"/"+p3:"");'
  + '}'
  + 'PIXI.WebGLRenderer.prototype.render=function(root,rt){'
  + '  if(!rt&&root&&root.children){'
  + '    var p=c1Pat(root),now=Date.now();'
  + '    if(p!==null){'
  + '      if(p!==_cand){_cand=p;_candTs=now;}'
  + '      else if(now-_candTs>=_STABLE_MS&&p!==window._kckit_c1_stable){'
  + '        window._kckit_c1_stable=p;'
  + '        window._kckit_c1_stable_ts=now;'
  + '      }'
  + '    }'
  + '  }'
  + '  return _o.apply(this,arguments);'
  + '};'
  + 'PIXI.WebGLRenderer.prototype._kckit_r=true;'
  + 'return {ok:true};'
  + '})()'

// ── Scene tree walk (perception v2) ──────────────────────────────────────────
// Captures the PIXI stage root via a render() hook, then exposes
// window._kckit_walk(maxNodes) which returns every visible node with its
// texture frame name, global bounds (renderer px) and PIXI.Text content.
// Same logic as tools/scene_dump.py, embedded so the plugin can broadcast
// the tree on every scene mutation without a Python round-trip.
// Bump when _SCENE_WALK_SETUP changes shape: stale in-frame walkers get
// replaced by the self-heal path in runTreeWalk (version mismatch → re-inject).
// v3: include alpha=0 interactive nodes — KC2 defines its REAL click regions
// as transparent sprites with interactive+buttonMode (verified on 入渠:
// dock click zones are alpha-0 repair_main_1 sprites, 252x72, exactly the
// hand-calibrated nameplate areas). Visible sprites are decoration;
// the interaction layer is invisible.
// v4: capture mask bounds (m:[x,y,w,h]) — scroll lists / sliding banners are
// clipped by PIXI masks in-game; without them the recon draws every off-window
// item on top of everything (e.g. 編成 ship-banner carousels).
// v5: raw (non-atlas) textures export the full /kcs2/... pathname so the
// client can load them through the simulator's resource proxy; Text nodes
// export fill/stroke colors + font size so recon text is readable.
// v6: in-page PUSH AGENT — the KC2 frame opens its own WebSocket to
// 127.0.0.1:23456 and pushes scene trees + clicks directly (verified: no CSP
// block, localhost exempt from mixed-content). The data plane thereby leaves
// @electron/remote entirely; remote remains for low-frequency control only
// (clicks via sendInputEvent, screenshots, self-heal injection).
var _WALK_VERSION = 6

var _SCENE_WALK_SETUP = '(function(){'
  + 'if (typeof PIXI === "undefined") return {skip:"no_pixi"};'
  + 'function hookCap(cls){'
  + '  if(!cls||!cls.prototype||cls.prototype._kckit_rootcap) return false;'
  + '  var o=cls.prototype.render;'
  + '  cls.prototype.render=function(root,rt){'
  + '    if(!rt&&root&&!root.parent){window._kckit_root=root;window._kckit_renderer=this;}'
  + '    return o.apply(this,arguments);'
  + '  };'
  + '  cls.prototype._kckit_rootcap=true;return true;'
  + '}'
  + 'hookCap(PIXI.WebGLRenderer);hookCap(PIXI.CanvasRenderer);'
  + 'window._kckit_walk=function(maxNodes){'
  + '  var root=window._kckit_root,rd=window._kckit_renderer;'
  + '  if(!root) return null;'
  + '  var RW=(rd&&rd.width)||1200,RH=(rd&&rd.height)||720;'
  + '  var MAX=maxNodes||800,out=[],trunc=false;'
  + '  function texId(n){'
  + '    try{var t=n._texture||n.texture;if(!t)return null;'
  + '    var ids=t.textureCacheIds||[];'
  + '    for(var i=0;i<ids.length;i++){if(ids[i]&&ids[i].length<80)return ids[i];}'
  + '    if(t.baseTexture&&t.baseTexture.imageUrl){'
  + '      var u=t.baseTexture.imageUrl;'
  + '      var k=u.indexOf("/kcs2/");'
  + '      if(k>=0)return "@"+u.slice(k).split("?")[0];'
  + '      return "@"+u.split("/").slice(-2).join("/").split("?")[0];'
  + '    }'
  + '    return null;}catch(e){return null;}'
  + '  }'
  + '  function walk(n,path,depth){'
  + '    if(out.length>=MAX){trunc=true;return;}'
  + '    if(!n.visible)return;'
  // alpha=0 nodes are normally skipped EXCEPT interactive ones — those are
  // the game-defined click regions (invisible by design).
  + '    var inter=!!n.interactive;'
  + '    if((n.alpha===0||n.renderable===false)&&!inter)return;'
  + '    try{var b=n.getBounds();'
  + '    if(b.width>=1&&b.height>=1&&b.x<RW&&b.y<RH&&b.x+b.width>0&&b.y+b.height>0){'
  + '      var e={p:path,t:texId(n),x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.round(b.height)};'
  + '      if(inter){e.i=1;if(n.buttonMode)e.btn=1;}'
  + '      if(n.alpha<0.99)e.a=Math.round(n.alpha*100)/100;'
  + '      if(typeof n.text==="string"&&n.text){'
  + '        e.txt=n.text.slice(0,60);'
  + '        try{var st=n.style||n._style;'
  + '        if(st){'
  + '          var fl=st.fill;if(Array.isArray(fl))fl=fl[0];'
  + '          if(fl!==undefined&&fl!==null)e.fc=String(fl);'
  + '          if(st.stroke&&st.strokeThickness)e.sc=String(st.stroke);'
  + '          var fz=parseInt(st.fontSize);if(fz>0)e.fs=fz;'
  + '        }}catch(e4){}'
  + '      }'
  + '      var mk=n._mask||n.mask;'
  + '      if(mk&&mk.getBounds){try{var mb=mk.getBounds();'
  + '        e.m=[Math.round(mb.x),Math.round(mb.y),Math.round(mb.width),Math.round(mb.height)];'
  + '      }catch(e2){}}'
  // Sprite quad corners in world space (TL,TR,BR,BL) — lets the client draw
  // rotated/scaled sprites exactly instead of stretching into the AABB.
  + '      if(n.vertexData&&n.vertexData.length===8){'
  + '        var vd=n.vertexData,v=[];'
  + '        for(var k=0;k<8;k++)v.push(Math.round(vd[k]*10)/10);'
  + '        e.v=v;'
  + '      }'
  + '      out.push(e);'
  + '    }}catch(err){}'
  + '    if(depth<12&&n.children){for(var i=0;i<n.children.length;i++)walk(n.children[i],path+"."+i,depth+1);}'
  + '  }'
  + '  walk(root,"r",0);'
  + '  return {rw:RW,rh:RH,count:out.length,truncated:trunc,nodes:out,ts:Date.now()};'
  + '};'
  + 'window._kckit_walk_version=' + _WALK_VERSION + ';'

  // ── In-page push agent ────────────────────────────────────────────────
  // Owns the data plane: detects scene changes locally (same triggers the
  // remote poll used: scene_ts, c1_stable_ts, last_click, 10s idle), walks
  // the tree IN-PAGE and pushes over its own WebSocket. Zero remote traffic.
  + 'if (!window._kckit_push) window._kckit_push = (function(){'
  + '  var ws=null,lastSceneTs=0,lastC1Ts=0,lastClickTs=0,lastWalkTs=0;'
  + '  var lastDigest="",settle=0,walkTimer=null;'
  + '  function connect(){'
  + '    if(ws&&(ws.readyState===0||ws.readyState===1))return;'
  + '    try{ws=new WebSocket("ws://127.0.0.1:23456");}catch(e){return;}'
  + '    ws.onopen=function(){ws.send(JSON.stringify({cmd:"kc2_hello"}));pushWalk();};'
  + '    ws.onclose=function(){setTimeout(connect,3000);};'
  + '    ws.onerror=function(){};'
  + '  }'
  + 'function digest(nodes){'
  + '    var p=[];'
  + '    for(var i=0;i<nodes.length;i++){var n=nodes[i];if(n.t)p.push(n.t+":"+(n.x>>3)+":"+(n.y>>3));}'
  + '    return p.join("|");'
  + '  }'
  + '  function pushWalk(){'
  + '    if(!ws||ws.readyState!==1||!window._kckit_walk)return;'
  + '    var r=window._kckit_walk(800);'
  + '    if(!r)return;'
  + '    lastWalkTs=Date.now();'
  + '    try{ws.send(JSON.stringify({cmd:"kc2_scene_tree",payload:r}));}catch(e){return;}'
  + '    var d=digest(r.nodes);'
  + '    if(d!==lastDigest){lastDigest=d;'
  + '      if(settle<2){settle++;setTimeout(pushWalk,450);}'
  + '    } else settle=0;'
  + '  }'
  + '  setInterval(function(){'
  + '    var st=window._kckit_scene_ts||0,c1=window._kckit_c1_stable_ts||0;'
  + '    var lc=window._kckit_last_click;'
  + '    if(lc&&lc.ts&&lc.ts!==lastClickTs){lastClickTs=lc.ts;'
  + '      if(ws&&ws.readyState===1)ws.send(JSON.stringify({cmd:"kc2_click",rx:lc.rx,ry:lc.ry}));'
  + '    }'
  + '    var trig=false;'
  + '    if(st&&st!==lastSceneTs){lastSceneTs=st;trig=true;}'
  + '    if(c1&&c1!==lastC1Ts){lastC1Ts=c1;trig=true;}'
  + '    if(trig){settle=0;'
  + '      if(walkTimer)clearTimeout(walkTimer);'
  + '      var wait=Math.max(180,700-(Date.now()-lastWalkTs));'
  + '      walkTimer=setTimeout(function(){walkTimer=null;pushWalk();},wait);'
  + '    } else if(Date.now()-lastWalkTs>10000) pushWalk();'
  + '  },300);'
  + '  connect();'
  + '  return {connect:connect};'
  + '})();'

  + 'return {ok:true};'
  + '})()'

// Throttled tree broadcast: at most one walk per _TREE_MIN_MS, with a short
// settle delay so we capture the post-transition tree, not mid-animation.
var _TREE_MIN_MS = 800
var _treeTimer = null
var _treeLastWalk = 0
var _treeInFlightTs = 0   // timestamp guard: auto-expires so a hung walk can't starve broadcasts

// Settle detection: scene_ts fires at the FIRST frame of a transition, but
// slide/fade-in animations move elements for ~300–500ms after that. After
// each broadcast we compare a coarse layout digest (texture + position/8px);
// if it changed, re-walk after 450ms (max 2 retries) so the recon view
// converges on the settled layout instead of freezing mid-animation.
// Position rounding ignores idle bobbing; text contents are excluded so
// ticking clocks/timers don't look like motion.
var _lastTreeDigest = ''
var _settleRetries = 0

function _treeDigest(nodes) {
  var parts = []
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i]
    if (n.t) parts.push(n.t + ':' + (n.x >> 3) + ':' + (n.y >> 3))
  }
  return parts.join('|')
}

function scheduleTreeWalk() {
  if (_treeTimer) return
  _settleRetries = 0   // new trigger → fresh settle budget
  var wait = Math.max(150, _TREE_MIN_MS - (Date.now() - _treeLastWalk))
  _treeTimer = setTimeout(function() {
    _treeTimer = null
    runTreeWalk(null)
  }, wait)
}

// Walks the tree in the KC2 frame and broadcasts (or replies to one client).
function runTreeWalk(replyWs) {
  if (Date.now() - _treeInFlightTs < 6000 && !replyWs) return
  var wc = getWebContents()
  var frame = wc ? findKC2Frame(wc) : null
  if (!frame) {
    if (replyWs) replyWs.send(JSON.stringify({ type: 'scene_tree', payload: null }))
    return
  }
  _treeInFlightTs = Date.now()
  _treeLastWalk = Date.now()
  // JSON.stringify INSIDE the frame: the result crosses @electron/remote as
  // ONE string instead of thousands of proxied objects. Without this, every
  // walk leaks ~node-count entries into remote's registry Map, which
  // overflows after long sessions ("Map maximum size exceeded" — kills ALL
  // remote calls until poi restarts).
  var WALK_CALL = 'JSON.stringify((window._kckit_walk && window._kckit_walk_version >= '
    + _WALK_VERSION + ') ? window._kckit_walk(800) : null)'
  function parseWalk(s) {
    try { return (typeof s === 'string' && s) ? JSON.parse(s) : null }
    catch (e) { return null }
  }
  frame.executeJavaScript(WALK_CALL)
    .then(function(s) {
      var r = parseWalk(s)
      if (r !== null) return r
      // Walker missing (game reloaded, or spy used the script-tag fallback
      // which can't install it) — self-heal: inject setup, let a frame
      // render so the root gets captured, then retry once.
      return frame.executeJavaScript(_SCENE_WALK_SETUP)
        .then(function() {
          return new Promise(function(res) { setTimeout(res, 250) })
        })
        .then(function() {
          return frame.executeJavaScript(WALK_CALL)
        })
        .then(parseWalk)
    })
    .then(function(r) {
      _treeInFlightTs = 0
      var msg = { type: 'scene_tree', payload: (r && r.nodes) ? r : null }
      if (replyWs) replyWs.send(JSON.stringify(msg))
      else if (msg.payload) broadcast(msg)
      // settle loop: layout still moving → walk again shortly
      if (!replyWs && msg.payload) {
        var dig = _treeDigest(r.nodes)
        var moved = dig !== _lastTreeDigest
        _lastTreeDigest = dig
        if (moved && _settleRetries < 2) {
          _settleRetries++
          setTimeout(function() { runTreeWalk(null) }, 450)
        }
      }
    })
    .catch(function() { _treeInFlightTs = 0 })
}

function injectWebGLHook(frame) {
  if (!frame) return Promise.resolve({ skip: 'no_frame' })
  return frame.executeJavaScript(_WEBGL_HOOK_CODE).then(function(r) {
    if (r && r.ok) console.log('[kckit-bridge] WebGL render hook applied')
    else console.log('[kckit-bridge] WebGL hook:', JSON.stringify(r))
    return r
  }).catch(function(e) {
    console.warn('[kckit-bridge] WebGL hook failed:', e)
    return { error: String(e) }
  })
}

function injectScreenSpy() {
  var wc = getWebContents()
  if (!wc) return Promise.resolve({ ok: false, reason: 'no webContents' })

  // Try Electron WebFrameMain API first — bypasses cross-origin restrictions
  var kc2Frame = findKC2Frame(wc)
  if (kc2Frame) {
    return kc2Frame.executeJavaScript(_SPY_CODE).then(function(result) {
      if (result && result.ok) {
        _spyInjected = true
        startSpyPoll()
        console.log('[kckit-bridge] spy injected via WebFrameMain, initial:', result.initialScreen)
        // Apply WebGL render hook (stable vis-pattern detection)
        injectWebGLHook(kc2Frame)
        // Install scene-tree walker (root capture + window._kckit_walk)
        kc2Frame.executeJavaScript(_SCENE_WALK_SETUP).then(function(r) {
          console.log('[kckit-bridge] scene walk setup:', JSON.stringify(r))
        }).catch(function(e) {
          console.warn('[kckit-bridge] scene walk setup failed:', e)
        })
        // Inject click hook (canvas pointerdown → operation recording + nav inference)
        kc2Frame.executeJavaScript(_CLICK_HOOK_CODE).then(function(r) {
          console.log('[kckit-bridge] click hook:', JSON.stringify(r))
          broadcast({ type: 'click_hook_result', payload: r || {} })
        }).catch(function(e) {
          console.warn('[kckit-bridge] click hook failed:', e)
          broadcast({ type: 'click_hook_result', payload: { error: String(e) } })
        })
      }
      return result || { ok: false }
    }).catch(function(e) {
      console.warn('[kckit-bridge] WebFrameMain spy failed:', e, '— falling back to script tag')
      return _injectSpyViaScriptTag()
    })
  }

  // Fallback: inject <script> tag via gadget container context
  return _injectSpyViaScriptTag()
}

function _injectSpyViaScriptTag() {
  return execInDMM(
    '(function() {'
    + 'var gf = document.querySelector("' + _GF + '");'
    + 'if (!gf) return { ok:false, reason:"no game frame" };'
    + 'var kw, kd;'
    + 'try { kw=gf.contentWindow; kd=gf.contentDocument; }'
    + 'catch(e) { return { ok:false, reason:"access blocked: "+String(e) }; }'
    + 'if (!kw||!kd) return { ok:false, reason:"KC2 window/doc null" };'
    + 'try {'
    + '  var s=kd.createElement("script"); s.id="kckit-spy";'
    + '  s.textContent=' + JSON.stringify(_SPY_CODE) + ';'
    + '  (kd.head||kd.body||kd.documentElement).appendChild(s);'
    + '  return { ok:true, initialScreen:kw._kckit_screen||null };'
    + '} catch(e) { return { ok:false, reason:"script inject: "+String(e) }; }'
    + '})()'
  ).then(function(result) {
    if (result && result.ok) {
      _spyInjected = true
      startSpyPoll()
    }
    return result || { ok: false, reason: 'null result' }
  })
}

var _lastSceneTs = 0
var _lastC1StableTs = 0
var _lastClickTs = 0

// Poll KC2's spy state every 500ms.
// Detectors:
//   1. hash-based screen name (hash-router fallback, KC2 doesn't actually use)
//   2. PIXI scene_ts → screenshot_needed (addChild/removeChild signal)
//   3. _kckit_c1_stable_ts → pixi_stage with stable vis fingerprint (screen detection)
function startSpyPoll() {
  if (_spyPollTimer) return
  _spyPollTimer = setInterval(function() {
    // Push agent alive → data plane is in-page; skip ALL remote polling
    // (this poll was the @electron/remote registry-leak driver).
    if (pushAgentFresh()) return
    var wc = getWebContents()
    if (!wc) return
    var frame = findKC2Frame(wc)
    if (!frame) return

    // Health sentinel: executeJavaScript HANGS (never settles) when
    // @electron/remote's registry overflows — race a timeout and broadcast
    // a health alert after sustained failures so the UI shows "restart poi"
    // instead of silently going black.
    var pollPromise =
    // stringified to keep @electron/remote's registry from growing (see
    // runTreeWalk) — this poll runs every 500ms for the whole session
    frame.executeJavaScript(
      'JSON.stringify({ screen: (typeof _kckit_screen!=="undefined"?_kckit_screen:null),'
      + '  source: (typeof _kckit_screen_source!=="undefined"?_kckit_screen_source:null),'
      + '  scene_ts: (typeof _kckit_scene_ts!=="undefined"?_kckit_scene_ts:0),'
      + '  c1_stable_ts: (typeof _kckit_c1_stable_ts!=="undefined"?_kckit_c1_stable_ts:0),'
      + '  c1_stable: (typeof _kckit_c1_stable!=="undefined"?_kckit_c1_stable:null),'
      + '  last_click: (typeof _kckit_last_click!=="undefined"?_kckit_last_click:null) })'
    ).then(function(rs) {
      var r = null
      try { r = rs ? JSON.parse(rs) : null } catch (e) {}
      if (!r) return

      // ① Canvas click → navigation inference + raw broadcast for server-side state machine
      if (r.last_click && r.last_click.ts && r.last_click.ts !== _lastClickTs) {
        _lastClickTs = r.last_click.ts
        // Raw click → server does element lookup + full state-machine transition
        broadcast({ type: 'canvas_click', rx: r.last_click.rx, ry: r.last_click.ry,
                    screen: _lastPolledScreen })
        // Immediate local inference for port wheel / sortie_type (no server roundtrip needed)
        var navTarget = inferNavClick(r.last_click.rx, r.last_click.ry, _lastPolledScreen)
        if (navTarget && navTarget !== _lastPolledScreen) {
          _lastPolledScreen = navTarget
          broadcast({ type: 'screen_change', screen: navTarget, source: 'click_nav' })
        }
      }

      // ② Hash-based screen from spy (KC2 hash router — catches cache-miss transitions)
      if (r.screen && r.screen !== _lastPolledScreen) {
        _lastPolledScreen = r.screen
        broadcast({ type: 'screen_change', screen: r.screen, source: r.source || 'spy_poll' })
        broadcast({ type: 'state', payload: buildState() })
      }

      // ③ PIXI addChild/removeChild → screenshot refresh hint + scene tree broadcast
      if (r.scene_ts && r.scene_ts !== _lastSceneTs && r.scene_ts > 0) {
        _lastSceneTs = r.scene_ts
        broadcast({ type: 'screenshot_needed', reason: 'pixi_scene_change', ts: r.scene_ts })
        scheduleTreeWalk()
      }

      // ④ Stable vis-pattern changed — KC2 switches many screens by toggling
      // container.visible (no addChild → scene_ts doesn't move), so this is
      // a REQUIRED scene_tree trigger, not just a fingerprint event.
      if (r.c1_stable_ts && r.c1_stable_ts !== _lastC1StableTs) {
        _lastC1StableTs = r.c1_stable_ts
        broadcast({ type: 'pixi_stage', ts: r.c1_stable_ts, sig: r.c1_stable || '' })
        scheduleTreeWalk()
      }

      // ⑤ Fallback: animations and in-place updates (HP bars, timers) change
      // the tree without firing ①–④. Re-walk at most every 10s when idle so
      // the recon view can never freeze even if all hooks are lost.
      if (Date.now() - _treeLastWalk > 10000) scheduleTreeWalk()
    }).catch(function() {})

    // Sentinel: if the poll promise doesn't settle within 4s, the remote
    // channel is likely wedged (registry overflow). After 5 consecutive
    // timeouts broadcast a health alert (throttled to one per 5 min).
    var settled = false
    pollPromise.then(function() { settled = true; _remoteTimeouts = 0 },
                     function() { settled = true })
    setTimeout(function() {
      if (settled) return
      _remoteTimeouts++
      if (_remoteTimeouts >= 5 && Date.now() - _lastHealthAlert > 300000) {
        _lastHealthAlert = Date.now()
        broadcast({ type: 'health', ok: false, reason: 'remote_channel_dead',
                    hint: 'executeJavaScript hangs — @electron/remote registry likely overflowed; RESTART POI (Cmd+Q, not page reload)' })
        console.error('[kckit-bridge] HEALTH: remote channel dead — restart poi')
      }
    }, 4000)
  }, 500)
}

var _remoteTimeouts = 0
var _lastHealthAlert = 0

function stopSpyPoll() {
  if (_spyPollTimer) { clearInterval(_spyPollTimer); _spyPollTimer = null; }
}

function getSpyScreen() {
  return execInDMM(
    '(function() {'
    + 'var gf=document.querySelector("' + _GF + '");'
    + 'if (!gf) return null;'
    + 'try { var w=gf.contentWindow; return { screen:w._kckit_screen||null, ts:w._kckit_screen_ts||null, source:w._kckit_screen_source||null }; }'
    + 'catch(e) { return null; }'
    + '})()'
  )
}

// ── KC2 direct frame access via Electron WebFrameMain API ────────────────────
// Electron's frame API lets us execute JS in any sub-frame without cross-origin
// restrictions — this is the privileged Node.js level, not in-page JS.

function findKC2Frame(wc) {
  // Walk the frame tree to find the KC2 GAME frame specifically (/kcs2/ path).
  // There are multiple kancolle-server.com frames; we want the one with the game.
  function score(url) {
    if (!url) return 0
    if (url.includes('/kcs2/')) return 3      // kcs2 = game (highest priority)
    if (url.includes('/kcs/'))  return 2      // kcs = legacy game
    try {
      var h = new URL(url).hostname
      if (/kancolle-server\.com$/.test(h)) return 1  // other KC frames
    } catch(e) {}
    return 0
  }
  function walk(frame, depth) {
    if (!frame || depth > 8) return []
    var results = []
    try {
      var s = score(frame.url)
      if (s > 0) results.push({ frame: frame, score: s })
      ;(frame.frames || []).forEach(function(f) {
        results = results.concat(walk(f, depth + 1))
      })
    } catch(e) {}
    return results
  }
  try {
    var candidates = walk(wc.mainFrame, 0)
    if (!candidates.length) return null
    candidates.sort(function(a, b) { return b.score - a.score })
    return candidates[0].frame
  } catch(e) { return null }
}

function executeInKC2Frame(code) {
  var wc = getWebContents()
  if (!wc) return Promise.resolve({ error: 'no webContents' })
  var frame = findKC2Frame(wc)
  if (!frame) return Promise.resolve({ error: 'KC2 frame not found', allFrames: getAllFrameUrls(wc) })
  return frame.executeJavaScript(code).catch(function(e) {
    return { error: String(e) }
  })
}

function getAllFrameUrls(wc) {
  var urls = []
  function walk(frame, depth) {
    if (!frame || depth > 5) return
    try { urls.push({ url: frame.url, depth: depth }) } catch(e) {}
    try { (frame.frames || []).forEach(function(f) { walk(f, depth + 1) }) } catch(e) {}
  }
  try { walk(wc.mainFrame, 0) } catch(e) {}
  return urls
}

// ── Resource cache path ───────────────────────────────────────────────────────
// Returns the filesystem path where poi stores KC game assets (kcs2/ images, JS).
// Python side can use this for pixel-perfect template matching from local files.
function getResourcePath() {
  var remote = window.remote || (window.require && window.require('@electron/remote'))
  if (!remote) return null
  try {
    var config = remote.require('./lib/config')
    var nodePath = remote.require('path')
    var os = remote.require('os')
    var defaultCache = nodePath.join(remote.app.getPath('appData'), 'poi', 'MyCache')
    var cachePath = config.get('poi.misc.cache.path', defaultCache)
    return {
      cache_root: cachePath,
      kc_root: nodePath.join(cachePath, 'KanColle'),
      kcs2: nodePath.join(cachePath, 'KanColle', 'kcs2'),
      kcs:  nodePath.join(cachePath, 'KanColle', 'kcs'),
    }
  } catch(e) {
    return { error: String(e) }
  }
}

var _navChangeHandler = null

function hashToScreen(hash) {
  var screen = null
  Object.keys(KC_HASH_SCREEN).forEach(function(pattern) {
    if (hash.indexOf(pattern) === 0) screen = KC_HASH_SCREEN[pattern]
  })
  return screen
}

function attachNavWatcher() {
  // Watch outer webview (DMM portal) navigation — catches full-page loads
  var wv = document.querySelector('webview')
  if (wv) {
    _navChangeHandler = function(e) {
      var url = e.url || ''
      var hashMatch = url.match(/#(.*)$/)
      var hash = hashMatch ? '/' + hashMatch[1].replace(/^\//, '') : ''
      var screen = hashToScreen(hash)
      if (screen) {
        broadcast({ type: 'screen_change', screen: screen, source: 'navigation', url: url })
      } else if (hash) {
        broadcast({ type: 'screen_change', screen: null, source: 'navigation', url: url, hash: hash })
      }
    }
    wv.addEventListener('did-navigate-in-page', _navChangeHandler)
  }

  // Also inject a hashchange listener directly into KC2's game_frame context.
  // KC2 navigation (補給→改装 etc.) changes the hash inside the iframe without
  // triggering did-navigate-in-page on the outer webview.
  execInDMM(
    '(function() {'
    + 'var gf=document.querySelector("' + _GF + '");'
    + 'if (!gf) return;'
    + 'try {'
    + '  var kw=gf.contentWindow;'
    + '  if (!kw||kw._kckit_nav_watcher) return;'
    // Store current hash on DMM window so poll can detect changes
    + '  function onHash() {'
    + '    var h=kw.location.hash||"";'
    + '    window._kckit_kc_hash=h; window._kckit_kc_hash_ts=Date.now();'
    + '  }'
    + '  kw.addEventListener("hashchange", onHash);'
    + '  kw._kckit_nav_watcher=true;'
    + '  onHash();'
    + '} catch(e) {}'
    + '})()'
  ).then(function() {
    // Poll KC2's hash independently for screen detection fallback
    var _lastHash = null
    setInterval(function() {
      execInDMM('window._kckit_kc_hash||null').then(function(hash) {
        if (!hash || hash === _lastHash) return
        _lastHash = hash
        var h = '/' + hash.replace(/^#?\/?/, '')
        var screen = hashToScreen(h)
        if (screen && screen !== _lastPolledScreen) {
          broadcast({ type: 'screen_change', screen: screen, source: 'hashchange' })
        }
      })
    }, 600)
  })

  console.log('[kckit-bridge] nav watcher attached (outer webview + KC2 frame)')
}

function handleCommand(ws, msg) {
  try {
    const cmd = JSON.parse(msg)
    if (cmd.cmd && cmd.cmd.indexOf('kc2_') === 0 && handleKc2Push(ws, cmd)) return
    if (cmd.cmd === 'get_state') {
      ws.send(JSON.stringify({ type: 'state', payload: buildState() }))
    } else if (cmd.cmd === 'get_canvas_info') {
      ws.send(JSON.stringify({ type: 'canvas_info', payload: getCanvasInfo() }))
    } else if (cmd.cmd === 'get_page_state') {
      getPageState().then(function(info) {
        ws.send(JSON.stringify({ type: 'page_state', payload: info }))
      })
    } else if (cmd.cmd === 'probe_kc_globals') {
      // Dump KC globals + DOM structure from webview for screen mapping discovery
      probeKcGlobals().then(function(info) {
        ws.send(JSON.stringify({ type: 'kc_globals', payload: info }))
      })
    } else if (cmd.cmd === 'inject_screen_spy') {
      // Install persistent DOM observer in KC webview
      injectScreenSpy().then(function(result) {
        ws.send(JSON.stringify({ type: 'spy_result', payload: result }))
      })
    } else if (cmd.cmd === 'get_spy_screen') {
      // Fast poll: read _kckit_screen global set by the spy
      getSpyScreen().then(function(result) {
        ws.send(JSON.stringify({ type: 'spy_screen', payload: result }))
      })
    } else if (cmd.cmd === 'get_resource_path') {
      ws.send(JSON.stringify({ type: 'resource_path', payload: getResourcePath() }))
    } else if (cmd.cmd === 'exec_kc2') {
      // Execute arbitrary JS in KC2's game frame (WebFrameMain, bypasses cross-origin)
      var wcE = getWebContents()
      var kc2f = wcE ? findKC2Frame(wcE) : null
      if (!kc2f) {
        ws.send(JSON.stringify({ type: 'exec_kc2_result', payload: { error: 'KC2 frame not found' } }))
      } else {
        kc2f.executeJavaScript(cmd.code || 'null').then(function(r) {
          ws.send(JSON.stringify({ type: 'exec_kc2_result', payload: { result: r } }))
        }).catch(function(e) {
          ws.send(JSON.stringify({ type: 'exec_kc2_result', payload: { error: String(e) } }))
        })
      }
    } else if (cmd.cmd === 'get_scene_tree') {
      // On-demand scene tree (frontend initial load / Python pull).
      // Serve the push agent's latest tree when fresh — no remote round-trip;
      // fall back to a remote walk otherwise (also self-heals the agent).
      if (_lastPushedTree && pushAgentFresh()) {
        ws.send(JSON.stringify({ type: 'scene_tree', payload: _lastPushedTree }))
      } else {
        runTreeWalk(ws)
      }
    } else if (cmd.cmd === 'list_frames') {
      // Dump the full WebFrameMain frame tree for debugging
      var wcF = getWebContents()
      var frames = wcF ? getAllFrameUrls(wcF) : []
      ws.send(JSON.stringify({ type: 'frame_list', payload: frames }))
    } else if (cmd.cmd === 'probe_kc2_frame') {
      // Probe KC2's actual game frame (Level 3) via Electron WebFrameMain API
      var wc2 = getWebContents()
      if (!wc2) {
        ws.send(JSON.stringify({ type: 'kc2_frame', payload: { error: 'no wc' } }))
      } else {
        var kc2Frame = findKC2Frame(wc2)
        if (!kc2Frame) {
          ws.send(JSON.stringify({ type: 'kc2_frame', payload: { error: 'KC2 frame not found', frames: getAllFrameUrls(wc2) } }))
        } else {
          kc2Frame.executeJavaScript(
            '(function() {'
            + 'var h=location.hash, href=location.href;'
            + 'var globals=Object.keys(window).filter(function(k){return/^KC|^kcs|Scene|Port|Ship/i.test(k);}).slice(0,30);'
            + 'var ids=[]; document.querySelectorAll("[id]").forEach(function(el){ids.push(el.id);});'
            + 'var vis=[]; document.querySelectorAll("[id]").forEach(function(el){'
            + '  var s=window.getComputedStyle(el); if(s.display!=="none"&&s.visibility!=="hidden") vis.push({id:el.id,tag:el.tagName,cls:el.className.slice(0,60)});'
            + '});'
            + 'return { href:href, hash:h, globals:globals, allIds:ids.slice(0,80), visibleIds:vis.slice(0,30) };'
            + '})()'
          ).then(function(result) {
            ws.send(JSON.stringify({ type: 'kc2_frame', payload: Object.assign({ frameUrl: kc2Frame.url }, result) }))
          }).catch(function(e) {
            ws.send(JSON.stringify({ type: 'kc2_frame', payload: { error: String(e), frameUrl: kc2Frame.url } }))
          })
        }
      }
    } else if (cmd.cmd === 'click_kc2') {
      // Virtual click on KC2 canvas at fractional (rx, ry) position [0-1].
      // Uses wc.sendInputEvent() — goes through Electron's normal rendering pipeline.
      // Coordinates are fractions of the KC2 game frame (same reference as screen_layout.yaml).
      var wc = getWebContents()
      if (!wc) {
        ws.send(JSON.stringify({ type: 'click_result', ok: false, err: 'no webContents' }))
        return
      }
      var clickRx = Number(cmd.rx) || 0
      var clickRy = Number(cmd.ry) || 0
      var clickLabel = cmd.label || ''

      // Get KC2 game frame position within the DMM portal page.
      // getBoundingClientRect() returns viewport-relative CSS pixel coords —
      // same coordinate space that sendInputEvent() expects.
      execInDMM(
        '(function(){'
        + 'var gf=document.querySelector("' + _GF + '");'
        + 'if(!gf)return null;'
        + 'var r=gf.getBoundingClientRect();'
        + 'return{l:r.left,t:r.top,w:r.width,h:r.height};'
        + '})()'
      ).then(function(pos) {
        if (!pos || !pos.w) {
          ws.send(JSON.stringify({ type: 'click_result', ok: false, err: 'KC2 game frame not found' }))
          return
        }
        // Convert fraction → DMM portal viewport CSS pixel coords
        var x = Math.round(pos.l + clickRx * pos.w)
        var y = Math.round(pos.t + clickRy * pos.h)
        // Human-like ±2px jitter
        var jx = x + Math.round((Math.random() - 0.5) * 4)
        var jy = y + Math.round((Math.random() - 0.5) * 4)
        try {
          wc.sendInputEvent({ type: 'mouseMove', x: jx, y: jy, modifiers: [] })
          setTimeout(function() {
            try {
              wc.sendInputEvent({ type: 'mouseDown', x: jx, y: jy, button: 'left', clickCount: 1, modifiers: [] })
              setTimeout(function() {
                try {
                  wc.sendInputEvent({ type: 'mouseUp', x: jx, y: jy, button: 'left', clickCount: 1, modifiers: [] })
                  ws.send(JSON.stringify({ type: 'click_result', ok: true,
                    rx: clickRx, ry: clickRy, x: jx, y: jy, label: clickLabel }))
                } catch(e) {
                  ws.send(JSON.stringify({ type: 'click_result', ok: false, err: 'mouseUp: ' + String(e) }))
                }
              }, 60 + Math.floor(Math.random() * 60))
            } catch(e) {
              ws.send(JSON.stringify({ type: 'click_result', ok: false, err: 'mouseDown: ' + String(e) }))
            }
          }, 100 + Math.floor(Math.random() * 80))
        } catch(e) {
          ws.send(JSON.stringify({ type: 'click_result', ok: false, err: 'mouseMove: ' + String(e) }))
        }
      })
    } else if (cmd.cmd === 'reload') {
      // Hot-reload this plugin from its file on disk — no poi UI interaction needed
      ws.send(JSON.stringify({ type: 'reload_ack', message: 'reloading…' }))
      setTimeout(selfReload, 80)  // tiny delay so ack is sent before WS closes
    } else {
      ws.send(JSON.stringify({ type: 'error', message: 'Unknown cmd: ' + cmd.cmd }))
    }
  } catch (e) {
    ws.send(JSON.stringify({ type: 'error', message: String(e) }))
  }
}

// ── Game events that trigger a state push ────────────────────────────────────

const PUSH_EVENTS = {
  // Port / general
  '/kcsapi/api_port/port': true,
  // Navigation events — fire on screen entry, update last_event in snapshot
  '/kcsapi/api_get_member/deck':     true,  // → hensei (編成)
  '/kcsapi/api_get_member/ship2':    true,  // → supply (補給)
  '/kcsapi/api_get_member/ndock':    true,  // → repair (入渠)
  '/kcsapi/api_get_member/kdock':    true,  // → factory (工廠)
  '/kcsapi/api_get_member/mapinfo':  true,  // → sortie world selection (出撃)
  '/kcsapi/api_get_member/practice': true,  // → practice/exercise (演習)
  '/kcsapi/api_get_member/mission':  true,  // → expedition selection (遠征)
  // Fleet composition
  '/kcsapi/api_req_hensei/change': true,
  '/kcsapi/api_req_hensei/preset_select': true,
  '/kcsapi/api_req_hensei/combined': true,
  // Supply
  '/kcsapi/api_req_hokyu/charge': true,
  // Equipment / modernize
  '/kcsapi/api_req_kaisou/powerup': true,
  '/kcsapi/api_req_kaisou/remodel_slot': true,
  '/kcsapi/api_req_kaisou/marriage': true,
  // Repair
  '/kcsapi/api_req_nyukyo/start': true,
  '/kcsapi/api_req_nyukyo/speedchange': true,
  // Factory — construction
  '/kcsapi/api_req_kousyou/createship': true,
  '/kcsapi/api_req_kousyou/getship': true,
  // Factory — development
  '/kcsapi/api_req_kousyou/createitem': true,
  // Expedition
  '/kcsapi/api_req_mission/start': true,
  '/kcsapi/api_req_mission/result': true,
  // Quests
  '/kcsapi/api_get_member/questlist': true,
  '/kcsapi/api_req_quest/clearitemget': true,
  // Practice
  '/kcsapi/api_req_member/get_practice_enemyinfo': true,
  '/kcsapi/api_req_practice/battle': true,
  '/kcsapi/api_req_practice/midnight_battle': true,
  '/kcsapi/api_req_practice/battle_result': true,
  // Sortie
  '/kcsapi/api_req_map/start': true,
  '/kcsapi/api_req_map/next': true,
  '/kcsapi/api_req_sortie/battle': true,
  '/kcsapi/api_req_sortie/battleresult': true,
  '/kcsapi/api_req_battle_midnight/battle': true,
  '/kcsapi/api_req_battle_midnight/sp_midnight': true,
  // Combined fleet sortie
  '/kcsapi/api_req_combined_battle/battle': true,
  '/kcsapi/api_req_combined_battle/each_battle': true,
  '/kcsapi/api_req_combined_battle/battleresult': true,
  '/kcsapi/api_req_combined_battle/midnight_battle': true,
  '/kcsapi/api_req_combined_battle/sp_midnight': true,
}

// ── Screenshot HTTP server ───────────────────────────────────────────────────

function captureGameView() {
  return new Promise(function(resolve, reject) {
    // The game runs inside a <webview> element in poi
    const wv = document.querySelector('webview')
    if (!wv) { reject(new Error('no webview found')); return }

    // webContentsId lets us get the webContents from the main process via remote
    const wcId = wv.getWebContentsId ? wv.getWebContentsId() : null
    const remote = window.remote || (window.require && window.require('@electron/remote'))
    if (!remote) { reject(new Error('electron remote not available')); return }

    const wc = wcId ? remote.webContents.fromId(wcId) : null
    if (!wc) { reject(new Error('webContents not found (id=' + wcId + ')')); return }

    wc.capturePage().then(function(img) {
      // Resize to 800×480 (the canonical game canvas size)
      const sized = img.resize({ width: 800, height: 480 })
      resolve(sized.toJPEG(82))
    }).catch(reject)
  })
}

function startScreenshotServer() {
  screenshotServer = http.createServer(function(req, res) {
    if (req.url !== '/screenshot') {
      res.writeHead(404); res.end(); return
    }
    captureGameView().then(function(jpegBuf) {
      res.writeHead(200, {
        'Content-Type': 'image/jpeg',
        'Content-Length': jpegBuf.length,
        'Cache-Control': 'no-cache, no-store',
      })
      res.end(jpegBuf)
    }).catch(function(err) {
      console.error('[kckit-bridge] screenshot error:', err)
      res.writeHead(503, { 'Content-Type': 'text/plain' })
      res.end('screenshot unavailable: ' + String(err.message || err))
    })
  })

  screenshotServer.listen(SCREENSHOT_PORT, '127.0.0.1', function() {
    console.log('[kckit-bridge] Screenshot server on http://127.0.0.1:' + SCREENSHOT_PORT + '/screenshot')
  })

  screenshotServer.on('error', function(err) {
    console.error('[kckit-bridge] Screenshot server error:', err)
  })
}

// ── Plugin lifecycle ─────────────────────────────────────────────────────────

function pluginDidLoad() {
  wss = new WebSocket.Server({ port: PORT, host: '127.0.0.1' })

  wss.on('listening', function() {
    console.log('[kckit-bridge] WebSocket server listening on ws://127.0.0.1:' + PORT)
  })

  wss.on('connection', function(ws) {
    console.log('[kckit-bridge] Client connected')
    ws.send(JSON.stringify({ type: 'state', payload: buildState() }))
    ws.on('message', function(msg) { handleCommand(ws, msg.toString()) })
    ws.on('error', function(err) { console.error('[kckit-bridge] WS error:', err) })
  })

  wss.on('error', function(err) {
    console.error('[kckit-bridge] Server error:', err)
  })

  gameResponseHandler = function(e) {
    const detail = e.detail || {}
    const path = detail.path
    if (!path) return

    // Track last event for screen detection
    buildState._lastEvent = path

    // Map API event → screen name and emit screen_change.
    // This is the PRIMARY screen detection mechanism: game API calls flow through
    // the outer DMM frame's XHR (not KC2's frame), so only the plugin's game.response
    // listener can reliably catch them.
    const short = path.startsWith('/kcsapi/') ? path.slice(8) : path
    var mappedScreen = KC2_API_SCREEN[short] || null

    // For map/start and map/next, refine based on event_id (battle vs routing)
    if (short === 'api_req_map/start' || short === 'api_req_map/next') {
      const body = detail.body || {}
      const eventId = body.api_event_id
      if (eventId !== undefined && eventId !== null) {
        // event_id 4=air raid 5=battle 6=boss 7=night_start → formation_select
        // event_id 0=nothing 2=resource 3=item → sortie_routing
        mappedScreen = [4, 5, 6, 7].includes(Number(eventId)) ? 'formation_select' : 'sortie_routing'
      }
    }

    if (mappedScreen && mappedScreen !== _lastPolledScreen) {
      _lastPolledScreen = mappedScreen
      broadcast({ type: 'screen_change', screen: mappedScreen, source: 'game_response', api: short })
    } else if (!mappedScreen) {
      broadcast({ type: 'unknown_api', api: short })
    }

    if (PUSH_EVENTS[path]) {
      // Wait 200ms for poi's reducers to update the store
      setTimeout(function() {
        const state = buildState()
        broadcast({ type: 'state', payload: state })
        // Write snapshot on all PUSH_EVENTS
        fs.mkdir(nodePath.dirname(SNAPSHOT_PATH), { recursive: true }, function() {
          fs.writeFile(SNAPSHOT_PATH, JSON.stringify(state, null, 2), function(err) {
            if (err) console.error('[kckit-bridge] snapshot write error:', err)
          })
        })
      }, 200)
    }

    // Broadcast ALL game API events — Python side filters what it needs
    broadcast({ type: 'event', event: path, payload: { body: detail.body, postBody: detail.postBody } })
  }

  window.addEventListener('game.response', gameResponseHandler)
  // Attach webview navigation watcher for non-API screen detection
  attachNavWatcher()
  startScreenshotServer()

  // Inject persistent screen spy into KC webview.
  // The spy sets window._kckit_screen on every scene transition, giving us
  // ground-truth screen state independent of whether KC fires API events.
  // Retry until the webview is ready (KC may not be loaded yet on plugin init).
  var _spyRetries = 0
  function tryInjectSpy() {
    injectScreenSpy().then(function(r) {
      if (r && r.ok) {
        console.log('[kckit-bridge] Screen spy active, initial screen:', r.initialScreen)
        // Broadcast initial screen so Python side is in sync
        if (r.initialScreen) {
          broadcast({ type: 'screen_change', screen: r.initialScreen, source: 'spy_init' })
        }
      } else {
        // Webview may not be ready; retry up to 10 times with 3s gaps
        if (_spyRetries++ < 10) {
          setTimeout(tryInjectSpy, 3000)
        } else {
          console.warn('[kckit-bridge] Screen spy injection failed after retries:', r)
        }
      }
    })
  }
  setTimeout(tryInjectSpy, 2000)

  // Watch own file — reload automatically whenever the source is updated on disk
  if (_fileWatcher) { try { _fileWatcher.close() } catch (e) {} }
  _fileWatcher = fs.watch(__filename, function (event) {
    if (event === 'change') {
      console.log('[kckit-bridge] Source changed, hot-reloading…')
      // Debounce: editors write files in multiple events
      clearTimeout(_fileWatcher._debounce)
      _fileWatcher._debounce = setTimeout(selfReload, 300)
    }
  })

  console.log('[kckit-bridge] Plugin loaded, watching', __filename)
}

function pluginWillUnload() {
  if (gameResponseHandler) {
    window.removeEventListener('game.response', gameResponseHandler)
    gameResponseHandler = null
  }
  var wv = document.querySelector('webview')
  if (wv && _navChangeHandler) {
    wv.removeEventListener('did-navigate-in-page', _navChangeHandler)
    _navChangeHandler = null
  }
  if (_fileWatcher) {
    try { _fileWatcher.close() } catch (e) {}
    _fileWatcher = null
  }
  stopSpyPoll()
  _spyInjected = false
  _lastPolledScreen = null
  if (wss) {
    // wss.close() only stops LISTENING — existing client sockets stay open,
    // married to this (now dead) instance: they'd never receive another
    // message but still answer protocol pings, looking healthy forever.
    // Terminate them so clients reconnect to the new server instance.
    try {
      wss.clients.forEach(function(c) { try { c.terminate() } catch (e) {} })
    } catch (e) {}
    wss.close(function() { console.log('[kckit-bridge] WebSocket server stopped') })
    wss = null
  }
  if (screenshotServer) {
    screenshotServer.close()
    screenshotServer = null
  }
  console.log('[kckit-bridge] Plugin unloaded')
}

module.exports = {
  pluginDidLoad: pluginDidLoad,
  pluginWillUnload: pluginWillUnload,
}
