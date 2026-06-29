"""
VAPE IN BKK — Restock Manager
ระบบเติมสินค้า: คลัง → หน้าร้าน (real-time จาก ZORT)

Cloud deployment (Railway):
  Set env vars: ZORT_STORENAME, ZORT_APIKEY, ZORT_APISECRET,
                ZORT_KHLANG_CODE, ZORT_FRONT_CODE
  Then: railway up

Local:
  pip install flask requests
  python restock_app.py
"""

import os, json, socket
from datetime import datetime, timedelta
from collections import defaultdict

import requests as req
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restock_config.json")
ZORT_BASE   = "https://open-api.zortout.com/v4"

# In-memory config overlay (used on cloud where disk is ephemeral)
_mem_cfg = {}

# ────────────────────────────────────────────────────────────
# Config  (env vars → file → defaults)
# ────────────────────────────────────────────────────────────
def load_cfg():
    # Defaults
    base = {"storename":"","apikey":"","apisecret":"",
            "cycle_days":4,"buffer":1.5,"khlang_code":"","front_code":""}
    # 1) File (local / Railway volume)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            base.update(json.load(f))
    # 2) Environment variables (Railway dashboard) — override file
    env_map = {
        "ZORT_STORENAME":   "storename",
        "ZORT_APIKEY":      "apikey",
        "ZORT_APISECRET":   "apisecret",
        "ZORT_KHLANG_CODE": "khlang_code",
        "ZORT_FRONT_CODE":  "front_code",
        "ZORT_CYCLE":       "cycle_days",
        "ZORT_BUFFER":      "buffer",
    }
    for env_k, cfg_k in env_map.items():
        v = os.environ.get(env_k)
        if v:
            base[cfg_k] = int(v) if cfg_k == "cycle_days" else \
                          float(v) if cfg_k == "buffer" else v
    # 3) In-memory overlay (UI changes on ephemeral cloud)
    base.update(_mem_cfg)
    return base

def save_cfg(cfg):
    global _mem_cfg
    _mem_cfg = dict(cfg)          # always keep in memory
    try:                           # also try disk (works locally)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ────────────────────────────────────────────────────────────
# ZORT API helpers
# ────────────────────────────────────────────────────────────
def hdrs(cfg):
    return {"storename": cfg["storename"],
            "apikey":    cfg["apikey"],
            "apisecret": cfg["apisecret"]}

def zort_get(cfg, path, params=None):
    r = req.get(f"{ZORT_BASE}{path}", headers=hdrs(cfg),
                params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

def all_pages(cfg, path, extra=None):
    items, page = [], 1
    params = dict(extra or {})
    while True:
        params.update({"page": page, "limit": 500})
        d = zort_get(cfg, path, params)
        if d.get("res") != 200:
            break
        batch = d.get("list", [])
        if not batch:
            break
        items.extend(batch)
        if len(items) >= d.get("count", 0):
            break
        page += 1
    return items

# ────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return Response(HTML, mimetype="text/html; charset=utf-8")

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_cfg())

@app.route("/api/config", methods=["POST"])
def set_config():
    cfg = load_cfg()
    cfg.update(request.json or {})
    save_cfg(cfg)
    return jsonify({"ok": True})

