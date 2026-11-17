import unittest

from app.context import resolve_scope


class ContextRoutingTests(unittest.TestCase):
    def test_defaults_to_active_page(self):
        self.assertEqual(resolve_scope("哪些任务逾期？", "workflow"), ("workflow", "哪些任务逾期？"))

    def test_explicit_prefix_overrides_page(self):
        self.assertEqual(resolve_scope("@排名 最近为什么下降？", "workflow"), ("ranking", "最近为什么下降？"))
        self.assertEqual(resolve_scope("@全局 判断上线风险", "ranking"), ("global", "判断上线风险"))


if __name__ == "__main__":
    unittest.main()
