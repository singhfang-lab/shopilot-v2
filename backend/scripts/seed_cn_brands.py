#!/usr/bin/env python3
"""
Seed Chinese F&B brand data into brand_profiles + brand_reports.

Flow per brand:
  1. Crawl Baidu Baike + Wikipedia
  2. Claude Haiku extracts structured fields → BrandProfile
  3. Amap POI sampling → avg_price, avg_rating, delivery_rate
  4. Claude Haiku generates 10-module BrandReport
  5. Claude Haiku generates lightweight profile_json (onboarding scan card)

Usage:
  cd /Users/singhfang/shopilot-v2
  venv/bin/python -m backend.scripts.seed_cn_brands [--limit N] [--skip-crawl] [--only-report] [--brand 瑞幸咖啡]
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import anthropic
from dotenv import load_dotenv
from sqlmodel import Session, select

load_dotenv(ROOT / "backend" / ".env")

from backend.db import BrandProfile, BrandReport, engine  # noqa: E402

# ── Load chains_cn.json ───────────────────────────────────────────────────────

_CHAINS_CN_PATH = Path(__file__).parent.parent / "chains_cn.json"

def _load_brands() -> list[dict]:
    data = json.loads(_CHAINS_CN_PATH.read_text(encoding="utf-8"))
    return data.get("brands", [])

# ── Amap sampling ─────────────────────────────────────────────────────────────

SAMPLE_CITIES = ["北京市", "上海市", "成都市", "广州市"]

async def _amap_sample(brand_name: str, hq: str) -> dict:
    import os as _os
    import httpx as _httpx

    key = _os.environ.get("AMAP_KEY", "")
    if not key:
        return {}

    cities = list(dict.fromkeys([c for c in ([hq] if hq else []) + SAMPLE_CITIES if c]))

    GEO_URL    = "https://restapi.amap.com/v3/geocode/geo"
    SEARCH_URL = "https://restapi.amap.com/v3/place/around"

    costs: list[float] = []
    ratings: list[float] = []
    delivery_flags: list[int] = []

    async with _httpx.AsyncClient(timeout=15) as client:
        for city in cities[:4]:
            try:
                geo_r = await client.get(GEO_URL, params={"key": key, "address": city, "output": "json"})
                geo_d = geo_r.json()
                if geo_d.get("status") != "1" or not geo_d.get("geocodes"):
                    continue
                location = geo_d["geocodes"][0]["location"]
            except Exception:
                continue

            try:
                s_r = await client.get(SEARCH_URL, params={
                    "key": key,
                    "location": location,
                    "keywords": brand_name,
                    "radius": 5000,
                    "offset": 25,
                    "page": 1,
                    "extensions": "all",
                    "output": "json",
                })
                s_d = s_r.json()
                if s_d.get("status") != "1":
                    continue
                pois = s_d.get("pois", [])
            except Exception:
                continue

            for poi in pois:
                biz = poi.get("biz_ext", {})
                cost   = biz.get("cost")
                rating = biz.get("rating")
                meal   = biz.get("meal_ordering", "0")

                if cost and cost != [] and str(cost).replace(".", "").isdigit():
                    costs.append(float(cost))
                if rating and rating != [] and str(rating).replace(".", "").isdigit():
                    ratings.append(float(rating))
                try:
                    delivery_flags.append(int(meal))
                except Exception:
                    delivery_flags.append(0)

    result: dict = {"sample_size": len(delivery_flags)}
    if costs:
        result["avg_price"] = round(sum(costs) / len(costs), 1)
    if ratings:
        result["avg_rating"] = round(sum(ratings) / len(ratings), 2)
    if delivery_flags:
        result["delivery_rate"] = round(sum(delivery_flags) / len(delivery_flags), 2)
    return result


# ── Crawl helpers ─────────────────────────────────────────────────────────────

async def _crawl_brand(crawler, name: str) -> dict:
    baidu_url = f"https://baike.baidu.com/item/{name}"
    wiki_url  = f"https://en.wikipedia.org/wiki/{name.replace(' ', '_')}"
    raw: dict[str, str] = {}

    for label, url in [("baidu", baidu_url), ("wiki", wiki_url)]:
        try:
            result = await crawler.arun(url=url)
            if result.success and result.markdown:
                raw[label] = result.markdown[:4000]
        except Exception as e:
            print(f"  [crawl] {label} failed for {name}: {e}")
    return raw


# ── Field extraction ──────────────────────────────────────────────────────────

def _extract_fields(name: str, business_type: str, chain: dict, raw: dict) -> dict:
    client = anthropic.Anthropic()
    combined = ""
    if raw.get("baidu"):
        combined += f"=== 百度百科 ===\n{raw['baidu'][:2000]}\n\n"
    if raw.get("wiki"):
        combined += f"=== Wikipedia ===\n{raw['wiki'][:2000]}\n\n"

    chain_ctx = f"""