@app.route("/api/warehouses")
def warehouses():
    cfg = load_cfg()
    try:
        d = zort_get(cfg, "/Warehouse/GetWarehouses", {"limit": 100})
        return jsonify(d.get("list", []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/refresh")
def refresh():
    cfg = load_cfg()
    if not cfg.get("storename") or not cfg.get("apikey") or not cfg.get("apisecret"):
        return jsonify({"error": "กรุณาตั้งค่า API Key ก่อน (กดไอคอน ⚙️)"}), 400

    kc = cfg.get("khlang_code","")
    fc = cfg.get("front_code","")

    try:
        # ── 1. Stock per warehouse ──────────────────────────
        khlang_list = all_pages(cfg, "/Product/GetProducts",
                                {"warehousecode": kc, "activestatus": 1}) if kc else []
        front_list  = all_pages(cfg, "/Product/GetProducts",
                                {"warehousecode": fc, "activestatus": 1}) if fc else []

        khlang_qty = {p["sku"]: float(p.get("stock",0) or 0) for p in khlang_list}
        front_qty  = {p["sku"]: float(p.get("stock",0) or 0) for p in front_list}

        meta = {}
        for p in khlang_list + front_list:
            s = p.get("sku","")
            if s and s not in meta:
                meta[s] = {"name": p.get("name", s),
                           "category": p.get("category","ไม่ระบุ") or "ไม่ระบุ"}

        # ── 2. 90-day sales ─────────────────────────────────
        since  = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        orders = all_pages(cfg, "/Order/GetOrders", {"orderdateafter": since})

        qty90 = defaultdict(float)
        rev90 = defaultdict(float)
        for order in orders:
            items = (order.get("products") or order.get("orderProducts") or
                     order.get("items")    or order.get("list") or [])
            for it in items:
                s = it.get("sku") or it.get("productSku","")
                if not s:
                    continue
                qty90[s] += float(it.get("number",0) or 0)
                rev90[s] += float(it.get("totalprice",0) or 0)

        # ── 3. ABC from 90-day revenue ──────────────────────
        sorted_r  = sorted(rev90.items(), key=lambda x: x[1], reverse=True)
        total_rev = sum(v for _,v in sorted_r)
        abc_map, cum = {}, 0
        for s, v in sorted_r:
            cum += v
            p = cum / total_rev if total_rev > 0 else 1
            abc_map[s] = "A" if p <= 0.70 else ("B" if p <= 0.90 else "C")

        # ── 4. Build product list ───────────────────────────
        all_skus = set(list(khlang_qty) + list(front_qty))
        products = []
        for s in all_skus:
            kq = khlang_qty.get(s, 0)
            fq = front_qty.get(s────────────────────────────
        since  = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        orders = all_pages(cfg, "/Order/GetOrders", {"orderdateafter": since})

        qty90 = defaultdict(float)
        rev90 = defaultdict(float)
        for order in orders:
            items = (order.get("products") or order.get("orderProducts") or
                     order.get("items")    or order.get("list") or [])
            for it in items:
                s = it.get("sku") or it.get("productSku","")
                if not s:
                    continue
                qty90[s] += float(it.get("number",0) or 0)
                rev90[s] += float(it.get("totalprice",0) or 0)

        # ── 3. ABC from 90-day revenue ──────────────────────
        sorted_r  = sorted(rev90.items(), key=lambda x: x[1], reverse=True)
        total_rev = sum(v for _,v in sorted_r)
        abc_map, cum = {}, 0
        for s, v in sorted_r:
            cum += v
            p = cum / total_rev if total_rev > 0 else 1
            abc_map[s] = "A" if p <= 0.70 else ("B" if p <= 0.90 else "C")

        # ── 4. Build product list ───────────────────────────
        all_skus = set(list(khlang_qty) + list(front_qty))
        products = []
        for s in all_skus:
            kq = khlang_qty.get(s, 0)
            fq = front_qty.get(s, 0)
            q90 = qty90.get(s, 0)
            if q90 <= 0 and fq <= 0:
                continue
            m = meta.get(s, {"name": s, "category": "ไม่ระบุ"})
            products.append({
                "sku":      s,
                "name":     m["name"],
                "category": m["category"],
                "khlang":   int(kq),
                "front":    int(fq),
                "daily":    round(q90 / 90, 2),
                "sales90":  int(q90),
                "abc":      abc_map.get(s, "C"),
            })

        return jsonify({
            "products":     products,
            "refreshed_at": datetime.now().strftime("%d/%m %H:%M"),
            "cycle_days":   int(cfg.get("cycle_days", 4)),
            "buffer":       float(cfg.get("buffer", 1.5)),
        })

    except req.exceptions.ConnectionError:
        return jsonify({"error": "เชื่อมต่อ ZORT ไม่ได้ ตรวจสอบอินเทอร์เน็ต"}), 503
    except req.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        return jsonify({"error": f"ZORT ตอบ {code} — ตรวจสอบ API Key / Secret"}), 502
    except Exception as e:
        return jsonify({"error": f"เกิดข้อผิดพลาด: {e}"}), 500


# ────────────────────────────────────────────────────────────
# HTML (mobile-first, single-page)
# ────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>เติมสินค้า | VAPE IN BKK</title>
<style>
:root {
  --bg: #f0f4f8;
  --card: #ffffff;
  --bar: #1a2e4a;
  --bar-txt: #ffffff;
  --a: #16a34a;   --a-bg: #dcfce7;  --a-bd: #86efac;
  --b: #d97706;   --b-bg: #fef3c7;  --b-bd: #fcd34d;
  --c: #dc2626;   --c-bg: #fee2e2;  --c-bd: #fca5a5;
  --pill: #e2e8f0;
  --pill-act: #1a2e4a;
  --pill-act-txt: #fff;
  --txt: #1e293b;
  --sub: #64748b;
  --border: #e2e8f0;
  --red: #ef4444;
  --shadow: 0 1px 3px rgba(0,0,0,.10), 0 1px 2px rgba(0,0,0,.06);
  --shadow-lg: 0 4px 16px rgba(0,0,0,.12);
}
* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
body { font-family: -apple-system, 'Sarabun', sans-serif; background: var(--bg);
       color: var(--txt); font-size: 15px; }

/* ── Top bar ── */
#topbar {
  position: sticky; top: 0; z-index: 100;
  background: var(--bar); color: var(--bar-txt);
  padding: 12px 16px 10px;
  display: flex; align-items: center; gap: 10px;
  box-shadow: var(--shadow-lg);
}
#topbar .title { flex: 1; font-size: 17px; font-weight: 700; letter-spacing: .3px; }
#topbar .sub   { font-size: 11px; opacity: .65; margin-top: 1px; }
.icon-btn {
  width: 38px; height: 38px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: rgba(255,255,255,.15); border: none; color: #fff;
  font-size: 18px; cursor: pointer; flex-shrink: 0;
  transition: background .15s;
}
.icon-btn:active { background: rgba(255,255,255,.3); }
#spin { display: none; width: 20px; height: 20px;
        border: 2px solid rgba(255,255,255,.4); border-top-color: #fff;
        border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Stats bar ── */
#stats {
  background: var(--bar); color: rgba(255,255,255,.9);
  padding: 0 16px 12px;
  display: flex; gap: 6px; flex-wrap: wrap;
}
.stat-chip {
  background: rgba(255,255,255,.12); border-radius: 20px;
  padding: 4px 10px; font-size: 12px; font-weight: 600;
}
.stat-chip.a { background: var(--a-bg); color: var(--a); }
.stat-chip.b { background: var(--b-bg); color: var(--b); }
.stat-chip.c { background: var(--c-bg); color: var(--c); }

/* ── Cycle slider bar ── */
#cycle-bar {
  background: #fff; border-bottom: 1px solid var(--border);
  padding: 10px 16px 8px;
  display: flex; align-items: center; gap: 10px;
}
#cycle-bar label { font-size: 13px; color: var(--sub); white-space: nowrap; }
#cycle-bar input[type=range] {
  flex: 1; height: 4px; accent-color: var(--bar); cursor: pointer;
}
#cycle-val { font-size: 14px; font-weight: 700; color: var(--bar); min-width: 52px; text-align: right; }

