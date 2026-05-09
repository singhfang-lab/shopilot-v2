import asyncio
import json
import logging
import os
import httpx
import pandas as pd
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Form, Request, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

from .prompts import build_system_prompt
from .competitor import search_competitors, get_competitor_details
from .parser import merge_reports, analyze
from . import rag
from . import tools as octools
from . import llm as llm_module
from .db import init_db, get_db, LLMConfig, PlatformKBDocument, KBDocument, UserMerchant
from sqlmodel import Session, select, func
from .auth import router as auth_router, get_current_user_optional, get_merchant_for_user
from .conversations import router as conv_router
from .kb import router as kb_router
from .onboarding import router as onboarding_router
from .admin import router as admin_router

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger("usb_assistant")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    octools.scheduler.start()
    logger.info("APScheduler started")
    asyncio.create_task(_cleanup_data_sessions())
    yield
    octools.scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped")


app = FastAPI(title="USB Assistant API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "http://localhost:3001,http://127.0.0.1:3001").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(conv_router)
app.include_router(kb_router)
app.include_router(onboarding_router)
app.include_router(admin_router)

# Serve frontend static files (production: /app/frontend, dev: ../frontend)
_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(_FRONTEND_DIR / "index.html"))

    @app.get("/{page}.html")
    async def serve_page(page: str):
        path = _FRONTEND_DIR / f"{page}.html"
        if path.exists():
            return FileResponse(str(path))
        return FileResponse(str(_FRONTEND_DIR / "index.html"))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "System busy, please try again"},
    )

UPLOADS_DIR = Path.home() / "usb-assistant" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ── Data Agent session store ──────────────────────────────────────────────────
import time as _time

# session_key → {conn, schema, filenames, created_at}
DATA_SESSIONS: dict[str, dict] = {}
DATA_SESSION_TTL = 3600  # 60 minutes


def _session_key(current_user, shop_id: str) -> str | None:
    if current_user:
        return f"user:{current_user.id}"
    if shop_id:
        return f"shop:{shop_id}"
    return None


async def _cleanup_data_sessions():
    while True:
        await asyncio.sleep(600)
        now = _time.time()
        expired = [k for k, v in list(DATA_SESSIONS.items())
                   if now - v["created_at"] > DATA_SESSION_TTL]
        for k in expired:
            try:
                DATA_SESSIONS[k]["conn"].close()
            except Exception:
                pass
            del DATA_SESSIONS[k]
        if expired:
            logger.info("Cleaned %d expired data sessions", len(expired))


class ChatRequest(BaseModel):
    message: str = ""
    messages: list[dict] = []
    history: list[dict] = []
    shop_config: dict = {}
    model_preference: str = "auto"  # "auto" | "gemini" | "claude" | "openai" | "ollama"
    files: list[str] = []


async def is_online() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.get("https://www.google.com")
        return True
    except Exception:
        return False




