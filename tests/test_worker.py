import os
import unittest
import tempfile
from src.planner import TaskSpec
from src.worker import Worker, WorkerResult

class TestWorkerTemplate(unittest.TestCase):
    def setUp(self):
        self.task_spec = TaskSpec(
            opportunity_id="test_opp_123",
            task="Resolve issue #42 (Fix edge case in parser) in repository pydantic/pydantic.",
            priority="HIGH",
            expected_output="Working code fix addressing 'Fix edge case in parser'. Must include root cause analysis, modified code implementation, test verification, and documentation.",
            estimated_cost=0.0001,
            input_tokens=150
        )

    def test_prompt_template_byte_for_byte_equivalence(self):
        """Confirm the prompt string produced by format_prompt exactly matches the previous hardcoded string."""
        worker = Worker()
        
        # Exact literal construction from pre-extraction src/worker.py:
        # f"{task_spec.task}\nExpected Output: {task_spec.expected_output}"
        expected_hardcoded_prompt = f"{self.task_spec.task}\nExpected Output: {self.task_spec.expected_output}"
        
        actual_template_prompt = worker.format_prompt(self.task_spec)
        
        self.assertEqual(
            actual_template_prompt,
            expected_hardcoded_prompt,
            "Template-substituted prompt must be byte-for-byte identical to the original hardcoded string."
        )
        self.assertEqual(
            actual_template_prompt.encode("utf-8"),
            expected_hardcoded_prompt.encode("utf-8")
        )

    def test_missing_template_raises_error_and_does_not_silently_fallback(self):
        """Confirm Worker raises FileNotFoundError when template_path does not exist."""
        with self.assertRaises(FileNotFoundError):
            Worker(template_path="sops/non_existent_worker_template.md")

    def test_template_cached_at_init(self):
        """Confirm prompt template is loaded once and cached in self.prompt_template."""
        worker = Worker()
        self.assertIsNotNone(worker.prompt_template)
        self.assertIn("{{task}}", worker.prompt_template)
        self.assertIn("{{expected_output}}", worker.prompt_template)

if __name__ == "__main__":
    unittest.main()
