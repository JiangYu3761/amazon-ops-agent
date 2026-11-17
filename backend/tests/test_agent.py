import unittest
from datetime import date, timedelta

from app.agent import analyze_product


class AgentAnalysisTests(unittest.TestCase):
    def test_flags_overdue_and_insufficient_baseline(self):
        tasks = [{
            "title": "确认合规状态",
            "status": "in_progress",
            "due_date": date.today() - timedelta(days=1),
        }]
        rankings = [{
            "keyword": "indoor security camera",
            "observation_type": "organic",
            "observed_at": date.today(),
            "rank_value": 31,
        }]
        types = {x["type"] for x in analyze_product(tasks, rankings)}
        self.assertEqual(types, {"overdue_tasks", "insufficient_rank_baseline"})


if __name__ == "__main__":
    unittest.main()

