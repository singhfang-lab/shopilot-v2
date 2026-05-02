"""
批量扩充 chains.json 的脚本。
按赛道分批调用 Claude API，生成新品牌条目，去重后合并写入。
用法：
  python3 -m backend.expand_chains
  python3 -m backend.expand_chains --sector 咖啡   # 只跑某个赛道
  python3 -m backend.expand_chains --dry-run       # 只打印 prompt，不调 API
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

_CHAINS_PATH = Path(__file__).parent / "chains.json"
_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_MODEL = "claude-sonnet-4-6"

# ── 每次调用 Claude 要生成多少个新品牌 ───────────────────────────────────────
_BATCH_SIZE = 15

# ── 分赛道任务清单 ────────────────────────────────────────────────────────────
# 每条：(sector_label, existing_category_keywords, target_new_categories, hint)
SECTOR_TASKS = [
    (
        "精品咖啡",
        ["精品咖啡连锁", "国际精品咖啡连锁"],
        "精品咖啡连锁、独立精品咖啡店连锁",
        "聚焦雅加达/泗水核心商圈的精品咖啡品牌，门店数 5-200 家",
    ),
    (
        "大众咖啡",
        ["大众咖啡连锁", "新零售咖啡连锁", "科技驱动咖啡连锁", "便利店咖啡连锁",
         "便利店咖啡", "本土咖啡连锁", "韩式咖啡连锁", "跨界咖啡连锁",
         "移动咖啡车连锁", "咖啡+烘焙连锁", "咖啡豆零售+咖啡馆"],
        "大众咖啡连锁、平价咖啡连锁、移动咖啡连锁",
        "以 Rp 10-30k 价格带为主的大众/平价咖啡连锁，门店数 20-1000 家",
    ),
    (
        "新中式茶饮",
        ["新中式茶饮连锁"],
        "新中式茶饮连锁",
        "来自中国的新中式茶饮品牌，2022-2025年进入印尼市场",
    ),
    (
        "台式珍珠奶茶",
        ["台式茶饮连锁", "台式黑糖珍珠奶茶连锁", "台式手工珍珠奶茶连锁", "美式奶茶连锁"],
        "台式茶饮连锁、珍珠奶茶连锁",
        "台式/日式/美式奶茶品牌，门店数 10-200 家",
    ),
    (
        "平价茶饮",
        ["平价茶饮连锁", "大众茶饮连锁", "大众茶饮+小食连锁",
         "平价茶饮+冰淇淋连锁", "平价珍珠奶茶连锁", "果茶连锁", "创意茶饮连锁",
         "马来西亚茶饮连锁", "泰式茶饮连锁", "日式芝士茶饮连锁"],
        "平价茶饮连锁、大众茶饮连锁、果茶连锁",
        "Rp 5-20k 价格带的平价/大众茶饮品牌，门店数 20-500 家",
    ),
    (
        "印尼炸鸡/烤鸡",
        ["印尼炸鸡连锁", "印尼烤鸡连锁"],
        "印尼炸鸡连锁、印尼烤鸡连锁、印尼香料炸鸡连锁",
        "印尼本土炸鸡/烤鸡连锁（ayam goreng/ayam bakar），门店数 10-500 家",
    ),
    (
        "美式/韩式炸鸡",
        ["美式炸鸡连锁", "韩式炸鸡连锁", "韩式炸鸡+热狗连锁", "美式鸡翅连锁"],
        "美式炸鸡连锁、韩式炸鸡连锁",
        "美式/韩式炸鸡品牌（含 crispy chicken、hot dog），门店数 10-150 家",
    ),
    (
        "国际快餐",
        ["国际快餐连锁", "美式快餐连锁", "墨西哥快餐连锁"],
        "国际快餐连锁、美式快餐连锁",
        "KFC/McDonald's 类国际快餐品牌，以及印尼本土仿制连锁",
    ),
    (
        "本土印尼快餐",
        ["本土快餐连锁", "印尼快餐连锁", "印尼大众快餐连锁", "印尼粥+中式快餐连锁",
         "印尼家庭餐厅连锁", "印尼甜品+快餐连锁"],
        "印尼本土快餐连锁、印尼家庭餐厅连锁",
        "印尼本土平价快餐和家庭餐厅连锁，门店数 10-500 家",
    ),
    (
        "汉堡",
        ["国际快餐连锁", "美式快餐连锁", "美式汉堡连锁", "汉堡连锁", "平价汉堡连锁",
         "日式汉堡连锁"],
        "汉堡连锁、美式汉堡连锁",
        "汉堡品牌（含印尼本土汉堡连锁），门店数 5-200 家",
    ),
    (
        "披萨",
        ["国际披萨连锁", "披萨连锁", "意式披萨连锁"],
        "披萨连锁、意式披萨连锁",
        "披萨品牌，含本土和国际，门店数 5-300 家",
    ),
    (
        "日式餐厅",
        ["日式寿司连锁", "日式拉面连锁", "日式乌冬面连锁", "日式铁板连锁",
         "日式回转寿司连锁", "日式油拌面连锁", "日式快餐连锁", "日式牛丼连锁",
         "日式烤肉自助连锁", "日式烤肉+火锅自助连锁", "日式火锅自助连锁", "日式餐厅连锁"],
        "日式寿司连锁、日式拉面连锁、日式烤肉连锁、日式餐厅连锁",
        "日式餐厅全品类，门店数 5-150 家，在印尼雅加达有门店",
    ),
    (
        "韩式餐厅",
        ["韩式餐厅连锁", "韩式快餐连锁", "韩式拌饭杯连锁", "韩式烤肉连锁",
         "韩式烤肉自助连锁", "韩式街头小吃连锁", "韩式面包烘焙连锁"],
        "韩式餐厅连锁、韩式烤肉连锁、韩式街头小吃连锁",
        "韩式餐厅全品类，门店数 5-100 家，在印尼有门店",
    ),
    (
        "西式牛排/海鲜",
        ["美式牛排连锁", "美式烧烤牛排连锁", "西式牛排连锁", "印尼牛排连锁",
         "美式海鲜连锁", "美式家庭餐厅连锁"],
        "西式牛排连锁、印尼牛排连锁、美式海鲜连锁",
        "牛排/海鲜/西式餐厅连锁，门店数 5-100 家",
    ),
    (
        "中式餐厅",
        ["中式餐厅连锁", "台式点心连锁", "中式点心连锁", "中式烤鸭连锁"],
        "中式餐厅连锁、港式茶楼连锁、台式餐厅连锁",
        "中式/港式/台式餐厅连锁，门店数 5-100 家，在印尼雅加达有门店",
    ),
    (
        "印尼本土特色菜",
        ["印尼巴东菜连锁", "印尼鸭肉连锁", "印尼烤鸡连锁", "印尼面食连锁",
         "印尼肉丸汤连锁", "印尼汤面连锁", "印尼沙爹连锁", "印尼爪哇菜连锁",
         "印尼牛肉汤连锁", "印尼炒饭连锁", "印尼盖饭连锁", "印尼辣面连锁",
         "印尼鲶鱼快餐连锁", "印尼美食广场品牌"],
        "印尼巴东菜连锁、印尼面食连锁、印尼肉丸连锁、印尼本土特色菜连锁",
        "印尼本土特色菜连锁（巴东菜/面食/肉丸/烤鸡等），门店数 5-300 家",
    ),
    (
        "面包烘焙甜品",
        ["面包烘焙连锁", "印尼面包烘焙连锁", "韩式面包烘焙连锁", "烘焙咖啡面包连锁",
         "高端烘焙甜品连锁", "甜甜圈+咖啡连锁", "甜甜圈连锁", "冻酸奶甜品连锁",
         "印尼甜品+快餐连锁", "香蕉小食+饮品连锁", "印尼糕点特产连锁"],
        "面包烘焙连锁、甜品连锁、甜甜圈连锁",
        "面包/烘焙/甜品/甜甜圈连锁，门店数 5-200 家",
    ),
    (
        "东南亚/其他国际",
        ["越南粉连锁", "泰式火锅连锁", "国际三明治连锁", "三明治+吐司连锁",
         "云厨房多品牌连锁", "土耳其烤肉连锁"],
        "越南菜连锁、泰式餐厅连锁、东南亚菜连锁、国际街头小吃连锁",
        "越南/泰式/其他东南亚及国际连锁，门店数 5-100 家，在印尼有门店",
    ),
]


def _load_existing() -> tuple[list[dict], set[str]]:
    data = json.loads(_CHAINS_PATH.read_text(encoding="utf-8"))
    brands = data["brands"]
    existing_names = {b["name"].lower() for b in brands}
    # also add aliases
    for b in brands:
        for alias in (b.get("aliases") or []):
            existing_names.add(alias.lower())
    return brands, existing_names


def _build_prompt(task: tuple, existing_in_sector: list[dict]) -> str:
    sector_label, _, target_categories, hint = task
    existing_json = json.dumps(existing_in_sector, ensure_ascii=False, indent=2)

    format_example = json.dumps({
        "name": "品牌英文名",
        "aliases": ["中文名或别名"],
        "category": "品类（从 target_categories 中选）",
        "storeCount": 50,
        "storeCountAsOf": "2024 或 2025-H1",
        "founded": "2015 或 2020 (Indonesia)",
        "founder": "创始人（可选，不确定填 null）",
        "hq": "Jakarta 或 总部城市",
        "priceBand": "Rp 20–45k",
        "markets": ["Indonesia"],
        "expansion": "一句扩张状态描述",
        "instagram": "账号名（无@，不确定填 null）",
        "tiktok": "账号名（无@，不确定填 null）"
    }, ensure_ascii=False, indent=2)

    return f"""你是印尼餐饮行业数据库的数据工程师。请补充「{sector_label}」赛道在印尼的连锁品牌数据。