def _parse_analysis_card(text: str) -> dict | None:
    import re
    # Standard format: <analysis_card>JSON</analysis_card>
    m = re.search(r'<analysis_card>(.*?)</analysis_card>', text, re.DOTALL)
    if m:
        raw_str = m.group(1).strip()
    else:
        # Gemma variant: <analysis_card{JSON} or <analysis_card>{JSON} (no closing tag)
        m = re.search(r'<analysis_card>?(\{.*)', text, re.DOTALL)
        if not m:
            return None
        raw_str = m.group(1).strip()
        # Trim trailing garbage after the last }
        last = raw_str.rfind('}')
        if last >= 0:
            raw_str = raw_str[:last + 1]
        else:
            return None

    def _normalize(raw: dict) -> dict:
        actions_raw = raw.get("actions") or raw.get("action") or []
        actions = []
        for a in actions_raw:
            if not isinstance(a, dict):
                continue
            task = a.get("task") or a.get("detail") or a.get("text") or str(a)
            urgency = a.get("urgency") or "week"
            if task:
                actions.append({"urgency": urgency, "task": str(task)[:60]})
        fields_raw = raw.get("fields") or []
        fields = [
            {"label": str(f.get("label", "")), "value": str(f.get("value", ""))}
            for f in fields_raw
            if isinstance(f, dict) and f.get("label") and f.get("value")
        ]
        return {
            "type":     str(raw.get("type", "diagnosis")),
            "severity": str(raw.get("severity", "medium")),
            "title":    str(raw.get("title", ""))[:80],
            "subtitle": str(raw.get("subtitle", ""))[:120],
            "fields":   fields[:4],
            "actions":  actions[:5],
        }

    try:
        return _normalize(json.loads(raw_str))
    except Exception:
        pass

    # Gemma often emits unescaped " inside string values and trailing garbage.
    # Scan character-by-character: treat a " as interior (not closing) when
    # the next non-space character is NOT a structural char (, } ] :).
    def _scan_string(src: str, start: int) -> tuple:
        chars = []
        i = start
        while i < len(src):
            c = src[i]
            if c == '\\' and i + 1 < len(src):
                chars.append(src[i + 1])
                i += 2
                continue
            if c == '"':
                rest = src[i + 1:].lstrip()
                if not rest or rest[0] in (',', '}', ']', ':'):
                    return ''.join(chars), i + 1
                i += 1
                continue
            chars.append(c)
            i += 1
        return ''.join(chars), i

    def _extract_str(key: str) -> str:
        pat = '"' + re.escape(key) + '"' + r'\s*:\s*"'
        hit = re.search(pat, raw_str)
        if not hit:
            return ''
        val, _ = _scan_string(raw_str, hit.end())
        return val.strip()

    def _extract_arr(key: str) -> list:
        pat = '"' + re.escape(key) + '"' + r'\s*:\s*\['
        hit = re.search(pat, raw_str)
        if not hit:
            return []
        depth = 1
        i = hit.end()
        while i < len(raw_str) and depth > 0:
            if raw_str[i] == '[':
                depth += 1
            elif raw_str[i] == ']':
                depth -= 1
            i += 1
        arr_content = raw_str[hit.end():i - 1]
        objects = []
        j = 0
        while j < len(arr_content):
            if arr_content[j] != '{':
                j += 1
                continue
            d2 = 1
            k = j + 1
            while k < len(arr_content) and d2 > 0:
                if arr_content[k] == '{':
                    d2 += 1
                elif arr_content[k] == '}':
                    d2 -= 1
                k += 1
            obj_src = arr_content[j + 1:k - 1]
            obj = {}
            pos = 0
            while pos < len(obj_src):
                km = re.search(r'"(\w+)"\s*:\s*"', obj_src[pos:])
                if not km:
                    break
                field_key = km.group(1)
                val_start = pos + km.end()
                val, val_end = _scan_string(obj_src, val_start)
                obj[field_key] = val.strip()
                pos = val_start + (val_end - val_start)
            if obj:
                objects.append(obj)
            j = k
        return objects

    card_type = _extract_str('type') or 'diagnosis'
    severity  = _extract_str('severity') or 'medium'
    title     = _extract_str('title')
    subtitle  = _extract_str('subtitle')
    if not title:
        return None
    return _normalize({
        'type':     card_type,
        'severity': severity,
        'title':    title,
        'subtitle': subtitle,
        'fields':   _extract_arr('fields'),
        'actions':  _extract_arr('actions') or _extract_arr('action'),
    })

def _strip_analysis_card(text: str) -> str:
    import re
    text = re.sub(r'\s*<analysis_card>.*?</analysis_card>', '', text, flags=re.DOTALL)
    text = re.sub(r'\s*<analysis_card\{.*', '', text, flags=re.DOTALL)
    text = re.sub(r'\s*<analysis_card>\{.*', '', text, flags=re.DOTALL)
    return text.strip()


def _parse_chart_config(text: str) -> list | None:
    import re
    m = re.search(r'<chart_config>(.*?)</chart_config>', text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1).strip())
        # Support both array and single object
        return data if isinstance(data, list) else [data]
    except Exception:
        return None


def _strip_chart_config(text: str) -> str:
    import re
    return re.sub(r'\s*<chart_config>.*?</chart_config>', '', text, flags=re.DOTALL).strip()


