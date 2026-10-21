"""对正在运行的点评 + 巷口 做最小端到端检查。"""

import os

import httpx


def main() -> None:
    dianping = os.getenv("DIANPING_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
    xiangkou = os.getenv("XIANGKOU_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
    token = os.getenv("XIANGKOU_AUTH_TOKEN")
    if not token:
        raise RuntimeError("请先设置 XIANGKOU_AUTH_TOKEN 为点评登录令牌，再运行联调脚本")
    with httpx.Client(timeout=10, trust_env=False, headers={"authorization": token}) as client:
        types_response = client.get(f"{dianping}/shop-type/list")
        types_response.raise_for_status()
        types = types_response.json()
        assert types.get("success") is True and types.get("data"), "点评分类接口没有数据"

        search_response = client.get(f"{dianping}/shop/search", params={
            "typeId": 1, "keyword": "火锅", "maxPrice": 120, "minScore": 45, "page": 1, "size": 10,
        })
        search_response.raise_for_status()
        search = search_response.json()
        assert search.get("success") is True and search.get("total", 0) > 0, "点评组合搜索没有结果"
        assert all("火锅" in shop["name"] and shop["avgPrice"] <= 120 and shop["score"] >= 45
                   for shop in search["data"]), "点评组合筛选结果不符合条件"

        coupon_response = client.get(f"{dianping}/shop/search", params={
            "typeId": 1, "hasVoucher": "true", "page": 1, "size": 3,
        })
        coupon_response.raise_for_status()
        coupon_search = coupon_response.json()
        assert coupon_search.get("success") is True and coupon_search.get("total") == 1, "优惠券筛选结果异常"
        assert [shop["id"] for shop in coupon_search["data"]] == [1], "优惠券筛选返回了无券店铺"

        ready_response = client.get(f"{xiangkou}/ready")
        ready_response.raise_for_status()
        assert ready_response.json().get("status") == "ready", "巷口 未就绪"

        chat_response = client.post(f"{xiangkou}/api/chat", json={"message": "推荐评分高的美食"})
        chat_response.raise_for_status()
        chat = chat_response.json()
        assert chat.get("status") == "ok" and chat.get("shops"), "巷口 未返回真实店铺"
        assert any(item["tool"] == "search_shops" for item in chat.get("trace", [])), "巷口 未使用组合搜索工具"
        first = chat["shops"][0]
        page_response = client.get(first["url"])
        page_response.raise_for_status()

        coupon_chat_response = client.post(f"{xiangkou}/api/chat", json={"message": "推荐美食，有优惠券吗"})
        coupon_chat_response.raise_for_status()
        coupon_chat = coupon_chat_response.json()
        assert coupon_chat.get("status") == "ok", "巷口 优惠推荐失败"
        assert [shop["id"] for shop in coupon_chat["shops"]] == [1], "巷口 未按 既有服务 优惠券筛选推荐"
        assert coupon_chat["shops"][0]["coupons"], "巷口 未读取优惠券详情"
        assert [item["tool"] for item in coupon_chat["trace"]] == ["list_types", "search_shops", "list_vouchers"], "巷口 优惠工作流调用异常"

        itinerary_response = client.post(f"{xiangkou}/api/chat", json={
            "message": "两个人先吃火锅再唱歌，总预算400元，尽量近",
        })
        itinerary_response.raise_for_status()
        itinerary = itinerary_response.json()
        assert itinerary.get("status") == "ok" and itinerary.get("itineraries"), "两站方案未返回"
        assert all(plan["estimated_total"] <= 400 and len(plan["stops"]) == 2
                   for plan in itinerary["itineraries"]), "两站方案违反总预算"
        assert itinerary.get("session_id") and itinerary.get("events"), "会话或事件轨迹缺失"

        followup_response = client.post(f"{xiangkou}/api/chat", json={
            "message": "第二家太贵，换便宜点的", "session_id": itinerary["session_id"],
        })
        followup_response.raise_for_status()
        followup = followup_response.json()
        assert followup.get("status") == "ok" and followup["plan"]["total_budget"] == 400, "追问未继承预算"
        assert followup["shops"][1]["id"] != itinerary["shops"][1]["id"], "追问未换掉第二家店"

        notes_response = client.post(f"{xiangkou}/api/chat", json={"message": "想看茶餐厅的探店笔记"})
        notes_response.raise_for_status()
        notes = notes_response.json()
        assert any(item["field"] == "blog" for shop in notes.get("shops", [])
                   for item in shop.get("evidence", [])), "未关联探店笔记来源"
        print(f"联调通过：{len(types['data'])} 个分类、两站预算与追问、有券推荐、探店来源；详情页 HTTP {page_response.status_code}")


if __name__ == "__main__":
    main()
