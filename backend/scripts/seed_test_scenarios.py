"""
One-time migration: insert all 20 hardcoded SCENE_META entries into test_scenarios table.
Run: cd /Users/singhfang/shopilot-v2 && venv/bin/python -m backend.scripts.seed_test_scenarios
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.db import TestScenario, engine
from sqlmodel import Session, select

SCENE_META = {
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


def main():
    with Session(engine) as session:
        inserted = 0
        skipped = 0
        for sid, meta in SCENE_META.items():
            existing = session.exec(select(TestScenario).where(TestScenario.sid == sid)).first()
            if existing:
                print(f"[skip] {sid} already exists")
                skipped += 1
                continue

            row = TestScenario(
                sid=sid,
                name=meta["name"],
                business_type=meta.get("business_type", ""),
                shop_json=json.dumps(meta.get("shop", {}), ensure_ascii=False),
                csv_name=meta.get("csv_name", ""),
                q1=meta.get("q1", ""),
                q2=meta.get("q2", ""),
                q3=meta.get("q3", ""),
                must_json=json.dumps(meta.get("must", []), ensure_ascii=False),
                red_flags_json=json.dumps(meta.get("red_flags", []), ensure_ascii=False),
            )
            session.add(row)
            inserted += 1
            print(f"[insert] {sid} {meta['name']}")

        session.commit()
        print(f"\nDone: {inserted} inserted, {skipped} skipped.")


if __name__ == "__main__":
    main()