def _build_data_summary(merged: list[dict], filenames: list[str]) -> str:
    """Turn parsed records into a level-2 granularity text block for the AI prompt."""
    df = pd.DataFrame(merged)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(1)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "store" not in df.columns:
        df["store"] = ""

    valid_dates = df["date"].dropna()
    date_range = (
        f"{valid_dates.min().date()} ~ {valid_dates.max().date()}"
        if not valid_dates.empty else "unknown"
    )
    total_records = len(df)
    total_revenue = df["amount"].sum()
    total_orders = len(df)
    avg_order_value = total_revenue / total_orders if total_orders else 0

    # ── 每日趋势 ──────────────────────────────────────────────────────────────
    daily = (
        df.groupby(df["date"].dt.date)
        .agg(orders=("amount", "count"), revenue=("amount", "sum"))
        .reset_index()
        .sort_values("date")
    )
    daily_lines = [
        f"  {row['date']} 订单:{row['orders']} 收入:{row['revenue']:,.0f}"
        for _, row in daily.iterrows()
    ]
    daily_block = "\n每日趋势：\n" + "\n".join(daily_lines)

    # ── 星期分布 ──────────────────────────────────────────────────────────────
    dow_map = {0:"周一",1:"周二",2:"周三",3:"周四",4:"周五",5:"周六",6:"周日"}
    dow = df.groupby(df["date"].dt.dayofweek).agg(orders=("amount","count"), revenue=("amount","sum"))
    dow_lines = [f"  {dow_map[i]}: 订单{int(row['orders'])} 收入{row['revenue']:,.0f}" for i, row in dow.iterrows()]
    dow_block = "\n星期分布：\n" + "\n".join(dow_lines)

    # ── 商品 TOP20（销量 + 收入） ──────────────────────────────────────────────
    item_df = (
        df.groupby("item_name")
        .agg(qty=("quantity", "sum"), revenue=("amount", "sum"))
        .sort_values("revenue", ascending=False)
        .head(20)
    )
    item_lines = [
        f"  {row.name}: 销量{int(row['qty'])} 收入{row['revenue']:,.0f}"
        for _, row in item_df.iterrows()
    ]
    item_block = "\n商品TOP20（按收入）：\n" + "\n".join(item_lines)

    # ── 客单价分布 ────────────────────────────────────────────────────────────
    aov_per_order = df["amount"]
    low = (aov_per_order < 30000).sum()
    mid = ((aov_per_order >= 30000) & (aov_per_order < 80000)).sum()
    high = (aov_per_order >= 80000).sum()
    aov_block = (
        f"\n客单价分布：\n"
        f"  低(<30k IDR): {low}单({low/total_orders*100:.1f}%) "
        f"中(30k-80k): {mid}单({mid/total_orders*100:.1f}%) "
        f"高(>80k): {high}单({high/total_orders*100:.1f}%)"
    )

    # ── 门店对比（多门店时） ───────────────────────────────────────────────────
    store_block = ""
    has_stores = df["store"].nunique() > 1
    if has_stores:
        stores = sorted(df["store"].dropna().unique())
        lines = []
        max_date = df["date"].max()
        for s in stores:
            sdf = df[df["store"] == s]
            rev = sdf["amount"].sum()
            orders_cnt = len(sdf)
            aov_s = rev / orders_cnt if orders_cnt else 0
            recent = sdf[sdf["date"] >= max_date - pd.Timedelta(days=13)]["amount"].sum()
            prior = sdf[(sdf["date"] >= max_date - pd.Timedelta(days=27)) & (sdf["date"] < max_date - pd.Timedelta(days=13))]["amount"].sum()
            trend = (
                f"+{((recent-prior)/prior*100):.1f}%" if prior > 0 and recent > prior
                else f"{((recent-prior)/prior*100):.1f}%" if prior > 0
                else "无对比数据"
            )
            lines.append(f"  [{s}] 订单:{orders_cnt} 收入:{rev:,.0f} 均单:{aov_s:,.0f} 趋势:{trend}")
        store_block = "\n门店对比：\n" + "\n".join(lines)

    # ── 渠道分布 ──────────────────────────────────────────────────────────────
    channel_block = ""
    if "channel" in df.columns and df["channel"].nunique() > 1:
        ch = df.groupby("channel")["amount"].sum().sort_values(ascending=False)
        ch_lines = [f"  {c}: {v:,.0f} IDR ({v/total_revenue*100:.1f}%)" for c, v in ch.items()]
        channel_block = "\n渠道分布：\n" + "\n".join(ch_lines)

    return (
        f"数据文件：{', '.join(filenames)}\n"
        f"日期范围：{date_range}\n"
        f"总记录数：{total_records} | 总收入：{total_revenue:,.0f} IDR | 均单金额：{avg_order_value:,.0f} IDR\n"
        f"货币单位：印尼盾（IDR），请勿换算"
        f"{daily_block}"
        f"{dow_block}"
        f"{item_block}"
        f"{aov_block}"
        f"{store_block}"
        f"{channel_block}"
    )


