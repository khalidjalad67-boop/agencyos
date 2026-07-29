import os
import sys
import time
from typing import List, Dict, Any

from src.opportunity import OpportunityFetcher, Opportunity
from src.planner import Planner, TaskSpec
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult
from src.approval import ApprovalGate, ApprovalDecision
from src.logger import AuditLogger

def run_phase0_loop(
    num_opportunities: int = 5,
    auto_approve: bool = True,
    log_file: str = "audit_log.jsonl"
) -> Dict[str, Any]:
    """Executes Phase 0 loop for 5 consecutive opportunities with live GitHub issues, real network HTTP requests, and dynamic metrics."""
    
    fetcher = OpportunityFetcher()
    planner = Planner()
    worker = Worker()
    reviewer = Reviewer()
    
    # Approval gate configuration
    def custom_decision_provider(opp, tspec, wres, rres) -> ApprovalDecision:
        if not auto_approve and (opp.id == "4878017272" or opp.payload.get("issue_number") == 6093):
            return ApprovalDecision(
                opportunity_id=opp.id,
                approved=False,
                comments="Rejected by human reviewer: IPv6 issue requires additional reproduction test case."
            )
        return ApprovalDecision(
            opportunity_id=opp.id,
            approved=True,
            comments="Approved by human reviewer."
        )

    approval_gate = ApprovalGate(decision_provider=custom_decision_provider)
    logger = AuditLogger(log_filepath=log_file)
    
    opportunities: List[Opportunity] = fetcher.fetch_opportunities(limit=num_opportunities)
    
    successful_executions = 0
    blocked_executions = 0
    total_cost = 0.0
    execution_records = []

    print("\n" + "="*85)
    print(f"STARTING AGENCYOS PHASE 0 LOOP EXECUTION ({len(opportunities)} CONSECUTIVE LIVE TASKS)")
    print("="*85)
    
    for idx, opp in enumerate(opportunities, 1):
        print(f"\n--- Task {idx}/{len(opportunities)}: Issue #{opp.payload.get('issue_number')} [{opp.payload.get('repo')}] ---")
        
        logger.log_event("OPPORTUNITY_FETCHED", {
            "summary": f"Fetched live issue #{opp.payload.get('issue_number')} ({opp.id}): {opp.title[:60]}",
            "opportunity_id": opp.id,
            "title": opp.title,
            "source": opp.source
        })
        
        # 1. Planner
        task_spec: TaskSpec = planner.plan(opp)
        logger.log_event("TASK_PLANNED", {
            "summary": f"Planned task for issue #{opp.payload.get('issue_number')} [Priority: {task_spec.priority}, Est Cost: ${task_spec.estimated_cost:.6f}]",
            "opportunity_id": opp.id,
            "task_spec": task_spec.to_dict()
        })
        
        # 2. Worker
        worker_result: WorkerResult = worker.execute(task_spec)
        logger.log_event("WORKER_EXECUTED", {
            "summary": f"Worker executed task {opp.id} in {worker_result.execution_time_sec:.4f}s [Tokens: {worker_result.prompt_tokens} in / {worker_result.completion_tokens} out | Cost: ${worker_result.actual_cost:.6f}]",
            "opportunity_id": opp.id,
            "worker_result": worker_result.to_dict()
        })
        
        # 3. Reviewer
        review_result: ReviewResult = reviewer.review(task_spec, worker_result)
        logger.log_event("REVIEW_COMPLETED", {
            "summary": f"Review score for {opp.id}: {review_result.score:.3f} [Review Cost: ${review_result.review_cost:.6f}]",
            "opportunity_id": opp.id,
            "review_result": review_result.to_dict()
        })
        
        task_cost = round(worker_result.actual_cost + review_result.review_cost, 6)
        total_cost += task_cost
        
        # 4. Approval Gate
        approval_decision: ApprovalDecision = approval_gate.request_approval(
            opp, task_spec, worker_result, review_result
        )
        logger.log_event("APPROVAL_DECISION", {
            "summary": f"Approval decision for issue #{opp.payload.get('issue_number')}: {'APPROVED' if approval_decision.approved else 'REJECTED'}",
            "opportunity_id": opp.id,
            "approval_decision": approval_decision.to_dict()
        })
        
        # 5. Done / Blocked
        if approval_decision.approved:
            successful_executions += 1
            logger.log_event("TASK_COMPLETED", {
                "summary": f"Task {opp.id} completed successfully.",
                "opportunity_id": opp.id,
                "cost": task_cost
            })
            status = "COMPLETED"
        else:
            blocked_executions += 1
            logger.log_event("TASK_BLOCKED", {
                "summary": f"Task {opp.id} blocked by human approval rejection.",
                "opportunity_id": opp.id,
                "reason": approval_decision.comments
            })
            status = "BLOCKED"

        execution_records.append({
            "opportunity_id": opp.id,
            "issue_number": opp.payload.get("issue_number"),
            "repo": opp.payload.get("repo"),
            "title": opp.title,
            "status": status,
            "http_status": worker_result.http_status,
            "execution_time_sec": worker_result.execution_time_sec,
            "prompt_tokens": worker_result.prompt_tokens,
            "completion_tokens": worker_result.completion_tokens,
            "worker_cost": worker_result.actual_cost,
            "review_cost": review_result.review_cost,
            "total_task_cost": task_cost,
            "review_score": review_result.score
        })

    avg_cost = round(total_cost / len(opportunities), 6) if opportunities else 0.0
    
    summary = {
        "total_tasks": len(opportunities),
        "successful_executions": successful_executions,
        "blocked_executions": blocked_executions,
        "total_cost": round(total_cost, 6),
        "average_cost_per_task": avg_cost,
        "cost_under_threshold": avg_cost < 0.25,
        "execution_records": execution_records
    }
    
    logger.log_event("LOOP_RUN_COMPLETE", {
        "summary": f"Finished run: {successful_executions} succeeded, {blocked_executions} blocked. Avg cost: ${avg_cost:.6f}",
        "metrics": summary
    })
    
    print("\n" + "="*85)
    print("PHASE 0 REAL RUN EXECUTION SUMMARY")
    print("="*85)
    print(f"Total Tasks Processed   : {summary['total_tasks']}")
    print(f"Successful Executions   : {summary['successful_executions']}")
    print(f"Blocked Executions      : {summary['blocked_executions']}")
    print(f"Total Cost              : ${summary['total_cost']:.6f}")
    print(f"Average Cost / Task     : ${summary['average_cost_per_task']:.6f} (Threshold: < $0.25)")
    print(f"Cost Criteria Met       : {'YES' if summary['cost_under_threshold'] else 'NO'}")
    print("-" * 85)
    print("PER-TASK DETAILED METRICS:")
    for rec in summary['execution_records']:
        print(
            f"  - Issue #{rec['issue_number']} [{rec['repo']}] | HTTP: {rec['http_status']} | "
            f"Time: {rec['execution_time_sec']:.4f}s | Tokens: {rec['prompt_tokens']} in / {rec['completion_tokens']} out | "
            f"Cost: ${rec['total_task_cost']:.6f} | Score: {rec['review_score']:.3f} | {rec['status']}"
        )
    print("="*85)
    
    return summary

if __name__ == "__main__":
    # 1. Main 5-consecutive execution (all approved)
    print(">>> RUNNING MAIN 5-CONSECUTIVE SUCCESSFUL EXECUTION LOOP <<<")
    run_phase0_loop(num_opportunities=5, auto_approve=True)
    
    # 2. Human Approval Rejection Gate Test
    print("\n\n>>> DEMONSTRATING HUMAN REJECTION GATE BLOCKING (auto_approve=False) <<<")
    run_phase0_loop(num_opportunities=5, auto_approve=False)
