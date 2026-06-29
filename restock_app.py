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

# Cache: เก็บผล refresh ล่าสุดไว้ ไม่ดึง ZORT ซ้ำถ้าข้อมูลยังใหม่อยู่
_cache_data = None
_cache_time = None
CACHE_TTL_MINUTES = 180  # โหลดใหม่ทุก 3 ชั่วโมง

# ────────────────────────────────────────────────────────────
# Config  (env vars → file → defaults)
# ────────────────────────────────────────────────────────────
def load_cfg():
    base = {"storename":"","apikey":"","apisecret":"",
            "cycle_days":4,"buffer":1.5,"khlang_code":"","front_code":""}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            base.update(json.load(f))
    env_map = {
        "ZORT_STORENAME":   "storename",
        "ZORT_APIKEY":      "apikey",
        "ZORT_APISECRET":   "apisecret",
        "ZORT_KHLANG_CODE": "khlang_code",
        "ZORT_FRONT_CODE":  "front_code",
        "ZORT_CYCLE":       "cycle_days",
        "ZORT_BUFFER":      "buffer",
    }
    base.update(_mem_cfg)
    for env_k, cfg_k in env_map.items():
        v = os.environ.get(env_k)
        if v:
            base[cfg_k] = int(v) if cfg_k == "cycle_days" else \
                          float(v) if cfg_k == "buffer" else v
    return base

def save_cfg(cfg):
    global _mem_cfg
    _mem_cfg = dict(cfg)
    try:
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
                params=params or {}, timeout=60)
    r.raise_for_status()
    return r.json()

def all_pages(cfg, path, extra=None, max_pages=999, page_size=200):
    items, page = [], 1
    params = dict(extra or {})
    while page <= max_pages:
        params.update({"page": page, "limit": page_size})
        for attempt in range(2):
            try:
                d = zort_get(cfg, path, params)
                break
            except Exception:
                if attempt == 1:
                    raise
        res = d.get("res")
        ok = (isinstance(res, dict) and res.get("resCode") == "200") or res == 200
        if not ok:
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

@app.route("/api/debug")
def debug():
    cfg = load_cfg()
    kc = cfg.get("khlang_code","")
    fc = cfg.get("front_code","")
    info = {
        "storename": cfg.get("storename",""),
        "apikey_prefix": cfg.get("apikey","")[:8] + "..." if cfg.get("apikey") else "",
        "apisecret": "set" if cfg.get("apisecret") else "MISSING",
        "khlang_code": kc, "front_code": fc,
    }
    try:
        if kc:
            r = zort_get(cfg, "/Product/GetProducts", {"warehousecode": kc, "activestatus": 1, "page": 1, "limit": 3})
            info["khlang_test"] = {"res": r.get("res"), "count": r.get("count"), "items": len(r.get("list",[]))}
        if fc:
            r = zort_get(cfg, "/Product/GetProducts", {"warehousecode": fc, "activestatus": 1, "page": 1, "limit": 3})
            info["front_test"] = {"res": r.get("res"), "count": r.get("count"), "items": len(r.get("list",[]))}
        r3 = zort_get(cfg, "/Product/GetProducts", {"activestatus": 1, "page": 1, "limit": 3})
        info["all_products"] = {"res": r3.get("res"), "count": r3.get("count"), "sample_keys": list((r3.get("list") or [{}])[0].keys()) if r3.get("list") else []}
        since7 = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        r2 = zort_get(cfg, "/Order/GetOrders", {"orderdateafter": since7, "page": 1, "limit": 3})
        info["orders_7d"] = {"res": r2.get("res"), "count": r2.get("count")}
        for o in (r2.get("list") or [])[:1]:
            items = o.get("products") or o.get("orderProducts") or o.get("items") or o.get("list") or []
            if items:
                info["orders_7d"]["item_keys"] = list(items[0].keys())
    except Exception as e:
        info["error"] = str(e)
    return jsonify(info)