@app.post("/chat")
async def chat(req: ChatRequest, current_user=Depends(get_current_user_optional), db: Session = Depends(get_db)):
    # Resolve LLM config: explicit preference overrides DB active config
    llm_cfg = llm_module.get_active_config(db)
    pref = req.model_preference or ""
    if pref and pref not in ("auto", ""):
        if ":" in pref:
            prov, mdl = pref.split(":", 1)
        else:
            prov, mdl = pref, llm_module.DEFAULT_MODELS.get(pref, "")
        llm_cfg = llm_module.LLMConfig(
            provider=prov,
            model=mdl,
            api_key="",
            is_active=True,
        )

    logger.info("chat provider=%s model=%s", llm_cfg.provider, llm_cfg.model)

    # Support both new (messages array) and old (message + history) formats
    if req.messages:
        history = [m for m in req.messages[:-1]]
        message = req.messages[-1].get("content", "") if req.messages else ""
    else:
        history = req.history
        message = req.message

    # Use active DB prompt if available, otherwise fall back to built-in
    from .db import SystemPrompt as _SP
    _active_prompt = db.exec(select(_SP).where(_SP.status == "active")).first()
    if _active_prompt:
        from .prompts import build_system_prompt as _bsp
        system_prompt = _bsp(req.shop_config, base_prompt=_active_prompt.content)
    else:
        system_prompt = build_system_prompt(req.shop_config)

    # ── Data Agent session lookup (before file parsing) ──────────────────────
    _raw_shop_id = req.shop_config.get("shop_name") or req.shop_config.get("store_name") or ""
    _skey = _session_key(current_user, _raw_shop_id)
    _data_session = DATA_SESSIONS.get(_skey) if _skey else None
    _use_sql_agent = bool(_data_session) and llm_cfg.provider in ("claude", "openai")

    if _use_sql_agent and _data_session:
        system_prompt += (
            f"\n\n## 已上传的数据表\n{_data_session['schema']}\n\n"
            "你可以调用 execute_sql 工具对上述表执行 SQL 查询（DuckDB 方言）。\n"
            "重要原则：\n"
            "- 每个问题都应先查询数据，即使对话历史里已有之前的查询结果，新的问题需要新的查询来提供针对性数据。\n"
            "- 对话中每一轮的建议都必须引用具体数字，不能用'根据之前分析'代替实际查询。\n"
            "- 建议策略：先用简单查询了解本轮问题相关的数据，再针对异常或机会点深入查询。\n"
            "最终输出仍需附 <analysis_card>，货币单位使用 IDR，数字用千位分隔符。"
        )


    # ── File parsing (optional) ───────────────────────────────────────────────
    _dbg = Path.home() / "usb-assistant" / "logs" / "debug.log"
    _dbg.parent.mkdir(parents=True, exist_ok=True)
    with open(_dbg, "a") as _f:
        _f.write(f"[CHAT] files={req.files!r} model={req.model_preference!r} provider={llm_cfg.provider!r} sql_agent={_use_sql_agent!r}\n")

    canvas_event: str | None = None
    parse_warning: str | None = None

    if req.files:
        _merchant = get_merchant_for_user(current_user, db) if current_user else None
        _upload_dir = (UPLOADS_DIR / f"m{_merchant.id}") if _merchant else UPLOADS_DIR

        missing = [f for f in req.files if not (_upload_dir / f).exists()]
        if missing:
            return JSONResponse(status_code=404, content={"error": f"Files not found: {missing}"})

        file_paths = [_upload_dir / f for f in req.files]
        try:
            merged, warnings = merge_reports(file_paths)
        except Exception as e:
            return JSONResponse(status_code=422, content={"error": f"Unable to read file(s): {e}"})

        with open(_dbg, "a") as _f:
            _f.write(f"[PARSE] merged={len(merged) if merged else 0} warnings={warnings!r}\n")

        if merged:
            try:
                charts = analyze(merged)
                canvas_event = json.dumps(
                    {"canvasUpdate": {"type": "sales_chart", "data": charts, "records_count": len(merged)}},
                    ensure_ascii=False,
                )
            except Exception:
                charts = None

            if not _use_sql_agent:
                # Static summary path: inject summary into message
                data_summary = _build_data_summary(merged, req.files)
                data_context = (
                    f"\n\n## 刚上传的销售数据\n{data_summary}\n\n"
                    f"请直接分析数据，用数据说话，货币单位使用 IDR，数字用千位分隔符。"
                )
                message = (message + data_context) if message else data_context

            if warnings:
                parse_warning = "⚠️ 数据解析提示：" + "；".join(warnings)
        else:
            # Parser couldn't extract sales records — if DuckDB session exists,
            # fall through and let LLM query the raw data via SQL agent.
            if not _data_session:
                return JSONResponse(status_code=422, content={"error": "Unable to parse file(s). Please check the file format."})

    # RAG: query merchant KB + platform KB
    shop_id = None
    if current_user:
        merchant = get_merchant_for_user(current_user, db)
        if merchant:
            shop_id = f"u{current_user.id}_m{merchant.id}"
    if not shop_id:
        shop_id = req.shop_config.get("shop_name") or req.shop_config.get("store_name") or None
    with open(_dbg, "a") as _f:
        _f.write(f"[RAG] user={current_user.id if current_user else None} shop_id={shop_id!r}\n")
    try:
        kb_results = rag.query_multi(message, shop_id=shop_id)
        with open(_dbg, "a") as _f:
            _f.write(f"[RAG] merchant={len(kb_results.get('merchant') or [])} platform={len(kb_results.get('platform') or [])}\n")
        if kb_results.get("merchant"):
            joined = "\n\n---\n\n".join(kb_results["merchant"])
            system_prompt += f"\n\n## 商家知识库\n以下是该商家的品牌和历史数据，请结合给出建议：\n\n{joined}"
        if kb_results.get("platform"):
            joined = "\n\n---\n\n".join(kb_results["platform"])
            system_prompt += f"\n\n## 平台知识库\n以下是平台提供的通用经营知识，供参考：\n\n{joined}"
    except Exception as _rag_err:
        with open(_dbg, "a") as _f:
            _f.write(f"[RAG] ERROR: {_rag_err}\n")
        import traceback as _tb
        with open(_dbg, "a") as _f:
            _f.write(_tb.format_exc())

    def sse(content: str) -> str:
        return f"data: {json.dumps({'content': content}, ensure_ascii=False)}\n\n"

    def _emit_card(full_text: str):
        card = _parse_analysis_card(full_text)
        if card:
            return f"data: {json.dumps({'canvasUpdate': {'type': 'analysis_card', 'card_type': card.get('type'), **{k:v for k,v in card.items() if k != 'type'}}}, ensure_ascii=False)}\n\n"
        if "<analysis_card>" in full_text:
            _dbg_path = Path.home() / "usb-assistant" / "logs" / "debug.log"
            with open(_dbg_path, "a") as _f:
                _f.write(f"[DEBUG] analysis_card parse FAILED — tail500={full_text[-500:]!r}\n")
        return None

    async def generate():
        if parse_warning:
            yield sse(parse_warning + "\n\n")

        if canvas_event:
            yield f"data: {canvas_event}\n\n"

        full_text = ""

        try:
            if _use_sql_agent and _data_session:
                # SQL Agent path: LLM writes SQL, executes via DuckDB
                from .data_agent import execute_sql as _exec_sql
                _conn = _data_session["conn"]

                async def _sql_fn(sql: str) -> str:
                    return await asyncio.to_thread(_exec_sql, _conn, sql)

                _stop_tags = ["<analysis_card>", "<chart_config>"]
                _card_reached = False
                _pushed_len = 0  # chars already sent to frontend
                async for item in llm_module.run_sql_agent_loop(
                    message, history, system_prompt, llm_cfg,
                    execute_sql_fn=_sql_fn,
                ):
                    if item["type"] == "tool_call":
                        yield f"data: {json.dumps({'tool_call': 'execute_sql', 'reason': item['reason']}, ensure_ascii=False)}\n\n"
                    elif item["type"] == "tool_result":
                        yield f"data: {json.dumps({'tool_result': 'execute_sql', 'summary': item['summary']}, ensure_ascii=False)}\n\n"
                    elif item["type"] == "text_delta":
                        full_text += item["text"]
                        if not _card_reached:
                            hits = [(full_text.index(t), t) for t in _stop_tags if t in full_text]
                            if not hits:
                                yield sse(item["text"])
                                _pushed_len = len(full_text)
                            else:
                                cut = min(hits, key=lambda x: x[0])[0]
                                to_push = full_text[_pushed_len:cut]
                                if to_push:
                                    yield sse(to_push)
                                _card_reached = True
            else:
                # Legacy path: static summary, plain stream
                chunks = []
                async for chunk in llm_module.stream_chat(message, history, system_prompt, llm_cfg):
                    chunks.append(chunk)
                full_text = "".join(chunks)
                yield sse(_strip_analysis_card(_strip_chart_config(full_text)))

            charts = _parse_chart_config(full_text)
            if charts:
                yield f"data: {json.dumps({'canvasUpdate': {'type': 'llm_chart', 'charts': charts}}, ensure_ascii=False)}\n\n"

            card_event = _emit_card(full_text)
            if card_event:
                yield card_event
        except Exception as e:
            logger.error("LLM stream failed (%s): %s", llm_cfg.provider, e)
            yield sse("AI service error, please try again")
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream; charset=utf-8")


