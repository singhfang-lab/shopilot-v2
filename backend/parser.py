import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
from markitdown import MarkItDown

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = "gemini-2.0-flash"
_OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

_md = MarkItDown()

EXTRACT_PROMPT = """You are a data extraction assistant. The file content below (in Markdown) is a sales/business report.

Extract every transaction or sales record and return ONLY a valid JSON array — no markdown, no explanation.

Each element must have exactly these fields:
- "date": "YYYY-MM-DD"
- "item_name": product or item name as string
- "quantity": integer (units sold or order count)
- "amount": float, total revenue in local currency (IDR — do NOT convert, keep as-is)
- "channel": sales channel string (e.g. "Walk-in", "Delivery App", "WhatsApp", or store name)
- "store": store ID or store name if available, else ""

Rules:
- If amount is missing but unit_price and quantity exist, compute amount = unit_price * quantity.
- Skip header rows, summary rows, empty rows, review/comment sheets.
- If the file has order-level data (no item names), use order total as amount and "order" as item_name.
- Prefer item-level rows (with SKU/product name) over order-level rows when both exist.

File content:
{content}
"""


def _file_to_markdown(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    # For Excel with multiple sheets, convert each sheet separately
    # and prioritise item/sales-level sheets to stay within token budget
    if suffix in (".xlsx", ".xls"):
        xl = pd.ExcelFile(file_path)
        SKIP_SHEETS = {"customer_reviews", "staff_shift_log", "competitor_observation"}
        PRIORITY_KEYWORDS = ["item", "sales", "order_item", "sku", "product", "transaction"]

        parts = []
        for sheet in xl.sheet_names:
            if sheet.lower().replace(" ", "_") in SKIP_SHEETS:
                continue
            df = xl.parse(sheet)
            csv_text = f"## {sheet}\n{df.to_csv(index=False)}"
            parts.append((sheet, csv_text))

        # Sort: sheets with priority keywords come first
        parts.sort(key=lambda x: not any(kw in x[0].lower() for kw in PRIORITY_KEYWORDS))
        return "\n\n".join(p[1] for p in parts)

    result = _md.convert(str(file_path))
    return result.text_content or ""


def _call_gemini_sync(prompt: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY not set")
    url = f"{GEMINI_API_BASE}/{GEMINI_MODEL}:generateContent?key={key}"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    resp = httpx.post(url, json=payload, timeout=60.0)
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_ollama_sync(prompt: str) -> str:
    resp = httpx.post(
        f"{_OLLAMA_BASE}/api/generate",
        json={"model": "gemma4:e4b", "prompt": prompt, "stream": False},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _parse_json_response(text: str) -> list[dict] | None:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("["):
                text = part
                break
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    return None


def _normalise_records(raw: list) -> list[dict]:
    out = []
    for r in raw:
        try:
            out.append({
                "date":      str(r.get("date", ""))[:10],
                "item_name": str(r.get("item_name", "Unknown")),
                "quantity":  int(float(r.get("quantity", 1) or 1)),
                "amount":    float(r.get("amount", 0) or 0),
                "channel":   str(r.get("channel", "other")),
                "store":     str(r.get("store", "")),
            })
        except (TypeError, ValueError):
            continue
    return [r for r in out if r["date"] and r["date"] != "NaT"]


def _validate(records: list[dict]) -> tuple[bool, list[str]]:
    if not records:
        return False, ["No records parsed"]
    warnings = []
    nonzero = sum(1 for r in records if r.get("amount", 0) > 0)
    zero_pct = (len(records) - nonzero) / len(records) * 100
    if zero_pct == 100:
        warnings.append("所有记录金额为 0，价格列可能未能识别")
    elif zero_pct > 50:
        warnings.append(f"{zero_pct:.0f}% 的记录金额为 0")
    return zero_pct < 100, warnings


def _pandas_fast_parse(file_path: Path) -> list[dict] | None:
    """
    Fast pandas parse for well-structured Excel/CSV files.
    Maps common column name variants to standard fields without AI.
    Returns None if file format is unrecognised.
    """
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".csv":
            sheets = {"_": pd.read_csv(file_path)}
        elif suffix in (".xlsx", ".xls"):
            xl = pd.ExcelFile(file_path)
            sheets = {name: xl.parse(name) for name in xl.sheet_names}
        else:
            return None
    except Exception:
        return None

    SKIP = {"customer_reviews", "staff_shift_log", "competitor_observation"}

    # Build a sku→price map from any sheet that looks like a menu/price master
    price_map: dict[str, float] = {}
    PRICE_COLS = ["list_price_idr", "unit_price_idr", "price_idr", "unit_price", "price", "list_price"]
    for name, df in sheets.items():
        lower_cols = {c.lower().strip(): c for c in df.columns}
        sku_col = lower_cols.get("sku") or lower_cols.get("item_name") or lower_cols.get("product")
        price_col = next((lower_cols[c] for c in PRICE_COLS if c in lower_cols), None)
        if sku_col and price_col:
            for _, row in df.iterrows():
                sku = str(row[sku_col]).strip()
                price = float(pd.to_numeric(row[price_col], errors="coerce") or 0)
                if sku and price:
                    price_map[sku] = price

    # Column name aliases → standard field
    COL_MAP = {
        "item_name": ["item_name", "sku", "product", "product_name", "item", "name", "menu_item"],
        "quantity":  ["quantity", "qty", "qty_sold", "units_sold", "count", "items_count"],
        "amount":    ["amount", "total_idr", "revenue_idr", "ticket_total_idr", "gross_sales_idr",
                      "net_sales_idr", "sales_amount"],
        "date":      ["date", "order_date", "transaction_date", "sale_date"],
        "channel":   ["channel", "sales_channel", "store_id", "platform", "source"],
        "store":     ["store", "store_id", "store_name", "branch"],
    }

    def _find_col(df_cols: list[str], candidates: list[str]) -> str | None:
        lower = {c.lower().strip(): c for c in df_cols}
        for cand in candidates:
            if cand in lower:
                return lower[cand]
        return None

    # Sort sheets: item/SKU-level sheets first, then order/daily-level
    ITEM_KEYWORDS = ["item", "sku", "product", "transaction", "order_item", "sales_raw"]
    sorted_sheets = sorted(
        sheets.items(),
        key=lambda x: not any(kw in x[0].lower() for kw in ITEM_KEYWORDS)
    )

    for name, df in sorted_sheets:
        if name.lower().replace(" ", "_") in SKIP:
            continue
        cols = list(df.columns)
        item_col  = _find_col(cols, COL_MAP["item_name"])
        qty_col   = _find_col(cols, COL_MAP["quantity"])
        amt_col   = _find_col(cols, COL_MAP["amount"])
        date_col  = _find_col(cols, COL_MAP["date"])
        chan_col  = _find_col(cols, COL_MAP["channel"])
        store_col = _find_col(cols, COL_MAP["store"])

        # Need at least date + (item or amount)
        if not date_col:
            continue
        if not item_col and not amt_col:
            continue

        # If amount missing, try: inline price col, then price_map lookup
        if not amt_col:
            price_col = _find_col(cols, ["unit_price_idr", "unit_price", "price_idr", "price", "list_price_idr", "list_price"])
            if price_col and qty_col:
                df = df.copy()
                df["_amount"] = (
                    pd.to_numeric(df[price_col], errors="coerce").fillna(0) *
                    pd.to_numeric(df[qty_col], errors="coerce").fillna(1)
                )
                amt_col = "_amount"
            elif price_map and item_col and qty_col:
                # Look up price from another sheet's price_map
                df = df.copy()
                df["_amount"] = df.apply(
                    lambda r: price_map.get(str(r[item_col]).strip(), 0) *
                              float(pd.to_numeric(r[qty_col], errors="coerce") or 1),
                    axis=1
                )
                amt_col = "_amount"

        records = []
        for _, row in df.iterrows():
            try:
                date_val = str(row[date_col])[:10] if date_col else ""
                if not date_val or date_val in ("nan", "NaT", ""):
                    continue
                records.append({
                    "date":      date_val,
                    "item_name": str(row[item_col]).strip() if item_col else "order",
                    "quantity":  int(pd.to_numeric(row[qty_col], errors="coerce") or 1) if qty_col else 1,
                    "amount":    float(pd.to_numeric(row[amt_col], errors="coerce") or 0) if amt_col else 0,
                    "channel":   str(row[chan_col]) if chan_col else "other",
                    "store":     str(row[store_col]) if store_col else "",
                })
            except Exception:
                continue

        if records:
            return records  # Return first usable sheet

    return None


def ai_parse_file(file_path) -> list[dict] | dict:
    path = Path(file_path)

    # ── Level 1: fast pandas parse (offline, no AI needed) ──
    result = _pandas_fast_parse(path)
    if result:
        ok, warnings = _validate(result)
        if ok:
            return result
        if result:
            return {"records": result, "warnings": warnings}

    # ── Level 2: AI parse on a sample via MarkItDown ──
    try:
        markdown = _file_to_markdown(path)
    except Exception as e:
        return {"error": f"Cannot read file: {e}", "warnings": []}

    if not markdown.strip():
        return {"error": "File appears to be empty", "warnings": []}

    # Send only first sheet's worth of data to stay within context
    content = markdown[:10000]
    prompt = EXTRACT_PROMPT.format(content=content)

    # Try Gemini first
    if os.environ.get("GEMINI_API_KEY"):
        try:
            raw_text = _call_gemini_sync(prompt)
            records = _parse_json_response(raw_text)
            if records:
                normalised = _normalise_records(records)
                ok, warnings = _validate(normalised)
                if ok:
                    return normalised
                if normalised:
                    return {"records": normalised, "warnings": warnings}
        except Exception:
            pass

    # Fallback: Ollama
    try:
        raw_text = _call_ollama_sync(prompt)
        records = _parse_json_response(raw_text)
        if records:
            normalised = _normalise_records(records)
            ok, warnings = _validate(normalised)
            if ok:
                return normalised
            if normalised:
                return {"records": normalised, "warnings": warnings}
    except Exception:
        pass

    return {"error": "Unable to extract records from file", "warnings": []}


def merge_reports(file_paths: list) -> tuple[list[dict], list[str]]:
    all_records = []
    all_warnings: list[str] = []
    for fp in file_paths:
        result = ai_parse_file(fp)
        if isinstance(result, list):
            all_records.extend(result)
        elif isinstance(result, dict):
            if "records" in result:
                all_records.extend(result["records"])
            if "warnings" in result:
                all_warnings.extend(result["warnings"])
    all_records.sort(key=lambda r: r.get("date", ""))
    return all_records, all_warnings


def analyze(merged_data: list[dict]) -> dict:
    if not merged_data:
        return {k: {} for k in ["sales_trend", "channel_breakdown", "top_items", "hourly_heatmap", "store_comparison"]}

    df = pd.DataFrame(merged_data)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(1)
    if "store" not in df.columns:
        df["store"] = ""

    has_stores = df["store"].nunique() > 1

    cutoff = df["date"].max() - timedelta(days=29)
    trend_base = df[df["date"] >= cutoff]
    all_dates = [(df["date"].max() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]

    STORE_COLORS = {
        "SJA": "#003178", "SJB": "#2563eb", "PIK": "#ef4444",
        "South Jakarta A": "#003178", "South Jakarta B": "#2563eb",
    }
    DEFAULT_COLORS = ["#003178", "#2563eb", "#22c55e", "#f59e0b", "#ef4444"]

    if has_stores:
        stores = sorted(df["store"].dropna().unique())
        datasets = []
        for i, store in enumerate(stores):
            sdf = trend_base[trend_base["store"] == store].groupby(
                trend_base["date"].dt.strftime("%Y-%m-%d"))["amount"].sum()
            color = STORE_COLORS.get(store, DEFAULT_COLORS[i % len(DEFAULT_COLORS)])
            datasets.append({
                "label": store,
                "data": [round(sdf.get(d, 0), 2) for d in all_dates],
                "borderColor": color,
                "backgroundColor": color + "22",
            })
    else:
        total = trend_base.groupby(trend_base["date"].dt.strftime("%Y-%m-%d"))["amount"].sum()
        datasets = [{"label": "每日销售额 (IDR)", "data": [round(total.get(d, 0), 2) for d in all_dates]}]

    sales_trend = {"labels": all_dates, "datasets": datasets}

    if has_stores:
        store_rev = df.groupby("store")["amount"].sum()
        store_ord = df.groupby("store")["quantity"].sum()
        stores_list = store_rev.index.tolist()
        store_comparison = {
            "labels": stores_list,
            "datasets": [
                {"label": "总销售额 (IDR)", "data": [round(v, 2) for v in store_rev.values], "yAxisID": "y"},
                {"label": "总订单/销量", "data": [int(v) for v in store_ord.reindex(stores_list, fill_value=0).values], "yAxisID": "y1", "type": "line"},
            ],
        }
    else:
        store_comparison = {}

    item_df = df.groupby("item_name")["quantity"].sum().nlargest(10)
    if has_stores:
        top10 = item_df.index.tolist()
        stores = sorted(df["store"].dropna().unique())
        pivot = df[df["item_name"].isin(top10)].groupby(["item_name", "store"])["quantity"].sum().unstack(fill_value=0)
        top_items = {
            "labels": top10,
            "datasets": [
                {
                    "label": store,
                    "data": [int(pivot.get(store, pd.Series()).get(item, 0)) for item in top10],
                    "backgroundColor": STORE_COLORS.get(store, DEFAULT_COLORS[i % len(DEFAULT_COLORS)]),
                }
                for i, store in enumerate(stores)
            ],
        }
    else:
        top_items = {
            "labels": item_df.index.tolist(),
            "datasets": [{"label": "销量", "data": item_df.values.tolist()}],
        }

    channel_df = df.groupby("channel")["amount"].sum()
    channel_breakdown = {
        "labels": channel_df.index.tolist(),
        "datasets": [{"label": "销售额 (IDR)", "data": [round(v, 2) for v in channel_df.values]}],
    }

    df["dow"] = df["date"].dt.day_name()
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    if has_stores:
        stores = sorted(df["store"].dropna().unique())
        datasets = []
        for i, store in enumerate(stores):
            sdf = df[df["store"] == store].groupby("dow")["amount"].sum().reindex(dow_order, fill_value=0)
            datasets.append({
                "label": store,
                "data": [round(v, 2) for v in sdf.values],
                "backgroundColor": STORE_COLORS.get(store, DEFAULT_COLORS[i % len(DEFAULT_COLORS)]),
            })
    else:
        dow_df = df.groupby("dow")["amount"].sum().reindex(dow_order, fill_value=0)
        datasets = [{"label": "销售额", "data": [round(v, 2) for v in dow_df.values]}]

    hourly_heatmap = {"labels": dow_labels, "datasets": datasets}

    return {
        "sales_trend": sales_trend,
        "store_comparison": store_comparison,
        "top_items": top_items,
        "channel_breakdown": channel_breakdown,
        "hourly_heatmap": hourly_heatmap,
    }