@app.route("/api/debug-order")
def debug_order():
    cfg = load_cfg()
    since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    try:
        orders = all_pages(cfg, "/Order/GetOrders", {"orderdateafter": since}, max_pages=1, page_size=3)
        if not orders:
            return jsonify({"error": "no orders found in last 3 days"})
        order = orders[0]
        raw_items = (order.get("products") or order.get("orderProducts") or
                     order.get("items") or order.get("list") or [])
        item_sample = raw_items[:2]
        return jsonify({
            "order_keys": list(order.keys()),
            "item_count": len(raw_items),
            "item_keys": list(raw_items[0].keys()) if raw_items else [],
            "item_sample": item_sample
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/debug-sales")
def debug_sales():
    """Show raw sales counts per SKU for last 14 days - for diagnosing qty accuracy"""
    cfg = load_cfg()
    since = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    try:
        orders = all_pages(cfg, "/Order/GetOrders", {"orderdateafter": since}, max_pages=40, page_size=500)
        total_orders = len(orders)
        sku_qty = {}
        sku_rev = {}
        sku_orders = {}
        for order in orders:
            items = (order.get("products") or order.get("orderProducts") or
                     order.get("items") or order.get("list") or [])
            for it in items:
                s = it.get("sku") or it.get("productSku","")
                if not s:
                    continue
                n = float(it.get("qty") or it.get("number") or it.get("quantity") or 0)
                bn = float(it.get("bundlenumber") or 0)
                sku_qty[s] = sku_qty.get(s, 0) + n
                sku_rev[s] = sku_rev.get(s, 0) + float(it.get("totalprice", 0) or 0)
                sku_orders[s] = sku_orders.get(s, 0) + 1
        # Sort by qty descending, return top 30
        top = sorted(sku_qty.items(), key=lambda x: x[1], reverse=True)[:30]
        return jsonify({
            "total_orders": total_orders,
            "since": since,
            "top_skus": [{"sku": s, "qty": q, "order_lines": sku_orders[s]} for s,q in top]
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()})


@app.route("/api/refresh")
def refresh():
    global _cache_data, _cache_time
    cfg = load_cfg()
    if not cfg.get("storename") or not cfg.get("apikey") or not cfg.get("apisecret"):
        return jsonify({"error": "กรุณาตั้งค่า API Key ก่อน (กดไอคอน ⚙️)"}), 400

    # คืน cache ถ้าข้อมูลยังไม่เก่าเกิน CACHE_TTL_MINUTES
    force = request.args.get("force") == "1"
    if not force and _cache_data and _cache_time:
        age_min = (datetime.now() - _cache_time).total_seconds() / 60
        if age_min < CACHE_TTL_MINUTES:
            cached = dict(_cache_data)
            cached["cached"] = True
            cached["cache_age_min"] = round(age_min, 1)
            return jsonify(cached)

    kc = cfg.get("khlang_code","")
    fc = cfg.get("front_code","")

    try:
        # 1. Stock per warehouse
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

        # 2. 14-day sales (fetch last 14 days of orders)
        since  = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        orders = all_pages(cfg, "/Order/GetOrders", {"orderdateafter": since}, max_pages=40, page_size=500)

        qty90 = defaultdict(float)
        rev90 = defaultdict(float)
        for order in orders:
            items = (order.get("products") or order.get("orderProducts") or
                     order.get("items")    or order.get("list") or [])
            for it in items:
                s = it.get("sku") or it.get("productSku","")
                if not s:
                    continue
                qty90[s] += float(it.get("qty") or it.get("number") or it.get("quantity") or it.get("amount") or 0)
                rev90[s] += float(it.get("totalprice",0) or 0)

        # 3. ABC from 90-day revenue (global ranking)
        sorted_r  = sorted(rev90.items(), key=lambda x: x[1], reverse=True)
        total_rev = sum(v for _,v in sorted_r)
        abc_map, cum = {}, 0
        for s, v in sorted_r:
            cum += v
            p = cum / total_rev if total_rev > 0 else 1
            abc_map[s] = "A" if p <= 0.70 else ("B" if p <= 0.90 else "C")

        # 4. Build product list
        all_skus = set(list(khlang_qty) + list(front_qty) + list(qty90))
        products = []
        for s in all_skus:
            kq  = khlang_qty.get(s, 0)
            fq  = front_qty.get(s, 0)
            q90 = qty90.get(s, 0)
            if s not in meta:
                continue  # skip ghost SKUs with no name/category
            m = meta.get(s, {"name": s, "category": "ไม่ระบุ"})
            products.append({
                "sku":      s,
                "name":     m["name"],
                "category": m["category"],
                "khlang":   int(kq),
                "front":    int(fq),
                "daily":    round(q90 / 10, 2),
                "sales14":  int(q90),
                "rev10":    float(sku_rev.get(s, 0)),
                "abc":      abc_map.get(s, "C"),
            })

        result = {
            "products":     products,
            "refreshed_at": datetime.now().strftime("%d/%m %H:%M"),
            "cycle_days":   int(cfg.get("cycle_days", 4)),
            "buffer":       float(cfg.get("buffer", 1.5)),
            "cached":       False,
        }
        _cache_data = result
        _cache_time = datetime.now()
        return jsonify(result)

    except req.exceptions.ConnectionError:
        return jsonify({"error": "เชื่อมต่อ ZORT ไม่ได้ ตรวจสอบอินเทอร์เน็ต"}), 503
    except req.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        return jsonify({"error": f"ZORT ตอบ {code} — ตรวจสอบ API Key / Secret"}), 502
    except Exception as e:
        return jsonify({"error": f"เกิดข้อผิดพลาด: {e}"}), 500


# ────────────────────────────────────────────────────────────
# HTML — two-screen mobile app
# ────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>เติมสินค้า | VAPE IN BKK</title>
<style>
:root{
  --bar:#1a2e4a; --bar2:#243b5e;
  --a:#15803d; --a-bg:#dcfce7; --a-lgt:#f0fdf4;
  --b:#b45309; --b-bg:#fef3c7; --b-lgt:#fffbeb;
  --c:#b91c1c; --c-bg:#fee2e2; --c-lgt:#fff5f5;
  --txt:#1e293b; --sub:#64748b; --bg:#f8fafc;
  --border:#e2e8f0; --white:#fff;
  --red:#dc2626; --green:#16a34a; --orange:#ea580c;
  --shadow:0 1px 3px rgba(0,0,0,.09);
  --shadow2:0 4px 14px rgba(0,0,0,.12);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden}
body{font-family:-apple-system,'Sarabun',sans-serif;font-size:13px;color:var(--txt);background:var(--bg)}
#app{position:fixed;inset:0;display:flex;flex-direction:column;overflow:hidden}

/* topbar */
.topbar{background:var(--bar);color:#fff;padding:10px 14px 9px;flex-shrink:0;
  display:flex;align-items:center;gap:8px;box-shadow:var(--shadow2)}
.topbar h1{font-size:15px;font-weight:800;flex:1;letter-spacing:.2px}
.topbar .sub{font-size:10px;opacity:.6;margin-top:1px}
.ico-btn{width:34px;height:34px;border-radius:50%;border:none;
  background:rgba(255,255,255,.15);color:#fff;font-size:16px;
  display:flex;align-items:center;justify-content:center;cursor:pointer}
.ico-btn:active{background:rgba(255,255,255,.28)}
.back-btn{display:none;align-items:center;gap:4px;
  background:none;border:none;color:rgba(255,255,255,.85);
  font-size:13px;font-weight:700;padding:4px 0;cursor:pointer}
.back-btn::before{content:"‹";font-size:22px;line-height:1}
#back-btn.show{display:flex}
#spin{display:none;width:18px;height:18px;border:2px solid rgba(255,255,255,.35);
  border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

/* cycle bar */
.cycle-bar{background:var(--bar2);padding:5px 14px 7px;
  display:flex;align-items:center;gap:8px;flex-shrink:0}
.cycle-bar label{font-size:11px;color:rgba(255,255,255,.7);white-space:nowrap}
.cycle-bar input[type=range]{flex:1;height:3px;accent-color:#7dd3fc;cursor:pointer}
#cycle-lbl{font-size:12px;font-weight:800;color:#7dd3fc;min-width:46px;text-align:right}

/* screens */
.screen{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;display:none}
.screen.active{display:block}
#detail-screen{display:none;flex-direction:column;overflow:hidden}
#detail-screen.active{display:flex}

/* error */
#err-box{background:#fee2e2;border-bottom:1px solid #fca5a5;padding:8px 14px;
  font-size:12px;color:#991b1b;font-weight:600;display:none;flex-shrink:0}

/* HOME stats row */
.stats-row{padding:8px 12px 4px;display:flex;gap:6px;flex-wrap:wrap;
  background:var(--white);border-bottom:1px solid var(--border);flex-shrink:0}
.s-chip{font-size:11px;font-weight:700;padding:3px 9px;border-radius:12px}
.s-chip.tot{background:#e0e7ff;color:#3730a3}
.s-chip.a{background:var(--a-bg);color:var(--a)}
.s-chip.b{background:var(--b-bg);color:var(--b)}
.s-chip.c{background:var(--c-bg);color:var(--c)}

/* HOME grid */
#home-screen{padding:10px 10px 24px}
.home-title{font-size:10px;font-weight:800;color:var(--sub);letter-spacing:.8px;
  text-transform:uppercase;padding:0 4px 6px}
.cat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.cat-card{background:var(--white);border-radius:10px;border:1.5px solid var(--border);
  padding:10px 11px 9px;cursor:pointer;box-shadow:var(--shadow);transition:transform .1s;
  border-top:3px solid var(--border);position:relative;overflow:hidden;
  display:flex;flex-direction:column;gap:5px}
.cat-card:active{transform:scale(.97)}
.cat-card.A{border-top-color:var(--a)}
.cat-card.B{border-top-color:var(--b)}
.cat-card.C{border-top-color:var(--c)}
.cat-card .abc{position:absolute;top:8px;right:8px;font-size:10px;font-weight:900;
  width:19px;height:19px;border-radius:5px;display:flex;align-items:center;justify-content:center}
.cat-card .abc.A{background:var(--a-bg);color:var(--a)}
.cat-card .abc.B{background:var(--b-bg);color:var(--b)}
.cat-card .abc.C{background:var(--c-bg);color:var(--c)}
.cat-card .rank-num{position:absolute;top:8px;left:9px;font-size:11px;font-weight:900;
  color:var(--sub);background:var(--bg);border:1px solid var(--border);
  width:20px;height:20px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;line-height:1}
.cat-card.A .rank-num{background:var(--a-lgt);border-color:#bbf7d0;color:var(--a)}
.cat-card.B .rank-num{background:var(--b-lgt);border-color:#fde68a;color:var(--b)}
.cat-card.C .rank-num{background:var(--c-lgt);border-color:#fecaca;color:var(--c)}
.cat-card .cname{font-size:12px;font-weight:800;color:var(--txt);line-height:1.3;
  padding-right:22px;padding-left:26px}
.cat-card .csales{font-size:10px;color:var(--sub)}
.cat-card .cneed{font-size:11px;font-weight:700;display:flex;align-items:center;gap:4px}
.cneed .n-badge{font-size:10px;font-weight:800;padding:2px 7px;border-radius:8px}
.cneed .n-badge.urgent{background:var(--c-bg);color:var(--red)}
.cneed .n-badge.ok{background:var(--a-bg);color:var(--a)}
.cneed .n-badge.warn{background:var(--b-bg);color:var(--b)}

/* DETAIL header */
#detail-header{background:var(--bg);border-bottom:1px solid var(--border);
  padding:8px 12px;flex-shrink:0}
.dh-row{display:flex;align-items:center;gap:6px}
.dh-abc{font-size:11px;font-weight:900;width:22px;height:22px;border-radius:6px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.dh-abc.A{background:var(--a-bg);color:var(--a)}
.dh-abc.B{background:var(--b-bg);color:var(--b)}
.dh-abc.C{background:var(--c-bg);color:var(--c)}
.dh-name{font-size:14px;font-weight:800;flex:1}
.dh-stats{display:flex;gap:10px;margin-top:5px;font-size:11px;color:var(--sub)}
.dh-stats span b{color:var(--txt);font-weight:800}

/* DETAIL filter pills */
#detail-filter{padding:6px 10px;background:var(--white);border-bottom:1px solid var(--border);
  display:flex;gap:5px;overflow-x:auto;flex-shrink:0;scrollbar-width:none}
#detail-filter::-webkit-scrollbar{display:none}
.fpill{flex-shrink:0;padding:4px 12px;border-radius:16px;font-size:11px;font-weight:700;
  background:var(--bg);color:var(--sub);border:1px solid var(--border);cursor:pointer}
.fpill.active{background:var(--bar);color:#fff;border-color:var(--bar)}
.fpill.fa.active{background:var(--a);color:#fff;border-color:var(--a)}
.fpill.fb.active{background:var(--b);color:#fff;border-color:var(--b)}
.fpill.fc.active{background:var(--c);color:#fff;border-color:var(--c)}
.fpill.fr{color:#1e7e34;border-color:#1e7e34}
.fpill.fr.active{background:#1e7e34;color:#fff;border-color:#1e7e34}

/* DETAIL product rows */
#prod-list{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding-bottom:24px}
.prod-row{display:flex;align-items:center;padding:8px 12px;gap:8px;
  border-bottom:1px solid var(--border);background:var(--white);cursor:default}
.prod-row.urgent{background:#fff5f5}
.prod-row.out{background:#fff7ed}
.pr-abc{flex-shrink:0;width:18px;height:18px;border-radius:4px;font-size:9px;font-weight:900;
  display:flex;align-items:center;justify-content:center}
.pr-abc.A{background:var(--a-bg);color:var(--a)}
.pr-abc.B{background:var(--b-bg);color:var(--b)}
.pr-abc.C{background:var(--c-bg);color:var(--c)}
.pr-info{flex:1;min-width:0}
.pr-name{font-size:12px;font-weight:700;color:var(--txt);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pr-sku{font-size:10px;color:var(--sub)}
.pr-stocks{flex-shrink:0;display:flex;flex-direction:column;align-items:flex-end;gap:2px}
.pr-stk{font-size:10px;color:var(--sub);white-space:nowrap}
.pr-stk b{color:var(--txt)}
.pr-stk.low b{color:var(--orange)}
.pr-stk.zero b{color:var(--red)}
.pr-action{flex-shrink:0;min-width:68px;text-align:center}
.act-need{background:var(--red);color:#fff;border-radius:7px;padding:4px 0;
  font-size:11px;font-weight:800;min-width:64px;display:inline-block}
.act-out{background:#fed7aa;color:#c2410c;border-radius:7px;padding:4px 0;
  font-size:10px;font-weight:700;min-width:64px;display:inline-block}
.act-ok{color:var(--green);font-size:11px;font-weight:700}
.no-result{text-align:center;padding:40px 20px;color:var(--sub)}
.no-result .ico{font-size:36px;margin-bottom:8px}

/* Settings overlay */
#settings{display:none;position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.5);align-items:flex-end}
#settings.open{display:flex}
#settings-box{width:100%;max-height:92vh;overflow-y:auto;background:#fff;
  border-radius:20px 20px 0 0;padding:20px 20px 40px}
#settings-box h2{font-size:17px;font-weight:800;margin-bottom:16px;color:var(--bar)}
.field{margin-bottom:14px}
.field label{display:block;font-size:11px;font-weight:700;color:var(--sub);
  margin-bottom:5px;letter-spacing:.4px}
.field input,.field select{width:100%;padding:10px 13px;border-radius:10px;
  border:1.5px solid var(--border);font-size:14px;background:#fff;color:var(--txt);outline:none}
.field input:focus,.field select:focus{border-color:var(--bar)}
.field .hint{font-size:11px;color:var(--sub);margin-top:4px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
#btn-save{width:100%;padding:13px;border-radius:12px;border:none;
  background:var(--bar);color:#fff;font-size:15px;font-weight:800;cursor:pointer;margin-top:8px}
#btn-save:active{opacity:.85}
#wh-status{font-size:12px;color:var(--sub);margin-top:6px}

/* empty state */
#empty{text-align:center;padding:60px 20px;color:var(--sub)}
#empty .ico{font-size:48px;margin-bottom:12px}
#empty .msg{font-size:15px;font-weight:600;margin-bottom:6px}
#empty .hint{font-size:12px}
</style>
</head>
<body>
<div id="app">

  <!-- Top bar -->
  <div class="topbar">
    <button class="ico-btn back-btn" id="back-btn" onclick="goHome()">ย้อนกลับ</button>
    <div style="flex:1" id="topbar-title">
      <div style="font-size:15px;font-weight:800">🛒 VAPE IN BKK</div>
      <div style="font-size:10px;opacity:.6;margin-top:1px" id="top-sub">ระบบเติมสินค้า</div>
    </div>
    <div id="spin"></div>
    <button class="ico-btn" onclick="openSettings()" title="ตั้งค่า">⚙️</button>
    <button class="ico-btn" onclick="loadData()" title="รีเฟรช">🔄</button>
  </div>

  <!-- Cycle bar -->
  <div class="cycle-bar">
    <label>🔄 Cycle เติมของ</label>
    <input type="range" id="cslider" min="1" max="14" value="4" oninput="setCycle(+this.value)">
    <span id="cycle-lbl">4 วัน</span>
  </div>

  <!-- Error -->
  <div id="err-box"></div>

  <!-- HOME SCREEN -->
  <div class="screen active" id="home-screen-wrap">
    <div class="stats-row" id="stats-row">
      <span class="s-chip tot">กด 🔄 เพื่อโหลดข้อมูล</span>
    </div>
    <div id="home-screen">
      <div class="home-title">หมวดหมู่ · เรียงตามยอดขาย 10 วัน</div>
      <div class="cat-grid" id="cat-grid">
        <div id="empty">
          <div class="ico">📦</div>
          <div class="msg">ยังไม่มีข้อมูล</div>
          <div class="hint">กด ⚙️ ตั้งค่า แล้วกด 🔄 โหลด</div>
        </div>
      </div>
    </div>
  </div>

  <!-- DETAIL SCREEN -->
  <div id="detail-screen">
    <div id="detail-header">
      <div class="dh-row">
        <div class="dh-abc" id="dh-abc">A</div>
        <div class="dh-name" id="dh-name">—</div>
      </div>
      <div class="dh-stats" id="dh-stats"></div>
    </div>
    <div id="detail-filter">
      <button class="fpill active" onclick="setDF('all',this)">ทั้งหมด</button>
      <button class="fpill"        onclick="setDF('need',this)">⚠️ ต้องเติม</button>
      <button class="fpill fa"     onclick="setDF('A',this)">🟢 A</button>
      <button class="fpill fb"     onclick="setDF('B',this)">🟡 B</button>
      <button class="fpill fc"     onclick="setDF('C',this)">🔴 C</button>
      <button class="fpill fr"     onclick="setDF('ready',this)">🟩 พร้อมขาย</button>
    </div>
    <div id="prod-list"></div>
  </div>

</div>

<!-- Settings overlay -->
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
    <hr style="margin:14px 0;border:none;border-top:1px solid var(--border)">
    <div class="field">
      <label>คลังหลัก (STOCK / คลัง)</label>
      <select id="f-khlang"><option value="">— เลือกคลัง —</option></select>
      <div class="hint">ต้นทาง: สินค้าสำรองหลัก</div>
    </div>
    <div class="field">
      <label>หน้าร้าน (FRONT / รวมส่ง)</label>
      <select id="f-front"><option value="">— เลือกหน้าร้าน —</option></select>
      <div class="hint">ปลายทาง: จุดขาย</div>
    </div>
    <div id="wh-status"></div>
    <button style="width:100%;padding:10px;border-radius:9px;border:1.5px solid var(--border);
                   background:#f8fafc;font-size:13px;font-weight:700;color:var(--bar);cursor:pointer;margin-bottom:14px"
            onclick="loadWarehouses()">📡 โหลดรายชื่อคลังจาก ZORT</button>
    <hr style="margin:14px 0;border:none;border-top:1px solid var(--border)">
    <div class="row2">
      <div class="field">
        <label>CYCLE (วัน)</label>
        <input id="f-cycle" type="number" min="1" max="30" value="4">
        <div class="hint">เติมทุกกี่วัน</div>
      </div>
      <div class="field">
        <label>BUFFER (เท่า)</label>
        <input id="f-buffer" type="number" min="1" max="3" step="0.1" value="1.5">
        <div class="hint">สต็อกเผื่อ</div>
      </div>
    </div>
    <button id="btn-save" onclick="saveSettings()">💾 บันทึกและโหลดข้อมูล</button>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────
let allProducts = [];
let cycle  = 4;
let buffer = 1.5;
let dFilter = 'all';
let currentCat = null;
let CATS = [];

// ── Boot ───────────────────────────────────────────────────
(async () => {
  const cfg = await (await fetch('/api/config')).json();
  if (!cfg.storename || !cfg.apikey) { openSettings(); return; }
  cycle  = cfg.cycle_days || 4;
  buffer = cfg.buffer     || 1.5;
  document.getElementById('cslider').value = cycle;
  document.getElementById('cycle-lbl').textContent = cycle + ' วัน';
  await loadData();
})();

// ── Load data ─────────────────────────────────────────────
async function loadData() {
  setLoading(true); hideError();
  try {
    const r = await fetch('/api/refresh');
    const d = await r.json();
    if (!r.ok) { showError(d.error || 'เกิดข้อผิดพลาด'); return; }
    allProducts = d.products || [];
    cycle  = d.cycle_days || cycle;
    buffer = d.buffer     || buffer;
    document.getElementById('cslider').value = cycle;
    document.getElementById('cycle-lbl').textContent = cycle + ' วัน';
    document.getElementById('top-sub').textContent = 'อัปเดต ' + d.refreshed_at;
    buildHome();
  } catch(e) {
    showError('เชื่อมต่อเซิร์ฟเวอร์ไม่ได้');
  } finally {
    setLoading(false);
  }
}

// ── Calc per product ──────────────────────────────────────
function calc(p) {
  const target  = Math.round(p.daily * cycle * buffer);
  const needed  = Math.max(0, target - p.front);
  const canMove = Math.min(needed, p.khlang);
  return {...p, target, needed, canMove, noKhl: p.khlang === 0 && needed > 0};
}

// ── Build categories ──────────────────────────────────────
function buildCategories() {
  const cats = {};
  allProducts.forEach(p => {
    if (!cats[p.category]) cats[p.category] = {name:p.category, s90:0, products:[]};
    cats[p.category].s90 += p.rev10;
    cats[p.category].products.push(p);
  });
  const sorted = Object.values(cats).sort((a,b) => b.s90 - a.s90);
  const total  = sorted.reduce((s,c) => s + c.s90, 0);
  let cum = 0;
  sorted.forEach(c => {
    cum += c.s90;
    const pct = cum / total;
    c.abc = pct <= 0.70 ? 'A' : pct <= 0.90 ? 'B' : 'C';
  });
  // ABC per product within category
  sorted.forEach(cat => {
    const ps  = [...cat.products].sort((a,b) => b.sales14 - a.sales14);
    const tot = ps.reduce((s,p) => s + p.sales14, 0);
    let c2 = 0;
    ps.forEach(p => {
      c2 += p.sales14;
      const pct = c2 / tot;
      p.abc = pct <= 0.70 ? 'A' : pct <= 0.90 ? 'B' : 'C';
    });
  });
  return sorted;
}

// ── Build home screen ─────────────────────────────────────
function buildHome() {
  CATS = buildCategories();
  const allCalc = allProducts.map(calc);
  const need    = allCalc.filter(p => p.canMove > 0);
  document.getElementById('stats-row').innerHTML =
    `<span class="s-chip tot">ต้องเติม ${need.length}/${allCalc.length} รายการ</span>
     <span class="s-chip a">🟢 A: ${need.filter(p=>p.abc==='A').length}</span>
     <span class="s-chip b">🟡 B: ${need.filter(p=>p.abc==='B').length}</span>
     <span class="s-chip c">🔴 C: ${need.filter(p=>p.abc==='C').length}</span>`;

  const grid = document.getElementById('cat-grid');
  if (!CATS.length) {
    grid.innerHTML = `<div id="empty"><div class="ico">📦</div>
      <div class="msg">ยังไม่มีข้อมูล</div>
      <div class="hint">กด ⚙️ ตั้งค่า แล้วกด 🔄 โหลด</div></div>`;
    return;
  }
  grid.innerHTML = CATS.map((cat, i) => {
    const prods   = cat.products.map(calc);
    const needN   = prods.filter(p => p.canMove > 0).length;
    const total   = prods.length;
    const urgPct  = needN / total;
    const noKhlN  = prods.filter(p => p.noKhl).length;
    const anyNeed = needN + noKhlN;
    const bdgCls  = anyNeed === 0 ? 'ok' : (urgPct >= 0.5 || noKhlN > 0) ? 'urgent' : 'warn';
    const bdgTxt  = anyNeed === 0 ? `✓ OK ทั้งหมด` : (needN > 0 && noKhlN > 0) ? `⚠️ ${needN} ⛔ ${noKhlN}` : needN > 0 ? `⚠️ เติม ${needN}/${total}` : `⛔ สั่ง ${noKhlN}`;
    return `<div class="cat-card ${cat.abc}" onclick="openCat('${cat.name.replace(/'/g,"\\'")}')">
      <div class="rank-num">${i+1}</div>
      <div class="abc ${cat.abc}">${cat.abc}</div>
      <div class="cname">${cat.name}</div>
      <div class="csales">${(cat.s90/1000).toFixed(1)}K ชิ้น / 14 วัน</div>
      <div class="cneed"><span class="n-badge ${bdgCls}">${bdgTxt}</span></div>
    </div>`;
  }).join('');
  // ── Zero-stock section ──────────────────────────────────────
  const zeroProds = allCalc.filter(p => p.khlang === 0 && p.front === 0 && p.daily > 0);
  let zs = document.getElementById('zero-sec');
  if (!zs) {
    zs = document.createElement('div');
    zs.id = 'zero-sec';
    zs.style.cssText = 'margin:4px 0 12px;background:var(--card);border-radius:12px;overflow:hidden';
    grid.insertAdjacentElement('afterend', zs);
  }
  if (zeroProds.length > 0) {
    const grps = {};
    zeroProds.slice().sort((a,b) => b.daily - a.daily).forEach(p => {
      if (!grps[p.category]) grps[p.category] = [];
      grps[p.category].push(p);
    });
    zs.innerHTML = '<div style="padding:10px 16px;font-size:12px;font-weight:700;color:var(--red);border-bottom:1px solid rgba(0,0,0,.08)">⛔ สต๊อกหมดทุกที่ (' + zeroProds.length + ' รายการ) — ต้องสั่งซัพพลายเออร์</div>' +
      Object.entries(grps).map(([cat,ps]) =>
        '<div style="padding:5px 16px;font-size:10px;font-weight:600;color:var(--sub);background:var(--bg)">' + cat + '</div>' +
        ps.map(p => '<div class="prod-row out"><div class="pr-abc ' + p.abc + '">' + p.abc + '</div><div class="pr-info"><div class="pr-name">' + p.name + '</div><div class="pr-sku">' + p.sku + ' · ' + p.daily.toFixed(2) + '/วัน</div></div><div class="pr-stocks"><div class="pr-stk">ร้าน: <b class="zero">0</b></div><div class="pr-stk">คลัง: <b>0</b></div></div><div class="pr-action"><span class="act-out">⛔ สั่งเพิ่ม</span></div></div>').join('')
      ).join('');
  } else {
    zs.innerHTML = '';
  }
}

// ── Open category detail ──────────────────────────────────
function openCat(catName) {
  currentCat = CATS.find(c => c.name === catName);
  if (!currentCat) return;
  dFilter = 'all';
  document.getElementById('dh-abc').textContent = currentCat.abc;
  document.getElementById('dh-abc').className   = `dh-abc ${currentCat.abc}`;
  document.getElementById('dh-name').textContent = catName;
  renderDetail();
  document.getElementById('home-screen-wrap').classList.remove('active');
  const ds = document.getElementById('detail-screen');
  ds.style.display = 'flex'; ds.classList.add('active');
  document.getElementById('back-btn').classList.add('show');
  document.getElementById('top-sub').textContent = catName;
  document.querySelectorAll('#detail-filter .fpill').forEach(b => b.classList.remove('active'));
  document.querySelector('#detail-filter .fpill').classList.add('active');
}

function goHome() {
  document.getElementById('home-screen-wrap').classList.add('active');
  const ds = document.getElementById('detail-screen');
  ds.style.display = 'none'; ds.classList.remove('active');
  document.getElementById('back-btn').classList.remove('show');
  const cfg_ts = document.getElementById('top-sub').getAttribute('data-ts') || '';
  document.getElementById('top-sub').textContent = cfg_ts || 'ระบบเติมสินค้า';
  buildHome();
}

// ── Render detail ─────────────────────────────────────────
function renderDetail() {
  if (!currentCat) return;
  let prods = currentCat.products.map(calc);
  if (dFilter === 'need')                      prods = prods.filter(p => p.canMove > 0 || p.noKhl);
  else if (['A','B','C'].includes(dFilter))    prods = prods.filter(p => p.abc === dFilter);
  else if (dFilter === 'ready')                   prods = prods.filter(p => p.front > 0);
  const ao = {A:0,B:1,C:2};
  prods.sort((a,b) =>
    ((b.canMove>0||b.noKhl)?1:0) - ((a.canMove>0||a.noKhl)?1:0) ||
    ao[a.abc] - ao[b.abc] || b.daily - a.daily
  );
  const allCalc = currentCat.products.map(calc);
  const needN   = allCalc.filter(p => p.canMove > 0).length;
  const noKhlN  = allCalc.filter(p => p.noKhl).length;
  document.getElementById('dh-stats').innerHTML =
    `<span><b>${(currentCat.s90/1000).toFixed(1)}K</b> ชิ้น/14วัน</span>
     <span><b>${allCalc.length}</b> SKUs</span>
     <span style="color:var(--red)"><b>${needN}</b> ต้องเติม</span>
     ${noKhlN ? `<span style="color:var(--orange)"><b>${noKhlN}</b> คลังหมด</span>` : ''}`;

  const el = document.getElementById('prod-list');
  if (!prods.length) {
    el.innerHTML = `<div class="no-result"><div class="ico">✅</div>ไม่มีรายการที่ตรงเงื่อนไข</div>`;
    return;
  }
  el.innerHTML = prods.map(p => {
    const rowCls = p.noKhl ? 'out' : p.canMove > 0 ? 'urgent' : '';
    const isLow  = p.front <= Math.floor(p.daily * 1.5);
    const isZero = p.front === 0;
    const fCls   = isZero ? 'zero' : isLow ? 'low' : '';
    let actionHtml;
    if (p.noKhl)        actionHtml = `<span class="act-out">⛔ สั่งเพิ่ม</span>`;
    else if (p.canMove > 0) actionHtml = `<span class="act-need">+${Math.ceil(p.canMove)} ชิ้น</span>`;
    else                actionHtml = `<span class="act-ok">✓ OK</span>`;
    return `<div class="prod-row ${rowCls}">
      <div class="pr-abc ${p.abc}">${p.abc}</div>
      <div class="pr-info">
        <div class="pr-name">${p.name}</div>
        <div class="pr-sku">${p.sku} · ${p.daily}/วัน</div>
      </div>
      <div class="pr-stocks">
        <div class="pr-stk">ร้าน: <b class="${fCls}">${p.front}</b></div>
        <div class="pr-stk">คลัง: <b>${p.khlang}</b></div>
      </div>
      <div class="pr-action">${actionHtml}</div>
    </div>`;
  }).join('');
}

function setDF(mode, el) {
  dFilter = mode;
  document.querySelectorAll('#detail-filter .fpill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  renderDetail();
}

function setCycle(v) {
  cycle = v;
  document.getElementById('cycle-lbl').textContent = v + ' วัน';
  buildHome();
  if (currentCat && document.getElementById('detail-screen').classList.contains('active'))
    renderDetail();
  fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({cycle_days: cycle})});
}

// ── Settings ──────────────────────────────────────────────
async function openSettings() {
  const cfg = await (await fetch('/api/config')).json();
  document.getElementById('f-storename').value = cfg.storename || '';
  document.getElementById('f-apikey').value    = cfg.apikey    || '';
  document.getElementById('f-apisecret').value = '';
  document.getElementById('f-cycle').value     = cfg.cycle_days || 4;
  document.getElementById('f-buffer').value    = cfg.buffer     || 1.5;
  const ks = document.getElementById('f-khlang');
  const fs = document.getElementById('f-front');
  if (cfg.khlang_code && !ks.querySelector(`option[value="${cfg.khlang_code}"]`))
    ks.appendChild(new Option(cfg.khlang_code, cfg.khlang_code, true, true));
  if (cfg.front_code  && !fs.querySelector(`option[value="${cfg.front_code}"]`))
    fs.appendChild(new Option(cfg.front_code,  cfg.front_code,  true, true));
  ks.value = cfg.khlang_code || '';
  fs.value = cfg.front_code  || '';
  document.getElementById('settings').classList.add('open');
}
function closeSettings() { document.getElementById('settings').classList.remove('open'); }

async function loadWarehouses() {
  const status = document.getElementById('wh-status');
  status.textContent = '⏳ กำลังโหลด...';
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
    ks.appendChild(new Option(`${w.name} [${w.code}]`, w.code, false, w.code === curK));
    fs.appendChild(new Option(`${w.name} [${w.code}]`, w.code, false, w.code === curF));
  });
  d.forEach(w => {
    const n = w.name.toLowerCase();
    if (!ks.value && (n.includes('คลัง') || n.includes('stock'))) ks.value = w.code;
    if (!fs.value && (n.includes('รวมส่ง') || n.includes('หน้าร้าน') || n.includes('front'))) fs.value = w.code;
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
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
               body: JSON.stringify(cfg)});
  cycle  = cfg.cycle_days;
  buffer = cfg.buffer;
  document.getElementById('cslider').value = cycle;
  document.getElementById('cycle-lbl').textContent = cycle + ' วัน';
  closeSettings();
  await loadData();
}

// ── Utils ─────────────────────────────────────────────────
function setLoading(on) { document.getElementById('spin').style.display = on?'block':'none'; }
function showError(msg) {
  const el = document.getElementById('err-box');
  el.textContent = '⚠️ ' + msg; el.style.display = 'block';
}
function hideError() { document.getElementById('err-box').style.display = 'none'; }
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