class SummarizeRequest(BaseModel):
    history: list[dict]
    model_preference: str = "auto"


@app.post("/summarize")
async def summarize_history(req: SummarizeRequest):
    """把一段对话历史压缩成摘要，供前端替换掉旧历史。"""
    if not req.history:
        return {"summary": ""}

    turns = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{m.get('content', '')}"
        for m in req.history
    )
    prompt = (
        "请将以下对话内容压缩成一段简洁的中文摘要（200字以内），"
        "保留所有关键信息：商户的业务背景、已确认的问题、给出的建议和结论。"
        "直接输出摘要文字，不要加任何前缀。\n\n"
        f"{turns}"
    )

    from .db import get_db as _get_db
    with next(_get_db()) as _db:
        cfg = llm_module.get_active_config(_db)
    spref = req.model_preference or ""
    if spref and spref not in ("auto", ""):
        if ":" in spref:
            sprov, smdl = spref.split(":", 1)
        else:
            sprov, smdl = spref, llm_module.DEFAULT_MODELS.get(spref, "")
        cfg = llm_module.LLMConfig(
            provider=sprov,
            model=smdl,
            api_key="",
            is_active=True,
        )

    try:
        summary_parts = []
        async for chunk in llm_module.stream_chat(prompt, [], "你是一个对话摘要助手。", cfg):
            summary_parts.append(chunk)
        return {"summary": "".join(summary_parts).strip()}
    except Exception as e:
        logger.warning("Summarize failed: %s", e)
        return {"summary": ""}


