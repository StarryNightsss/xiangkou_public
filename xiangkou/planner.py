"""需求解析。模型只生成结构化查询计划，不直接决定业务事实。"""

from __future__ import annotations

import json
import re

from .llm import LLMConfig, LLMProvider
from .harness import AgentHarness, mask_personal_info
from .schemas import SearchPlan, StopPlan
from .schedule import requested_time


CUISINES = ("茶餐厅", "火锅", "烤肉", "日料", "烧烤", "咖啡", "面馆", "西餐", "甜品")
CATEGORY_ALIASES = {
    "美食": ("美食", "吃饭", "吃点", "餐馆", "餐厅", "饭店"),
    "KTV": ("ktv", "唱歌", "k歌"),
    "丽人·美发": ("丽人·美发", "美发", "理发", "剪头发"),
    "健身运动": ("健身运动", "健身", "运动", "健身房"),
    "按摩·足疗": ("按摩·足疗", "按摩", "足疗", "足浴"),
    "美容SPA": ("美容spa", "spa", "美容院"),
    "亲子游乐": ("亲子游乐", "亲子", "游乐"),
    "酒吧": ("酒吧", "喝酒"),
    "轰趴馆": ("轰趴馆", "轰趴"),
    "美睫·美甲": ("美睫·美甲", "美甲", "美睫"),
}
UNVERIFIED_PREFERENCES = {
    "安静": ("安静", "不吵", "清静"),
    "适合约会": ("约会", "情侣"),
    "靠近地铁": ("地铁近", "离地铁近", "靠近地铁", "地铁站附近"),
    "无需排队": ("不排队", "排队少", "不用等位"),
}
MODEL_PLAN_FIELDS = {"category", "keyword", "area", "max_price", "min_score", "wants_coupon",
                     "wants_notes", "nearby", "sort", "followup", "relative_price", "goal", "visit_day", "party_size", "visit_time", "total_budget", "area_unrestricted", "budget_unrestricted"}


def _category(text: str) -> str | None:
    lowered = text.lower()
    for name, aliases in CATEGORY_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return name
    return None


def _keyword(message: str) -> str | None:
    matches = sorted(((message.index(word), word) for word in CUISINES if word in message), key=lambda item: item[0])
    for position, word in matches:
        prefix = message[max(0, position - 4):position]
        if not re.search(r"(?:不要|不吃|别吃|排除|除了|不想吃)$", prefix):
            return word
    return None


def _max_price(message: str) -> int | None:
    range_match = re.search(r"(?:人均|预算|每人|每位|价格)\s*(\d{1,5})\s*(?:到|至|[-~])\s*(\d{1,5})", message)
    if range_match:
        return int(range_match.group(2))
    patterns = (
        r"(?:人均|预算|每人|每位|价格)\s*(?:控制在|不超过|不高于|低于|最多|至多|在)?\s*(\d{1,5})\s*(?:元|块钱|块)?\s*(?:以内|以下|之内)?",
        r"(?:不超过|不高于|低于|最多|至多)\s*(\d{1,5})\s*(?:元|块钱|块)?",
        r"(\d{1,5})\s*(?:元|块钱|块)?\s*(?:以内|以下|之内)",
    )
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            if re.match(r"(?:左右|上下|大约)", message[match.end():]):
                continue
            value = int(match.group(1))
            if 1 <= value <= 100000:
                return value
    return None


def _min_score(message: str) -> float | None:
    patterns = (
        r"(?:评分|评价)\s*(?:至少|不低于|高于|达到)?\s*(\d(?:\.\d)?)\s*分?\s*(?:以上|起)?",
        r"(\d(?:\.\d)?)\s*分\s*(?:以上|起|及以上)",
    )
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            value = float(match.group(1))
            if 0 <= value <= 5:
                return value
    return None


def _area(message: str) -> str | None:
    patterns = (
        r"(?:在|去|找|靠近)([\u4e00-\u9fa5]{2,12}?)(?:附近|周边|一带)",
        r"^([\u4e00-\u9fa5]{2,12}?)(?:附近|周边|一带)",
    )
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            area = match.group(1)
            generic = {"地铁站", "公司", "学校", "小区", "我家", "这里", "这儿", "想找", "想去", "推荐", "请找", "帮我找"}
            if area not in generic and not area.endswith(("地铁站", "想找", "想去")) and "评分" not in area:
                return area
    return None


