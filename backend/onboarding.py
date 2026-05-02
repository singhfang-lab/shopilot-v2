from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import get_current_user
from .db import Merchant, UserMerchant, get_db
from .rag import ingest_text
from . import rag as _rag

# ── Competitor lookup helpers ────────────────────────────────────────────────

def _broad_category_keywords(category: str) -> list[str]:
    """Extract broad sector keywords from a category string for cross-category matching."""
    sectors = [
        "茶", "咖啡", "烤鸡", "炸鸡", "汉堡", "披萨", "寿司", "拉面", "火锅", "烧烤",
        "快餐", "冰淇淋", "面包", "烘焙", "牛排", "海鲜", "米饭", "面食",
        # 补充印尼/亚洲常见赛道关键词
        "烤肉", "鸭", "鸡", "肉丸", "沙爹", "巴东", "爪哇", "三明治", "甜品",
        "日式", "韩式", "中式", "台式", "西式", "印尼", "泰式", "越南",
    ]
    return [s for s in sectors if s in category]


def _get_competitors(brand: dict, all_brands: list[dict]) -> dict:
    """Return tiered competitor data for a brand from chains.json."""
    category = brand.get("category", "")
    brand_name = brand.get("name", "").lower()
    store_count = brand.get("storeCount", 0) or 0

    # Build sector peers: exact category first, then broad keyword expansion
    broad_keys = _broad_category_keywords(category)

    exact_cat = [
        b for b in all_brands
        if b.get("category") == category and b.get("name", "").lower() != brand_name
    ]
    broad_cat = [
        b for b in all_brands
        if b.get("category") != category
        and b.get("name", "").lower() != brand_name
        and broad_keys
        and any(k in b.get("category", "") for k in broad_keys)
    ]

    # same_cat = full sector pool, sorted by store count desc
    same_cat = exact_cat + [b for b in broad_cat if b not in exact_cat]
    same_cat_sorted = sorted(same_cat, key=lambda b: b.get("storeCount", 0) or 0, reverse=True)

    # Head reference: must be larger than self; threshold 5x (10x for tiny brands)
    # Fallback: if no brand meets threshold, take the single largest in sector as reference
    head_threshold = 10 if store_count < 20 else 5
    head_strict = [b for b in same_cat_sorted if (b.get("storeCount") or 0) > store_count * head_threshold]
    if head_strict:
        head = head_strict[:1]
    elif same_cat_sorted and (same_cat_sorted[0].get("storeCount") or 0) > store_count:
        # Fallback: largest in sector (even if not threshold-distant) — gives context benchmark
        head = [same_cat_sorted[0]]
    else:
        head = []

    # Direct: exact-category brands by closeness first, then broad fill-ins (cap 4 total)
    direct_exact = sorted(
        [b for b in exact_cat if 0.3 * store_count <= (b.get("storeCount") or 0) <= store_count * 3],
        key=lambda b: abs((b.get("storeCount") or 0) - store_count)
    )
    direct_broad = sorted(
        [b for b in broad_cat
         if 0.3 * store_count <= (b.get("storeCount") or 0) <= store_count * 3
         and b not in head and b not in direct_exact],
        key=lambda b: abs((b.get("storeCount") or 0) - store_count)
    )
    # Fill up to 4: exact first, then broad
    direct = (direct_exact + direct_broad)[:4]

    # Growth: brands not in head/direct sorted by closeness, includes small threats (>0.05x)
    growth_pool = [
        b for b in same_cat_sorted
        if b not in head and b not in direct
        and (b.get("storeCount") or 0) >= max(1, store_count * 0.05)
    ]
    growth_pool.sort(key=lambda b: abs((b.get("storeCount") or 0) - store_count))
    growth = growth_pool[:2]

    def _fmt(b: dict) -> dict:
        return {
            "name": b["name"],
            "category": b.get("category", ""),
            "storeCount": b.get("storeCount"),
            "priceBand": b.get("priceBand"),
            "founded": b.get("founded"),
            "expansion": b.get("expansion"),
            "instagram": b.get("instagram"),
        }

    return {
        "head": [_fmt(b) for b in head],
        "direct": [_fmt(b) for b in direct],
        "growth": [_fmt(b) for b in growth],
    }

