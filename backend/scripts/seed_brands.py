#!/usr/bin/env python3
"""
One-time script: crawl brand data for ~100 Chinese F&B chains,
then generate 10-module brand reports via Claude.

Usage:
  cd /Users/singhfang/usb-assistant
  python -m backend.scripts.seed_brands [--brands all|咖啡|奶茶|...] [--limit N] [--skip-crawl]
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

# ── Make sure we can import backend package ───────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import anthropic
from dotenv import load_dotenv
from sqlmodel import Session, select

load_dotenv(ROOT / "backend" / ".env")

from backend.db import BrandProfile, BrandReport, engine  # noqa: E402

# ── Brand list ────────────────────────────────────────────────────────────────

BRANDS: list[dict] = [
    # 咖啡
    {"name": "瑞幸咖啡", "business_type": "咖啡"},
    {"name": "库迪咖啡", "business_type": "咖啡"},
    {"name": "Manner咖啡", "business_type": "咖啡"},
    {"name": "M Stand", "business_type": "咖啡"},
    {"name": "挪瓦咖啡", "business_type": "咖啡"},
    {"name": "星巴克", "business_type": "咖啡"},
    {"name": "太平洋咖啡", "business_type": "咖啡"},
    {"name": "COSTA咖啡", "business_type": "咖啡"},
    {"name": "Tim Hortons", "business_type": "咖啡"},
    {"name": "皮爷咖啡", "business_type": "咖啡"},
    {"name": "鱼眼咖啡", "business_type": "咖啡"},
    {"name": "三顿半", "business_type": "咖啡"},
    {"name": "Seesaw咖啡", "business_type": "咖啡"},
    {"name": "幸运咖", "business_type": "咖啡"},
    {"name": "arabica咖啡", "business_type": "咖啡"},
    # 奶茶/茶饮
    {"name": "蜜雪冰城", "business_type": "奶茶"},
    {"name": "喜茶", "business_type": "奶茶"},
    {"name": "奈雪的茶", "business_type": "奶茶"},
    {"name": "茶百道", "business_type": "奶茶"},
    {"name": "古茗", "business_type": "奶茶"},
    {"name": "沪上阿姨", "business_type": "奶茶"},
    {"name": "书亦烧仙草", "business_type": "奶茶"},
    {"name": "茶颜悦色", "business_type": "奶茶"},
    {"name": "益禾堂", "business_type": "奶茶"},
    {"name": "霸王茶姬", "business_type": "奶茶"},
    {"name": "一点点", "business_type": "奶茶"},
    {"name": "柠季", "business_type": "奶茶"},
    {"name": "七分甜", "business_type": "奶茶"},
    {"name": "ChaTime日出茶太", "business_type": "奶茶"},
    {"name": "LINLEE林里", "business_type": "奶茶"},
    # 快餐
    {"name": "老乡鸡", "business_type": "快餐"},
    {"name": "塔斯汀", "business_type": "快餐"},
    {"name": "华莱士", "business_type": "快餐"},
    {"name": "正新鸡排", "business_type": "快餐"},
    {"name": "真功夫", "business_type": "快餐"},
    {"name": "吉野家", "business_type": "快餐"},
    {"name": "德克士", "business_type": "快餐"},
    {"name": "汉堡王", "business_type": "快餐"},
    {"name": "肯德基", "business_type": "快餐"},
    {"name": "麦当劳", "business_type": "快餐"},
    {"name": "萨莉亚", "business_type": "快餐"},
    {"name": "乡村基", "business_type": "快餐"},
    {"name": "米村拌饭", "business_type": "快餐"},
    {"name": "和府捞面", "business_type": "快餐"},
    {"name": "遇见小面", "business_type": "快餐"},
    # 火锅
    {"name": "海底捞", "business_type": "火锅"},
    {"name": "呷哺呷哺", "business_type": "火锅"},
    {"name": "小龙坎", "business_type": "火锅"},
    {"name": "巴奴毛肚火锅", "business_type": "火锅"},
    {"name": "捞王", "business_type": "火锅"},
    {"name": "凑凑", "business_type": "火锅"},
    {"name": "大龙燚", "business_type": "火锅"},
    {"name": "珮姐老火锅", "business_type": "火锅"},
    {"name": "七欣天", "business_type": "火锅"},
    {"name": "锅圈食汇", "business_type": "火锅"},
    # 烘焙/甜品
    {"name": "好利来", "business_type": "烘焙"},
    {"name": "鲍师傅", "business_type": "烘焙"},
    {"name": "虎头局渣打饼行", "business_type": "烘焙"},
    {"name": "墨茉点心局", "business_type": "烘焙"},
    {"name": "泸溪河桃酥", "business_type": "烘焙"},
    {"name": "熊猫不走蛋糕", "business_type": "烘焙"},
    {"name": "85度C", "business_type": "烘焙"},
    {"name": "原麦山丘", "business_type": "烘焙"},
    {"name": "克莉丝汀", "business_type": "烘焙"},
    {"name": "东海堂", "business_type": "烘焙"},
    # 正餐/其他
    {"name": "西贝莜面村", "business_type": "正餐"},
    {"name": "外婆家", "business_type": "正餐"},
    {"name": "绿茶餐厅", "business_type": "正餐"},
    {"name": "九毛九", "business_type": "正餐"},
    {"name": "太二酸菜鱼", "business_type": "正餐"},
    {"name": "杨国福麻辣烫", "business_type": "正餐"},
    {"name": "张亮麻辣烫", "business_type": "正餐"},
    {"name": "五谷鱼粉", "business_type": "正餐"},
    {"name": "沙县小吃", "business_type": "正餐"},
    {"name": "马记永兰州牛肉面", "business_type": "正餐"},
    {"name": "陈香贵", "business_type": "正餐"},
    {"name": "张拉拉", "business_type": "正餐"},
    {"name": "夸父炸串", "business_type": "正餐"},
    {"name": "炊烟小炒黄牛肉", "business_type": "正餐"},
    {"name": "十八汆", "business_type": "正餐"},
    # 零售/便利
    {"name": "全家FamilyMart", "business_type": "便利"},
    {"name": "罗森Lawson", "business_type": "便利"},
    {"name": "7-Eleven", "business_type": "便利"},
    {"name": "便利蜂", "business_type": "便利"},
    {"name": "盒马鲜生", "business_type": "便利"},
    {"name": "奥乐齐ALDI", "business_type": "便利"},
    {"name": "山姆会员商店", "business_type": "便利"},
    {"name": "胖东来", "business_type": "便利"},
    {"name": "钱大妈", "business_type": "便利"},
    {"name": "朴朴超市", "business_type": "便利"},
]

# ── Amap sampling ────────────────────────────────────────────────────────────

SAMPLE_CITIES = ["北京市", "上海市", "成都市"]  # 固定抽样城市，覆盖华北/华东/西南

async def _amap_sample(brand_name: str, headquarters: str) -> dict:
    """
    Query Amap POI around brand name in sample cities + HQ city.
    Returns aggregated: avg_price, avg_rating, delivery_rate, sample_size.
    """
    import os as _os
    import httpx as _httpx

    key = _os.environ.get("AMAP_KEY", "")
    if not key:
        return {}

    cities = list(dict.fromkeys([c for c in [headquarters] + SAMPLE_CITIES if c]))

    GEO_URL    = "https://restapi.amap.com/v3/geocode/geo"
    SEARCH_URL = "https://restapi.amap.com/v3/place/around"

    costs: list[float] = []
    ratings: list[float] = []
    delivery_flags: list[int] = []

    async with _httpx.AsyncClient(timeout=15) as client:
        for city in cities:
            # Geocode city center
            try:
                geo_r = await client.get(GEO_URL, params={"key": key, "address": city, "output": "json"})
                geo_d = geo_r.json()
                if geo_d.get("status") != "1" or not geo_d.get("geocodes"):
                    continue
                location = geo_d["geocodes"][0]["location"]  # "lng,lat"
            except Exception:
                continue

            # Search POIs within 5km of city center
            try:
                s_r = await client.get(SEARCH_URL, params={
                    "key": key,
                    "location": location,
                    "keywords": brand_name,
                    "radius": 5000,
                    "offset": 20,
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
                cost = biz.get("cost")
                rating = biz.get("rating")
                meal = biz.get("meal_ordering", "0")

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
    """Fetch Baidu Baike + English Wikipedia for a brand, return raw text."""
    baidu_url = f"https://baike.baidu.com/item/{name}"
    wiki_url = f"https://en.wikipedia.org/wiki/{name.replace(' ', '_')}"

    raw: dict[str, str] = {}

    for label, url in [("baidu", baidu_url), ("wiki", wiki_url)]:
        try:
            result = await crawler.arun(url=url)
            if result.success and result.markdown:
                raw[label] = result.markdown[:4000]
        except Exception as e:
            print(f"  [crawl] {label} failed for {name}: {e}")

    return raw


def _extract_brand_fields(name: str, business_type: str, raw: dict) -> dict:
    """Use Claude Haiku to extract structured fields from raw crawl text."""
    client = anthropic.Anthropic()
    combined = ""
    if raw.get("baidu"):
        combined += f"=== 百度百科 ===\n{raw['baidu'][:2000]}\n\n"
    if raw.get("wiki"):
        combined += f"=== Wikipedia ===\n{raw['wiki'][:2000]}\n\n"

    if not combined.strip():
        return {}

    prompt = f"""从以下关于「{name}」（{business_type}品牌）的文本中提取结构化信息。

