from pathlib import Path
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from models.review import LLMMode, ReviewPosition
from services.event_service import ReviewEventStore
from services.review_service import ReviewOrchestratorAgent
from services.review_workflow import MAIN_NODE_ORDER, RISK_NODE_ORDER


class LangGraphWorkflowTest(unittest.TestCase):
    def test_compiled_graph_contains_fixed_main_flow_and_risk_subgraph(self):
        agent = ReviewOrchestratorAgent(ReviewEventStore())
        graph_nodes = set(agent.graph.get_graph(xray=True).nodes)

        for node_name in MAIN_NODE_ORDER:
            if node_name == "risk_subgraph":
                continue
            self.assertIn(node_name, graph_nodes)
        for node_name in RISK_NODE_ORDER:
            self.assertIn(f"risk_subgraph:{node_name}", graph_nodes)

    def test_risk_results_are_aggregated_in_source_order(self):
        unordered = [
            {"index": 2, "value": "third"},
            {"index": 0, "value": "first"},
            {"index": 1, "value": "second"},
        ]

        ordered = ReviewOrchestratorAgent._ordered_results(unordered)

        self.assertEqual(
            [item["value"] for item in ordered],
            ["first", "second", "third"],
        )

    def test_deepseek_parallelism_is_bounded_and_local_mode_is_serial(self):
        store = ReviewEventStore()
        agent = ReviewOrchestratorAgent(store, llm_max_concurrency=2)
        local_state = store.create_task(
            "local.docx",
            "docx",
            ReviewPosition.PARTY_A,
            llm_mode=LLMMode.LOCAL_STRUCTURED,
        )
        deepseek_state = store.create_task(
            "deepseek.docx",
            "docx",
            ReviewPosition.PARTY_A,
            llm_mode=LLMMode.DEEPSEEK,
        )

        self.assertEqual(agent._batch_width(local_state), 1)
        self.assertEqual(agent._batch_width(deepseek_state), 2)


if __name__ == "__main__":
    unittest.main()