def _party_size(message: str) -> int | None:
    match = re.search(r"(\d{1,2})\s*(?:个人|人|位)", message)
    if match:
        value = int(match.group(1))
        return value if 1 <= value <= 20 else None
    for word, value in (("四个人", 4), ("四人", 4), ("五个人", 5), ("五人", 5), ("两个人", 2), ("两人", 2), ("三个人", 3), ("三人", 3), ("一个人", 1), ("一人", 1)):
        if word in message:
            return value
    return None


def _stops(message: str) -> list[StopPlan]:
    if not re.search(r"(?:先|首先).{1,80}?(?:然后|接着|再去|再|之后|最后)", message):
        return []
    pieces = re.split(r"(?:然后|接着|再去|再|之后|最后)", message)
    if len(pieces) < 2:
        return []
    stops = []
    for piece in pieces[:4]:
        category = _category(piece)
        keyword = _keyword(piece)
        if keyword and not category:
            category = "美食"
        if not category:
            return []
        stops.append(StopPlan(category=category, keyword=keyword))
    return stops


def _total_budget(message: str) -> int | None:
    match = re.search(r"(?:总预算|总共|合计|一共|总花费)\s*(?:不超过|控制在|最多|在)?\s*(\d{1,6})\s*(?:元|块)?", message)
    return int(match.group(1)) if match else None


def relative_price_direction(message: str) -> str | None:
    # Offline fallback only; configured models interpret broader relative intents.
    if re.search(r"(?:更贵|再贵|贵一点|贵一些|更高档|高档一点)", message):
        return "higher"
    if re.search(r"(?:更便宜|再便宜|便宜一点|便宜一些)", message):
        return "lower"
    return None


def booking_day(message: str) -> str | None:
    match = re.search(r"今天|今晚|明天|明晚|后天|周[一二三四五六日天末]|星期[一二三四五六日天]|\d{4}-\d{2}-\d{2}", message)
    return match.group() if match else None


def booking_request(message: str) -> bool:
    return bool(re.search(r"(?:订|定|预订|预定|预约).{0,12}(?:ktv|KTV|唱歌|包厢)", message))


def rule_plan(message: str) -> SearchPlan:
    category = _category(message)
    keyword = _keyword(message)
    stops = _stops(message)
    if stops:
        category = stops[0].category
        keyword = stops[0].keyword
    if keyword and not category:
        category = "美食"
    total_budget = _total_budget(message)
    # 总额和人均上限分别提取，避免“总预算200”被预算正则当成人均200。
    price_source = re.sub(r"(?:总预算|总共|合计|一共|总花费)\s*(?:不超过|控制在|最多|在)?\s*\d{1,6}\s*(?:元|块)?", "", message)
    return SearchPlan(
        goal="booking" if booking_request(message) else "search",
        visit_day=booking_day(message),
        area_unrestricted=bool(re.search(r"(?:商圈|地点|区域|位置).{0,3}不限|不限.{0,3}(?:商圈|地点|区域|位置)|哪里都行|地点随便", message)),
        budget_unrestricted=bool(re.search(r"预算不限|价格不限|不限制预算", message)),
        relative_price=relative_price_direction(message),
        category=category,
        keyword=keyword,
        area=_area(message),
        max_price=None if (stops and total_budget is not None) or (booking_request(message) and not re.search(r"人均|每人", message)) else _max_price(price_source),
        min_score=_min_score(message),
        wants_coupon=any(word in message for word in ("优惠", "券", "团购", "折扣")),
        wants_notes=any(word in message for word in ("探店", "笔记", "口碑", "评论", "评价内容")),
        nearby=any(word in message for word in ("附近", "周边", "离我近", "最近")),
        sort="price" if "便宜" in message or "低价" in message else "score",
        unverified_preferences=[name for name, aliases in UNVERIFIED_PREFERENCES.items()
                                if any(alias in message for alias in aliases)],
        stops=stops,
        party_size=_party_size(message),
        total_budget=total_budget,
        visit_time=requested_time(message),
    )