# ── Market size reference data (Indonesia, 2024 estimates) ──────────────────
# Used to inject accurate sector market scale into LLM prompt, avoiding hallucination
_MARKET_SIZE: dict[str, dict] = {
    "咖啡":    {"size": "约 USD 50亿", "growth": "~15%", "note": "印尼咖啡饮料市场（含连锁+独立店）"},
    "精品咖啡": {"size": "约 USD 8亿",  "growth": "~20%", "note": "印尼精品咖啡连锁赛道"},
    "大众咖啡": {"size": "约 USD 12亿", "growth": "~18%", "note": "印尼大众咖啡连锁赛道"},
    "茶":      {"size": "约 USD 15亿", "growth": "~22%", "note": "印尼茶饮市场（含奶茶/果茶）"},
    "新中式茶": {"size": "约 USD 4亿",  "growth": "~35%", "note": "印尼新中式茶饮赛道（快速增长中）"},
    "炸鸡":    {"size": "约 USD 30亿", "growth": "~12%", "note": "印尼炸鸡/快餐市场"},
    "烤鸡":    {"size": "约 USD 20亿", "growth": "~10%", "note": "印尼烤鸡/家禽连锁市场"},
    "快餐":    {"size": "约 USD 60亿", "growth": "~10%", "note": "印尼快餐市场（含各国料理）"},
    "汉堡":    {"size": "约 USD 8亿",  "growth": "~12%", "note": "印尼汉堡连锁赛道"},
    "披萨":    {"size": "约 USD 5亿",  "growth": "~10%", "note": "印尼披萨连锁赛道"},
    "寿司":    {"size": "约 USD 4亿",  "growth": "~15%", "note": "印尼日式寿司连锁赛道"},
    "拉面":    {"size": "约 USD 3亿",  "growth": "~18%", "note": "印尼日式拉面连锁赛道"},
    "烘焙":    {"size": "约 USD 12亿", "growth": "~10%", "note": "印尼面包烘焙连锁市场"},
    "面包":    {"size": "约 USD 12亿", "growth": "~10%", "note": "印尼面包烘焙连锁市场"},
    "牛排":    {"size": "约 USD 3亿",  "growth": "~12%", "note": "印尼牛排西餐连锁赛道"},
    "火锅":    {"size": "约 USD 5亿",  "growth": "~20%", "note": "印尼火锅连锁赛道"},
    "中式":    {"size": "约 USD 8亿",  "growth": "~15%", "note": "印尼中式餐厅连锁赛道"},
    "韩式":    {"size": "约 USD 6亿",  "growth": "~20%", "note": "印尼韩式餐厅连锁赛道"},
    "日式":    {"size": "约 USD 7亿",  "growth": "~15%", "note": "印尼日式餐厅连锁赛道"},
    "巴东":    {"size": "约 USD 5亿",  "growth": "~8%",  "note": "印尼巴东菜连锁赛道"},
    "印尼":    {"size": "约 USD 25亿", "growth": "~8%",  "note": "印尼本土餐饮连锁市场"},
}


def _get_market_hint(category: str) -> dict:
    """Return market size reference for a given category string.
    Matches longest key first to prefer specific entries over broad ones."""
    for key in sorted(_MARKET_SIZE.keys(), key=len, reverse=True):
        if key in category:
            return _MARKET_SIZE[key]
    return {}


router = APIRouter(prefix="/onboarding", tags=["onboarding"])

GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "")
CLAUDE_API_BASE = "https://api.anthropic.com/v1"
CLAUDE_MODEL = "claude-sonnet-4-6"

_CHAINS_PATH = Path(__file__).parent / "chains.json"
_chains_cache: Optional[list[dict]] = None


def _load_chains() -> list[dict]:
    global _chains_cache
    if _chains_cache is None:
        try:
            data = json.loads(_CHAINS_PATH.read_text(encoding="utf-8"))
            _chains_cache = data.get("brands", [])
        except Exception:
            _chains_cache = []
    return _chains_cache