{combined}

请以 JSON 格式返回，只填写能从文本中找到明确依据的字段，无法确认的填 null：
{{
  "founded_year": 数字或null,
  "store_count": 数字或null,
  "store_count_year": 门店数对应的年份数字或null,
  "headquarters": "城市名"或null,
  "revenue": 年营收数字（亿人民币）或null,
  "revenue_year": 营收对应年份或null,
  "avg_price_min": 客单价下限（元）或null,
  "avg_price_max": 客单价上限（元）或null,
  "price_tier": "低价"或"中价"或"高价"（必填，根据客单价判断：<30低价，30-80中价，>80高价）,
  "quality_perception": "低"或"中"或"高"（必填，根据品牌定位判断）,
  "main_competitors": ["竞品品牌名1", "竞品品牌名2"],
  "user_tags": ["用户认知标签1", "标签2", "标签3"],
  "description": "品牌简介，2-3句话",
  "data_sources": {{
    "store_count": "来源说明",
    "revenue": "来源说明"
  }}
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


def _generate_report(brand_id: int) -> Optional[str]:
    """Generate 10-module brand report via Claude Haiku."""
    client = anthropic.Anthropic()

    with Session(engine) as db:
        brand = db.get(BrandProfile, brand_id)
        if not brand:
            return None

    competitors = json.loads(brand.main_competitors or "[]")
    user_tags = json.loads(brand.user_tags or "[]")

    # 客单价：优先用高德实测均值，其次用爬取区间
    price_str = "未知"
    if brand.avg_price_amap:
        price_str = f"{brand.avg_price_amap}元（高德{brand.amap_sample_size or '?'}家门店均值）"
    elif brand.avg_price_min:
        price_str = f"{brand.avg_price_min}-{brand.avg_price_max}元"

    rating_str = f"{brand.avg_rating_amap}分（大众点评，高德{brand.amap_sample_size or '?'}家门店均值）" if brand.avg_rating_amap else "未知"
    delivery_str = f"{int(brand.delivery_rate * 100)}%" if brand.delivery_rate is not None else "未知"

    prompt = f"""你是餐饮行业品牌分析专家，为「{brand.name}」（{brand.business_type}）生成品牌体检报告。

品牌基础数据：
- 成立年份：{brand.founded_year or "未知"}
- 门店数量：{f"{brand.store_count}家（{brand.store_count_year}年）" if brand.store_count else "未知"}
- 总部：{brand.headquarters or "未知"}
- 年营收：{f"{brand.revenue}亿（{brand.revenue_year}年）" if brand.revenue else "未知"}
- 客单价：{price_str}
- 大众点评均分：{rating_str}
- 外卖覆盖率：{delivery_str}
- 价格定位：{brand.price_tier}
- 品质感知：{brand.quality_perception}
- 主要竞品：{", ".join(competitors) if competitors else "未知"}
- 用户标签：{", ".join(user_tags) if user_tags else "未知"}
- 品牌简介：{brand.description or ""}

请生成完整的10模块品牌体检报告，JSON格式：
{{
  "brand_summary": {{
    "one_liner": "一句话品牌定位（20字以内）",
    "tagline": "品牌核心价值关键词（10字以内）"
  }},
  "positioning": {{
    "x_label": "X轴标签（如：价格）",
    "y_label": "Y轴标签（如：品质感知）",
    "x_value": X轴位置（-10到10的数字，负左正右）,
    "y_value": Y轴位置（-10到10的数字，负下正上）,
    "competitors": [
      {{"name": "竞品名", "x": 数字, "y": 数字}}
    ]
  }},
  "competitor_table": [
    {{
      "name": "竞品名",
      "price_tier": "价格定位",
      "strength": "核心优势",
      "weakness": "主要弱点",
      "threat_level": "高/中/低"
    }}
  ],
  "user_perception": {{
    "positive": ["正向标签1", "标签2", "标签3"],
    "negative": ["负向标签1", "标签2"],
    "neutral": ["中性标签1", "标签2"]
  }},
  "issues": [
    {{"title": "核心问题标题", "description": "问题描述（40字以内）", "severity": "high/medium/low"}},
    {{"title": "问题2", "description": "...", "severity": "medium"}}
  ],
  "opportunities": [
    {{"title": "机会点标题", "description": "机会描述（40字以内）", "priority": "high/medium/low"}},
    {{"title": "机会点2", "description": "...", "priority": "medium"}}
  ],
  "growth_path": {{
    "route": "核心增长路径描述（30字以内）",
    "reason": "选择这条路径的原因（50字以内）",
    "steps": [
      {{"phase": "第一步", "action": "具体行动", "timeframe": "1-3个月", "kpi": "衡量指标"}},
      {{"phase": "第二步", "action": "具体行动", "timeframe": "3-6个月", "kpi": "衡量指标"}},
      {{"phase": "第三步", "action": "具体行动", "timeframe": "6-12个月", "kpi": "衡量指标"}}
    ]
  }},
  "optimization": [
    {{
      "title": "优化策略标题",
      "priority": "高优先级/中优先级/快速见效",
      "description": "策略说明（50字以内）",
      "actions": ["行动1", "行动2", "行动3"]
    }}
  ],
  "risks": [
    {{
      "title": "风险标题",
      "level": "high/medium/low",
      "description": "风险说明（40字以内）",
      "mitigation": "应对措施（40字以内）"
    }}
  ],
  "trend": {{
    "years": [2023, 2024, 2025, 2026, 2027],
    "metrics": [
      {{
        "name": "市场热度指数",
        "values": [数字, 数字, 数字, 数字, 数字],
        "unit": "指数"
      }},
      {{
        "name": "品牌竞争压力",
        "values": [数字, 数字, 数字, 数字, 数字],
        "unit": "指数"
      }}
    ],
    "insight": "趋势洞察（40字以内）"
  }}
}}

注意：
- positioning 的坐标值必须基于品牌真实定位，竞品也要有合理相对位置
- issues 和 opportunities 各2-3条，聚焦最重要的
- 所有分析内容是 AI 观点，不是财务预测
- trend 的数值范围建议 0-100，要有波动有趋势"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
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
                # Try removing control characters
                cleaned = re.sub(r'[\x00-\x1f\x7f]', ' ', report_json)
                try:
                    json.loads(cleaned)
                    return cleaned
                except Exception:
                    pass
    except Exception as e:
        print(f"  [report] generation failed for brand_id={brand_id}: {e}")
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def seed(
    filter_type: Optional[str] = None,
    limit: Optional[int] = None,
    skip_crawl: bool = False,
):
    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        print("[seed] crawl4ai not installed — install it first: pip install crawl4ai")
        return

    brands_to_process = BRANDS
    if filter_type and filter_type != "all":
        brands_to_process = [b for b in BRANDS if b["business_type"] == filter_type]
    if limit:
        brands_to_process = brands_to_process[:limit]

    print(f"[seed] Processing {len(brands_to_process)} brands (skip_crawl={skip_crawl})")

    async with AsyncWebCrawler(verbose=False) as crawler:
        for i, brand_def in enumerate(brands_to_process):
            name = brand_def["name"]
            business_type = brand_def["business_type"]
            print(f"\n[{i+1}/{len(brands_to_process)}] {name} ({business_type})")

            # Check if already exists
            with Session(engine) as db:
                existing = db.exec(select(BrandProfile).where(BrandProfile.name == name)).first()

            if existing:
                brand_id = existing.id
                # Check if report already exists
                with Session(engine) as db:
                    rpt = db.exec(select(BrandReport).where(BrandReport.brand_id == brand_id)).first()
                if rpt:
                    print(f"  [skip] already seeded")
                    continue
                print(f"  [exists] profile found (id={brand_id}), generating report only")
            else:
                # Crawl
                raw: dict = {}
                if not skip_crawl:
                    print(f"  [crawl] fetching baidu+wiki...")
                    raw = await _crawl_brand(crawler, name)
                    time.sleep(1)  # polite delay

                # Extract fields
                print(f"  [extract] running Claude Haiku...")
                fields = _extract_brand_fields(name, business_type, raw)

                # Amap sampling: avg price, rating, delivery rate
                hq = fields.get("headquarters") or ""
                print(f"  [amap] sampling {SAMPLE_CITIES + ([hq] if hq else [])}...")
                amap = await _amap_sample(name, hq)
                if amap:
                    print(f"  [amap] sample_size={amap.get('sample_size')} avg_price={amap.get('avg_price')} avg_rating={amap.get('avg_rating')} delivery_rate={amap.get('delivery_rate')}")
                else:
                    print(f"  [amap] no data (key missing or no POIs)")

                # Save BrandProfile
                with Session(engine) as db:
                    profile = BrandProfile(
                        name=name,
                        business_type=business_type,
                        founded_year=fields.get("founded_year"),
                        store_count=fields.get("store_count"),
                        store_count_year=fields.get("store_count_year"),
                        headquarters=hq,
                        revenue=fields.get("revenue"),
                        revenue_year=fields.get("revenue_year"),
                        avg_price_min=fields.get("avg_price_min"),
                        avg_price_max=fields.get("avg_price_max"),
                        avg_price_amap=amap.get("avg_price"),
                        avg_rating_amap=amap.get("avg_rating"),
                        delivery_rate=amap.get("delivery_rate"),
                        amap_sample_size=amap.get("sample_size"),
                        price_tier=fields.get("price_tier") or "中价",
                        quality_perception=fields.get("quality_perception") or "中",
                        main_competitors=json.dumps(fields.get("main_competitors") or [], ensure_ascii=False),
                        user_tags=json.dumps(fields.get("user_tags") or [], ensure_ascii=False),
                        description=fields.get("description") or "",
                        data_sources=json.dumps(fields.get("data_sources") or {}, ensure_ascii=False),
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                    db.add(profile)
                    db.commit()
                    db.refresh(profile)
                    brand_id = profile.id
                    print(f"  [saved] BrandProfile id={brand_id}")

            # Generate report
            print(f"  [report] generating 10-module report...")
            report_json = _generate_report(brand_id)
            if report_json:
                with Session(engine) as db:
                    report = BrandReport(
                        brand_id=brand_id,
                        report_json=report_json,
                        generated_at=datetime.now(timezone.utc),
                        model_used="claude-haiku-4-5-20251001",
                    )
                    db.add(report)
                    db.commit()
                print(f"  [done] BrandReport saved")
            else:
                print(f"  [warn] report generation failed, skipping")

    print(f"\n[seed] Done!")


async def enrich_amap(limit: Optional[int] = None):
    """Only update amap fields (avg_price_amap, avg_rating_amap, delivery_rate)
    for existing brand_profiles. Does not re-crawl or re-generate reports."""
    with Session(engine) as db:
        brands = db.exec(select(BrandProfile)).all()

    if limit:
        brands = brands[:limit]

    print(f"[enrich-amap] Updating {len(brands)} brands...")

    for i, brand in enumerate(brands):
        print(f"\n[{i+1}/{len(brands)}] {brand.name} (hq={brand.headquarters or '?'})")
        amap = await _amap_sample(brand.name, brand.headquarters or "")
        if not amap or not amap.get("sample_size"):
            print(f"  [skip] no POIs found")
            continue

        print(f"  sample={amap.get('sample_size')} price={amap.get('avg_price')} rating={amap.get('avg_rating')} delivery={amap.get('delivery_rate')}")

        with Session(engine) as db:
            b = db.get(BrandProfile, brand.id)
            if not b:
                continue
            b.avg_price_amap = amap.get("avg_price")
            b.avg_rating_amap = amap.get("avg_rating")
            b.delivery_rate = amap.get("delivery_rate")
            b.amap_sample_size = amap.get("sample_size")
            b.updated_at = datetime.now(timezone.utc)
            db.add(b)
            db.commit()

        await asyncio.sleep(0.3)  # polite delay between brands

    print(f"\n[enrich-amap] Done!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--brands", default="all", help="Filter by business_type (or 'all')")
    parser.add_argument("--limit", type=int, default=None, help="Max brands to process")
    parser.add_argument("--skip-crawl", action="store_true", help="Skip web crawling (use empty raw data)")
    parser.add_argument("--enrich-amap", action="store_true", help="Only update amap fields for existing brands")
    args = parser.parse_args()

    if args.enrich_amap:
        asyncio.run(enrich_amap(limit=args.limit))
    else:
        asyncio.run(seed(
            filter_type=args.brands,
            limit=args.limit,
            skip_crawl=args.skip_crawl,
        ))