CONFIG_PATH = Path.home() / "usb-assistant" / "config" / "app.config.json"


class ConfigRequest(BaseModel):
    shop_name: str = ""
    business_type: str = ""
    address: str = ""


@app.post("/config")
async def save_config(req: ConfigRequest):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.update({k: v for k, v in req.model_dump().items() if v})
    CONFIG_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


@app.get("/config")
async def get_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


_PROVIDER_MODELS = {
    "claude": ["claude-opus-4-7", "claude-sonnet-4-6"],
    "openai": ["gpt-4o", "o4-mini"],
}


@app.get("/status")
async def status(db: Session = Depends(get_db)):
    online = await is_online()

    llm_cfg = llm_module.get_active_config(db)
    model_name = llm_cfg.model or llm_module.DEFAULT_MODELS.get(llm_cfg.provider, "")

    env_keys = {
        "claude": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openai": os.environ.get("OPENAI_API_KEY", ""),
    }
    enabled_configs = db.exec(select(LLMConfig).where(LLMConfig.is_active == True)).all()
    provider_models: dict[str, list[str]] = {}
    for cfg in enabled_configs:
        if cfg.provider not in ("claude", "openai"):
            continue
        has_key = bool(cfg.api_key) or bool(env_keys.get(cfg.provider, ""))
        if not has_key:
            continue
        provider_models.setdefault(cfg.provider, []).append(cfg.model or llm_module.DEFAULT_MODELS.get(cfg.provider, ""))
    available_providers = [
        {"provider": p, "models": provider_models[p]}
        for p in ["claude", "openai"]
        if p in provider_models
    ]

    knowledge_version = "unknown"
    if CONFIG_PATH.exists():
        try:
            cfg_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            knowledge_version = cfg_data.get("knowledge_version", "unknown")
        except Exception:
            pass

    return {
        "network": "online" if online else "offline",
        "model": llm_cfg.provider,
        "model_name": model_name,
        "available_providers": available_providers,
        "scheduler_status": octools.scheduler_status(),
        "knowledge_docs": db.exec(select(func.count()).select_from(PlatformKBDocument).where(PlatformKBDocument.status == "indexed")).one(),
        "knowledge_version": knowledge_version,
    }


ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv", ".png", ".jpg", ".jpeg", ".webp"}