已知数据（直接采用，不要修改）：
- 门店数：{chain.get('storeCount', '未知')}（{chain.get('storeCountAsOf', '')}）
- 总部：{chain.get('hq', '')}
- 成立：{chain.get('founded', '')}
- 价格带：{chain.get('priceBand', '')}
- 价格定位：{chain.get('priceTier', '')}
"""

    prompt = f"""从以下关于「{name}」（{business_type}品牌）的文本中提取结构化信息。

{chain_ctx}

{combined if combined.strip() else "（无网页数据，请根据已知数据和行业常识填写）"}

请以 JSON 格式返回，只填写能从文本或已知数据中确认的字段，无法确认填 null：
{{
  "founded_year": 数字或null,
  "store_count": {chain.get('storeCount') or 'null'},
  "store_count_year": 年份数字或null,
  "headquarters": "{chain.get('hq', '') or 'null'}",
  "revenue": 年营收亿人民币或null,
  "revenue_year": 年份或null,
  "avg_price_min": 客单价下限元或null,
  "avg_price_max": 客单价上限元或null,
  "price_tier": "{chain.get('priceTier', '')}（低价/中价/高价，必填）",
  "quality_perception": "低/中/高（必填，根据品牌定位）",
  "main_competitors": ["竞品1", "竞品2", "竞品3"],
  "user_tags": ["用户认知标签1", "标签2", "标签3", "标签4"],
  "description": "品牌简介，2-3句话",
  "data_sources": {{"store_count": "来源", "revenue": "来源"}}
}}

price_tier 和 quality_perception 必须给出值，不能为 null。"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        m = re.search(r'\{[\s\S]+\}', text)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"  [extract] failed for {name}: {e}")
    return {}


# ── 10-module brand report ────────────────────────────────────────────────────