## 目标
生成 {_BATCH_SIZE} 个该赛道在印尼运营的连锁品牌（门店数 5 家以上），聚焦：{hint}

## 已有品牌（不要重复）
{existing_json}

## 目标品类范围
{target_categories}

## 数据要求
- 只收录**真实存在**且在**印尼有实体门店**的品牌
- storeCount 填印尼门店数（全国，非仅雅加达），基于 2024-2025 年公开信息
- priceBand 填印尼市场实际价格（印尼盾，如 Rp 25–45k）
- expansion 用中文一句话描述（如：稳定扩张、快速增长、停滞、缩减）
- 不确定的字段填 null，不要编造
- instagram / tiktok 只填账号名（无@），不确定填 null

## 输出格式
只输出一个 JSON 数组，不加任何解释或 markdown 标记：

[
{format_example},
  ...（共 {_BATCH_SIZE} 条）
]"""


def _call_claude(prompt: str) -> list[dict]:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": _MODEL,
            "max_tokens": 4096,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        },
        headers={
            "x-api-key": _API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    # strip markdown code fences if present
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    # extract first JSON array in case of trailing content
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def _merge(existing: list[dict], new_brands: list[dict], existing_names: set[str]) -> tuple[list[dict], int]:
    added = 0
    for b in new_brands:
        name = b.get("name", "").strip()
        if not name:
            continue
        # dedup by name + aliases
        check = {name.lower()} | {a.lower() for a in (b.get("aliases") or [])}
        if check & existing_names:
            continue
        existing.append(b)
        existing_names.update(check)
        added += 1
    return existing, added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", help="只运行指定赛道（sector_label）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 prompt 不调 API")
    args = parser.parse_args()

    if not _API_KEY and not args.dry_run:
        print("错误：未设置 ANTHROPIC_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    brands, existing_names = _load_existing()
    total_added = 0

    tasks = SECTOR_TASKS
    if args.sector:
        tasks = [t for t in SECTOR_TASKS if args.sector in t[0]]
        if not tasks:
            print(f"未找到赛道：{args.sector}，可用赛道：{[t[0] for t in SECTOR_TASKS]}")
            sys.exit(1)

    for task in tasks:
        sector_label, existing_keywords, _, _ = task
        existing_in_sector = [
            b for b in brands
            if any(kw in b.get("category", "") for kw in existing_keywords)
        ]

        print(f"\n{'='*60}")
        print(f"赛道：{sector_label}  （现有 {len(existing_in_sector)} 个品牌）")

        prompt = _build_prompt(task, existing_in_sector)

        if args.dry_run:
            print(f"--- PROMPT ({len(prompt)} chars) ---")
            print(prompt[:800], "...")
            continue

        print("调用 Claude API...", end=" ", flush=True)
        try:
            new_brands = _call_claude(prompt)
            print(f"返回 {len(new_brands)} 条")
        except Exception as e:
            print(f"失败：{e}")
            continue

        brands, added = _merge(brands, new_brands, existing_names)
        total_added += added
        print(f"新增 {added} 个品牌（跳过 {len(new_brands)-added} 个重复）")

        # 每批次写入一次，防止中途崩溃丢数据
        _save(brands)
        print(f"已写入 chains.json（当前总计 {len(brands)} 个品牌）")

        # 避免触发速率限制
        time.sleep(2)

    if not args.dry_run:
        print(f"\n完成！共新增 {total_added} 个品牌，总计 {len(brands)} 个品牌")


def _save(brands: list[dict]):
    data = json.loads(_CHAINS_PATH.read_text(encoding="utf-8"))
    data["brands"] = brands
    data["_meta"]["lastUpdated"] = "2026-05-02"
    _CHAINS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


if __name__ == "__main__":
    main()