def _match_chain(query: str) -> Optional[dict]:
    q = query.strip().lower()
    for brand in _load_chains():
        if brand.get("name", "").lower() == q:
            return brand
        for alias in (brand.get("aliases") or []):
            if alias.lower() == q:
                return brand
    # partial match fallback
    for brand in _load_chains():
        if q in brand.get("name", "").lower():
            return brand
    return None


# ── /onboarding/search ──────────────────────────────────────────────────────

@router.get("/search")
async def search_brand(
    q: str,
    user=Depends(get_current_user),
):
    """Search brand via Google Places + local chains KB."""
    if not q or len(q.strip()) < 1:
        return {"candidates": []}

    chain_match = _match_chain(q)
    candidates: list[dict] = []

    google_key = os.environ.get("GOOGLE_MAPS_KEY", "")
    if google_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    json={
                        "textQuery": f"{q} Indonesia",
                        "maxResultCount": 15,
                        "languageCode": "en",
                        "locationRestriction": {
                            "rectangle": {
                                "low": {"latitude": -11.0, "longitude": 95.0},
                                "high": {"latitude": 6.0, "longitude": 141.0},
                            }
                        },
                    },
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": google_key,
                        "X-Goog-FieldMask": "places.id,places.displayName,places.rating,places.userRatingCount,places.priceLevel,places.formattedAddress,places.photos",
                    },
                )
            if res.status_code == 200:
                raw_places = res.json().get("places", [])
                candidates = _aggregate_places(raw_places, q, chain_match, google_key)
        except Exception as e:
            print(f"[onboarding/search] Google Places error: {e}")

    # If Google fails or returns nothing, fall back to chains KB only
    if not candidates and chain_match:
        candidates = [{
            "name": chain_match["name"],
            "rating": 0,
            "review_count": 0,
            "store_count": chain_match.get("storeCount", 1),
            "addresses": [],
            "place_ids": [],
            "photo_url": None,
            "chain_kb": _chain_kb_fields(chain_match),
        }]

    return {"candidates": candidates}


def _aggregate_places(raw: list[dict], query: str, chain_match: Optional[dict], google_key: str = "") -> list[dict]:
    """Group chain places together; return individual places as separate candidates."""
    q_lower = query.strip().lower()
    merge_keys: set[str] = {q_lower}
    if chain_match:
        merge_keys.add(chain_match["name"].lower())
        for alias in (chain_match.get("aliases") or []):
            merge_keys.add(alias.lower())

    chain_entries: list[dict] = []
    individual: list[dict] = []

    for p in raw:
        name = p.get("displayName", {}).get("text", "").strip()
        if not name:
            continue
        n_lower = name.lower()
        photo_ref = (p.get("photos") or [{}])[0].get("name")

        entry = {
            "place_id": p.get("id", ""),
            "name": name,
            "rating": p.get("rating", 0) or 0,
            "review_count": p.get("userRatingCount", 0) or 0,
            "address": p.get("formattedAddress", ""),
            "photo_name": photo_ref,
        }

        is_chain_match = any(mk in n_lower or n_lower in mk for mk in merge_keys)
        if chain_match and is_chain_match:
            chain_entries.append(entry)
        else:
            individual.append(entry)

    results: list[dict] = []

    # Aggregate all chain locations into one candidate
    if chain_entries:
        best = max(chain_entries, key=lambda e: e["review_count"])
        photo_url = (
            f"https://places.googleapis.com/v1/{best['photo_name']}/media?maxHeightPx=200&key={google_key}"
            if best["photo_name"] else None
        )
        cand: dict = {
            "name": chain_match["name"],
            "rating": round(sum(e["rating"] for e in chain_entries) / len(chain_entries), 1),
            "review_count": sum(e["review_count"] for e in chain_entries),
            "store_count": chain_match.get("storeCount") or len(chain_entries),
            "addresses": [e["address"] for e in chain_entries[:3]],
            "place_ids": [e["place_id"] for e in chain_entries],
            "photo_url": photo_url,
            "merchant_type": "existing",
            "chain_kb": _chain_kb_fields(chain_match),
        }
        results.append(cand)

    # Each individual (non-chain) place is its own candidate
    for entry in individual[:8]:
        photo_url = (
            f"https://places.googleapis.com/v1/{entry['photo_name']}/media?maxHeightPx=200&key={google_key}"
            if entry["photo_name"] else None
        )
        results.append({
            "name": entry["name"],
            "rating": entry["rating"],
            "review_count": entry["review_count"],
            "store_count": 1,
            "addresses": [entry["address"]],
            "place_ids": [entry["place_id"]],
            "photo_url": photo_url,
            "merchant_type": "existing",
        })

    return results


