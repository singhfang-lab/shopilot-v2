"""
USB Assistant — 20场景真实商家测试
=====================================
基于「问题库&模拟商家数据」目录，对20个真实商家场景进行多轮对话测试，
每个场景3轮对话，所有问答原文保存，生成完整 HTML + JSON 测试报告。

运行方式（从项目根目录）：
  python -m backend.test_scenarios [--report FILE] [--cases C01,C02,...] [--no-judge]

输出：
  - test_report_scenarios_YYYYMMDD_HHMMSS.json  完整数据（含所有问答原文）
  - test_report_scenarios_YYYYMMDD_HHMMSS.html  可视化报告
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_port = os.environ.get("PORT", "8081")
BASE_URL = os.environ.get("TEST_BASE_URL", f"http://localhost:{_port}")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
JUDGE_MODEL = "claude-sonnet-4-6"

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"; RED    = "\033[91m"; YELLOW = "\033[93m"
CYAN   = "\033[96m"; BOLD   = "\033[1m";  DIM    = "\033[2m"; RESET  = "\033[0m"

def _ok(m):      print(f"  {GREEN}✓{RESET} {m}")
def _fail(m):    print(f"  {RED}✗{RESET} {m}")
def _warn(m):    print(f"  {YELLOW}⚠{RESET} {m}")
def _info(m):    print(f"  {DIM}·{RESET} {m}")
def _head(t):    print(f"\n{BOLD}{CYAN}{'═'*70}\n  {t}\n{'═'*70}{RESET}")
def _sub(t):     print(f"\n{BOLD}  ── {t} ──{RESET}")
def _bar(s, w=20):
    n = min(w, round(s / 5.0 * w))
    color = GREEN if s >= 4.0 else (YELLOW if s >= 3.0 else RED)
    return f"{color}{'█'*n}{'░'*(w-n)}{RESET} {s:.1f}"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class JudgeScore:
    overall:       float = 0.0
    accuracy:      float = 0.0
    actionability: float = 0.0
    completeness:  float = 0.0
    clarity:       float = 0.0
    relevance:     float = 0.0
    data_usage:    float = 0.0
    strengths:     list[str] = field(default_factory=list)
    weaknesses:    list[str] = field(default_factory=list)
    red_flags:     list[str] = field(default_factory=list)
    summary:       str = ""
    error:         str = ""

@dataclass
class Turn:
    turn_no:       int
    user_msg:      str
    ai_response:   str   = ""
    has_card:      bool  = False
    has_chart:     bool  = False
    has_tool_call: bool  = False
    chars:         int   = 0
    latency_ms:    int   = 0
    score:         JudgeScore = field(default_factory=JudgeScore)

@dataclass
class ScenarioResult:
    sid:           str
    name:          str
    business_type: str
    turns:         list[Turn] = field(default_factory=list)
    error:         str = ""
    must_hit:      list[str] = field(default_factory=list)   # 合格必须包含
    red_flag_def:  list[str] = field(default_factory=list)   # 红旗定义
    red_flag_hits: list[str] = field(default_factory=list)   # 实际触发的红旗
    must_hit_pass: list[bool] = field(default_factory=list)  # 合格标准是否命中


# ── 20 场景定义 ───────────────────────────────────────────────────────────────

def _load_scene_meta_from_db() -> dict[str, dict]:
    """Load active test scenarios from DB. Returns empty dict on failure."""
    try:
        from sqlmodel import Session, select
        from backend.db import engine, TestScenario
        with Session(engine) as session:
            rows = session.exec(
                select(TestScenario).where(TestScenario.is_active == True).order_by(TestScenario.sid)
            ).all()
        result = {}
        for row in rows:
            result[row.sid] = {
                "name": row.name,
                "business_type": row.business_type,
                "shop": json.loads(row.shop_json) if row.shop_json else {},
                "csv_name": row.csv_name,
                "q1": row.q1,
                "q2": row.q2,
                "q3": row.q3,
                "must": json.loads(row.must_json) if row.must_json else [],
                "red_flags": json.loads(row.red_flags_json) if row.red_flags_json else [],
            }
        return result
    except Exception:
        return {}


_SCENE_META_FALLBACK: dict[str, dict] = {
"C01": {
    "name": "多门店 PIK 单店异常诊断",
    "business_type": "餐饮/多门店",
    "shop": {"name": "三店连锁餐厅", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C01_multi_store_raw_upload.xlsx",
    "q1": "我有三家店，两家在南雅加达，一家在 PIK。PIK 最近感觉越来越冷清，其他两家还可以。我有点怀疑是不是 PIK 这个位置不行，还是员工问题？你帮我看看大概从哪查。",
    "q2": "你帮我从数据里看看，PIK 和另外两家的订单数、客单价、各时段差距有多大？",
    "q3": "如果问题主要是晚高峰订单掉了，我这周内具体能做什么？给我一个行动清单。",
    "must": ["识别单店异常", "展开竞品/执行/商品结构", "PIK晚高峰或套餐", "具体To-do"],
    "red_flags": ["位置不行", "先降价", "加强营销", "品牌整体"],
},
"C02": {
    "name": "韩料店竞品分流应对",
    "business_type": "餐饮/竞品",
    "shop": {"name": "韩式烤肉 Senopati", "category": "korean_restaurant", "address": "Senopati Jakarta"},
    "csv_name": "C02_senopati_korean_raw_upload.xlsx",
    "q1": "我是一家韩料店，在 Senopati，人均大概 200k。附近最近开了好几家价位差不多的韩料店，我营业额开始慢慢掉了。我是不是要做活动或者重新定位？",
    "q2": "从数据里能看出是哪个时段、哪类客人流失最多吗？我想搞清楚到底哪里被抢了。",
    "q3": "如果主要是晚餐/周末聚餐场景被抢走了，我怎么把这部分客人拉回来？给我最快能落地的3个动作。",
    "must": ["分时段分析", "对比竞品套餐", "不直接做全场折扣", "验证指标"],
    "red_flags": ["护城河", "品牌格调", "商务宴请", "发微信"],
},
"C03": {
    "name": "蛋挞店稳定增长突破口",
    "business_type": "烘焙/增长策略",
    "shop": {"name": "西雅加达蛋挞店", "category": "bakery", "address": "West Jakarta"},
    "csv_name": "C03_egg_tart_growth_raw_upload.xlsx",
    "q1": "我是一家蛋糕店，主要做蛋挞，单价 30k-50k，在西雅加达。每个月营收大概 150jt，很稳定，但我想再往上冲一点，不知道该做新品、礼盒还是企业订单？",
    "q2": "从我上传的数据来看，现在的订单结构、客单价、售罄情况是什么样的？增长最快的路径是哪个？",
    "q3": "好，我想先从礼盒和企业订单测试，你帮我设计一个14天的小规模测试方案，要具体到定价和执行步骤。",
    "must": ["先判断增长杠杆", "AOV/渠道/产能多维度", "优先级排序", "14天测试"],
    "red_flags": ["继续卖更多蛋挞", "随意给目标", "假设送礼敏感"],
},
"C04": {
    "name": "忙但不赚钱的利润诊断",
    "business_type": "餐饮/利润结构",
    "shop": {"name": "Warung Pak Budi", "category": "restaurant", "address": "Bandung"},
    "csv_name": "C04_campaign_raw_upload.xlsx",
    "q1": "这个月感觉比以前忙，单也不少，但月底算下来利润没怎么上去，我有点懵。是不是人太多了，还是活动做错了？",
    "q2": "你能从数据里算出来订单数、客单价、毛利率的变化吗？帮我搞清楚利润到底漏在哪里。",
    "q3": "如果问题是低价套餐和折扣订单挤压了毛利，我应该怎么调整？给我本周可以执行的具体方案。",
    "must": ["区分忙和赚钱", "订单/销售/客单/毛利对比", "低价套餐/折扣结构", "停止扩大低价活动"],
    "red_flags": ["继续促销", "笼统控制成本", "加人裁人", "不提毛利"],
},
"C05": {
    "name": "菜单SKU过多整理优化",
    "business_type": "餐饮/菜单管理",
    "shop": {"name": "多品类餐厅", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C05_menu_sku_raw_upload.xlsx",
    "q1": "我们菜单越做越多，客人经常问有什么推荐。老板想继续上新品，但店员说已经推不过来了。到底该加新品还是先整理菜单？",
    "q2": "从数据来看，哪些SKU是销量高毛利高的，哪些是低销量低毛利的？帮我列出来。",
    "q3": "根据分析，给我一个菜单整理方案：哪些前排主推、哪些下架、哪些整改，以及4周验证标准。",
    "must": ["销量×毛利四象限", "不立即大规模下架", "菜单前排主推", "验证标准"],
    "red_flags": ["继续上新", "优化菜单但无分类规则"],
},
"C06": {
    "name": "美容店新客复购率低",
    "business_type": "美容/客户留存",
    "shop": {"name": "美容沙龙", "category": "beauty_salon", "address": "Jakarta"},
    "csv_name": "C06_beauty_repeat_raw_upload.xlsx",
    "q1": "我最近新客其实不少，但来一次以后很多人就没动静了。我在想是不是优惠不够，还是要办会员卡？",
    "q2": "从数据里能看出二次到店率是多少吗？流失主要发生在哪个阶段？",
    "q3": "如果流失原因是体验后没有设计二次护理路径，我怎么搭建一套7-14天的回访机制？给我具体话术和触达方式。",
    "must": ["不先发大券", "区分体验问题和触达问题", "7-14天回访路径", "升级权益设计"],
    "red_flags": ["加大优惠", "强推会员卡", "提升体验（泛话）"],
},
"C07": {
    "name": "奶茶竞品半价活动应对",
    "business_type": "奶茶/竞品策略",
    "shop": {"name": "奶茶店", "category": "bubble_tea", "address": "Jakarta"},
    "csv_name": "C07_milk_tea_pricing_raw_upload.xlsx",
    "q1": "附近一家奶茶店最近一直第二杯半价，我客人好像少了一些。我是不是也要跟？我怕不跟就被抢，但跟了又没利润。",
    "q2": "从数据来看，我的毛利率是多少？如果跟进第二杯半价，对利润的实际影响有多大？",
    "q3": "你建议我怎么做？不是直接跟价、也不是完全不管——给我一个小范围测试方案，7天内能看到结果。",
    "must": ["价差分析", "毛利承受计算", "小范围测试", "验证订单/毛利"],
    "red_flags": ["直接跟半价", "直接说不管竞品", "忽略毛利"],
},
"C08": {
    "name": "美甲IG咨询转化率低",
    "business_type": "美甲/线上转化",
    "shop": {"name": "美甲工作室", "category": "nail_salon", "address": "Jakarta"},
    "csv_name": "C08_nail_leads_raw_upload.xlsx",
    "q1": "我IG上每天都有人问价格和款式，但最后真的预约的人不多。我不知道是我回复慢，还是价格太高，还是作品图不够好。",
    "q2": "从数据来看，咨询到预约的转化率是多少？回复时效和报价后的转化是什么关系？",
    "q3": "针对转化率低的主要原因，给我一套可以本周就部署的改进方案，包括具体话术模板和作品图优化方式。",
    "must": ["计算咨询到预约转化率", "指出回复时效", "价格透明化", "7天验证预约率"],
    "red_flags": ["只建议多发图", "做广告", "降价"],
},
"C09": {
    "name": "咖啡馆高峰期差评上升",
    "business_type": "咖啡/运营质量",
    "shop": {"name": "Kopi Kita Cafe", "category": "cafe", "address": "Jakarta"},
    "csv_name": "C09_cafe_peak_ops_raw_upload.xlsx",
    "q1": "最近评分掉了，员工说是因为我们太忙了，但我看订单也没有涨很多。差评有点乱，我不知道先改什么。",
    "q2": "从数据里看，差评集中在哪些时段、哪些类型？出餐时间有没有变化？",
    "q3": "针对午高峰出餐慢的问题，给我一个本周内可以落地的改进方案，要具体到SKU和排班调整。",
    "must": ["差评分类", "时段关联", "出餐时间变化", "具体改午高峰SKU/排班"],
    "red_flags": ["笼统提升服务", "要求刷好评", "责怪员工忙"],
},
"C10": {
    "name": "面包店库存缺货与积压并存",
    "business_type": "烘焙/库存管理",
    "shop": {"name": "面包店", "category": "bakery", "address": "Jakarta"},
    "csv_name": "C10_bakery_inventory_raw_upload.xlsx",
    "q1": "我们有些热门品老是卖完，有些又放到快过期。老板觉得要多备货，但店长说会压库存。怎么判断？",
    "q2": "从库存数据来看，哪些品是缺货型、哪些是积压型？周末和平日的差异有多大？",
    "q3": "给我一套分品类的备货策略：热门品怎么备、低销量高损耗品怎么处理，以及2周验证指标。",
    "must": ["区分缺货和积压商品", "热门品周末备货", "低销量高损耗降推荐", "验证售罄和损耗"],
    "red_flags": ["只说多备货", "只说清库存", "忽略周末平日差异"],
},
"C11": {
    "name": "现金流紧张诊断",
    "business_type": "餐饮/现金流",
    "shop": {"name": "餐厅", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C11_cashflow_raw_upload.xlsx",
    "q1": "我每个月流水不算低，但到月底总觉得钱很紧。账上留不住钱，是不是我该做更多活动增加收入？",
    "q2": "从我的收支数据来看，钱主要流向哪里？有没有明显的现金流漏洞？",
    "q3": "给我3个本月内能改善现金流的具体动作，不是增加收入，是控制和优化现金节奏。",
    "must": ["不要直接做更多活动", "区分销售利润现金流", "采购/库存/活动/回款", "具体现金流动作"],
    "red_flags": ["加大活动冲销售", "只说控制成本"],
},
"C12": {
    "name": "员工执行力与门店标准化",
    "business_type": "餐饮/运营执行",
    "shop": {"name": "连锁餐厅", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C12_staff_execution_raw_upload.xlsx",
    "q1": "我店营业额看起来还不错，客人也不少，但总感觉利润很薄。是不是菜价该涨？我又怕顾客跑。",
    "q2": "从门店检查数据来看，执行层面有哪些明显问题？加购推荐率、陈列、接待是什么情况？",
    "q3": "如果加购推荐率很低是核心问题之一，给我一套提升加购的具体方案：话术、陈列、考核。",
    "must": ["区分收入和利润", "SKU毛利分层", "不直接涨价", "加购/组合建议"],
    "red_flags": ["直接涨价", "降低成本泛话", "忽略SKU结构"],
},
"C13": {
    "name": "高端护理项目转化率低",
    "business_type": "美容/服务升级",
    "shop": {"name": "美容沙龙高端项目", "category": "beauty_salon", "address": "Jakarta"},
    "csv_name": "C13_membership_raw_upload.xlsx",
    "q1": "我们有个高端护理项目，利润不错，但顾客基本只选基础款。是不是这个项目价格太高了？",
    "q2": "从会员数据来看，选基础款 vs 高端款的客户行为有什么区别？咨询后转化率是多少？",
    "q3": "如果问题不是价格而是价值表达，给我一套具体的升级话术、中间档设计和案例展示方案。",
    "must": ["不要直接降价", "补差异说明/案例", "中间档位", "员工推荐话术"],
    "red_flags": ["直接降价", "说高端人群不足", "提升服务（泛话）"],
},
"C14": {
    "name": "新品第一周销量低要不要继续",
    "business_type": "餐饮/新品管理",
    "shop": {"name": "餐厅新品测试", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C14_new_product_raw_upload.xlsx",
    "q1": "我们上了一个新品，第一周卖得一般。店员说不用推了，但我觉得可能还没宣传开。新品要不要继续？",
    "q2": "从数据来看，这个新品的毛利、复购评价、在菜单上的位置是什么情况？",
    "q3": "如果新品值得继续，给我一个14天的推广测试方案，包括前排展示、员工推荐话术和验证指标。",
    "must": ["不只看首周销量", "看评价/毛利/展示", "14天测试方案", "验证销量占比"],
    "red_flags": ["直接下架", "大推没有依据"],
},
"C15": {
    "name": "线上渠道优化与引流",
    "business_type": "餐饮/线上运营",
    "shop": {"name": "餐厅", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C15_online_channel_raw_upload.xlsx",
    "q1": "下个月有节日，我想做活动，但怕折扣做大了亏，也怕不做被别人抢。要不要做全场折扣？",
    "q2": "从线上数据来看，我现在的流量转化漏斗是什么情况？哪个环节流失最多？",
    "q3": "给我一个节日活动方案，不是全场折扣，而是针对高毛利品类和目标客群的精准活动。",
    "must": ["目标先行", "毛利和接待能力", "不要全场折扣", "预热和复盘指标"],
    "red_flags": ["全场折扣", "只讲节日氛围", "忽略接待差评"],
},
"C16": {
    "name": "转介绍活动效果评估",
    "business_type": "餐饮/活动复盘",
    "shop": {"name": "餐厅", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C16_referral_raw_upload.xlsx",
    "q1": "上周活动单量确实涨了，但店里很乱，差评也多了一些。老板觉得活动挺成功，我有点担心。怎么判断？",
    "q2": "从数据来看，活动期间的毛利率、客单价、差评数量、新客复购有什么变化？",
    "q3": "如果这次活动属于'虚假繁荣'，下次活动应该怎么设计才能同时保证单量和利润？给我具体方案。",
    "must": ["不只看订单", "活动健康度", "改套餐/限量/二次券", "验证毛利和差评"],
    "red_flags": ["说活动成功继续做", "只建议多招人"],
},
"C17": {
    "name": "咖啡店客户流失分析",
    "business_type": "咖啡/客户留存",
    "shop": {"name": "咖啡店", "category": "cafe", "address": "Jakarta"},
    "csv_name": "C17_customer_churn_raw_upload.xlsx",
    "q1": "我这家咖啡店现在还可以，朋友劝我开第二家。我也想试，但又怕管理不过来。到底什么时候适合扩店？",
    "q2": "从客户数据来看，现在的复购率、客户生命周期是什么情况？现店运营稳定吗？",
    "q3": "如果现店运营还没做稳，开店前需要具体达到哪些指标？给我一个扩店前的检查清单。",
    "must": ["扩店判断条件", "不只看销售", "SOP/店长/现金储备", "老板离店运营能力"],
    "red_flags": ["直接建议开新店选址", "只做营销计划"],
},
"C18": {
    "name": "食材成本上升利润压缩",
    "business_type": "餐饮/成本管理",
    "shop": {"name": "餐厅", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C18_supplier_cost_raw_upload.xlsx",
    "q1": "最近外卖和线上订单多了，但堂食少了。收入看起来没差很多，可是我总觉得不是好事。要不要继续做外卖？",
    "q2": "从数据来看，外卖和堂食的毛利率、客单价、平台费用分别是多少？渠道迁移对整体利润的影响是什么？",
    "q3": "如果外卖毛利明显低于堂食，给我一个同时优化外卖和恢复堂食的具体方案。",
    "must": ["区分收入和渠道利润", "不简单停止外卖", "优化外卖菜单/定价", "恢复堂食套餐"],
    "red_flags": ["继续扩大外卖", "完全停止外卖", "无数据分析"],
},
"C19": {
    "name": "多门店菜单差异化策略",
    "business_type": "餐饮/多门店管理",
    "shop": {"name": "多门店连锁", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C19_holiday_stock_raw_upload.xlsx",
    "q1": "我们几家店卖得最好的东西不一样。A店套餐卖得好，B店低价单品卖得多。我要不要把菜单统一？",
    "q2": "从数据来看，A店和B店在客单价、商品结构、毛利上有什么差异？这些差异是客群导致的还是运营导致的？",
    "q3": "给我一个菜单管理方案：哪些保持统一，哪些允许本地化，以及怎么衡量效果。",
    "must": ["不要全统一", "判断商圈客群差异", "复用可复用结构", "验证客单和毛利"],
    "red_flags": ["所有门店菜单完全一致", "忽略客群差异"],
},
"C20": {
    "name": "多门店财务费用结构分析",
    "business_type": "餐饮/财务管理",
    "shop": {"name": "多门店连锁", "category": "restaurant", "address": "Jakarta"},
    "csv_name": "C20_multi_store_finance_raw_upload.xlsx",
    "q1": "我每个月流水不算低，但到月底总觉得钱很紧。账上留不住钱，是不是我该做更多活动增加收入？",
    "q2": "从财务数据来看，各门店的收入、成本、利润率分别是多少？哪个门店最赚钱、哪个在亏？",
    "q3": "给我一个基于数据的现金流改善方案：采购节奏、费用控制、活动策略，本月内可以执行的。",
    "must": ["不要直接做更多活动", "区分销售利润现金流", "采购/库存/活动/回款", "具体现金流动作"],
    "red_flags": ["加大活动冲销售", "只说控制成本"],
},
}

# Load from DB at import time; fall back to hardcoded dict if DB unavailable
SCENE_META: dict[str, dict] = _load_scene_meta_from_db() or _SCENE_META_FALLBACK


# ── Judge ─────────────────────────────────────────────────────────────────────

JUDGE_SYSTEM = """你是一位资深的商业顾问质量评审专家，专门评估 AI 助手针对实体店商户的经营建议质量。"""

def _make_judge_prompt(scene: dict, turn_no: int, q: str, a: str,
                       all_qa: list[dict], has_tool: bool, must: list[str], red_flags: list[str]) -> str:
    history_text = ""
    for t in all_qa[:-1]:
        history_text += f"\n[轮{t['turn']}] 商户: {t['q']}\n[轮{t['turn']}] AI: {t['a'][:400]}{'…' if len(t['a'])>400 else ''}\n"

    must_str = "\n".join(f"- {m}" for m in must)
    rf_str = "\n".join(f"- {r}" for r in red_flags)

    data_ctx = "是（AI本轮已执行SQL查询）" if has_tool else "有上传数据，但本轮未执行SQL查询"

    return f"""请评估以下商户经营顾问 AI 的回答质量。

