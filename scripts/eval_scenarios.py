"""离线合成场景与故障注入：独立于正在运行的 既有服务/模型服务。"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xiangkou.dianping import DianpingClient  # noqa: E402
from xiangkou.planner import Planner  # noqa: E402
from xiangkou.schemas import ChatRequest  # noqa: E402
from xiangkou.workflow import DiscoveryWorkflow  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "scenario_shops.json"
REPORT = ROOT / "reports" / "scenario-eval.json"


async def evaluate_case(case, fixture):
    def mock(request):
        path = request.url.path
        if case.get("outage"):
            return httpx.Response(503)
        if path == "/shop-type/list":
            data = fixture["types"]
        elif path == "/shop/search":
            data = [shop for shop in fixture["search"]
                    if not request.url.params.get("maxPrice")
                    or shop["avgPrice"] is None or shop["avgPrice"] <= int(request.url.params["maxPrice"])]
            if case.get("only_unknown_price"):
                data = [shop for shop in data if shop["avgPrice"] is None]
            return httpx.Response(200, json={"success": True, "data": data, "total": len(data)})
        elif path.startswith("/shop/"):
            data = fixture["details"].get(path.rsplit("/", 1)[-1], {})
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True, "data": data})

    client = DianpingClient("http://synthetic.test", transport=httpx.MockTransport(mock))
    try:
        workflow = DiscoveryWorkflow(client, Planner(), "http://synthetic.test")
        response = await workflow.run(ChatRequest(message=case["message"]))
        record = await workflow.runs.get(response.run_id)
        issues = []
        if response.status != case["expected_status"]:
            issues.append(f"status={response.status}")
        if response.status == "ok" and not response.shops:
            issues.append("missing_shops")
        if (case.get("max_price_100") and
                any(shop.avg_price is not None and shop.avg_price > 100 for shop in response.shops)):
            issues.append("price_constraint_violation")
        if response.events[0]["event"] != "run_start" or response.events[-1]["event"] != "run_end":
            issues.append("incomplete_events")
        if not record or record["events"] != response.events:
            issues.append("replay_mismatch")
        if case.get("expected_tool") and case["expected_tool"] not in [item["tool"] for item in response.trace]:
            issues.append("missing_expected_tool")
        return {"name": case["name"], "passed": not issues, "issues": issues,
                "status": response.status,
                "tool_calls": len(response.trace), "event_count": len(response.events)}
    finally:
        await client.close()


async def main():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = [
        {"name": "正常找店", "message": "推荐运河上街的美食", "expected_status": "ok"},
        {"name": "价格硬约束", "message": "找人均100以内的美食", "expected_status": "ok", "max_price_100": True},
        {"name": "缺失字段补查后拒绝", "message": "找人均100以内的美食", "only_unknown_price": True,
         "expected_status": "no_results", "expected_tool": "get_shop"},
        {"name": "缺少位置", "message": "找附近的美食", "expected_status": "needs_location"},
        {"name": "上游故障", "message": "推荐美食", "outage": True, "expected_status": "upstream_error"},
    ]
    results = [await evaluate_case(case, fixture) for case in cases]
    report = {"dataset": "synthetic", "case_count": len(results),
              "pass_count": sum(item["passed"] for item in results),
              "constraint_violations": sum("price_constraint_violation" in item["issues"] for item in results),
              "cases": results}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for item in results:
        print(f"{'PASS' if item['passed'] else 'FAIL'} {item['name']} ({item['status']})")
    print(f"合成场景 {report['pass_count']}/{report['case_count']}；报告：{REPORT}")
    raise SystemExit(report["pass_count"] != report["case_count"])


if __name__ == "__main__":
    asyncio.run(main())