def _generate_report(brand_id: int) -> Optional[str]:
    client = anthropic.Anthropic()

    with Session(engine) as db:
        b = db.get(BrandProfile, brand_id)
        if not b:
            return None

    competitors = json.loads(b.main_competitors or "[]")
    user_tags   = json.loads(b.user_tags or "[]")

    price_str = "未知"
    if b.avg_price_amap:
        price_str = f"{b.avg_price_amap}元（高德{b.amap_sample_size or '?'}家均值）"
    elif b.avg_price_min:
        price_str = f"{b.avg_price_min}-{b.avg_price_max}元"
    elif b.price_band:
        price_str = b.price_band

    rating_str   = f"{b.avg_rating_amap}分（大众点评，{b.amap_sample_size or '?'}家均值）" if b.avg_rating_amap else "未知"
    delivery_str = f"{int(b.delivery_rate * 100)}%" if b.delivery_rate is not None else "未知"

    prompt = f"""你是餐饮行业品牌分析专家，为「{b.name}」（{b.business_type}）生成品牌体检报告。所有分析基于中国市场，货币单位用人民币（元/CNY）。

品牌数据：
- 成立：{b.founded_year or '未知'}年
- 门店：{f"{b.store_count}家（{b.store_count_year}年）" if b.store_count else '未知'}
- 总部：{b.headquarters or '未知'}
- 年营收：{f"{b.revenue}亿（{b.revenue_year}年）" if b.revenue else '未知'}
- 客单价：{price_str}
- 大众点评均分：{rating_str}
- 外卖覆盖率：{delivery_str}
- 价格定位：{b.price_tier}
- 品质感知：{b.quality_perception}
- 主要竞品：{', '.join(competitors) if competitors else '未知'}
- 用户标签：{', '.join(user_tags) if user_tags else '未知'}
- 品牌简介：{b.description or ''}

生成完整10模块品牌体检报告，JSON格式：
{{
  "brand_summary": {{
    "one_liner": "一句话品牌定位（20字以内）",
    "tagline": "品牌核心价值关键词（10字以内）"
  }},
  "positioning": {{
    "x_label": "X轴标签（如：价格）",
    "y_label": "Y轴标签（如：品质感知）",
    "x_value": X轴位置（-10到10）,
    "y_value": Y轴位置（-10到10）,
    "competitors": [{{"name": "竞品名", "x": 数字, "y": 数字}}]
  }},
  "competitor_table": [
    {{"name": "竞品名", "price_tier": "价格定位", "strength": "核心优势", "weakness": "主要弱点", "threat_level": "高/中/低"}}
  ],
  "user_perception": {{
    "positive": ["正向标签1", "标签2", "标签3"],
    "negative": ["负向标签1", "标签2"],
    "neutral": ["中性标签1", "标签2"]
  }},
  "issues": [
    {{"title": "核心问题", "description": "问题描述（40字以内）", "severity": "high/medium/low"}}
  ],
  "opportunities": [
    {{"title": "机会点", "description": "机会描述（40字以内）", "priority": "high/medium/low"}}
  ],
  "growth_path": {{
    "route": "核心增长路径（30字以内）",
    "reason": "选择原因（50字以内）",
    "steps": [
      {{"phase": "第一步", "action": "具体行动", "timeframe": "1-3个月", "kpi": "衡量指标"}},
      {{"phase": "第二步", "action": "具体行动", "timeframe": "3-6个月", "kpi": "衡量指标"}},
      {{"phase": "第三步", "action": "具体行动", "timeframe": "6-12个月", "kpi": "衡量指标"}}
    ]
  }},
  "optimization": [
    {{"title": "优化策略", "priority": "高优先级/中优先级/快速见效", "description": "策略说明（50字以内）", "actions": ["行动1", "行动2", "行动3"]}}
  ],
  "risks": [
    {{"title": "风险标题", "level": "high/medium/low", "description": "风险说明（40字以内）", "mitigation": "应对措施（40字以内）"}}
  ],
  "trend": {{
    "years": [2023, 2024, 2025, 2026, 2027],
    "metrics": [
      {{"name": "市场热度指数", "values": [数字, 数字, 数字, 数字, 数字], "unit": "指数"}},
      {{"name": "品牌竞争压力", "values": [数字, 数字, 数字, 数字, 数字], "unit": "指数"}}
    ],
    "insight": "趋势洞察（40字以内）"
  }}
}}

注意：issues 和 opportunities 各2-3条；trend 数值范围0-100；所有货币单位用元（CNY）。"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        m = re.search(r'\{[\s\S]+\}', text)
        if m:
            report_json = m.group()
            try:
                json.loads(report_json)
                return report_json
            except json.JSONDecodeError:
                cleaned = re.sub(r'[\x00-\x1f\x7f]', ' ', report_json)
                try:
                    json.loads(cleaned)
                    return cleaned
                except Exception:
                    pass
    except Exception as e:
        print(f"  [report] failed for brand_id={brand_id}: {e}")
    return None


# ── Lightweight profile_json (onboarding scan card) ───────────────────────────

def _generate_profile_json(brand_id: int) -> Optional[str]:
    """Generate the lightweight scan card used in onboarding (same format as generate-kb)."""
    client = anthropic.Anthropic()

    with Session(engine) as db:
        b  = db.get(BrandProfile, brand_id)
        rpt = db.exec(select(BrandReport).where(BrandReport.brand_id == brand_id).order_by(BrandReport.generated_at.desc())).first()
        if not b:
            return None

    competitors = json.loads(b.main_competitors or "[]")
    price_str   = b.price_band or (f"{b.avg_price_amap}元" if b.avg_price_amap else f"{b.avg_price_min}-{b.avg_price_max}元" if b.avg_price_min else "未知")
    rating_str  = f"{b.avg_rating_amap}分" if b.avg_rating_amap else "未知"

    # Pull issues/opps from report if available
    issues_ctx = ""
    opps_ctx   = ""
    if rpt:
        try:
            rj = json.loads(rpt.report_json)
            iss  = [i.get("title", "") for i in rj.get("issues", [])[:2]]
            opps = [o.get("title", "") for o in rj.get("opportunities", [])[:2]]
            if iss:  issues_ctx = "核心问题：" + "；".join(iss)
            if opps: opps_ctx   = "机会点：" + "；".join(opps)
        except Exception:
            pass

    store_count = b.store_count or 1
    if store_count <= 10:
        scale_hint = "小规模品牌，聚焦单店口碑"
    elif store_count <= 100:
        scale_hint = "小型连锁，区域扩张阶段"
    elif store_count <= 1000:
        scale_hint = "中型连锁，全国布局阶段"
    else:
        scale_hint = "大型连锁，规模效率竞争"

    prompt = f"""你是「Shopilot」平台的 AI 顾问，为「{b.name}」（{b.business_type}）生成品牌注册扫描画像。所有分析基于中国市场，货币单位用元（CNY）。