## 场景信息
- 场景：{scene['name']}（{scene['business_type']}）
- 当前轮次：第 {turn_no} 轮 / 共3轮

## 对话历史（前几轮摘要）
{history_text if history_text else "（无，这是第1轮）"}

## 本轮对话
【商户问题】
{q}

【AI 回答】
{a[:3000]}{"…(截断)" if len(a) > 3000 else ""}

## 数据情况
{data_ctx}

## 评估维度（各打1-5分）
1. accuracy      (准确性)   : 信息准确，逻辑严密，无错误推断
2. actionability (可操作性) : 给出商户可直接执行的具体步骤和时间节点
3. completeness  (完整性)   : 覆盖问题的所有关键要点，无明显遗漏
4. clarity       (清晰度)   : 结构清晰，语言简练，短句易读
5. relevance     (相关性)   : 紧扣问题，不跑题，不无谓延伸
6. data_usage    (数据使用) : 有数据时是否引用了具体数字支撑结论（无数据默认4）

data_usage 细则：
- 有数据且AI查询并引用具体数字：4-5分
- 有数据但AI未引用数字：1-2分
- 无数据：默认4分

## 本场景合格标准（必须包含）
{must_str}

## 本场景红旗（不合格表现，若出现则严重扣分）
{rf_str}