# 先提取规则计划，再结合模型语义补充；模型字段经过来源约束，失败时返回规则计划。
class Planner:
    def __init__(self, api_key: str = "", base_url: str = "https://api.anthropic.com",
                 model: str = "claude-3-5-sonnet-20241022", provider: LLMProvider | None = None):
        self.provider = provider or LLMProvider(LLMConfig(provider="anthropic", api_key=api_key.strip(),
                                                          base_url=base_url.rstrip("/"), model=model))

    async def plan(self, message: str, harness: AgentHarness | None = None, context: dict | None = None) -> tuple[SearchPlan, str]:
        # 优先组合上次执行后尚未处理的需求，从最新类别起截取，避免旧目标覆盖追问。
        pending = (context or {}).get("pending_user_messages", [])[-6:]
        starts = [i for i, text in enumerate(pending) if _category(text) or _keyword(text)]
        pending = pending[starts[-1]:] if starts else pending
        source = message + "\n" + "\n".join(reversed(pending))
        explicit = rule_plan(message)
        rules = rule_plan(source)
        # Newest explicit category wins; a category change cannot retain the old cuisine keyword.
        if explicit.category:
            rules.category, rules.keyword = explicit.category, explicit.keyword
        if rules.category and rules.category != "美食":
            rules.keyword = explicit.keyword if explicit.category == rules.category else None
        if not self.provider.enabled:
            if harness:
                harness.record("planner_rules", reason="model_not_configured")
            return rules, "rules"
        system = ("你是本地生活搜索规划器。只输出 JSON 对象，字段为 category、keyword、area、max_price、"
                  "min_score、wants_coupon、wants_notes、nearby、sort。category 用点评大类如美食/KTV，"
                  "keyword 是店名可匹配的短词，如火锅。max_price 只表示单站人均价格上限；"
                  "总预算不能写进 max_price。金额单位元，评分 0 到 5。未知可选值填 null，"
                  "布尔值填 false，sort 必须是 score/price，未指定时填 score，不能为 null。"
                  "不要编造地点或店铺。安静、约会、地铁、排队等没有可验证数据，不要写进 keyword。"
                  "额外字段followup为布尔值：用户继续调整或比较上一轮方案时为true，全新需求为false。"
                  "relative_price只可为higher/lower/null：比上轮推荐更贵或更高档用higher，"
                  "更便宜用lower；不要猜测比较金额，后端使用真实上轮店铺价格计算。"
                  "仅询问相对价格时保留上轮类别/关键词，不要把更贵理解为提高原预算上限。"
                  "goal为search或booking，想订/定/预约KTV为booking，只找店为search。"
                  "预约信息不足不要补猜：party_size、visit_day、visit_time、total_budget、area仅取用户明确给出的内容。"
                  "pending_user_messages为上次业务执行后尚未处理的用户请求，比previous_task.plan优先；"
                  "例如旧任务火锅，新请求KTV，再补充100以内、直接帮我查，要生成KTV/100，而不是火锅。"
                  "读取previous_task.plan和recent_user_messages，补充信息时followup=true，沿用最新目标。"
                  "area_unrestricted/budget_unrestricted仅用户明确地点/预算不限时为true。")
        try:
            request = lambda: self.provider.complete(system, [{"role": "user", "content": mask_personal_info(message) if context is None else mask_personal_info(json.dumps({"message": message,
                                                      "previous_task": context}, ensure_ascii=False))}],
                                                     max_tokens=500)
            completion = (await harness.model_call(request, timeout=self.provider.config.timeout)
                          if harness else await request())
            raw = completion.text
            raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("模型计划不是对象")
            # 模型只能提供允许的计划字段，不能注入内部排除列表或固定店铺状态。
            ignored_fields = sorted(set(payload) - MODEL_PLAN_FIELDS)
            payload = {key: value for key, value in payload.items() if key in MODEL_PLAN_FIELDS}
            sort_fallback = payload.get("sort") not in ("score", "price")
            if sort_fallback:
                payload["sort"] = rules.sort
            plan = SearchPlan.model_validate(payload)
            model_plan = plan.model_copy(deep=True)
            # 明确规则字段优先；关键词、商圈和数值还要检查用户原文来源，降低模型补造条件的风险。
            plan.category = rules.category or _category(plan.category or "")
            if plan.keyword and plan.keyword not in source:
                plan.keyword = None
            elif plan.keyword:
                normalized_keyword = _keyword(plan.keyword)
                plan.keyword = normalized_keyword or plan.keyword
                if plan.keyword in CUISINES and _keyword(source) != plan.keyword:
                    plan.keyword = None
            if plan.area and plan.area not in source:
                plan.area = None
            if rules.max_price is not None:
                plan.max_price = rules.max_price
            elif plan.max_price is not None and str(plan.max_price) not in source:
                plan.max_price = None
            if rules.min_score is not None:
                plan.min_score = rules.min_score
            elif plan.min_score is not None and str(plan.min_score).rstrip("0").rstrip(".") not in source:
                plan.min_score = None
            plan.wants_coupon = rules.wants_coupon
            plan.area_unrestricted = rules.area_unrestricted
            plan.budget_unrestricted = rules.budget_unrestricted
            plan.unverified_preferences = rules.unverified_preferences
            # 站点、人数和总预算由规则提供，避免把总预算误当人均上限或改写活动顺序。
            plan.stops = rules.stops
            plan.party_size = rules.party_size
            plan.total_budget = rules.total_budget
            if rules.total_budget is not None:
                plan.max_price = rules.max_price
            plan.visit_time = rules.visit_time
            previous = (context or {}).get("plan") or {}
            if rules.goal == "booking" or (plan.followup and previous.get("goal") == "booking"):
                plan.goal = "booking"
            # 预约追问从最近一次预约需求开始合并；只提取明确提供的信息，缺项留给工作流澄清。
            if plan.goal == "booking":
                # Extract only explicitly supplied values, newest user text first.
                recent = (context or {}).get("recent_user_messages", [])[-5:]
                starts = [i for i, text in enumerate(recent) if booking_request(text)]
                recent = recent[starts[-1]:] if starts else []
                source = message + "\n" + "\n".join(reversed(recent))
                if not re.search(r"人均|每人", source):
                    plan.max_price = None
                else:
                    plan.max_price = _max_price(source) or previous.get("max_price")
                plan.party_size = _party_size(source) or previous.get("party_size")
                plan.visit_day = booking_day(source) or previous.get("visit_day")
                plan.visit_time = requested_time(source) or previous.get("visit_time")
                plan.total_budget = _total_budget(source) or previous.get("total_budget")
                plan.area = _area(source) or (plan.area if plan.area and plan.area in source else None) or previous.get("area")
                plan.area_unrestricted = rule_plan(source).area_unrestricted or previous.get("area_unrestricted", False)
                plan.budget_unrestricted = bool(re.search(r"预算不限|价格不限|不限制预算", source)) or previous.get("budget_unrestricted", False)
            plan.wants_notes = rules.wants_notes
            # 多站沿用首站类别供上下文展示，总费用由组合阶段计算，不把全程预算塞进单站价格。
            if rules.stops:
                plan.category = rules.stops[0].category
                plan.keyword = rules.stops[0].keyword
                plan.max_price = None
                # 多站间距离由组合排序处理，不能把它变成用户定位要求。
                if not rules.nearby:
                    plan.nearby = False
            if harness:
                rule_fields = {"wants_coupon", "wants_notes",
                               "unverified_preferences", "stops", "party_size", "total_budget", "visit_time"}
                if rules.category:
                    rule_fields.add("category")
                if rules.stops:
                    rule_fields.update(("keyword", "max_price"))
                if rules.max_price is not None:
                    rule_fields.add("max_price")
                if rules.min_score is not None:
                    rule_fields.add("min_score")
                if sort_fallback:
                    rule_fields.add("sort")
                # 记录最终字段来自规则、模型还是后续校验，便于解释规划结果与定位识别问题。
                sources = {field: ("rules" if field in rule_fields else
                                   "model" if getattr(plan, field) == getattr(model_plan, field) else "validated")
                           for field in MODEL_PLAN_FIELDS | rule_fields}
                harness.record("planner_model", provider=self.provider.config.provider,
                               model=self.provider.config.model, usage=completion.usage,
                               field_sources=sources, ignored_fields=ignored_fields)
            return plan, "model"
        except Exception as exc:
            if harness:
                harness.record("planner_fallback", error=type(exc).__name__)
            return rules, "rules"