品牌数据：
- 门店数：{store_count}家，规模：{scale_hint}
- 总部：{b.headquarters or '未知'}
- 客单价：{price_str}，大众点评均分：{rating_str}
- 价格定位：{b.price_tier}
- 主要竞品：{', '.join(competitors[:4]) if competitors else '同类头部品牌'}
- {issues_ctx}
- {opps_ctx}

严格输出以下 JSON，不要任何额外内容：
{{
  "scan_summary": {{
    "mirror": "用具体数字说明现状，{scale_hint}，语气像朋友在复盘",
    "window": "揭示一个商家可能没意识到的洞察，必须引用竞品名或数字，句式：「[竞品]正在[动作]，这意味着[机会/风险]」",
    "door": "最值得立刻做的具体行动，句式：「先把[具体数据]导入平台，找出[具体问题]」",
    "action": "平台能优先帮到的两件事，句式：「平台优先帮助：**[事项1]**和**[事项2]**」"
  }},
  "market": {{
    "market_size": "该品类中国市场规模估算",
    "yoy_growth": "年增长率估算",
    "store_gap": "{store_count} vs [头部品牌]家",
    "store_gap_note": "头部品牌名",
    "expansion_trend": "扩张趋势描述",
    "expansion_note": "一句评价",
    "core_market": "核心市场城市",
    "core_market_note": "核心商圈",
    "market_bar_data": [
      {{"name": "竞品名", "is_self": false, "pct": 门店数占比整数, "store_count": 门店数}},
      {{"name": "{b.name}", "is_self": true, "pct": 占比整数, "store_count": {store_count}}}
    ]
  }},
  "positioning": {{
    "axis_items": [
      {{"name": "竞品名", "price_low": 价格下限整数, "price_high": 价格上限整数, "is_self": false}},
      {{"name": "{b.name}", "price_low": 价格下限, "price_high": 价格上限, "is_self": true}}
    ]
  }},
  "social": [
    {{"platform": "微信公众号", "handle": "待核实", "followers": "估算关注量", "note": "微信运营评价", "ai_inferred": true}},
    {{"platform": "抖音", "handle": "待核实", "followers": "估算粉丝量", "note": "抖音运营评价", "ai_inferred": true}},
    {{"platform": "美团/饿了么", "handle": "全店已入驻", "followers": "评分约{rating_str}", "note": "外卖平台表现", "ai_inferred": true}}
  ],
  "competitors": {{
    "head": [
      {{"name": "头部竞品名", "category": "{b.business_type}", "founded": "年份", "store_count": 门店数, "price_band": "价格带", "avg_review_per_store": null, "rating": 评分, "is_warn": false, "insight": "与你的核心差距，用数字说明"}}
    ],
    "direct": [
      {{"name": "{b.name}", "is_self": true, "store_count": {store_count}, "price_band": "{price_str}", "avg_review_per_store": null, "rating": {b.avg_rating_amap or 0}, "founded": "{b.founded_year or ''}", "insight": ""}},
      {{"name": "直接竞品名", "category": "{b.business_type}", "founded": "年份", "store_count": 门店数, "price_band": "价格带", "avg_review_per_store": null, "rating": 评分, "is_warn": false, "insight": "与你的直接对比"}}
    ],
    "growth": [
      {{"name": "成长型竞品名", "category": "{b.business_type}", "founded": "年份", "store_count": 门店数, "price_band": "价格带", "avg_review_per_store": null, "rating": 评分, "is_warn": true, "insight": "为何是威胁，用数字说明"}}
    ]
  }},
  "ability": {{
    "basic": [
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}},
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}},
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}}
    ],
    "growth": [
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}},
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}},
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}}
    ],
    "scale": [
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}},
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}},
      {{"level": "strong/mid/weak", "title": "能力描述", "module": "平台模块名"}}
    ]
  }},
  "diagnosis": {{
    "because": "用数字说明核心现状和最大威胁",
    "therefore": "核心问题的本质是什么",
    "action": "最重要的第一步行动"
  }}
}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        clean = re.sub(r"^```[a-z]*\n?|\n?```$", "", text.strip())
        m = re.search(r'\{[\s\S]+\}', clean)
        if m:
            pj = m.group()
            try:
                json.loads(pj)  # validate
                return pj
            except json.JSONDecodeError:
                # Try replacing curly/smart quotes that Claude occasionally emits
                pj_fixed = (pj
                    .replace('"', '"').replace('"', '"')
                    .replace(''', "'").replace(''', "'"))
                json.loads(pj_fixed)  # validate again, raises if still broken
                return pj_fixed
    except Exception as e:
        print(f"  [profile_json] failed for brand_id={brand_id}: {e}")
    return None