/* ── Filter tabs ── */
#filter-bar {
  position: sticky; top: 60px; z-index: 90;
  background: #fff; border-bottom: 1px solid var(--border);
  padding: 8px 12px; display: flex; gap: 6px; overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
#filter-bar::-webkit-scrollbar { display: none; }
.pill {
  flex-shrink: 0; padding: 6px 14px; border-radius: 20px; font-size: 13px;
  font-weight: 600; background: var(--pill); color: var(--sub);
  border: none; cursor: pointer; transition: all .15s;
}
.pill.active { background: var(--pill-act); color: var(--pill-act-txt); }
.pill.a { background: var(--a-bg); color: var(--a); }
.pill.a.active { background: var(--a); color: #fff; }
.pill.b { background: var(--b-bg); color: var(--b); }
.pill.b.active { background: var(--b); color: #fff; }
.pill.c { background: var(--c-bg); color: var(--c); }
.pill.c.active { background: var(--c); color: #fff; }
.pill.ready { background: #e6f4ea; color: #1e7e34; }
.pill.ready.active { background: #1e7e34; color: #fff; }

/* ── Search ── */
#search-wrap { padding: 8px 12px; background: var(--bg); }
#search {
  width: 100%; padding: 9px 14px; border-radius: 10px;
  border: 1.5px solid var(--border); font-size: 14px;
  background: #fff; color: var(--txt); outline: none;
}
#search:focus { border-color: var(--bar); }

/* ── Category filter ── */
#cat-bar {
  padding: 0 12px 8px; background: var(--bg);
  display: flex; gap: 6px; overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
#cat-bar::-webkit-scrollbar { display: none; }
.cat-pill {
  flex-shrink: 0; padding: 5px 12px; border-radius: 20px; font-size: 12px;
  font-weight: 600; background: #fff; color: var(--sub);
  border: 1.5px solid var(--border); cursor: pointer; transition: all .15s;
}
.cat-pill.active { background: var(--bar); color: #fff; border-color: var(--bar); }

/* ── Section header ── */
.section-hdr {
  padding: 10px 16px 4px;
  font-size: 12px; font-weight: 700; color: var(--sub);
  letter-spacing: .6px; text-transform: uppercase;
}

/* ── Product card ── */
.card {
  margin: 0 12px 8px; padding: 13px 14px;
  background: var(--card); border-radius: 12px;
  box-shadow: var(--shadow); border-left: 4px solid transparent;
  display: flex; flex-direction: column; gap: 6px;
}
.card.A { border-left-color: var(--a); }
.card.B { border-left-color: var(--b); }
.card.C { border-left-color: var(--c); }
.card-top { display: flex; align-items: flex-start; gap: 8px; }
.abc-badge {
  flex-shrink: 0; width: 24px; height: 24px; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 800; margin-top: 1px;
}
.abc-badge.A { background: var(--a-bg); color: var(--a); }
.abc-badge.B { background: var(--b-bg); color: var(--b); }
.abc-badge.C { background: var(--c-bg); color: var(--c); }
.card-name { flex: 1; }
.card-name .sku { font-size: 11px; color: var(--sub); font-weight: 600; letter-spacing: .3px; }
.card-name .name { font-size: 15px; font-weight: 700; color: var(--txt); line-height: 1.3; margin-top: 1px; }
.card-name .cat  { font-size: 11px; color: var(--sub); margin-top: 2px; }

.card-stock {
  display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
}
.stock-box {
  background: var(--bg); border-radius: 8px;
  padding: 7px 10px; text-align: center;
}
.stock-box .lbl { font-size: 10px; color: var(--sub); font-weight: 600; }
.stock-box .val { font-size: 20px; font-weight: 800; color: var(--txt); line-height: 1.1; }
.stock-box.low .val { color: var(--red); }

.card-restock {
  display: flex; align-items: center; gap: 10px;
  background: #1a2e4a; border-radius: 9px;
  padding: 9px 14px;
}
.card-restock .restock-lbl { font-size: 12px; color: rgba(255,255,255,.75); font-weight: 600; }
.card-restock .restock-val { font-size: 26px; font-weight: 900; color: #fff; line-height: 1; }
.card-restock .restock-unit{ font-size: 12px; color: rgba(255,255,255,.75); margin-left: 2px; }
.card-restock .restock-right { margin-left: auto; text-align: right; }
.card-restock .target-lbl { font-size: 10px; color: rgba(255,255,255,.6); }
.card-restock .target-val  { font-size: 13px; font-weight: 700; color: rgba(255,255,255,.9); }

/* no restock needed */
.card-ok {
  background: #f0fdf4; border-radius: 8px; padding: 7px 12px;
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; color: var(--a); font-weight: 600;
}
.card-ok::before { content: "✓"; font-size: 15px; }

.daily-txt { font-size: 11px; color: var(--sub); text-align: right; }

/* ── Empty / Loading ── */
#empty { text-align: center; padding: 60px 20px; color: var(--sub); }
#empty .icon { font-size: 48px; margin-bottom: 12px; }
#empty .msg  { font-size: 16px; font-weight: 600; margin-bottom: 6px; }
#empty .hint { font-size: 13px; }
#err-box {
  margin: 12px; padding: 12px 14px; background: #fee2e2; border-radius: 10px;
  color: #991b1b; font-size: 13px; font-weight: 600; display: none;
}

/* ── Settings overlay ── */
#settings {
  display: none; position: fixed; inset: 0; z-index: 200;
  background: rgba(0,0,0,.5); align-items: flex-end;
}
#settings.open { display: flex; }
#settings-box {
  width: 100%; max-height: 92vh; overflow-y: auto;
  background: #fff; border-radius: 20px 20px 0 0;
  padding: 20px 20px 40px;
}
#settings-box h2 { font-size: 18px; font-weight: 800; margin-bottom: 16px; color: var(--bar); }
.field { margin-bottom: 14px; }
.field label { display: block; font-size: 12px; font-weight: 700; color: var(--sub);
               margin-bottom: 5px; letter-spacing: .4px; }
.field input, .field select {
  width: 100%; padding: 11px 14px; border-radius: 10px;
  border: 1.5px solid var(--border); font-size: 14px;
  background: #fff; color: var(--txt); outline: none;
}
.field input:focus, .field select:focus { border-color: var(--bar); }
.field .hint { font-size: 11px; color: var(--sub); margin-top: 4px; }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
#btn-save {
  width: 100%; padding: 14px; border-radius: 12px; border: none;
  background: var(--bar); color: #fff; font-size: 16px; font-weight: 800;
  cursor: pointer; margin-top: 8px; letter-spacing: .3px;
}
#btn-save:active { opacity: .85; }
#wh-status { font-size: 12px; color: var(--sub); margin-top: 6px; }

/* ── Print mode ── */
@media print {
  #topbar, #stats, #cycle-bar, #filter-bar,
  #search-wrap, #cat-bar, #settings { display: none !important; }
  .card { box-shadow: none; border: 1px solid #ccc; margin: 4px; page-break-inside: avoid; }
  body { font-size: 12px; }
}

/* ── Bottom padding for iOS ── */
#list { padding-bottom: 24px; }
</style>
</head>
<body>

<!-- Top bar -->
<div id="topbar">
  <div style="flex:1">
    <div class="title">🛒 เติมสินค้า</div>
    <div class="sub" id="refresh-time">ยังไม่ได้โหลดข้อมูล</div>
  </div>
  <div id="spin"></div>
  <button class="icon-btn" onclick="doPrint()" title="พิมพ์">🖨️</button>
  <button class="icon-btn" onclick="openSettings()" title="ตั้งค่า">⚙️</button>
  <button class="icon-btn" onclick="loadData()" title="รีเฟรช">🔄</button>
</div>

<!-- Stats -->
<div id="stats">
  <span class="stat-chip" id="stat-total">—</span>
  <span class="stat-chip a" id="stat-a">A: —</span>
  <span class="stat-chip b" id="stat-b">B: —</span>
  <span class="stat-chip c" id="stat-c">C: —</span>
</div>

<!-- Cycle slider -->
<div id="cycle-bar">
  <label>🔄 Cycle</label>
  <input type="range" id="cycle-slider" min="1" max="14" value="4" oninput="onCycleChange(this.value)">
  <span id="cycle-val">4 วัน</span>
</div>

<!-- Filter tabs -->
<div id="filter-bar">
  <button class="pill active" onclick="setFilter('all',this)">ทั้งหมด</button>
  <button class="pill a"      onclick="setFilter('restock',this)">⚠️ ต้องเติม</button>
  <button class="pill ready"  onclick="setFilter('ready',this)">🟩 พร้อมขาย</button>
  <button class="pill a"      onclick="setFilter('A',this)">🟢 A</button>
  <button class="pill b"      onclick="setFilter('B',this)">🟡 B</button>
  <button class="pill c"      onclick="setFilter('C',this)">🔴 C</button>
</div>

<!-- Search -->
<div id="search-wrap">
  <input type="search" id="search" placeholder="🔍 ค้นหาชื่อ / SKU..." oninput="render()">
</div>

<!-- Category chips -->
<div id="cat-bar">
  <button class="cat-pill active" onclick="setCat('',this)">หมวดทั้งหมด</button>
</div>

<!-- Error -->
<div id="err-box"></div>

<!-- Product list -->
<div id="list">
  <div id="empty">
    <div class="icon">📦</div>
    <div class="msg">กดปุ่ม 🔄 เพื่อโหลดข้อมูล</div>
    <div class="hint">หากยังไม่ได้ตั้งค่า กด ⚙️ ก่อน</div>
  </div>
</div>

<!-- Settings -->
<div id="settings" onclick="if(event.target===this)closeSettings()">
  <div id="settings-box">
    <h2>⚙️ ตั้งค่าระบบ</h2>

    <div class="field">
      <label>STORE NAME (ชื่อร้านใน ZORT)</label>
      <input id="f-storename" type="text" placeholder="เช่น vapeinbkk" autocomplete="off">
    </div>
    <div class="field">
      <label>API KEY</label>
      <input id="f-apikey" type="text" placeholder="xxxxxxxxxxxxxxxxxx" autocomplete="off">
    </div>
    <div class="field">
      <label>API SECRET</label>
      <input id="f-apisecret" type="password" placeholder="••••••••••••••••" autocomplete="off">
    </div>

    <hr style="margin:16px 0;border:none;border-top:1px solid var(--border)">

    <div class="field">
      <label>รหัสคลัง (STOCK WAREHOUSE)</label>
      <select id="f-khlang">
        <option value="">— เลือกคลัง —</option>
      </select>
      <div class="hint">สินค้าสำรองหลัก (ต้นทาง)</div>
    </div>
    <div class="field">
      <label>รหัสหน้าร้าน (FRONT WAREHOUSE)</label>
      <select id="f-front">
        <option value="">— เลือกหน้าร้าน —</option>
      </select>
      <div class="hint">รวมส่ง / หน้าร้านที่ขาย (ปลายทาง)</div>
    </div>
    <div id="wh-status"></div>
    <button style="width:100%;padding:10px;border-radius:9px;border:1.5px solid var(--border);
                   background:#f8fafc;font-size:13px;font-weight:700;color:var(--bar);cursor:pointer;margin-bottom:14px"
            onclick="loadWarehouses()">📡 โหลดรายชื่อคลังจาก ZORT</button>

    <hr style="margin:16px 0;border:none;border-top:1px solid var(--border)">

    <div class="row2">
      <div class="field">
        <label>CYCLE เติมของ (วัน)</label>
        <input id="f-cycle" type="number" min="1" max="30" value="4">
        <div class="hint">เฉลี่ยเติมทุกกี่วัน</div>
      </div>
      <div class="field">
        <label>BUFFER (x เท่า)</label>
        <input id="f-buffer" type="number" min="1" max="3" step="0.1" value="1.5">
        <div class="hint">สต็อกเผื่อความผันผวน</div>
      </div>
    </div>

    <button id="btn-save" onclick="saveSettings()">💾 บันทึกและโหลดข้อมูล</button>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────
let allProducts = [];
let cycle       = 4;
let buffer      = 1.5;
let filterMode  = 'all';
let filterCat   = '';
let categories  = [];

// ── Boot ───────────────────────────────────────────────────
(async () => {
  const cfg = await (await fetch('/api/config')).json();
  if (!cfg.storename || !cfg.apikey) {
    openSettings();
    return;
  }
  cycle  = cfg.cycle_days || 4;
  buffer = cfg.buffer     || 1.5;
  document.getElementById('cycle-slider').value = cycle;
  document.getElementById('cycle-val').textContent = cycle + ' วัน';
  await loadData();
})();

// ── Load data from ZORT ────────────────────────────────────
async function loadData() {
  setLoading(true);
  hideError();
  try {
    const r   = await fetch('/api/refresh');
    const d   = await r.json();
    if (!r.ok) { showError(d.error || 'เกิดข้อผิดพลาด'); return; }

    allProducts = d.products || [];
    cycle       = d.cycle_days || cycle;
    buffer      = d.buffer     || buffer;

    document.getElementById('cycle-slider').value        = cycle;
    document.getElementById('cycle-val').textContent     = cycle + ' วัน';
    document.getElementById('refresh-time').textContent  = 'อัปเดต ' + d.refreshed_at;

    buildCategoryBar();
    render();
  } catch(e) {
    showError('เชื่อมต่อเซิร์ฟเวอร์ไม่ได้');
  } finally {
    setLoading(false);
  }
}

// ── Recalculate per product based on current cycle ─────────
function calcProduct(p) {
  const target   = Math.round(p.daily * cycle * buffer);
  const needed   = Math.max(0, target - p.front);
  const can_move = Math.min(needed, p.khlang);
  return { ...p, target, needed, can_move };
}

// ── Build category chips ───────────────────────────────────
function buildCategoryBar() {
  categories = [...new Set(allProducts.map(p => p.category))].sort();
  const bar  = document.getElementById('cat-bar');
  bar.innerHTML = `<button class="cat-pill active" onclick="setCat('',this)">หมวดทั้งหมด</button>`;
  categories.forEach(cat => {
    const b = document.createElement('button');
    b.className   = 'cat-pill';
    b.textContent = cat;
    b.onclick     = function() { setCat(cat, this); };
    bar.appendChild(b);
  });
}

// ── Render list ────────────────────────────────────────────
function render() {
  const q = document.getElementById('search').value.trim().toLowerCase();

  let products = allProducts.map(calcProduct);

  // Filter
  if (filterMode === 'restock') products = products.filter(p => p.can_move > 0);
  else if (filterMode === 'ready') products = products.filter(p => p.front > 0);
  else if (['A','B','C'].includes(filterMode)) products = products.filter(p => p.abc === filterMode);
  if (filterCat) products = products.filter(p => p.category === filterCat);
  if (q)         products = products.filter(p =>
    p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q) ||
    p.category.toLowerCase().includes(q)
  );

  // Sort: need-restock first, then ABC, then needed desc
  const ao = {A:0,B:1,C:2};
  products.sort((a,b) =>
    (b.can_move > 0 ? 1:0) - (a.can_move > 0 ? 1:0) ||
    ao[a.abc] - ao[b.abc] ||
    b.needed - a.needed
  );

  // Stats
  const all_calc   = allProducts.map(calcProduct);
  const restock    = all_calc.filter(p => p.can_move > 0);
  document.getElementById('stat-total').textContent = `ต้องเติม ${restock.length} / ${all_calc.length} รายการ`;
  document.getElementById('stat-a').textContent = `🟢 A: ${restock.filter(p=>p.abc==='A').length}`;
  document.getElementById('stat-b').textContent = `🟡 B: ${restock.filter(p=>p.abc==='B').length}`;
  document.getElementById('stat-c').textContent = `🔴 C: ${restock.filter(p=>p.abc==='C').length}`;

  // Render cards
  const list = document.getElementById('list');
  if (products.length === 0) {
    list.innerHTML = `<div id="empty">
      <div class="icon">✅</div>
      <div class="msg">ไม่พบสินค้าที่ตรงเงื่อนไข</div>
      <div class="hint">ลองเปลี่ยน filter หรือค้นหาใหม่</div>
    </div>`;
    return;
  }

  // Group by category
  const groups = {};
  products.forEach(p => {
    if (!groups[p.category]) groups[p.category] = [];
    groups[p.category].push(p);
  });

  let html = '';
  Object.entries(groups).forEach(([cat, items]) => {
    const urgCount = items.filter(p => p.can_move > 0).length;
    html += `<div class="section-hdr">${cat}
      ${urgCount > 0 ? `<span style="color:var(--red);font-size:11px"> · ต้องเติม ${urgCount} รายการ</span>` : ''}
    </div>`;

    items.forEach(p => {
      const isLow  = p.front <= Math.floor(p.daily * 2);
      const urgent = p.can_move > 0;

      html += `<div class="card ${p.abc}">
        <div class="card-top">
          <div class="abc-badge ${p.abc}">${p.abc}</div>
          <div class="card-name">
            <div class="sku">${p.sku}</div>
            <div class="name">${p.name}</div>
            <div class="cat">${p.category}</div>
          </div>
        </div>

        <div class="card-stock">
          <div class="stock-box">
            <div class="lbl">📦 คลัง</div>
            <div class="val">${p.khlang.toLocaleString()}</div>
          </div>
          <div class="stock-box ${isLow ? 'low' : ''}">
            <div class="lbl">🏪 หน้าร้าน</div>
            <div class="val">${p.front.toLocaleString()}</div>
          </div>
        </div>

        ${urgent ? `
        <div class="card-restock">
          <div>
            <div class="restock-lbl">ต้องเติม</div>
            <div>
              <span class="restock-val">${Math.ceil(p.can_move)}</span>
              <span class="restock-unit">ชิ้น</span>
            </div>
          </div>
          <div class="restock-right">
            <div class="target-lbl">เป้าหน้าร้าน</div>
            <div class="target-val">${p.target} ชิ้น</div>
            <div class="target-lbl">${p.daily}/วัน · ${p.daily*7>0?(p.daily*7).toFixed(0):'-'}/สัปดาห์</div>
          </div>
        </div>` : `
        <div class="card-ok">หน้าร้านพอแล้ว (${p.front} ชิ้น)</div>`}

        <div class="daily-txt">ขายเฉลี่ย ${p.daily} ชิ้น/วัน · 90 วัน: ${p.sales90.toLocaleString()} ชิ้น</div>
      </div>`;
    });
  });

  list.innerHTML = html;
}

// ── Cycle change ───────────────────────────────────────────
function onCycleChange(v) {
  cycle = parseInt(v);
  document.getElementById('cycle-val').textContent = v + ' วัน';
  render();
  // Save cycle to server
  fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({cycle_days: cycle})});
}

