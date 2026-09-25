# -*- coding: utf-8 -*-
"""
sync_sheet.py — GitHub Actions 版：腾讯文档表格 → prices.json 自动同步

数据源：腾讯文档表格（公开只读分享），链接通过环境变量 SHEET_URL 传入
  （由 GitHub Actions Secret 提供，不在代码中明文保存）。
  只读取「名称」(A列) 与「计算器价格」(B列)。

流程（由 GitHub Actions 每 30 分钟触发一次）：
  1. playwright 无头打开表格，等待渲染完成后从内存模型取单元格值
  2. 对比仓库根 prices.json：名称或价格有变化 → 重建并写回
  3. 无变化 → 不写文件（不触发提交与部署）

用法：
  python3 sync_sheet.py            # 单次同步（GitHub Actions 使用）
"""
import json
import os
import re
import sys
import time
import datetime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRICES_JSON = os.path.join(BASE_DIR, "prices.json")
# 腾讯文档链接从 GitHub Actions Secret（SHEET_URL）读取，避免公开在仓库代码中
SHEET_URL = os.environ.get("SHEET_URL", "")
if not SHEET_URL:
    raise SystemExit("缺少 SHEET_URL 环境变量（腾讯文档链接未配置）")

# 表格名称 → 固定 id（商品）；其余自动分配 fl-xx
NAME_TO_ID = {
    "mini20p 盒装": "mini20p",
    "mini10p 盒装": "mini10p",
    "迷你锡纸": "mini-tinfoil",
    "SQ 20p": "sq20p",
    "SQ 锡纸": "sq-tinfoil",
    "WIDE20p": "wide20p",
    "WIDE 锡纸": "wide-tinfoil",
    "一次性胶片机": "film-camera",
}
SPEC_MAP = {
    "mini20p 盒装": "20 张/盒", "mini10p 盒装": "10 张/盒", "迷你锡纸": "散装",
    "SQ 20p": "20 张/盒", "SQ 锡纸": "散装", "WIDE20p": "20 张/盒",
    "WIDE 锡纸": "散装", "一次性胶片机": "一次性",
}
UNIT_MAP = {"一次性胶片机": "台", "迷你锡纸": "包", "SQ 锡纸": "包", "WIDE 锡纸": "包"}