# ── Main seed loop ────────────────────────────────────────────────────────────

async def seed(
    filter_name: Optional[str] = None,
    limit: Optional[int] = None,
    skip_crawl: bool = False,
    only_report: bool = False,
):
    brands = _load_brands()
    if filter_name:
        brands = [b for b in brands if filter_name in b["name"]]
    if limit:
        brands = brands[:limit]

    print(f"[seed] Processing {len(brands)} brands (skip_crawl={skip_crawl}, only_report={only_report})")

    # Only initialize crawler when actually needed
    crawler_ctx = None
    if not skip_crawl:
        try:
            from crawl4ai import AsyncWebCrawler
            crawler_ctx = AsyncWebCrawler(verbose=False)
        except ImportError:
            print("[seed] crawl4ai not installed — falling back to skip_crawl mode")
            skip_crawl = True

    async def _run(crawler=None):
        for i, chain in enumerate(brands):
            name          = chain["name"]
            business_type = chain.get("category", "")
            hq            = chain.get("hq", "")
            print(f"\n[{i+1}/{len(brands)}] {name} ({business_type})")

            with Session(engine) as db:
                existing = db.exec(select(BrandProfile).where(BrandProfile.name == name)).first()

            if existing:
                brand_id = existing.id
                with Session(engine) as db:
                    rpt = db.exec(select(BrandReport).where(BrandReport.brand_id == brand_id)).first()
                if rpt and existing.profile_json and not only_report:
                    print(f"  [skip] already fully seeded")
                    continue
                print(f"  [exists] profile id={brand_id}, generating missing parts")
            else:
                if only_report:
                    print(f"  [skip] --only-report but profile doesn't exist")
                    continue

                # Step 1: Crawl
                raw: dict = {}
                if not skip_crawl:
                    print(f"  [crawl] fetching baidu+wiki...")
                    raw = await _crawl_brand(crawler, name)
                    time.sleep(0.5)

                # Step 2: Extract fields
                print(f"  [extract] Claude Haiku...")
                fields = _extract_fields(name, business_type, chain, raw)

                # Step 3: Amap sampling
                print(f"  [amap] sampling {SAMPLE_CITIES}...")
                amap = await _amap_sample(name, hq)
                if amap:
                    print(f"  [amap] size={amap.get('sample_size')} price={amap.get('avg_price')} rating={amap.get('avg_rating')}")
                else:
                    print(f"  [amap] no data")

                # Parse price band
                price_band = chain.get("priceBand", "")
                price_min = fields.get("avg_price_min")
                price_max = fields.get("avg_price_max")
                if not price_min and price_band:
                    nums = re.findall(r'\d+', price_band)
                    if len(nums) >= 2:
                        price_min, price_max = float(nums[0]), float(nums[1])
                    elif len(nums) == 1:
                        price_min = price_max = float(nums[0])

                # Save BrandProfile
                with Session(engine) as db:
                    profile = BrandProfile(
                        name=name,
                        region="cn",
                        business_type=business_type,
                        founded_year=fields.get("founded_year") or (int(chain["founded"]) if chain.get("founded", "").isdigit() else None),
                        store_count=chain.get("storeCount") or fields.get("store_count"),
                        store_count_year=fields.get("store_count_year") or (int(chain["storeCountAsOf"]) if str(chain.get("storeCountAsOf", "")).isdigit() else None),
                        headquarters=hq or fields.get("headquarters", ""),
                        revenue=fields.get("revenue"),
                        revenue_year=fields.get("revenue_year"),
                        avg_price_min=price_min,
                        avg_price_max=price_max,
                        price_band=price_band,
                        avg_price_amap=amap.get("avg_price"),
                        avg_rating_amap=amap.get("avg_rating"),
                        delivery_rate=amap.get("delivery_rate"),
                        amap_sample_size=amap.get("sample_size"),
                        price_tier=chain.get("priceTier") or fields.get("price_tier") or "中价",
                        quality_perception=fields.get("quality_perception") or "中",
                        main_competitors=json.dumps(fields.get("main_competitors") or [], ensure_ascii=False),
                        user_tags=json.dumps(fields.get("user_tags") or [], ensure_ascii=False),
                        description=fields.get("description") or chain.get("expansion", ""),
                        aliases=json.dumps(chain.get("aliases") or [], ensure_ascii=False),
                        markets=json.dumps(chain.get("markets") or ["China"], ensure_ascii=False),
                        expansion=chain.get("expansion", ""),
                        data_sources=json.dumps(fields.get("data_sources") or {}, ensure_ascii=False),
                        is_active=True,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                    db.add(profile)
                    db.commit()
                    db.refresh(profile)
                    brand_id = profile.id
                    print(f"  [saved] BrandProfile id={brand_id}")

            # Step 4: Generate 10-module report
            with Session(engine) as db:
                rpt = db.exec(select(BrandReport).where(BrandReport.brand_id == brand_id)).first()

            if not rpt:
                print(f"  [report] generating 10-module report...")
                report_json = _generate_report(brand_id)
                if report_json:
                    with Session(engine) as db:
                        db.add(BrandReport(
                            brand_id=brand_id,
                            report_json=report_json,
                            model_used="claude-haiku-4-5-20251001",
                            generated_at=datetime.now(timezone.utc),
                        ))
                        db.commit()
                    print(f"  [report] saved")
                else:
                    print(f"  [report] generation failed")

            # Step 5: Generate lightweight profile_json
            with Session(engine) as db:
                bp = db.get(BrandProfile, brand_id)

            if not bp.profile_json:
                print(f"  [profile_json] generating scan card...")
                pj = _generate_profile_json(brand_id)
                if pj:
                    with Session(engine) as db:
                        bp2 = db.get(BrandProfile, brand_id)
                        if bp2:
                            bp2.profile_json = pj
                            bp2.updated_at   = datetime.now(timezone.utc)
                            db.add(bp2)
                            db.commit()
                    print(f"  [profile_json] saved")
                else:
                    print(f"  [profile_json] generation failed")

            await asyncio.sleep(0.3)

        print(f"\n[seed] Done!")

    if crawler_ctx:
        async with crawler_ctx as crawler:
            await _run(crawler)
    else:
        await _run()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",       type=int,  default=None, help="最多处理几个品牌")
    parser.add_argument("--brand",       type=str,  default=None, help="只处理包含该名字的品牌")
    parser.add_argument("--skip-crawl",  action="store_true",     help="跳过网页爬取")
    parser.add_argument("--only-report", action="store_true",     help="只重新生成报告（不新建 profile）")
    args = parser.parse_args()

    asyncio.run(seed(
        filter_name=args.brand,
        limit=args.limit,
        skip_crawl=args.skip_crawl,
        only_report=args.only_report,
    ))
