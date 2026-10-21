"""Compare rules/model discovery with public 既有服务 reads; no login or business writes."""
import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xiangkou.dianping import DianpingClient
from xiangkou.llm import LLMConfig, LLMProvider
from xiangkou.planner import Planner
from xiangkou.schemas import ChatRequest
from xiangkou.workflow import DiscoveryWorkflow
from scripts.eval_tasks import check_response, output_shop_ids


async def evaluate(mode, destination):
    path = Path('tests/task_cases.json')
    cases = [case for case in json.loads(path.read_text()) if not case.get('requires_account')]
    config = LLMConfig.from_env() if mode == 'model' else LLMConfig()
    provider = LLMProvider(config)
    if mode == 'model' and not provider.enabled:
        raise RuntimeError('Model evaluation requires configured provider and model')
    client = DianpingClient('http://127.0.0.1:8081')
    workflow = DiscoveryWorkflow(client, Planner(provider=provider), 'http://127.0.0.1:8080')
    outcomes = []
    try:
        types = {item['name']: item['id'] for item in await client.list_types()}
        for case in cases:
            start = time.monotonic()
            result = await workflow.run(ChatRequest(message=case['message']))
            turns = [result]
            async def verify(result, annotation):
                data = result.model_dump()
                ids = output_shop_ids(data)
                actual = await client.get_shops(ids) if ids else []
                return check_response(annotation, data, {int(shop['id']): shop for shop in actual}, types)
            failures = await verify(result, case)
            if case.get('followup') and result.status == 'ok':
                changed = await workflow.run(ChatRequest(message=case['followup'], session_id=result.session_id))
                turns.append(changed)
                failures.extend(await verify(changed, case))
                if changed.status == 'ok':
                    if changed.shops[0].id != result.shops[0].id: failures.append('追问没有保留首站')
                    if changed.shops[1].avg_price >= result.shops[1].avg_price: failures.append('替代第二站未更便宜')
            previous = result
            for edit in case.get('followups', []):
                previous = await workflow.run(ChatRequest(message=edit['message'], session_id=previous.session_id))
                turns.append(previous)
                failures.extend(await verify(previous, {**case, **edit}))
            if case.get('requires_budget_relaxation') and result.relaxations:
                import re
                option = result.relaxations[0]
                budget = int(re.search(r'\d+', option.message).group())
                retry = await workflow.run(ChatRequest(message=option.message, session_id=result.session_id))
                turns.append(retry)
                failures.extend(await verify(retry, {**case, 'expected_status': 'ok', 'requires_budget_relaxation': False,
                                                    'total_budget': budget, 'party_size': 2}))
            events = [event for turn in turns for event in turn.events]
            elapsed = round((time.monotonic() - start) * 1000, 1)
            outcomes.append({'message': case['message'], 'passed': not failures, 'failures': failures,
                             'status': result.status, 'mode': result.mode, 'run_id': result.run_id,
                             'turn_count': len(turns), 'elapsed_ms': elapsed,
                             'tool_calls': sum(len(turn.trace) for turn in turns),
                             'model_plans': sum(event['event'] == 'planner_model' for event in events),
                             'planner_fallbacks': sum(event['event'] == 'planner_fallback' for event in events),
                             'input_tokens': sum(event.get('usage', {}).get('input_tokens', 0) for event in events),
                             'output_tokens': sum(event.get('usage', {}).get('output_tokens', 0) for event in events)})
            print(('PASS' if not failures else 'FAIL'), case['message'], f'{elapsed:g} ms', failures, flush=True)
    finally:
        await client.close()
    latencies = sorted(item['elapsed_ms'] for item in outcomes)
    report = {'evaluated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'scope': 'public_discovery_workflow_direct_go_api_reads_without_account_or_business_writes',
              'cases_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'llm': {'provider': config.provider if mode == 'model' else None,
                      'model': config.model if mode == 'model' else None, 'requested_mode': mode},
              'case_count': len(outcomes), 'pass_count': sum(item['passed'] for item in outcomes),
              'hard_constraint_violations': sum(issue.startswith('HARD:') for item in outcomes for issue in item['failures']),
              'p50_ms': statistics.median(latencies), 'p95_ms': latencies[max(0, int(len(latencies) * .95 + .999) - 1)],
              'input_tokens': sum(item['input_tokens'] for item in outcomes),
              'output_tokens': sum(item['output_tokens'] for item in outcomes),
              'model_plans': sum(item['model_plans'] for item in outcomes),
              'planner_fallbacks': sum(item['planner_fallbacks'] for item in outcomes), 'cases': outcomes}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('Report:', destination, flush=True)
    return report['pass_count'] != len(outcomes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['rules', 'model'], required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args.mode, args.report)))
