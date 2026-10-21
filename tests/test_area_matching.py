"""行政区写法（南山区）要能匹配到库里按商圈存的店（南山科技园/南山后海）。"""
from xiangkou.workflow import _area_key, _district_area, _hard_reasons
from xiangkou.schemas import SearchPlan


def test_district_area_reads_district_from_message():
    assert _district_area("南山区的美食，人均100以内") == "南山"
    assert _district_area("福田区有什么好吃的") == "福田"
    assert _district_area("我要去南山科技园吃火锅") is None
    assert _district_area("随便哪都行") is None
    assert _district_area("除了罗湖区都行") is None
    assert _district_area("不去宝安区，找福田区") == "福田"


def _plan(area: str) -> SearchPlan:
    return SearchPlan(goal="search", category="美食", area=area, min_score=4.0, max_price=100)


def test_area_key_strips_district_suffix_but_keeps_business_area():
    assert _area_key("南山区") == "南山"
    assert _area_key("深圳市") == "深圳"
    assert _area_key("南山科技园") == "南山科技园"
    assert _area_key(" 福田区 ") == "福田"
    assert _area_key(None) is None
    assert _area_key("") is None
    assert _area_key("区") == "区"


def test_district_area_matches_business_area_shops():
    shop = {"id": 31, "name": "喜茶HEYTEA", "typeId": 1, "avgPrice": 25, "score": 47,
            "area": "南山科技园", "address": "万象天地L1"}
    assert _hard_reasons(shop, _plan("南山区"), 1) == []
    assert _hard_reasons(shop, _plan("南山"), 1) == []
    assert _hard_reasons(shop, _plan("南山科技园"), 1) == []
    # 真的不在南山的店仍然要被排除
    other = {**shop, "area": "福田购物公园", "address": "福华三路星河COCO Park"}
    assert _hard_reasons(other, _plan("南山区"), 1) == ["不在指定商圈"]