// ── Filters ────────────────────────────────────────────────
function setFilter(mode, el) {
  filterMode = mode;
  document.querySelectorAll('#filter-bar .pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  render();
}
function setCat(cat, el) {
  filterCat = cat;
  document.querySelectorAll('#cat-bar .cat-pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  render();
}

// ── Settings ───────────────────────────────────────────────
async function openSettings() {
  const cfg = await (await fetch('/api/config')).json();
  document.getElementById('f-storename').value = cfg.storename || '';
  document.getElementById('f-apikey').value    = cfg.apikey    || '';
  document.getElementById('f-apisecret').value = '';  // don't pre-fill secret
  document.getElementById('f-cycle').value     = cfg.cycle_days || 4;
  document.getElementById('f-buffer').value    = cfg.buffer     || 1.5;
  // Populate warehouse selects from config
  const ks = document.getElementById('f-khlang');
  const fs = document.getElementById('f-front');
  if (cfg.khlang_code) {
    const o = new Option(cfg.khlang_code, cfg.khlang_code, true, true);
    ks.appendChild(o);
  }
  if (cfg.front_code) {
    const o = new Option(cfg.front_code, cfg.front_code, true, true);
    fs.appendChild(o);
  }
  document.getElementById('settings').classList.add('open');
}
function closeSettings() {
  document.getElementById('settings').classList.remove('open');
}

arync function loadWarehouses() {
  const status = document.getElementById('wh-status');
  status.textContent = '⏳ กำลังโหลด...';

  // Temporarily save storename/key/secret to fetch warehouses
  const tmp = {
    storename: document.getElementById('f-storename').value.trim(),
    apikey:    document.getElementById('f-apikey').value.trim(),
    apisecret: document.getElementById('f-apisecret').value.trim() ||
               (await (await fetch('/api/config')).json()).apisecret,
  };
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
               body: JSON.stringify(tmp)});

  const r = await fetch('/api/warehouses');
  const d = await r.json();
  if (d.error) { status.textContent = '❌ ' + d.error; return; }
  if (!d.length) { status.textContent = '❌ ไม่พบคลัง'; return; }

  const ks = document.getElementById('f-khlang');
  const fs = document.getElementById('f-front');
  const curK = ks.value, curF = fs.value;
  ks.innerHTML = '<option value="">— เลือกคลัง —</option>';
  fs.innerHTML = '<option value="">— เลือกหน้าร้าน —</option>';

  d.forEach(w => {
    const labelK = new Option(`${w.name}  [${w.code}]`, w.code, false, w.code === curK);
    const labelF = new Option(`${w.name}  [${w.code}]`, w.code, false, w.code === curF);
    ks.appendChild(labelK);
    fs.appendChild(labelF);
  });

  // Auto-detect
  d.forEach(w => {
    const n = w.name.toLowerCase();
    if (!ks.value && (n.includes('คลัง') || n.includes('stock')))
      ks.value = w.code;
    if (!fs.value && (n.includes('รวมส่ง') || n.includes('หน้าร้าน') || n.includes('front')))
      fs.value = w.code;
  });

  status.textContent = `✅ พบ ${d.length} คลัง`;
}