## 输出格式
请严格按以下JSON格式返回，不要加任何其他文字：
{{
  "accuracy": <1-5>,
  "actionability": <1-5>,
  "completeness": <1-5>,
  "clarity": <1-5>,
  "relevance": <1-5>,
  "data_usage": <1-5>,
  "overall_score": <1.0-5.0，六维度加权平均，数据使用权重加倍>,
  "must_hit_results": {{
    {chr(10).join(f'    "{m}": <true/false>,' for m in must)}
  }},
  "red_flag_triggered": [<触发的红旗描述，若无则空数组>],
  "strengths": ["优点1（≤25字）", "优点2"],
  "weaknesses": ["不足1（≤40字）"],
  "summary": "不超过80字的总体评价"
}}"""


async def judge_turn(scene: dict, turn_no: int, q: str, a: str,
                     all_qa: list[dict], has_tool: bool) -> JudgeScore:
    if not ANTHROPIC_API_KEY:
        return JudgeScore(error="ANTHROPIC_API_KEY 未设置")
    if not a.strip():
        return JudgeScore(error="AI 响应为空")

    prompt = _make_judge_prompt(
        scene, turn_no, q, a, all_qa, has_tool,
        scene["must"], scene["red_flags"]
    )
    try:
        async with httpx.AsyncClient(timeout=45.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": JUDGE_MODEL, "max_tokens": 1200,
                      "temperature": 0,
                      "system": JUDGE_SYSTEM,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        if r.status_code != 200:
            return JudgeScore(error=f"Judge HTTP {r.status_code}")
        raw = r.json().get("content", [{}])[0].get("text", "")
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return JudgeScore(error="Judge 返回非 JSON")
        # Remove control characters that break JSON parsing
        json_str = re.sub(r'[\x00-\x1f\x7f]', ' ', m.group())
        # Remove trailing commas before closing braces/brackets
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
        try:
            obj = json.loads(json_str)
        except json.JSONDecodeError:
            # Last resort: try to extract with a more lenient approach
            try:
                import ast
                obj = ast.literal_eval(json_str)
            except Exception:
                return JudgeScore(error=f"JSON 解析失败: {json_str[:100]}")
        return JudgeScore(
            overall=float(obj.get("overall_score", 0)),
            accuracy=float(obj.get("accuracy", 0)),
            actionability=float(obj.get("actionability", 0)),
            completeness=float(obj.get("completeness", 0)),
            clarity=float(obj.get("clarity", 0)),
            relevance=float(obj.get("relevance", 0)),
            data_usage=float(obj.get("data_usage", 0)),
            strengths=obj.get("strengths", []),
            weaknesses=obj.get("weaknesses", []),
            red_flags=obj.get("red_flag_triggered", []),
            summary=obj.get("summary", ""),
        ), obj.get("must_hit_results", {})
    except Exception as e:
        return JudgeScore(error=str(e)), {}


# ── Stream chat ───────────────────────────────────────────────────────────────

async def stream_chat(c: httpx.AsyncClient, payload: dict) -> dict:
    text = ""; has_card = has_chart = has_tool = False; error = ""
    t0 = time.time()
    try:
        async with c.stream("POST", BASE_URL + "/chat", json=payload, timeout=180.0) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                return {"text": "", "has_card": False, "has_chart": False,
                        "has_tool": False, "latency_ms": 0,
                        "error": f"HTTP {resp.status_code}: {body.decode()[:200]}"}
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw.strip() == "[DONE]":
                    break
                try:
                    ev = json.loads(raw)
                    # Format 1: {"content": "..."} — main text chunk
                    if "content" in ev:
                        text += ev["content"]
                    # Format 2: {"tool_call": ...} — SQL agent tool events
                    elif "tool_call" in ev or "tool_result" in ev:
                        has_tool = True
                    # Format 3: {"canvasUpdate": {...}} — card/chart events
                    elif "canvasUpdate" in ev:
                        cu = ev["canvasUpdate"]
                        ct = cu.get("type", "")
                        if ct == "analysis_card":
                            has_card = True
                        elif ct == "chart_config":
                            has_chart = True
                        elif ct in ("tool_call", "tool_result"):
                            has_tool = True
                    # Format 3 (legacy): {"type": "text", "text": "..."}
                    elif ev.get("type") == "text":
                        text += ev.get("text", "")
                    elif ev.get("type") == "canvasUpdate":
                        cd = ev.get("data", {})
                        if cd.get("type") == "analysis_card":
                            has_card = True
                        elif cd.get("type") == "chart_config":
                            has_chart = True
                        elif cd.get("type") in ("tool_call", "tool_result"):
                            has_tool = True
                except Exception:
                    pass
    except Exception as e:
        error = str(e)
    return {"text": text, "has_card": has_card, "has_chart": has_chart,
            "has_tool": has_tool, "latency_ms": int((time.time()-t0)*1000),
            "error": error}


# ── File upload ───────────────────────────────────────────────────────────────

DATA_DIR = Path("/Users/singhfang/Downloads/问题库&模拟商家数据")

async def upload_file(c: httpx.AsyncClient, csv_name: str) -> str:
    fpath = DATA_DIR / csv_name
    if not fpath.exists():
        _warn(f"文件不存在: {fpath}")
        return ""
    with open(fpath, "rb") as fh:
        r = await c.post(BASE_URL + "/upload",
                         files={"file": (csv_name, fh, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                         timeout=60.0)
    if r.status_code == 200:
        fname = r.json().get("filename", "")
        _ok(f"已上传: {fname}")
        await asyncio.sleep(3)
        return fname
    _warn(f"上传失败 HTTP {r.status_code}: {r.text[:100]}")
    return ""


# ── Register test user ────────────────────────────────────────────────────────

async def register_user(c: httpx.AsyncClient, email: str, password: str) -> bool:
    r = await c.post(BASE_URL + "/auth/register",
                     json={"email": email, "password": password}, timeout=15.0)
    if r.status_code in (200, 201):
        return True
    if r.status_code == 400 and "already" in r.text.lower():
        # already exists — login
        r2 = await c.post(BASE_URL + "/auth/login",
                          json={"email": email, "password": password}, timeout=15.0)
        return r2.status_code == 200
    return False


# ── Run one scenario ──────────────────────────────────────────────────────────

async def run_scenario(sid: str, no_judge: bool = False) -> ScenarioResult:
    meta = SCENE_META[sid]
    result = ScenarioResult(
        sid=sid, name=meta["name"], business_type=meta["business_type"],
        must_hit=meta["must"], red_flag_def=meta["red_flags"],
    )
    _head(f"场景 {sid}: {meta['name']}  [{meta['business_type']}]")

    email = f"test_{sid.lower()}_{int(time.time())}@test.com"
    password = "Test@12345"

    async with httpx.AsyncClient(timeout=60.0, transport=httpx.AsyncHTTPTransport(retries=2)) as c:
        ok = await register_user(c, email, password)
        if not ok:
            result.error = "注册/登录失败"
            _fail(result.error)
            return result

        uploaded = await upload_file(c, meta["csv_name"])

        history: list[dict] = []
        all_qa: list[dict] = []

        for turn_no, q_key in enumerate(["q1", "q2", "q3"], 1):
            q = meta[q_key]
            _sub(f"轮 {turn_no}/3")
            _info(f"问: {q[:80]}{'…' if len(q)>80 else ''}")

            payload = {
                "message": q,
                "history": history,
                "shop_config": meta["shop"],
                "model_preference": "auto",
                "files": [uploaded] if uploaded else [],
            }
            res = await stream_chat(c, payload)
            ai_text = res["text"]

            turn = Turn(
                turn_no=turn_no, user_msg=q, ai_response=ai_text,
                has_card=res["has_card"], has_chart=res["has_chart"],
                has_tool_call=res["has_tool"],
                chars=len(ai_text), latency_ms=res["latency_ms"],
            )

            if res["error"]:
                _fail(f"请求异常: {res['error']}")
                turn.score.error = res["error"]
            elif not ai_text:
                _fail("AI 返回空响应")
                turn.score.error = "空响应"
            else:
                tags = []
                if res["has_tool"]: tags.append("[SQL]")
                if res["has_card"]: tags.append("[卡片]")
                if res["has_chart"]: tags.append("[图表]")
                _ok(f"响应 {len(ai_text)}字 / {res['latency_ms']}ms  {'  '.join(tags)}")

                all_qa.append({"turn": turn_no, "q": q, "a": ai_text})

                if not no_judge and ANTHROPIC_API_KEY:
                    _info("Judge 评分中…")
                    judge_result = await judge_turn(
                        meta, turn_no, q, ai_text, all_qa, res["has_tool"]
                    )
                    if isinstance(judge_result, tuple):
                        score, must_hit_map = judge_result
                    else:
                        score, must_hit_map = judge_result, {}
                    turn.score = score

                    if score.error:
                        _warn(f"Judge 跳过: {score.error}")
                    else:
                        color = GREEN if score.overall >= 4.0 else (YELLOW if score.overall >= 3.0 else RED)
                        print(f"  {color}Judge {score.overall:.1f}/5.0{RESET}  "
                              f"准:{score.accuracy} 操:{score.actionability} "
                              f"整:{score.completeness} 清:{score.clarity} "
                              f"关:{score.relevance} 数:{score.data_usage}")
                        if score.summary:
                            _info(f"评语: {score.summary[:90]}")
                        if score.weaknesses:
                            _info(f"不足: {score.weaknesses[0][:70]}")
                        if score.red_flags:
                            for rf in score.red_flags:
                                _fail(f"触发红旗: {rf}")
                            result.red_flag_hits.extend(score.red_flags)

                        # 记录合格标准命中
                        for m_idx, m_text in enumerate(meta["must"]):
                            hit = must_hit_map.get(m_text, False)
                            if m_idx >= len(result.must_hit_pass):
                                result.must_hit_pass.append(hit)
                            else:
                                result.must_hit_pass[m_idx] = (
                                    result.must_hit_pass[m_idx] or hit
                                )

            history += [
                {"role": "user", "content": q},
                {"role": "assistant", "content": ai_text},
            ]
            result.turns.append(turn)

    return result


# ── HTML report ───────────────────────────────────────────────────────────────

def _score_color_css(s: float) -> str:
    if s >= 4.5: return "#22c55e"
    if s >= 4.0: return "#86efac"
    if s >= 3.5: return "#fbbf24"
    if s >= 3.0: return "#f97316"
    return "#ef4444"

def build_html_report(results: list[ScenarioResult], total_ms: int) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    all_scores = [t.score.overall for sc in results for t in sc.turns if t.score and not t.score.error and t.score.overall > 0]
    all_data   = [t.score.data_usage for sc in results for t in sc.turns if t.score and not t.score.error and t.score.data_usage > 0]
    all_rel    = [t.score.relevance for sc in results for t in sc.turns if t.score and not t.score.error and t.score.relevance > 0]
    all_act    = [t.score.actionability for sc in results for t in sc.turns if t.score and not t.score.error and t.score.actionability > 0]

    avg = lambda lst: round(sum(lst)/len(lst), 2) if lst else 0
    red_flag_total = sum(len(sc.red_flag_hits) for sc in results)
    pass_count = sum(1 for sc in results if all(
        t.score and not t.score.error and t.score.overall >= 3.0
        for t in sc.turns if t.ai_response
    ))

    # Build scenario cards HTML
    cards_html = ""
    for sc in results:
        sc_scores = [t.score.overall for t in sc.turns if t.score and not t.score.error and t.score.overall > 0]
        sc_avg = avg(sc_scores)
        sc_color = _score_color_css(sc_avg)
        rf_html = ""
        if sc.red_flag_hits:
            rf_html = f'<div class="red-flags"><strong>🚩 触发红旗：</strong> ' + \
                      "".join(f'<span class="rf-tag">{r}</span>' for r in sc.red_flag_hits) + "</div>"

        must_html = ""
        for i, m in enumerate(sc.must_hit):
            hit = sc.must_hit_pass[i] if i < len(sc.must_hit_pass) else None
            icon = "✓" if hit else ("✗" if hit is False else "·")
            cls = "must-pass" if hit else ("must-fail" if hit is False else "must-unknown")
            must_html += f'<span class="{cls}">{icon} {m}</span> '

        turns_html = ""
        for t in sc.turns:
            s = t.score
            score_html = ""
            if s and not s.error and s.overall > 0:
                score_html = f"""
                <div class="turn-scores">
                  <span class="score-pill" style="background:{_score_color_css(s.overall)}">综合 {s.overall}</span>
                  <span class="score-dim">准:{s.accuracy}</span>
                  <span class="score-dim">操:{s.actionability}</span>
                  <span class="score-dim">整:{s.completeness}</span>
                  <span class="score-dim">清:{s.clarity}</span>
                  <span class="score-dim">关:{s.relevance}</span>
                  <span class="score-dim">数:{s.data_usage}</span>
                </div>
                {"<p class='judge-summary'>" + s.summary + "</p>" if s.summary else ""}
                {"<p class='weakness'>" + " | ".join(s.weaknesses) + "</p>" if s.weaknesses else ""}
                {"<p class='strength'>" + " | ".join(s.strengths) + "</p>" if s.strengths else ""}
                """
            elif s and s.error:
                score_html = f'<span class="err">Judge 错误: {s.error}</span>'

            tag_html = ""
            if t.has_tool_call: tag_html += '<span class="tag tag-sql">SQL</span>'
            if t.has_card:      tag_html += '<span class="tag tag-card">卡片</span>'
            if t.has_chart:     tag_html += '<span class="tag tag-chart">图表</span>'

            # Escape AI response for HTML
            ai_escaped = (t.ai_response
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>") if t.ai_response else "<em>无响应</em>")
            q_escaped = t.user_msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            turns_html += f"""
            <div class="turn">
              <div class="turn-header">
                <span class="turn-no">轮 {t.turn_no}</span>
                {tag_html}
                <span class="turn-meta">{t.chars}字 / {t.latency_ms}ms</span>
              </div>
              <div class="qa-block">
                <div class="q-block"><strong>🧑‍💼 商户：</strong>{q_escaped}</div>
                <div class="a-block"><strong>🤖 AI：</strong><div class="ai-text">{ai_escaped}</div></div>
              </div>
              {score_html}
            </div>"""

        cards_html += f"""
        <div class="scene-card" id="{sc.sid}">
          <div class="scene-header">
            <div>
              <span class="scene-id">{sc.sid}</span>
              <span class="scene-name">{sc.name}</span>
              <span class="biz-type">{sc.business_type}</span>
            </div>
            <div class="scene-score" style="color:{sc_color}">{sc_avg:.1f}</div>
          </div>
          <div class="must-checks">{must_html}</div>
          {rf_html}
          <details>
            <summary>查看完整对话 ({len(sc.turns)} 轮)</summary>
            {turns_html}
          </details>
        </div>"""

    # Heatmap rows
    heatmap_rows = ""
    dims = ["accuracy", "actionability", "completeness", "clarity", "relevance", "data_usage"]
    dim_labels = ["准确", "操作", "完整", "清晰", "相关", "数据"]
    for sc in results:
        sc_scores = []
        row_cells = f'<td class="sid-cell"><a href="#{sc.sid}">{sc.sid}</a></td>'
        for dim in dims:
            vals = [getattr(t.score, dim) for t in sc.turns
                    if t.score and not t.score.error and getattr(t.score, dim, 0) > 0]
            v = avg(vals)
            sc_scores.append(v)
            bg = _score_color_css(v)
            row_cells += f'<td style="background:{bg};color:#111;text-align:center;font-weight:bold">{v:.1f}</td>'
        total = avg(sc_scores)
        bg = _score_color_css(total)
        row_cells += f'<td style="background:{bg};color:#111;text-align:center;font-weight:bold">{total:.1f}</td>'
        row_cells += f'<td style="text-align:center">{"🚩" * len(sc.red_flag_hits) if sc.red_flag_hits else "✓"}</td>'
        heatmap_rows += f"<tr>{row_cells}</tr>"

    header_cells = "".join(f"<th>{l}</th>" for l in dim_labels) + "<th>综合</th><th>红旗</th>"

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>USB Assistant 场景测试报告 — {ts}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; line-height: 1.6; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
  h1 {{ font-size: 1.8rem; font-weight: 700; color: #f8fafc; margin-bottom: 4px; }}
  .subtitle {{ color: #94a3b8; font-size: 0.9rem; margin-bottom: 32px; }}
  .dashboard {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
                gap: 16px; margin-bottom: 32px; }}
  .kpi {{ background: #1e293b; border-radius: 12px; padding: 20px; text-align: center; }}
  .kpi-val {{ font-size: 2rem; font-weight: 700; }}
  .kpi-lbl {{ font-size: 0.8rem; color: #94a3b8; margin-top: 4px; }}
  .section-title {{ font-size: 1.1rem; font-weight: 600; color: #f8fafc;
                    margin: 32px 0 12px; border-left: 3px solid #6366f1; padding-left: 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: #1e293b;
           border-radius: 12px; overflow: hidden; }}
  th {{ background: #334155; color: #94a3b8; font-size: 0.78rem;
        padding: 10px 8px; text-align: center; }}
  td {{ padding: 9px 8px; font-size: 0.85rem; border-bottom: 1px solid #334155; }}
  .sid-cell a {{ color: #818cf8; text-decoration: none; font-weight: 600; }}
  .scene-card {{ background: #1e293b; border-radius: 12px; margin-bottom: 16px;
                 overflow: hidden; border: 1px solid #334155; }}
  .scene-header {{ display: flex; justify-content: space-between; align-items: center;
                   padding: 16px 20px; background: #263044; }}
  .scene-id {{ background: #6366f1; color: #fff; border-radius: 6px;
               padding: 2px 8px; font-size: 0.85rem; font-weight: 700; margin-right: 8px; }}
  .scene-name {{ font-weight: 600; font-size: 1rem; }}
  .biz-type {{ color: #94a3b8; font-size: 0.82rem; margin-left: 8px; }}
  .scene-score {{ font-size: 1.8rem; font-weight: 700; }}
  .must-checks {{ padding: 10px 20px; font-size: 0.82rem; }}
  .must-pass {{ color: #22c55e; margin-right: 12px; }}
  .must-fail {{ color: #ef4444; margin-right: 12px; }}
  .must-unknown {{ color: #94a3b8; margin-right: 12px; }}
  .red-flags {{ background: #450a0a; padding: 10px 20px; font-size: 0.82rem; color: #fca5a5; }}
  .rf-tag {{ background: #7f1d1d; border-radius: 4px; padding: 2px 6px; margin-right: 6px; }}
  details > summary {{ padding: 12px 20px; cursor: pointer; color: #818cf8;
                        font-size: 0.9rem; user-select: none; }}
  details > summary:hover {{ color: #a5b4fc; }}
  .turn {{ border-top: 1px solid #334155; padding: 16px 20px; }}
  .turn-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}
  .turn-no {{ background: #334155; border-radius: 6px; padding: 2px 8px;
              font-size: 0.8rem; font-weight: 700; }}
  .turn-meta {{ color: #64748b; font-size: 0.78rem; margin-left: auto; }}
  .tag {{ border-radius: 4px; padding: 2px 6px; font-size: 0.75rem; font-weight: 600; }}
  .tag-sql {{ background: #1e3a5f; color: #93c5fd; }}
  .tag-card {{ background: #1e3a2f; color: #86efac; }}
  .tag-chart {{ background: #3b1f60; color: #c4b5fd; }}
  .qa-block {{ margin-bottom: 12px; }}
  .q-block {{ background: #263044; border-radius: 8px; padding: 10px 14px;
              font-size: 0.88rem; margin-bottom: 8px; color: #bfdbfe; }}
  .a-block {{ background: #1a2332; border-radius: 8px; padding: 10px 14px; font-size: 0.85rem; }}
  .ai-text {{ max-height: 400px; overflow-y: auto; color: #cbd5e1; }}
  .turn-scores {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }}
  .score-pill {{ border-radius: 6px; padding: 3px 10px; font-size: 0.82rem; font-weight: 700; color: #0f172a; }}
  .score-dim {{ color: #94a3b8; font-size: 0.8rem; }}
  .judge-summary {{ color: #a3e635; font-size: 0.82rem; margin: 6px 0; font-style: italic; }}
  .weakness {{ color: #fca5a5; font-size: 0.8rem; margin: 4px 0; }}
  .strength {{ color: #86efac; font-size: 0.8rem; margin: 4px 0; }}
  .err {{ color: #f87171; font-size: 0.8rem; }}
  .toc {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 24px; }}
  .toc a {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px;
             padding: 6px 12px; color: #94a3b8; text-decoration: none; font-size: 0.82rem; }}
  .toc a:hover {{ border-color: #6366f1; color: #a5b4fc; }}
</style>
</head>
<body>
<div class="container">
  <h1>USB Assistant 场景测试报告</h1>
  <div class="subtitle">{ts} · {len(results)} 个场景 · 耗时 {total_ms//1000}s</div>

  <div class="dashboard">
    <div class="kpi"><div class="kpi-val" style="color:#22c55e">{avg(all_scores):.2f}</div><div class="kpi-lbl">综合 Judge 均分</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#86efac">{pass_count}/{len(results)}</div><div class="kpi-lbl">场景通过数</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#fbbf24">{avg(all_data):.2f}</div><div class="kpi-lbl">数据使用均分</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#a5b4fc">{avg(all_act):.2f}</div><div class="kpi-lbl">可操作性均分</div></div>
    <div class="kpi"><div class="kpi-val" style="color:{'#ef4444' if red_flag_total else '#22c55e'}">{red_flag_total}</div><div class="kpi-lbl">红旗触发总次数</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#94a3b8">{avg(all_rel):.2f}</div><div class="kpi-lbl">相关性均分</div></div>
  </div>

  <div class="section-title">快速导航</div>
  <div class="toc">
    {"".join(f'<a href="#{sc.sid}">{sc.sid} {sc.name}</a>' for sc in results)}
  </div>

  <div class="section-title">维度热力图（20场景 × 6维度）</div>
  <table>
    <thead><tr><th>场景</th>{header_cells}</tr></thead>
    <tbody>{heatmap_rows}</tbody>
  </table>

  <div class="section-title">场景详情（含完整对话原文）</div>
  {cards_html}
</div>
</body>
</html>"""