def extract_sheet():
    """用 playwright 打开表格，返回 (rows, updated_at)。
    rows: [[行号(idx, 0起), 名称, 价格数值], ...]；updated_at 为表格首行「更新时间」文本。
    打开失败自动重试（最多 5 次，间隔 30 秒，降低连续请求被限流概率）。"""
    from playwright.sync_api import sync_playwright
    chromium_path = os.environ.get("CHROMIUM_PATH") or None
    last_err = None
    with sync_playwright() as p:
        for attempt in range(1, 6):
            browser = None
            try:
                browser = p.chromium.launch(
                    executable_path=chromium_path,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = browser.new_page()
                page.goto(SHEET_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(15000)
                result = page.evaluate("""() => {
                    const wm = window.SpreadsheetApp.workbook.worksheetManager;
                    if (!wm || !wm.sheetList || !wm.sheetList.length) return {ok:false, err:'no sheet'};
                    const sheet = wm.sheetList[0];
                    const getVal = (cd) => {
                        if (!cd) return '';
                        let v = cd.value;
                        if (v && typeof v === 'object') {
                            if (v.formulaResult && v.formulaResult.value !== undefined) return v.formulaResult.value;
                            return '';
                        }
                        return (v === null || v === undefined) ? '' : v;
                    };
                    const out = [];
                    const maxR = Math.min(120, sheet.getRowCount());
                    for (let r=0; r<maxR; r++) {
                        let a='', b='';
                        try { const v=getVal(sheet.getCellDataAtPosition(r,0)); a=(v===null||v===undefined)?'':String(v); } catch(e){}
                        try { const v=getVal(sheet.getCellDataAtPosition(r,1)); b=(v===null||v===undefined)?'':String(v); } catch(e){}
                        if (a || b) out.push([r, a, b]);
                    }
                    return {ok:true, rows: out};
                }""")
                if not result.get("ok"):
                    raise RuntimeError(result.get("err", "extract failed"))
                rows = []
                updated_at = ""
                for row in result["rows"]:
                    if row[0] == 0:
                        updated_at = str(row[1])
                        continue
                    name, price = row[1], row[2]
                    if name and name != "名称":
                        # 价格可留空（如「一年以上」档位只填名称）；名称必须保留
                        price_num = None
                        if price not in ("", None):
                            try:
                                price_num = float(price)
                            except (TypeError, ValueError):
                                price_num = None
                        rows.append((row[0], name, price_num))
                return rows, updated_at
            except Exception as e:
                last_err = e
                print(f"[sync] 读取表格第 {attempt} 次失败: {e}")
                if attempt < 5:
                    time.sleep(30)
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
    raise RuntimeError(f"读取表格连续 5 次失败: {last_err}")


# 表格布局约定：
#   第2行（idx 1）= 一年以上档（A=名称，B=可留空，无折扣）
#   第3行（idx 2）= 27年档（A=名称，B=折扣价，写正数表示减X元）
#   第4行（idx 3）= 26年档（A=名称，B=折扣价，写正数表示减X元）
#   第6行起（idx >= 5）= 商品（A=名称，B=计算器价格）
DISCOUNT_ROW_KEYS = {1: "now", 2: "2027", 3: "2026"}


def parse_discounts(rows):
    """从折扣行（idx 1/2/3）解析年份折扣，返回 (labels, amounts)。
    labels: {'now': '一年以上', '2027': '27年', ...}；amounts: {'2027': -5, ...}"""
    labels, amounts = {}, {}
    for idx, name, price in rows:
        key = DISCOUNT_ROW_KEYS.get(idx)
        if not key:
            continue
        if name:
            labels[key] = str(name).strip()
        if key == "now":
            continue  # 一年以上为基础价档，无折扣
        if isinstance(price, (int, float)):
            amounts[key] = -abs(int(price))  # 表格写正数（减X元），存储为负
    return labels, amounts


def build_groups(rows, cur):
    """由表格行构建 groups（rows 为 (idx, name, price)，仅商品行；id 按名称稳定映射）。"""
    name_to_id = dict(NAME_TO_ID)
    used = set(name_to_id.values())
    fl_counter = 1
    if cur:
        for g in cur.get("groups", []):
            gid, name = g.get("id", ""), g.get("name", "")
            if gid.startswith("fl-") and name and name not in name_to_id:
                name_to_id[name] = gid
                used.add(gid)
    groups = []
    for _idx, name, price in rows:
        gid = name_to_id.get(name)
        if gid is None:
            while f"fl-{fl_counter:02d}" in used:
                fl_counter += 1
            gid = f"fl-{fl_counter:02d}"
            fl_counter += 1
            used.add(gid)
            name_to_id[name] = gid
        is_product = name in NAME_TO_ID
        if isinstance(price, float) and price.is_integer():
            price = int(price)
        groups.append({
            "id": gid, "name": name,
            "unit": UNIT_MAP.get(name, "盒"),
            "spec": SPEC_MAP.get(name, "mini 相纸") if is_product else "mini 相纸",
            "price": price, "formula": "", "badge": "",
            "stock": "现货", "image": "", "items": [],
        })
    return groups


def load_current():
    try:
        with open(PRICES_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def run_once():
    print(f"[sync] {time.strftime('%H:%M:%S')} 开始同步…")
    try:
        rows, updated_at = extract_sheet()
    except Exception as e:
        print("[sync] 读取表格失败:", e)
        sys.exit(1)
    if not rows:
        print("[sync] 表格无数据，跳过")
        sys.exit(1)
    # GitHub 服务器为 UTC，页面时间必须用北京时间（UTC+8）
    saved_at = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    cur = load_current()
    # 按行号分离：折扣行 idx 1/2/3；商品行 idx >= 5（第6行起，且必须有价格）
    goods_rows = [x for x in rows if x[0] >= 5 and x[2] is not None]
    new_groups = build_groups(goods_rows, cur)
    # 年份折扣：第2~3行（名称可改，行号固定）
    d_labels, d_amounts = parse_discounts(rows)
    new_discounts = d_amounts if d_amounts else {"2027": -8, "2026": -23}
    changed = False
    if cur is None:
        changed = True
    else:
        old_groups = cur.get("groups", [])
        old_map = {g["id"]: g for g in old_groups}
        if len(old_groups) != len(new_groups):
            changed = True
        else:
            for g in new_groups:
                og = old_map.get(g["id"])
                if og is None or og.get("price") != g["price"] or og.get("name") != g["name"]:
                    changed = True
                    break
        if not changed and (
            cur.get("discounts") != new_discounts
            or (cur.get("discount_labels") or {}) != d_labels
        ):
            changed = True
    # 每次同步都刷新 saved_at（页面顶部「价格更新」= GitHub 最近同步时间）；
    # 价格有变化时同样重写全部数据。
    data = cur if cur is not None else {}
    data["_说明"] = "价格数据由 GitHub Actions 定时从腾讯文档表格自动生成，请勿手改；改价请在腾讯文档表格中操作（第2~3行=年份折扣，第5行起=商品）。saved_at 为最近一次成功同步时间。"
    data.setdefault("shop", {"name": "拍立得价格计算器", "contact": "微信：Acssxiaolei", "notice": ""})
    data["saved_at"] = saved_at
    data.setdefault("currency", "¥")
    data["discounts"] = new_discounts
    if d_labels:
        data["discount_labels"] = d_labels
    else:
        data.pop("discount_labels", None)
    data["groups"] = new_groups
    with open(PRICES_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if changed:
        print(f"[sync] 价格有变化，已更新 prices.json（{len(new_groups)} 组，同步时间 {saved_at}）")
    else:
        print(f"[sync] 价格无变化，仅刷新同步时间（{saved_at}）")
    return changed


if __name__ == "__main__":
    run_once()
