from collections import defaultdict
from datetime import date


def analyze_product(tasks: list[dict], rankings: list[dict]) -> list[dict]:
    """Deterministic evidence layer used before any LLM explanation."""
    findings: list[dict] = []
    today = date.today()

    blocked = [t for t in tasks if t.get("status") == "blocked"]
    if blocked:
        findings.append({
            "risk": "critical",
            "type": "blocked_tasks",
            "title": f"{len(blocked)} 项关键任务受阻",
            "evidence": {"task_titles": [t["title"] for t in blocked]},
            "proposed_action": "要求主责人填写阻塞原因、解除条件和预计恢复日期。",
        })

    overdue = [
        t for t in tasks
        if t.get("due_date") and t["due_date"] < today
        and t.get("status") not in {"completed", "not_applicable"}
    ]
    if overdue:
        findings.append({
            "risk": "critical",
            "type": "overdue_tasks",
            "title": f"{len(overdue)} 项任务已经逾期",
            "evidence": {"task_titles": [t["title"] for t in overdue]},
            "proposed_action": "重新确认负责人和承诺日期；影响上线的任务进入每日升级。",
        })

    by_query: dict[str, list[dict]] = defaultdict(list)
    for row in rankings:
        if row.get("keyword") and row.get("observation_type") == "organic":
            by_query[row["keyword"]].append(row)
    for keyword, points in by_query.items():
        points.sort(key=lambda x: x["observed_at"])
        if len(points) < 7:
            findings.append({
                "risk": "warning",
                "type": "insufficient_rank_baseline",
                "title": f"{keyword} 排名样本不足",
                "evidence": {"observations": len(points), "minimum": 7},
                "proposed_action": "保持采集环境不变，至少积累7个日级样本后再判断趋势。",
            })
        elif points[-1]["rank_value"] - points[0]["rank_value"] >= 10:
            findings.append({
                "risk": "warning",
                "type": "organic_rank_drop",
                "title": f"{keyword} 自然排名明显下降",
                "evidence": {"from": points[0]["rank_value"], "to": points[-1]["rank_value"]},
                "proposed_action": "检查价格、配送、库存、评分、页面变更和同期广告变化。",
            })
    return findings