def _build_rag_chunks(merged: list[dict], filename: str) -> list[str]:
    """Convert parsed sales records into text chunks suitable for RAG indexing."""
    df = pd.DataFrame(merged)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(1)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    chunks = []
    date_range = f"{df['date'].min().date()} 至 {df['date'].max().date()}" if not df["date"].isna().all() else "未知"

    # Overview chunk
    total_rev = df["amount"].sum()
    total_qty = df["quantity"].sum()
    chunks.append(
        f"【销售数据概览】来源文件：{filename}\n"
        f"日期范围：{date_range}\n"
        f"总销售额：{total_rev:,.0f} IDR\n"
        f"总销量：{int(total_qty)} 件\n"
        f"记录条数：{len(merged)}"
    )

    # Top items chunk
    top_items = df.groupby("item_name").agg(
        qty=("quantity", "sum"), rev=("amount", "sum")
    ).nlargest(10, "qty")
    lines = [f"  {row.name}: 销量 {int(row.qty)}，销售额 {row.rev:,.0f} IDR"
             for _, row in top_items.iterrows()]
    chunks.append(f"【热销商品 Top10】来源：{filename}\n" + "\n".join(lines))

    # Channel breakdown chunk
    if "channel" in df.columns:
        ch = df.groupby("channel")["amount"].sum().nlargest(10)
        lines = [f"  {k}: {v:,.0f} IDR" for k, v in ch.items()]
        chunks.append(f"【渠道销售分布】来源：{filename}\n" + "\n".join(lines))

    # Store breakdown if multi-store
    if "store" in df.columns and df["store"].nunique() > 1:
        st = df.groupby("store").agg(rev=("amount", "sum"), qty=("quantity", "sum"))
        lines = [f"  {row.name}: 销售额 {row.rev:,.0f} IDR，销量 {int(row.qty)}"
                 for _, row in st.iterrows()]
        chunks.append(f"【门店对比】来源：{filename}\n" + "\n".join(lines))

    # Weekly trend chunk
    df["week"] = df["date"].dt.to_period("W").astype(str)
    weekly = df.groupby("week")["amount"].sum()
    lines = [f"  {w}: {v:,.0f} IDR" for w, v in weekly.items()]
    chunks.append(f"【周销售趋势】来源：{filename}\n" + "\n".join(lines))

    return chunks


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    shop_id: str = Form(""),
    folder: str = Form(""),
    current_user=Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {suffix}")

    merchant = get_merchant_for_user(current_user, db) if current_user else None
    upload_dir = (UPLOADS_DIR / f"m{merchant.id}") if merchant else UPLOADS_DIR
    upload_dir.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    bare_name = Path(file.filename).name
    original_name = f"{folder.strip('/')}/{bare_name}" if folder.strip("/") else bare_name

    # Dedup: check if same content already uploaded by this merchant
    import hashlib
    file_hash = hashlib.md5(content).hexdigest()
    if merchant:
        existing = db.exec(
            select(KBDocument).where(
                KBDocument.merchant_id == merchant.id,
                KBDocument.file_hash == file_hash,
            )
        ).first()
        if existing:
            return {
                "filename": existing.filename,
                "path": str(upload_dir / existing.filename),
                "size_bytes": len(content),
                "duplicate": True,
            }

    save_path = upload_dir / bare_name
    counter = 1
    while save_path.exists():
        stem = Path(bare_name).stem
        save_path = upload_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    save_path.write_bytes(content)

    # Write KBDocument record so the file appears in the merchant KB list
    kb_doc_id = None
    if merchant:
        um = db.exec(select(UserMerchant).where(UserMerchant.merchant_id == merchant.id)).first()
        owner_uid = um.user_id if um else current_user.id
        kb_doc = KBDocument(
            merchant_id=merchant.id,
            filename=save_path.name,
            original_name=original_name,
            file_hash=file_hash,
            status="processing",
            file_size=len(content),
            uploaded_by=current_user.id,
        )
        db.add(kb_doc)
        db.commit()
        db.refresh(kb_doc)
        kb_doc_id = kb_doc.id
        _sid = f"u{owner_uid}_m{merchant.id}"
    else:
        _sid = shop_id or None

    # Load into DuckDB session for Data Agent
    if suffix in (".xlsx", ".xls", ".csv"):
        try:
            from .data_agent import load_to_duckdb
            _skey = _session_key(current_user, shop_id)
            if _skey:
                if _skey in DATA_SESSIONS:
                    _, new_schema = load_to_duckdb(
                        [save_path], conn=DATA_SESSIONS[_skey]["conn"]
                    )
                    DATA_SESSIONS[_skey]["filenames"].append(save_path.name)
                    DATA_SESSIONS[_skey]["schema"] += "\n\n" + new_schema
                else:
                    conn, schema = load_to_duckdb([save_path])
                    DATA_SESSIONS[_skey] = {
                        "conn": conn,
                        "schema": schema,
                        "filenames": [save_path.name],
                        "created_at": _time.time(),
                    }
        except Exception as _e:
            logger.warning("Data session load failed: %s", _e)

    # Index in background so upload returns immediately
    async def _index_bg():
        try:
            merged, _ = merge_reports([save_path])
            if merged and _sid:  # _sid must be set — never write to platform KB
                if rag._use_pgvector():
                    await rag._pg_delete_by_source_async(save_path.name, _sid)
                    chunks = _build_rag_chunks(merged, save_path.name)
                    chunk_count = await rag._pg_add_async(chunks, source=save_path.name, shop_id=_sid)
                else:
                    rag.delete_by_source(save_path.name, shop_id=_sid)
                    chunks = _build_rag_chunks(merged, save_path.name)
                    rag.add(chunks, source=save_path.name, shop_id=_sid)
                    chunk_count = len(chunks)
            else:
                chunk_count = 0
            # Update KBDocument status
            if kb_doc_id:
                from datetime import datetime
                with Session(db.bind) as s:
                    doc = s.get(KBDocument, kb_doc_id)
                    if doc:
                        doc.status = "indexed"
                        doc.chunk_count = chunk_count
                        doc.indexed_at = datetime.utcnow()
                        s.add(doc)
                        s.commit()
        except Exception:
            if kb_doc_id:
                with Session(db.bind) as s:
                    doc = s.get(KBDocument, kb_doc_id)
                    if doc:
                        doc.status = "failed"
                        s.add(doc)
                        s.commit()

    asyncio.create_task(_index_bg())

    return {
        "filename": save_path.name,
        "path": str(save_path),
        "size_bytes": len(content),
    }


