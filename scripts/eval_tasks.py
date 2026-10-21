"""对正在运行的 巷口 + 点评做任务级回放，输出可复测的结果。"""

import json
import argparse
import hashlib
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiangkou.schedule import default_duration, minute_of_day, open_for_visit  # noqa: E402


CASES = Path(__file__).resolve().parents[1] / "tests" / "task_cases.json"
REPORT = Path(__file__).resolve().parents[1] / "reports" / "live-task-eval.json"


def output_shop_ids(data: dict) -> list[int]:
    return list(dict.fromkeys(int(shop["id"]) for shop in
                              (data.get("shops") or []) +
                              [shop for itinerary in data.get("itineraries") or []
                               for shop in itinerary.get("stops") or []]))


def check_response(case: dict, data: dict, actual_shops: dict[int, dict],
                   type_ids: dict[str, int]) -> list[str]:
    failures = []
    if data.get("status") != case["expected_status"]:
        failures.append(f"状态应为 {case['expected_status']}，实际 {data.get('status')}")
    shops = data.get("shops") or []
    if len(shops) < case.get("min_shops", 0):
        failures.append("推荐店铺数量不足")
    if case.get("expected_first_shop_id") and (not shops or shops[0]["id"] != case["expected_first_shop_id"]):
        failures.append("笔记证据没有提升目标店铺排序")
    if case.get("requires_budget_relaxation"):
        options = data.get("relaxations") or []
        if len(options) != 1 or not options[0].get("message", "").startswith("总预算"):
            failures.append("预算不足时缺少基于候选的调整建议")
    displayed = {int(shop["id"]): shop for shop in shops}
    for itinerary in data.get("itineraries") or []:
        displayed.update({int(shop["id"]): shop for shop in itinerary.get("stops") or []})
    plan = data.get("plan") or {}
    for shop_id, shop in displayed.items():
        actual = actual_shops.get(shop_id)
        if actual is None:
            failures.append(f"HARD: 店铺 {shop_id} 在点评数据中不存在")
            continue
        price = actual.get("avgPrice")
        score = actual.get("score")
        if case.get("hard_max_price") is not None and (price is None or price > case["hard_max_price"]):
            failures.append(f"HARD: 店铺 {shop_id} 超出价格上限或价格缺失")
        if case.get("hard_min_score") is not None and (score is None or score < case["hard_min_score"] * 10):
            failures.append(f"HARD: 店铺 {shop_id} 低于最低评分或评分缺失")
        if price is not None and shop.get("avg_price") != price:
            failures.append(f"HARD: 店铺 {shop_id} 展示价格与点评数据不一致")
        if score is not None and shop.get("score") != round(score / 10, 1):
            failures.append(f"HARD: 店铺 {shop_id} 展示评分与点评数据不一致")
        if plan.get("area") and plan["area"] not in str(actual.get("area") or "") + str(actual.get("address") or ""):
            failures.append(f"HARD: 店铺 {shop_id} 不在要求的商圈")
        if data.get("status") == "ok" and not shop.get("evidence"):
            failures.append(f"店铺 {shop_id} 缺少来源字段")
    if not plan.get("stops") and plan.get("category") in type_ids:
        for shop_id in displayed:
            if actual_shops.get(shop_id, {}).get("typeId") != type_ids[plan["category"]]:
                failures.append(f"HARD: 店铺 {shop_id} 类别与计划不符")
    if case.get("kind") in ("two_stop", "multi_stop") and data.get("status") == "ok":
        expected_count = case.get("stop_count", 2)
        for itinerary in data.get("itineraries") or []:
            if len(itinerary["stops"]) != expected_count:
                failures.append(f"方案不是 {expected_count} 站")
                continue
            if len({shop['id'] for shop in itinerary['stops']}) != expected_count:
                failures.append("HARD: 方案重复使用同一家店")
            if itinerary["party_size"] != case["party_size"]:
                failures.append("HARD: 人数口径错误")
            if itinerary["estimated_total"] > case["total_budget"]:
                failures.append("HARD: 方案超出总预算")
            actual_prices = [actual_shops.get(int(shop["id"]), {}).get("avgPrice")
                             for shop in itinerary["stops"]]
            if any(price is None for price in actual_prices):
                failures.append("HARD: 两站价格缺失，无法复核预算")
            elif itinerary["estimated_total"] != round(sum(actual_prices) * case["party_size"]):
                failures.append("HARD: 预计总价与点评当前价格不一致")
            if plan.get("stops") and len(plan["stops"]) == expected_count:
                for index, shop in enumerate(itinerary["stops"]):
                    expected_type = type_ids.get(plan["stops"][index]["category"])
                    if expected_type is not None and actual_shops.get(int(shop["id"]), {}).get("typeId") != expected_type:
                        failures.append(f"HARD: 第 {index + 1} 站类别与计划不符")
                    if case.get("categories") and actual_shops.get(int(shop["id"]), {}).get("typeId") != type_ids.get(case["categories"][index]):
                        failures.append(f"HARD: 第 {index + 1} 站类别与标注不符")
                    limit = case.get("stop_max_prices", {}).get(str(index + 1))
                    actual_price = actual_shops.get(int(shop["id"]), {}).get("avgPrice")
                    if limit is not None and (actual_price is None or actual_price > limit):
                        failures.append(f"HARD: 第 {index + 1} 站超出单站预算")
            if case.get("visit_time") and len(itinerary.get("visit_times") or []) == expected_count:
                for index, shop in enumerate(itinerary["stops"]):
                    visit_time = itinerary["visit_times"][index][:5]
                    actual = actual_shops.get(int(shop["id"]), {})
                    category = plan["stops"][index]["category"]
                    if open_for_visit(actual.get("openHours"), minute_of_day(visit_time),
                                      default_duration(category)) is not True:
                        failures.append(f"HARD: 第 {index + 1} 站在建议时段无法确认营业")
        if not data.get("itineraries"):
            failures.append("缺少两站方案")
    if case.get("visit_time") and (data.get("plan") or {}).get("visit_time") != case["visit_time"]:
        failures.append("到店时间未进入结构化计划")
    if case.get("requires_visit_times") and not all(
        len(itinerary.get("visit_times") or []) == case.get("stop_count", 2) for itinerary in data.get("itineraries") or []
    ):
        failures.append("两站方案缺少建议到店时刻")
    if case.get("expected_tool") and case["expected_tool"] not in [
        item.get("tool") for item in data.get("trace") or []
    ]:
        failures.append("未调用预期的点评业务接口")
    if case.get("requires_blog") and not any(
        evidence.get("field") == "blog" for shop in shops for evidence in shop.get("evidence", [])
    ):
        failures.append("缺少探店笔记来源")
    if case.get("requires_disclosure") and case["requires_disclosure"] not in data.get("answer", ""):
        failures.append("未说明无法验证的偏好")
    events = [event.get("event") for event in data.get("events") or []]
    if not events or events[0] != "run_start" or events[-1] != "run_end":
        failures.append("运行事件不完整")
    return failures


