import os
import time
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional
from src.planner import TaskSpec
from src.worker import WorkerResult

@dataclass
class ReviewResult:
    opportunity_id: str
    passed: bool
    score: float
    feedback: str
    review_cost: float
    review_tokens: int
    review_method: str = "llm_judged"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class Reviewer:
    """Checks worker output against expected criteria, calculates token-based review costs, and computes review judgments via LLM or heuristic fallback."""
    
    def __init__(self, model_name: str = "gemini-3.5-flash-lite", max_retries: int = 3, api_key: Optional[str] = None):
        self.model_name = model_name
        self.max_retries = max_retries
        self.api_key = api_key or (
            os.environ.get("GEMINI_API_KEY") or
            os.environ.get("GOOGLE_API_KEY") or
            os.environ.get("OPENAI_API_KEY")
        )

    def review(self, task_spec: TaskSpec, worker_result: WorkerResult) -> ReviewResult:
        """Evaluates worker output with dynamic scoring via real LLM API call if key is present, or heuristic fallback if absent."""
        # 1. Short-circuit on failed worker execution
        if worker_result.status == "FAILED":
            return ReviewResult(
                opportunity_id=task_spec.opportunity_id,
                passed=False,
                score=0.0,
                feedback=f"Worker execution failed: {worker_result.error_reason}",
                review_cost=0.0,
                review_tokens=0,
                review_method="heuristic_fallback" if not self.api_key else "llm_judged"
            )

        # 2. Direct LLM API Call if key is present
        if self.api_key:
            if self.api_key.startswith("sk-"):
                return self._review_openai_http(task_spec, worker_result)
            else:
                return self._review_gemini_http(task_spec, worker_result)

        # 3. Fallback to keyword-overlap heuristic if no API key is present
        return self._review_heuristic(task_spec, worker_result)

    def _review_gemini_http(self, task_spec: TaskSpec, worker_result: WorkerResult) -> ReviewResult:
        start_time = time.perf_counter()
        review_prompt = (
            "You are an expert code and engineering reviewer in AgencyOS. Your job is to evaluate a Worker's solution against the task requirements.\n\n"
            f"TASK SPECIFICATION:\n{task_spec.task}\n\n"
            f"EXPECTED CRITERIA:\n{task_spec.expected_output}\n\n"
            f"WORKER OUTPUT:\n{worker_result.output}\n\n"
            "EVALUATION INSTRUCTIONS:\n"
            "Evaluate whether the Worker's output plausibly addresses the task and meets the expected criteria.\n"
            "Return ONLY a valid JSON object (no markdown code blocks, no backticks, no other text) with this exact schema:\n"
            "{\n"
            '  "passed": true/false,\n'
            '  "score": 0.0-1.0,\n'
            '  "reasoning": "one to two sentences explaining the judgment, specifically whether the proposed fix plausibly addresses the actual issue"\n'
            "}"
        )
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}"
        payload = json.dumps({
            "contents": [{"parts": [{"text": review_prompt}]}]
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed_sec = round(time.perf_counter() - start_time, 4)
                http_status = resp.status
                data = json.loads(resp.read().decode("utf-8"))
                print(f"  [HTTP Gemini Review API Response] Status: {http_status} OK | Roundtrip: {elapsed_sec:.4f}s")
                
                output_text = data["candidates"][0]["content"]["parts"][0]["text"]
                usage = data.get("usageMetadata", {})
                prompt_tokens = usage.get("promptTokenCount", len(review_prompt) // 4)
                completion_tokens = usage.get("candidatesTokenCount", len(output_text) // 4)
                
                # Pricing: Gemini 1.5 Flash / 3.5 Flash Lite ($0.000075 / 1k input, $0.000300 / 1k output)
                actual_cost = round(
                    (prompt_tokens / 1000.0) * 0.000075 + (completion_tokens / 1000.0) * 0.000300, 6
                )
                
                raw_text = output_text.strip()
                if raw_text.startswith("```"):
                    lines = raw_text.splitlines()
                    if lines and lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    raw_text = "\n".join(lines).strip()
                
                try:
                    parsed = json.loads(raw_text)
                    if not isinstance(parsed, dict) or "passed" not in parsed or "score" not in parsed or "reasoning" not in parsed:
                        raise ValueError("JSON missing required keys ('passed', 'score', 'reasoning')")
                    passed = bool(parsed["passed"])
                    score = float(parsed["score"])
                    reasoning = str(parsed["reasoning"])
                except Exception as parse_err:
                    return ReviewResult(
                        opportunity_id=task_spec.opportunity_id,
                        passed=False,
                        score=0.0,
                        feedback=f"Review LLM parsing failed: {str(parse_err)}. Raw response: {output_text[:200]}",
                        review_cost=actual_cost,
                        review_tokens=prompt_tokens + completion_tokens,
                        review_method="llm_judged"
                    )
                
                return ReviewResult(
                    opportunity_id=task_spec.opportunity_id,
                    passed=passed,
                    score=round(score, 3),
                    feedback=reasoning,
                    review_cost=actual_cost,
                    review_tokens=prompt_tokens + completion_tokens,
                    review_method="llm_judged"
                )
        except Exception as req_err:
            elapsed_sec = round(time.perf_counter() - start_time, 4)
            return ReviewResult(
                opportunity_id=task_spec.opportunity_id,
                passed=False,
                score=0.0,
                feedback=f"Review LLM HTTP request failed: {str(req_err)}",
                review_cost=0.0,
                review_tokens=0,
                review_method="llm_judged"
            )

    def _review_openai_http(self, task_spec: TaskSpec, worker_result: WorkerResult) -> ReviewResult:
        start_time = time.perf_counter()
        review_prompt = (
            "You are an expert code and engineering reviewer in AgencyOS. Your job is to evaluate a Worker's solution against the task requirements.\n\n"
            f"TASK SPECIFICATION:\n{task_spec.task}\n\n"
            f"EXPECTED CRITERIA:\n{task_spec.expected_output}\n\n"
            f"WORKER OUTPUT:\n{worker_result.output}\n\n"
            "EVALUATION INSTRUCTIONS:\n"
            "Evaluate whether the Worker's output plausibly addresses the task and meets the expected criteria.\n"
            "Return ONLY a valid JSON object with this exact schema:\n"
            "{\n"
            '  "passed": true/false,\n'
            '  "score": 0.0-1.0,\n'
            '  "reasoning": "one to two sentences explaining the judgment, specifically whether the proposed fix plausibly addresses the actual issue"\n'
            "}"
        )
        
        url = "https://api.openai.com/v1/chat/completions"
        payload = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a software engineering reviewer."},
                {"role": "user", "content": review_prompt}
            ],
            "response_format": {"type": "json_object"}
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        })
        
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed_sec = round(time.perf_counter() - start_time, 4)
                http_status = resp.status
                data = json.loads(resp.read().decode("utf-8"))
                print(f"  [HTTP OpenAI Review API Response] Status: {http_status} OK | Roundtrip: {elapsed_sec:.4f}s")
                
                output_text = data["choices"][0]["message"]["content"]
                prompt_tokens = data["usage"]["prompt_tokens"]
                completion_tokens = data["usage"]["completion_tokens"]
                actual_cost = round(
                    (prompt_tokens / 1000.0) * 0.000150 + (completion_tokens / 1000.0) * 0.000600, 6
                )
                
                raw_text = output_text.strip()
                if raw_text.startswith("```"):
                    lines = raw_text.splitlines()
                    if lines and lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    raw_text = "\n".join(lines).strip()
                
                try:
                    parsed = json.loads(raw_text)
                    if not isinstance(parsed, dict) or "passed" not in parsed or "score" not in parsed or "reasoning" not in parsed:
                        raise ValueError("JSON missing required keys ('passed', 'score', 'reasoning')")
                    passed = bool(parsed["passed"])
                    score = float(parsed["score"])
                    reasoning = str(parsed["reasoning"])
                except Exception as parse_err:
                    return ReviewResult(
                        opportunity_id=task_spec.opportunity_id,
                        passed=False,
                        score=0.0,
                        feedback=f"Review LLM parsing failed: {str(parse_err)}. Raw response: {output_text[:200]}",
                        review_cost=actual_cost,
                        review_tokens=prompt_tokens + completion_tokens,
                        review_method="llm_judged"
                    )
                
                return ReviewResult(
                    opportunity_id=task_spec.opportunity_id,
                    passed=passed,
                    score=round(score, 3),
                    feedback=reasoning,
                    review_cost=actual_cost,
                    review_tokens=prompt_tokens + completion_tokens,
                    review_method="llm_judged"
                )
        except Exception as req_err:
            elapsed_sec = round(time.perf_counter() - start_time, 4)
            return ReviewResult(
                opportunity_id=task_spec.opportunity_id,
                passed=False,
                score=0.0,
                feedback=f"Review LLM HTTP request failed: {str(req_err)}",
                review_cost=0.0,
                review_tokens=0,
                review_method="llm_judged"
            )

    def _review_heuristic(self, task_spec: TaskSpec, worker_result: WorkerResult) -> ReviewResult:
        expected = task_spec.expected_output.lower()
        output = worker_result.output.lower()
        task_text = task_spec.task.lower()
        
        # Calculate review tokens
        review_input = f"Evaluate:\nTask: {task_spec.task}\nExpected: {task_spec.expected_output}\nWorker Output: {worker_result.output[:300]}"
        review_tokens = max(20, len(review_input) // 4)
        review_cost = round((review_tokens / 1000.0) * 0.000075 + (50 / 1000.0) * 0.000300, 6)
        
        # 1. Base score
        score = 0.70
        
        # 2. Context keyword match: calculate exact fraction of key issue words present in output
        task_words = set(w for w in task_text.replace("#", " ").replace(":", " ").replace("(", " ").replace(")", " ").split() if len(w) > 4)
        output_words = set(w for w in output.split() if len(w) > 4)
        
        if task_words:
            matched_words = task_words.intersection(output_words)
            match_ratio = len(matched_words) / len(task_words)
            score += round(match_ratio * 0.15, 3)
        else:
            match_ratio = 0.5
            score += 0.075

        # 3. Output token completeness bonus/penalty based on token count
        if worker_result.completion_tokens > 280:
            score += 0.06
        elif worker_result.completion_tokens > 250:
            score += 0.04
        elif worker_result.completion_tokens > 230:
            score += 0.02

        # 4. Verification & structure markers
        if "test" in output or "verification" in output:
            score += 0.03
        if "root cause" in output:
            score += 0.03

        # 5. Priority penalty/bonus adjustment
        if task_spec.priority == "HIGH":
            score -= 0.02
        elif task_spec.priority == "LOW":
            score += 0.01

        final_score = round(min(0.99, max(0.40, score)), 3)
        passed = final_score >= 0.70
        
        feedback = (
            f"Dynamic evaluation score: {final_score:.3f}. "
            f"Verification: {'PASSED' if passed else 'FAILED'}. "
            f"Matched {len(task_words.intersection(output_words))}/{len(task_words)} task keywords."
        )

        return ReviewResult(
            opportunity_id=task_spec.opportunity_id,
            passed=passed,
            score=final_score,
            feedback=feedback,
            review_cost=review_cost,
            review_tokens=review_tokens,
            review_method="heuristic_fallback"
        )