class AnalyzeRequest(BaseModel):
    files: list[str]


@app.post("/analyze")
async def analyze_files(req: AnalyzeRequest):
    if not req.files:
        raise HTTPException(status_code=400, detail="files list is empty")

    missing = [f for f in req.files if not (UPLOADS_DIR / f).exists()]
    if missing:
        raise HTTPException(status_code=404, detail=f"Files not found: {missing}")

    file_paths = [UPLOADS_DIR / f for f in req.files]

    try:
        merged, _ = merge_reports(file_paths)
    except Exception:
        return JSONResponse(
            status_code=422,
            content={"error": "Unable to read this file. Please use PDF, Excel, CSV or image format."},
        )

    if not merged:
        return JSONResponse(
            status_code=422,
            content={"error": "Unable to read this file. Please use PDF, Excel, CSV or image format."},
        )

    try:
        charts = analyze(merged)
    except Exception:
        return JSONResponse(
            status_code=422,
            content={"error": "Unable to read this file. Please use PDF, Excel, CSV or image format."},
        )

    return {"records_count": len(merged), "charts": charts}


class CompetitorRequest(BaseModel):
    address: str
    business_type: str
    radius: int = 1000
    language: str = "en"


@app.post("/competitors")
async def competitors(req: CompetitorRequest):
    try:
        result = await search_competitors(req.address, req.business_type, req.radius, language=req.language)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result


@app.get("/competitors/{place_id}")
async def competitor_detail(place_id: str, language: str = "en"):
    try:
        result = await get_competitor_details(place_id, language=language)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return result
