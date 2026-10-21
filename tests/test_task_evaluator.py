from scripts.eval_tasks import check_response, output_shop_ids


def test_evaluator_rechecks_all_itineraries_against_go_api_data():
    shop = lambda shop_id, price, category: {
        "id": shop_id, "name": f"店{shop_id}", "avg_price": price, "score": 4.8,
        "evidence": [{"field": "avgPrice"}],
    }
    data = {
        "status": "ok", "answer": "", "plan": {"stops": [{"category": "美食"}, {"category": "KTV"}]},
        "shops": [shop(1, 80, 1), shop(11, 70, 2)],
        "itineraries": [
            {"stops": [shop(1, 80, 1), shop(11, 70, 2)], "party_size": 2, "estimated_total": 300},
            {"stops": [shop(2, 60, 1), shop(12, 60, 2)], "party_size": 2, "estimated_total": 240},
        ],
        "events": [{"event": "run_start"}, {"event": "run_end"}],
    }
    actual = {
        1: {"id": 1, "typeId": 1, "avgPrice": 80, "score": 48},
        11: {"id": 11, "typeId": 2, "avgPrice": 70, "score": 48},
        2: {"id": 2, "typeId": 1, "avgPrice": 160, "score": 48},
        12: {"id": 12, "typeId": 2, "avgPrice": 60, "score": 48},
    }
    case = {"expected_status": "ok", "kind": "two_stop", "party_size": 2, "total_budget": 400}
    assert output_shop_ids(data) == [1, 11, 2, 12]
    failures = check_response(case, data, actual, {"美食": 1, "KTV": 2})
    assert any("店铺 2 展示价格与点评数据不一致" in issue for issue in failures)
    assert any("预计总价与点评当前价格不一致" in issue for issue in failures)


def test_evaluator_rejects_missing_go_api_shop():
    data = {"status": "ok", "answer": "", "shops": [{"id": 99, "evidence": [{"field": "score"}]}],
            "plan": {"category": "美食"}, "events": [{"event": "run_start"}, {"event": "run_end"}]}
    failures = check_response({"expected_status": "ok", "min_shops": 1}, data, {}, {"美食": 1})
    assert any("店铺 99 在点评数据中不存在" in issue for issue in failures)