# ── JSON report ───────────────────────────────────────────────────────────────

def build_json_report(results: list[ScenarioResult], total_ms: int) -> dict:
    def _score_dict(s: JudgeScore) -> dict:
        return {
            "overall": s.overall, "accuracy": s.accuracy,
            "actionability": s.actionability, "completeness": s.completeness,
            "clarity": s.clarity, "relevance": s.relevance,
            "data_usage": s.data_usage, "strengths": s.strengths,
            "weaknesses": s.weaknesses, "red_flags": s.red_flags,
            "summary": s.summary, "error": s.error,
        }

    all_scores = [t.score.overall for sc in results for t in sc.turns
                  if t.score and not t.score.error and t.score.overall > 0]
    avg = lambda lst: round(sum(lst)/len(lst), 3) if lst else 0

    return {
        "generated_at": datetime.now().isoformat(),
        "duration_ms": total_ms,
        "summary": {
            "avg_overall":       avg(all_scores),
            "avg_accuracy":      avg([t.score.accuracy for sc in results for t in sc.turns if t.score and t.score.accuracy > 0]),
            "avg_actionability": avg([t.score.actionability for sc in results for t in sc.turns if t.score and t.score.actionability > 0]),
            "avg_completeness":  avg([t.score.completeness for sc in results for t in sc.turns if t.score and t.score.completeness > 0]),
            "avg_clarity":       avg([t.score.clarity for sc in results for t in sc.turns if t.score and t.score.clarity > 0]),
            "avg_relevance":     avg([t.score.relevance for sc in results for t in sc.turns if t.score and t.score.relevance > 0]),
            "avg_data_usage":    avg([t.score.data_usage for sc in results for t in sc.turns if t.score and t.score.data_usage > 0]),
            "total_red_flags":   sum(len(sc.red_flag_hits) for sc in results),
            "scenes_total":      len(results),
            "scenes_passed":     sum(1 for sc in results if all(
                t.score and not t.score.error and t.score.overall >= 3.0
                for t in sc.turns if t.ai_response
            )),
        },
        "scenarios": [
            {
                "sid": sc.sid, "name": sc.name, "business_type": sc.business_type,
                "error": sc.error,
                "must_hit": sc.must_hit,
                "must_hit_pass": sc.must_hit_pass,
                "red_flag_def": sc.red_flag_def,
                "red_flag_hits": sc.red_flag_hits,
                "turns": [
                    {
                        "turn_no": t.turn_no,
                        "user_msg": t.user_msg,
                        "ai_response": t.ai_response,   # 完整原文
                        "has_card": t.has_card,
                        "has_chart": t.has_chart,
                        "has_tool_call": t.has_tool_call,
                        "chars": t.chars,
                        "latency_ms": t.latency_ms,
                        "score": _score_dict(t.score),
                    }
                    for t in sc.turns
                ],
            }
            for sc in results
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="USB Assistant 20场景真实商家测试")
    parser.add_argument("--report", default="", help="JSON 报告输出路径（默认自动命名）")
    parser.add_argument("--cases", default="", help="只跑指定场景，逗号分隔，如 C01,C03")
    parser.add_argument("--no-judge", action="store_true", help="跳过 Claude Judge 评分")
    args = parser.parse_args()

    all_ids = [f"C{i:02d}" for i in range(1, 21)]
    if args.cases:
        run_ids = [c.strip().upper() for c in args.cases.split(",") if c.strip().upper() in all_ids]
    else:
        run_ids = all_ids

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = args.report or f"test_report_scenarios_{ts}.json"
    html_path = json_path.replace(".json", ".html")

    print(f"\n{BOLD}{CYAN}USB Assistant 20场景测试{RESET}")
    print(f"  运行场景: {', '.join(run_ids)}")
    print(f"  Judge: {'关闭' if args.no_judge else JUDGE_MODEL}")
    print(f"  输出: {json_path} / {html_path}\n")

    t0 = time.time()
    results: list[ScenarioResult] = []

    for sid in run_ids:
        try:
            r = await run_scenario(sid, no_judge=args.no_judge)
            results.append(r)
        except Exception as e:
            _fail(f"{sid} 异常: {e}")
            traceback.print_exc()
            results.append(ScenarioResult(sid=sid, name=SCENE_META[sid]["name"],
                                          business_type=SCENE_META[sid]["business_type"],
                                          error=str(e)))

    total_ms = int((time.time() - t0) * 1000)

    # Print summary
    _head("测试结果总览")
    all_s = [t.score.overall for sc in results for t in sc.turns
             if t.score and not t.score.error and t.score.overall > 0]
    avg_f = lambda lst: sum(lst)/len(lst) if lst else 0

    print(f"\n  综合均分   {_bar(avg_f(all_s))} / 5.0")
    print(f"  数据使用   {_bar(avg_f([t.score.data_usage for sc in results for t in sc.turns if t.score and t.score.data_usage > 0]))}")
    print(f"  可操作性   {_bar(avg_f([t.score.actionability for sc in results for t in sc.turns if t.score and t.score.actionability > 0]))}")
    print(f"  相关性     {_bar(avg_f([t.score.relevance for sc in results for t in sc.turns if t.score and t.score.relevance > 0]))}")
    total_rf = sum(len(sc.red_flag_hits) for sc in results)
    print(f"\n  红旗触发   {RED if total_rf else GREEN}{total_rf} 次{RESET}")
    print(f"  总耗时     {total_ms//1000}s\n")

    for sc in results:
        sc_s = [t.score.overall for t in sc.turns if t.score and not t.score.error and t.score.overall > 0]
        sc_avg = avg_f(sc_s)
        color = GREEN if sc_avg >= 4.0 else (YELLOW if sc_avg >= 3.0 else RED)
        rf = f"  {RED}🚩×{len(sc.red_flag_hits)}{RESET}" if sc.red_flag_hits else ""
        print(f"  {color}{sc.sid}{RESET} {sc.name:<30} {color}{sc_avg:.1f}{RESET}{rf}")

    # Save reports
    json_report = build_json_report(results, total_ms)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_report, f, ensure_ascii=False, indent=2)
    print(f"\n  {GREEN}JSON 报告: {json_path}{RESET}")

    html_report = build_html_report(results, total_ms)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_report)
    print(f"  {GREEN}HTML 报告: {html_path}{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