async function saveSettings() {
  const cfg = {
    storename:   document.getElementById('f-storename').value.trim(),
    apikey:      document.getElementById('f-apikey').value.trim(),
    cycle_days:  parseInt(document.getElementById('f-cycle').value) || 4,
    buffer:      parseFloat(document.getElementById('f-buffer').value) || 1.5,
    khlang_code: document.getElementById('f-khlang').value,
    front_code:  document.getElementById('f-front').value,
  };
  const secret = document.getElementById('f-apisecret').value.trim();
  if (secret) cfg.apisecret = secret;

  await fetch('/api/config', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)});

  cycle  = cfg.cycle_days;
  buffer = cfg.buffer;
  document.getElementById('cycle-slider').value    = cycle;
  document.getElementById('cycle-val').textContent = cycle + ' วัน';
  closeSettings();
  await loadData();
}

// ── Utils ──────────────────────────────────────────────────
function setLoading(on) {
  document.getElementById('spin').style.display = on ? 'block' : 'none';
}
function showError(msg) {
  const el = document.getElementById('err-box');
  el.textContent = '⚠️ ' + msg;
  el.style.display = 'block';
}
function hideError() {
  document.getElementById('err-box').style.display = 'none';
}
function doPrint() {
  window.print();
}
</script>
</body>
</html>
"""

# ────────────────────────────────────────────────────────────
# Start
# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_cloud = "PORT" in os.environ

    if not is_cloud:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "localhost"
        print("\n" + "="*52)
        print("  🛒  VAPE IN BKK — Restock Manager")
        print("="*52)
        print(f"\n  📱  เปิดในโทรศัพท์:  http://{local_ip}:{port}")
        print(f"  💻  เปิดในคอม:       http://localhost:{port}")
        print("\n  ⚠️  โทรศัพท์ต้องอยู่ WiFi เดียวกับคอม")
        print("\n  กด Ctrl+C เพื่อปิดโปรแกรม")
        print("="*52 + "\n")
    else:
        print(f"🚀 Running on cloud, port {port}")

    app.run(host="0.0.0.0", port=port, debug=False)