def _chain_kb_fields(brand: dict) -> dict:
    return {
        "category": brand.get("category"),
        "storeCount": brand.get("storeCount"),
        "storeCountAsOf": brand.get("storeCountAsOf"),
        "priceBand": brand.get("priceBand"),
        "founded": brand.get("founded"),
        "founder": brand.get("founder"),
        "markets": brand.get("markets"),
        "expansion": brand.get("expansion"),
    }


# ── /onboarding/bind ────────────────────────────────────────────────────────

class BindRequest(BaseModel):
    name: str
    place_ids: list[str] = []
    store_count: int = 1
    rating: float = 0
    review_count: int = 0
    addresses: list[str] = []
    chain_kb: Optional[dict] = None
    merchant_type: str = "existing"  # "existing" | "new"


@router.post("/bind")
def bind_merchant(
    req: BindRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update the merchant bound to this user."""
    # Check existing binding
    existing_um = db.exec(select(UserMerchant).where(UserMerchant.user_id == user.id)).first()

    meta = {
        "place_ids": req.place_ids,
        "store_count": req.store_count,
        "rating": req.rating,
        "review_count": req.review_count,
        "addresses": req.addresses,
        "merchant_type": req.merchant_type,
    }
    if req.chain_kb:
        meta["chain_kb"] = req.chain_kb

    if existing_um:
        merchant = db.get(Merchant, existing_um.merchant_id)
        if merchant:
            brand_changed = merchant.name != req.name
            merchant.name = req.name
            merchant.meta_json = json.dumps(meta, ensure_ascii=False)
            db.add(merchant)
            db.commit()
            db.refresh(merchant)
            # Clear KB only when brand actually changes, and don't block on failure
            if brand_changed:
                try:
                    old_shop_id = f"u{user.id}_m{merchant.id}"
                    _rag.clear_shop_kb(old_shop_id)
                except Exception:
                    pass
            return {"merchant_id": merchant.id, "name": merchant.name}

    # Create new merchant
    merchant = Merchant(
        name=req.name,
        meta_json=json.dumps(meta, ensure_ascii=False),
    )
    db.add(merchant)
    db.flush()
    db.add(UserMerchant(user_id=user.id, merchant_id=merchant.id, role="owner"))
    db.commit()
    db.refresh(merchant)
    return {"merchant_id": merchant.id, "name": merchant.name}


# ── /onboarding/profile ─────────────────────────────────────────────────────

@router.get("/profile")
async def get_profile(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return structured brand profile JSON for rendering the brand canvas."""
    um = db.exec(select(UserMerchant).where(UserMerchant.user_id == user.id)).first()
    if not um:
        raise HTTPException(status_code=404, detail="No merchant bound")

    merchant = db.get(Merchant, um.merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    meta = {}
    try:
        meta = json.loads(merchant.meta_json or "{}")
    except Exception:
        pass

    chain_kb = meta.get("chain_kb") or {}
    brand_name = merchant.name
    store_count = meta.get("store_count", 1)
    rating = meta.get("rating", 0)
    review_count = meta.get("review_count", 0)
    place_ids = meta.get("place_ids", [])
    addresses = meta.get("addresses", [])
    merchant_type = meta.get("merchant_type", "existing")

    # Single-store avg reviews (sample)
    sample_size = len(place_ids) if place_ids else 1
    avg_review_per_store = round(review_count / sample_size) if sample_size and review_count else 0

    # Load chains for competitor lookup
    all_brands = _load_chains()
    chain_match = _match_chain(brand_name)
    competitors = _get_competitors(chain_match, all_brands) if chain_match else {"head": [], "direct": [], "growth": []}

    # Build competitor store avg reviews from meta (we don't have Places data for them,
    # use storeCount as denominator if review_count is unavailable — mark as estimated)
    def _comp_avg(comp: dict) -> Optional[int]:
        sc = comp.get("storeCount")
        if not sc:
            return None
        # We don't have per-brand review counts in chains.json; return None = AI will fill
        return None

    # Enrich direct competitors with avg_review estimate placeholder
    for tier in ("head", "direct", "growth"):
        for c in competitors[tier]:
            c["avg_review_per_store"] = _comp_avg(c)

    # If a profile_json was already generated and stored, return it merged with live meta
    stored_profile = meta.get("profile_json")

    profile = {
        "brand_name": brand_name,
        "merchant_type": merchant_type,
        "meta": {
            "store_count": store_count,
            "rating": rating,
            "review_count": review_count,
            "avg_review_per_store": avg_review_per_store,
            "sample_size": sample_size,
            "addresses": addresses[:3],
            "place_ids": place_ids,
        },
        "chain_kb": chain_kb,
        "competitors": competitors,
        "has_chain_data": bool(chain_match),
        "profile_json": stored_profile,  # None if not yet generated
    }
    return profile


# ── /onboarding/generate-kb ─────────────────────────────────────────────────

class GenerateKBRequest(BaseModel):
    merchant_id: int
    brand_name: str
    merchant_type: str = "existing"
    chain_kb: Optional[dict] = None


@router.post("/generate-kb")
async def generate_kb(
    req: GenerateKBRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream SSE: use Gemini with web grounding to generate initial KB."""
    # Verify user owns this merchant
    um = db.exec(
        select(UserMerchant).where(
            UserMerchant.user_id == user.id,
            UserMerchant.merchant_id == req.merchant_id,
        )
    ).first()
    if not um:
        raise HTTPException(status_code=403, detail="No access to this merchant")

    shop_id = f"u{user.id}_m{req.merchant_id}"

    # Clear any existing KB for this merchant before regenerating
    await _rag.clear_shop_kb_async(shop_id)

    async def _stream():
        yield _sse("status", {"msg": f"正在分析 {req.brand_name} 的品牌信息..."})

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            yield _sse("error", {"msg": "Anthropic API key not configured"})
            return

        # ── Gather all known data to inject ──────────────────────────────────
        merchant = db.get(Merchant, req.merchant_id)
        meta: dict = {}
        try:
            meta = json.loads(merchant.meta_json or "{}") if merchant else {}
        except Exception:
            pass

        rating = meta.get("rating", 0)
        review_count = meta.get("review_count", 0)
        store_count_sample = len(meta.get("place_ids", [])) or meta.get("store_count", 1)
        avg_review = round(review_count / store_count_sample) if store_count_sample and review_count else 0

        chain_match = _match_chain(req.brand_name)
        all_brands = _load_chains()
        competitors = _get_competitors(chain_match, all_brands) if chain_match else {"head": [], "direct": [], "growth": []}

        ckb = req.chain_kb or {}
        store_count_total = ckb.get("storeCount") or meta.get("store_count", 1)
        price_band = ckb.get("priceBand", "")
        category = ckb.get("category", "")
        founded = ckb.get("founded", "")
        expansion = ckb.get("expansion", "")
        instagram = chain_match.get("instagram", "") if chain_match else ""
        tiktok = chain_match.get("tiktok", "") if chain_match else ""

        # Format competitor context with pre-computed ratios vs self
        def _comp_lines(comps: list[dict]) -> str:
            lines = []
            for c in comps:
                parts = [c["name"]]
                if c.get("storeCount"):
                    parts.append(f"{c['storeCount']}家门店")
                if c.get("priceBand"):
                    parts.append(c["priceBand"])
                if c.get("expansion"):
                    parts.append(c["expansion"])
                if c.get("instagram"):
                    parts.append(f"IG: {c['instagram']}")
                lines.append("  - " + "，".join(parts))
            return "\n".join(lines) if lines else "  - 暂无数据"

        comp_ctx = ""
        if competitors["head"] or competitors["direct"] or competitors["growth"]:
            ratio_hint = ""
            if avg_review:
                ratio_hint = f"\n注意：{req.brand_name} 单店均评论为 {avg_review} 条，insight 里请直接写出竞品单店均评论是你的 X 倍（或你是竞品的 X 倍），用具体倍数。"
            comp_ctx = f"""
已知竞品数据（来自行业数据库，直接使用这些数字，不要编造）：
头部参照（规模远大于该品牌）：
{_comp_lines(competitors['head'])}
直接竞争（规模相近）：
{_comp_lines(competitors['direct'])}
成长型威胁（快速扩张中）：
{_comp_lines(competitors['growth'])}{ratio_hint}"""

        # Determine if brand is Chinese-origin
        is_cn_brand = chain_match.get("origin", "") == "CN" if chain_match else False

        # Market size anchor for prompt
        mkt = _get_market_hint(category)
        market_hint = ""
        if mkt:
            market_hint = f"\n- 赛道市场规模参考（直接用于 market.market_size 和 yoy_growth）：{mkt['size']}，年增长 {mkt['growth']}（{mkt['note']}）"

        # Scale label for tone calibration
        if store_count_total <= 10:
            scale_hint = "该品牌规模较小（≤10家），mirror 句式应聚焦单店口碑和本地扎根，不要用「X年深耕全国」等大连锁语气"
        elif store_count_total <= 30:
            scale_hint = "该品牌为小型连锁（10-30家），mirror 句式聚焦区域口碑建设和差异化竞争，避免和大连锁直接类比"
        elif store_count_total <= 100:
            scale_hint = "该品牌为中型连锁（30-100家），mirror 句式可提扩张节奏和竞品压力，数字要具体"
        else:
            scale_hint = "该品牌为大型连锁（100家以上），mirror 句式聚焦规模效率和市场份额竞争"

        known_data = f"""
已知数据（直接使用这些数字，勿修改）：
- Google Places 样本：{store_count_sample} 家门店，评分 {rating}，总评论数 {review_count}，单店均评论 {avg_review} 条
- 品牌总门店数：{store_count_total} 家
- 价格带：{price_band}
- 品类：{category}
- 成立年份：{founded}
- 扩张状态：{expansion}
- 品牌来源：{"中国" if is_cn_brand else "印尼本土"}{market_hint}
- Instagram 账号：{instagram or '待核实'}
- TikTok 账号：{tiktok or '待核实'}{comp_ctx}"""

        # Build self entry for competitors.direct
        self_entry = f'{{"name": "{req.brand_name}", "is_self": true, "store_count": {store_count_total}, "price_band": "{price_band}", "avg_review_per_store": {avg_review}, "rating": {rating}, "founded": "{founded}", "insight": ""}}'

        prompt = f"""你是「商户助手」平台的 AI 顾问。商家刚完成品牌注册，这是平台对其品牌的**首次自动扫描**，请生成一份供商家在手机端查看的品牌画像 JSON。

品牌名：{req.brand_name}
商家类型：{"连锁品牌" if req.merchant_type == "existing" else "新品牌/独立门店"}
{known_data}

请严格输出以下 JSON 结构，不要输出任何其他内容，不要加 markdown 代码块标记：

{{
  "scan_summary": {{
    "mirror": "用已知数据里的具体数字说明现状，{scale_hint}，语气直接像朋友在复盘",
    "window": "基于数据揭示一个商家自己可能没意识到的洞察，必须引用竞品名或具体数字，句式：「[竞品]正在/已经[动作]，这意味着[你的机会/风险]」",
    "door": "给出最值得立刻做的具体行动，必须具体到：上传什么数据、分析哪个指标，句式：「先把[具体数据]导入平台，找出[具体问题]」",
    "action": "平台能优先帮到的最核心两件事，句式：「平台优先帮助：**[事项1]**和**[事项2]**」，加粗用**标记"
  }},
  "market": {{
    "market_size": "直接使用已知数据里的赛道市场规模数字，禁止自行推断",
    "yoy_growth": "直接使用已知数据里的年增长率，禁止自行推断",
    "store_gap": "用已知数字：{store_count_total} vs [头部品牌门店数]家",
    "store_gap_note": "头部品牌名称",
    "expansion_trend": "用扩张状态数据填写，如：约 +2–3 家",
    "expansion_note": "一句评价，如：扩张几乎停滞",
    "core_market": "核心市场城市中文名",
    "core_market_note": "具体商圈，如：Senopati · Sudirman",
    "market_bar_data": [
      {{"name": "竞品名（用已知竞品数据里的品牌）", "is_self": false, "pct": 门店数占最大值的百分比整数, "store_count": 门店数整数}},
      {{"name": "{req.brand_name}", "is_self": true, "pct": 该品牌门店数占最大值的百分比整数, "store_count": {store_count_total}}}
    ]
  }},
  "positioning": {{
    "axis_items": [
      {{"name": "品牌名（用已知竞品）", "price_low": 价格下限整数(印尼盾), "price_high": 价格上限整数, "is_self": false}},
      {{"name": "{req.brand_name}", "price_low": 从价格带解析, "price_high": 从价格带解析, "is_self": true}}
    ]
  }},
  "social": [
    {{"platform": "INSTAGRAM", "handle": "{instagram or '@待核实'}", "followers": "基于公开信息估算粉丝量", "note": "一句描述社媒表现", "ai_inferred": true}},
    {{"platform": "TIKTOK", "handle": "{tiktok or '待核实'}", "followers": "声量高/中/低", "note": "TikTok运营评价", "ai_inferred": true}},
    {{"platform": "GOFOOD / GRABFOOD", "handle": "全店已入驻", "followers": "评分如：约 4.5+", "note": "外卖表现描述", "ai_inferred": true}}
  ],
  "competitors": {{
    "head": [
      {{
        "name": "使用已知头部竞品名", "category": "{category}", "founded": "创立年份",
        "store_count": 使用已知门店数整数, "price_band": "使用已知价格带",
        "avg_review_per_store": 基于公开数据推断的单店均评论整数,
        "rating": 评分小数, "ig_followers": "IG粉丝量估算",
        "is_warn": false,
        "insight": "必须包含：[竞品名]的单店均评论约[数字]条，是你的[X]倍/你是它的[X]倍。[一句对商家的启示]"
      }}
    ],
    "direct": [
      {self_entry},
      {{
        "name": "使用已知直接竞品名", "category": "{category}", "founded": "创立年份",
        "store_count": 使用已知门店数整数, "price_band": "使用已知价格带",
        "avg_review_per_store": 基于公开数据推断整数,
        "rating": 评分小数,
        "is_warn": 是否是近期快速增长的威胁(true/false),
        "insight": "必须引用具体数字：[竞品名]单店均评论[数字]条，[与你对比的结论]"
      }}
    ],
    "growth": [
      {{
        "name": "使用已知成长型竞品名", "category": "{category}", "founded": "创立年份",
        "store_count": 使用已知门店数整数, "price_band": "使用已知价格带",
        "avg_review_per_store": 基于公开数据推断整数,
        "rating": 评分小数,
        "is_warn": true,
        "insight": "必须引用具体数字，并说明为什么是威胁：[竞品]单店均评论[数字]条（你的[X]倍/[X]%），[核心威胁点]"
      }}
    ]
  }},
  "ability": {{
    "basic": [
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}},
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}},
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}}
    ],
    "growth": [
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}},
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}},
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}}
    ],
    "scale": [
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}},
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}},
      {{"level": "strong/mid/weak", "title": "如何[具体能力]：[基于品牌特征的判断]", "module": "对应平台模块名"}}
    ]
  }},
  "diagnosis": {{
    "because": "必须用已知数字：单店均评论{avg_review}条[排名评价]，但{store_count_total}家门店[扩张问题]。[最大威胁竞品]以[价格差异]正在[具体动作]",
    "therefore": "所以核心问题不是[表面问题]，而是[本质效率问题]",
    "action": "上传近3个月[具体数据类型]，找出[具体分析目标]，先把[最薄弱环节]拉到均值"
  }}
}}

