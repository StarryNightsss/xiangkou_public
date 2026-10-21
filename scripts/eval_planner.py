"""在固定中文找店提问集上检查规则解析，不依赖模型或点评服务。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xiangkou.planner import rule_plan


CASES = Path(__file__).resolve().parents[1] / "tests" / "planner_cases.json"


def evaluate() -> tuple[int, int, list[str]]:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    failures = []
    for case in cases:
        actual = rule_plan(case["message"]).model_dump()
        for field, expected in case["expected"].items():
            if actual[field] != expected:
                failures.append(f"{case['message']} | {field}: 期望 {expected!r}，实际 {actual[field]!r}")
    return len(cases), len(cases) - len({failure.split(" | ", 1)[0] for failure in failures}), failures


if __name__ == "__main__":
    total, passed, failures = evaluate()
    print(f"规则解析评测：{passed}/{total} 条提问通过")
    for failure in failures:
        print(f"- {failure}")
    raise SystemExit(bool(failures))
