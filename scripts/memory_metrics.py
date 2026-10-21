"""汇总 Agent 记忆相关指标：上下文压缩比、摘要覆盖、偏好命中率、任务恢复成功率。

用法：python scripts/memory_metrics.py [--db data/xiangkou.sqlite3]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def load(path: Path) -> dict:
    stats = {"runs": 0, "context_samples": [], "summaries": [], "memory_hits": 0,
             "resume_ok": 0, "resume_total": 0}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        for row in con.execute("SELECT record_json FROM runs"):
            stats["runs"] += 1
            try:
                record = json.loads(row["record_json"])
            except (TypeError, ValueError):
                continue
            if record.get("memory_applied"):
                stats["memory_hits"] += 1
            for event in record.get("events") or []:
                if event.get("event") == "context_metrics":
                    data = event
                    raw = data.get("raw_history_chars")
                    injected = data.get("injected_chars")
                    if isinstance(raw, int) and isinstance(injected, int) and raw > 0:
                        stats["context_samples"].append((raw, injected, data.get("summarized_pairs") or 0))
    except sqlite3.OperationalError:
        pass
    for table, sql in (("summary", "SELECT covered_turns FROM session_summaries"),
                       ("resume", "SELECT ok FROM resume_events")):
        try:
            for row in con.execute(sql):
                if table == "summary":
                    stats["summaries"].append(int(row["covered_turns"]))
                else:
                    stats["resume_total"] += 1
                    stats["resume_ok"] += int(row["ok"])
        except sqlite3.OperationalError:
            pass
    con.close()
    return stats


def report(stats: dict) -> None:
    print(f"运行记录：{stats['runs']} 条")
    samples = stats["context_samples"]
    if samples:
        raw = sum(item[0] for item in samples) / len(samples)
        injected = sum(item[1] for item in samples) / len(samples)
        drop = (1 - injected / raw) * 100 if raw else 0
        print(f"上下文压缩：平均原文 {raw:.0f} 字 → 注入 {injected:.0f} 字，"
              f"降幅 {drop:.1f}%（样本 {len(samples)} 次，平均已摘要 {sum(i[2] for i in samples) / len(samples):.1f} 组）")
    else:
        print("上下文压缩：暂无样本（多聊几轮后自动产生）")
    summaries = stats["summaries"]
    if summaries:
        print(f"会话摘要：{len(summaries)} 个会话有摘要，平均覆盖 {sum(summaries) / len(summaries):.1f} 组问答")
    else:
        print("会话摘要：暂无（对话超过 5 组后自动压缩）")
    runs = stats["runs"]
    if runs:
        print(f"偏好命中：{stats['memory_hits']}/{runs} 次运行注入了长期偏好"
              f"（{stats['memory_hits'] / runs * 100:.1f}%）")
    total = stats["resume_total"]
    if total:
        print(f"任务恢复：成功 {stats['resume_ok']}/{total} 次（{stats['resume_ok'] / total * 100:.1f}%）")
    else:
        print("任务恢复：暂无恢复尝试")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(Path(__file__).parent.parent / "data" / "xiangkou.sqlite3"))
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"找不到数据库：{path}")
    report(load(path))


if __name__ == "__main__":
    main()