严格要求：
1. scan_summary 三句话必须用已知数据里的具体数字，禁止空洞描述
2. competitors 里每个竞品的 insight 必须包含具体数字对比（倍数或绝对值）
3. competitors.direct 第一条必须是 is_self:true 的商家自身（已预填），不要修改
4. market_bar_data 必须用已知竞品门店数计算 pct（最大值=100）
5. avg_review_per_store 用整数，无法推断填 null
6. is_warn:true 的竞品前端会用橙色边框高亮，请准确标记真正的近期快速扩张威胁
7. 所有文字字段用中文，品牌名/地名/账号可保留英文
8. 直接输出 JSON，不加任何解释和 markdown 标记"""

        full_text = ""
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                async with client.stream(
                    "POST", f"{CLAUDE_API_BASE}/messages",
                    json={
                        "model": CLAUDE_MODEL,
                        "max_tokens": 4096,
                        "temperature": 0.3,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": True,
                    },
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        print(f"[generate-kb] Claude error {resp.status_code}: {body[:200]}")
                        yield _sse("error", {"msg": f"Claude error {resp.status_code}"})
                        return
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:].strip()
                            try:
                                obj = json.loads(raw)
                                if obj.get("type") == "content_block_delta":
                                    chunk = obj.get("delta", {}).get("text", "")
                                    if chunk:
                                        full_text += chunk
                                        yield _sse("chunk", {"text": chunk})
                            except Exception:
                                pass
        except Exception as e:
            yield _sse("error", {"msg": str(e)})
            return

        if not full_text.strip():
            yield _sse("error", {"msg": "未能获取品牌信息，请稍后重试"})
            return

        yield _sse("status", {"msg": "正在写入知识库..."})

        # Parse the JSON profile and store it; also ingest readable text for RAG
        profile_json: Optional[dict] = None
        try:
            # Claude may occasionally wrap in ```json ... ```
            clean = re.sub(r"^```[a-z]*\n?|\n?```$", "", full_text.strip())
            profile_json = json.loads(clean)
        except Exception:
            pass

        # Build human-readable RAG text from profile JSON (or fall back to raw)
        if profile_json:
            ss = profile_json.get("scan_summary", {})
            diag = profile_json.get("diagnosis", {})
            rag_text_parts = [
                f"# {req.brand_name} 品牌知识库\n",
                f"## 平台扫描摘要",
                ss.get("mirror", ""),
                ss.get("window", ""),
                ss.get("door", ""),
                ss.get("action", ""),
                f"\n## 诊断",
                diag.get("because", ""),
                diag.get("therefore", ""),
                diag.get("action", ""),
            ]
            # Competitor context for RAG
            comps = profile_json.get("competitors", {})
            all_comp_names = []
            for tier in ("head", "direct", "growth"):
                for c in comps.get(tier, []):
                    if not c.get("is_self"):
                        all_comp_names.append(f"{c['name']}（{c.get('store_count','')}家，{c.get('price_band','')}）")
            if all_comp_names:
                rag_text_parts.append(f"\n## 竞品\n" + "\n".join(f"- {n}" for n in all_comp_names))
            doc_text = "\n".join(p for p in rag_text_parts if p)
        else:
            doc_text = f"# {req.brand_name} 品牌知识库\n\n{full_text}"

        try:
            await ingest_text(
                text=doc_text,
                shop_id=shop_id,
                source=f"onboarding:{req.brand_name}",
            )
            # Store parsed profile JSON in merchant meta for the /profile endpoint
            if profile_json and merchant:
                try:
                    meta_updated = meta.copy()
                    meta_updated["profile_json"] = profile_json
                    merchant.meta_json = json.dumps(meta_updated, ensure_ascii=False)
                    db.add(merchant)
                    db.commit()
                except Exception as e:
                    print(f"[generate-kb] Failed to save profile_json: {e}")
            yield _sse("done", {"msg": "品牌画像生成完成", "chars": len(full_text), "has_profile": bool(profile_json)})
        except Exception as e:
            yield _sse("error", {"msg": f"写入知识库失败：{e}"})

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