def token_count(event, field):
    value = event.get('usage', {}).get(field, 0)
    return value if type(value) is int and value >= 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, default=CASES)
    parser.add_argument('--report', type=Path, default=REPORT)
    args = parser.parse_args()
    base_url = os.getenv("XIANGKOU_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
    dianping_url = os.getenv("DIANPING_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
    token = os.getenv("XIANGKOU_AUTH_TOKEN")
    if not token:
        raise RuntimeError("请先设置 XIANGKOU_AUTH_TOKEN 为点评登录令牌，再运行评测")
    if os.getenv("XK_EVAL_CLEAR_MEMORY") == "1":
        httpx.delete(f"{base_url}/api/memory", headers={"authorization": token}, timeout=10)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    passed = 0
    violations = 0
    latencies = []
    calls = []
    outcomes = []
    modes = {}
    with httpx.Client(timeout=45, trust_env=False, headers={"authorization": token}) as client:
        health = client.get(f"{base_url}/health").json()
        types = client.get(f"{dianping_url}/shop-type/list").json()
        if types.get("success") is not True:
            raise RuntimeError("无法读取点评分类用于独立评测")
        type_ids = {item["name"]: item["id"] for item in types.get("data") or []}
        for case in cases:
            started = time.monotonic()
            response = client.post(f"{base_url}/api/chat", json={"message": case["message"]})
            response.raise_for_status()
            data = response.json()
            turns = [data]
            elapsed_ms = (time.monotonic() - started) * 1000
            latencies.append(elapsed_ms)
            calls.append(len(data.get("trace") or []))
            ids = output_shop_ids(data)
            actual_shops = {}
            if ids:
                detail = client.get(f"{dianping_url}/shop/batch", params={"ids": ",".join(map(str, ids))})
                detail.raise_for_status()
                body = detail.json()
                if body.get("success") is not True:
                    raise RuntimeError("点评批量详情接口不可用，无法独立验证任务结果")
                actual_shops = {int(shop["id"]): shop for shop in body.get("data") or []}
            failures = check_response(case, data, actual_shops, type_ids)
            if case.get("followup") and data.get("status") == "ok":
                followup = client.post(f"{base_url}/api/chat", json={
                    "message": case["followup"], "session_id": data["session_id"],
                })
                followup.raise_for_status()
                changed = followup.json()
                turns.append(changed)
                if changed.get("status") != "ok" or changed.get("plan", {}).get("total_budget") != case["total_budget"]:
                    failures.append("追问未保留预算或未返回方案")
                else:
                    if data["shops"][0]["id"] != changed["shops"][0]["id"]:
                        failures.append("追问错误替换了第一家店")
                    if data["shops"][1]["id"] == changed["shops"][1]["id"]:
                        failures.append("追问未换掉第二家店")
                    if changed["shops"][1]["avg_price"] >= data["shops"][1]["avg_price"]:
                        failures.append("追问没有换成更便宜的第二家店")
                    detail = client.get(f"{dianping_url}/shop/batch", params={"ids": ','.join(map(str, output_shop_ids(changed)))})
                    detail.raise_for_status()
                    checked = detail.json()
                    if checked.get('success') is not True:
                        raise RuntimeError('追问结果无法从点评独立复核')
                    failures.extend(check_response({**case, 'requires_budget_relaxation': False}, changed,
                                                   {int(shop['id']): shop for shop in checked['data']}, type_ids))
            previous = data
            for edit in case.get('followups', []):
                response = client.post(f"{base_url}/api/chat", json={'message': edit['message'], 'session_id': previous['session_id']})
                response.raise_for_status()
                changed = response.json()
                turns.append(changed)
                ids = output_shop_ids(changed)
                actual = {}
                if ids:
                    detail = client.get(f"{dianping_url}/shop/batch", params={'ids': ','.join(map(str, ids))})
                    detail.raise_for_status()
                    checked = detail.json()
                    if checked.get('success') is not True: raise RuntimeError('编辑结果无法独立复核')
                    actual = {int(shop['id']): shop for shop in checked['data']}
                edit_case = {**case, **edit}
                failures.extend(check_response(edit_case, changed, actual, type_ids))
                previous = changed
            if case.get("requires_budget_relaxation") and data.get("relaxations"):
                option = data["relaxations"][0]
                retry = client.post(f"{base_url}/api/chat", json={
                    "message": option["message"], "session_id": data["session_id"],
                })
                retry.raise_for_status()
                changed = retry.json()
                turns.append(changed)
                if (changed.get("status") != "ok" or not changed.get("itineraries") or
                        changed["plan"]["total_budget"] != changed["itineraries"][0]["estimated_total"]):
                    failures.append("确认预算调整后未返回预算内方案")
            elapsed_ms = (time.monotonic() - started) * 1000
            latencies[-1] = elapsed_ms
            calls[-1] = sum(len(turn.get('trace') or []) for turn in turns)
            if failures:
                violations += sum(issue.startswith("HARD:") for issue in failures)
                print(f"FAIL {case['message']}：{'；'.join(failures)}")
            else:
                passed += 1
                print(f"PASS {case['message']}（{elapsed_ms:.0f} ms，{calls[-1]} 次工具调用）")
            outcomes.append({"message": case["message"], "passed": not failures,
                             "failures": failures, "status": data.get("status"),
                             "mode": data.get("mode"), "run_id": data.get("run_id"),
                             "tool_calls": calls[-1], "elapsed_ms": round(elapsed_ms, 1),
                             "turn_count": len(turns),
                             "input_tokens": sum(token_count(event, 'input_tokens') for turn in turns for event in turn.get('events', [])),
                             "output_tokens": sum(token_count(event, 'output_tokens') for turn in turns for event in turn.get('events', [])),
                             "planner_fallbacks": sum(event.get('event') == 'planner_fallback' for turn in turns for event in turn.get('events', []))})
            modes[data.get('mode')] = modes.get(data.get('mode'), 0) + 1
    p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95 + 0.999) - 1)]
    print(f"任务通过率 {passed}/{len(cases)}；硬条件违规 {violations}；平均工具调用 {statistics.mean(calls):.1f}；P95 {p95:.0f} ms")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": "local_demo_sql_with_labeled_synthetic_notes",
        "validator": "independent_go_api_batch_detail_for_every_displayed_shop",
        "llm": health.get("llm"), "case_count": len(cases), "pass_count": passed,
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(), "actual_modes": modes,
        "hard_constraint_violations": violations,
        "average_tool_calls": round(statistics.mean(calls), 2), "p50_ms": round(statistics.median(latencies), 1), "p95_ms": round(p95, 1),
        "cases": outcomes,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{args.report}")
    raise SystemExit(passed != len(cases))


if __name__ == "__main__":
    main()
