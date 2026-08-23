from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from .capabilities import CapabilityRegistry
from .models import BudgetLimit, Goal, Plan, RiskLevel, Run
from .policy import PolicyEngine
from .runtime import ControlPlane, RunBlocked
from .store import SQLiteEventStore


class _NoPlanner:
    def create_plan(self, *, run_id: str, goal: Goal, version: int) -> Plan:
        del run_id, goal, version
        raise RunBlocked("standalone CLI has no planner configured")

    def revise_plan(self, *, run: Run, reason: str, version: int) -> Plan:
        del run, reason, version
        raise RunBlocked("standalone CLI has no planner configured")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _print(value: Any, compact: bool) -> None:
    print(
        json.dumps(
            _jsonable(value),
            default=str,
            indent=None if compact else 2,
            sort_keys=True,
        )
    )


def _goal(path: str) -> tuple[Goal, RiskLevel, BudgetLimit]:
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    goal = Goal(
        objective=str(data["objective"]),
        success_criteria=tuple(str(item) for item in data["success_criteria"]),
        constraints=tuple(str(item) for item in data.get("constraints", [])),
        requested_outputs=tuple(str(item) for item in data.get("requested_outputs", [])),
        assumptions=tuple(str(item) for item in data.get("assumptions", [])),
    )
    risk = RiskLevel(str(data.get("risk_level", "low")))
    raw_budget = data.get("budget", {})
    if not isinstance(raw_budget, dict):
        raise ValueError("budget must be an object")
    budget = BudgetLimit(**raw_budget)
    return goal, risk, budget


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-control-plane")
    parser.add_argument("--db", default="control-plane.sqlite3")
    parser.add_argument("--json", action="store_true", dest="compact")
    groups = parser.add_subparsers(dest="group", required=True)

    run = groups.add_parser("run").add_subparsers(dest="command", required=True)
    create = run.add_parser("create")
    create.add_argument("goal")
    for name in ("inspect", "plan", "step", "execute", "pause", "resume", "cancel"):
        command = run.add_parser(name)
        command.add_argument("run_id")

    gate = groups.add_parser("gate").add_subparsers(dest="command", required=True)
    gate_list = gate.add_parser("list")
    gate_list.add_argument("run_id")
    for name in ("approve", "reject"):
        command = gate.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("gate_id")

    event = groups.add_parser("event").add_subparsers(dest="command", required=True)
    event_list = event.add_parser("list")
    event_list.add_argument("run_id")

    action = groups.add_parser("action").add_subparsers(dest="command", required=True)
    action_list = action.add_parser("list")
    action_list.add_argument("run_id")
    reconcile = action.add_parser("reconcile")
    reconcile.add_argument("run_id")
    reconcile.add_argument("intent_id")
    effect = reconcile.add_mutually_exclusive_group(required=True)
    effect.add_argument("--occurred", action="store_true")
    effect.add_argument("--no-effect", action="store_true")
    reconcile.add_argument("--cost", type=float, default=0.0)
    reconcile.add_argument("--effect", action="append", default=[])
    reconcile.add_argument("--rollback-ref")
    reconcile.add_argument("--artifact-ref", action="append", default=[])
    reconcile.add_argument("--precondition-ref", action="append", default=[])

    checkpoint = groups.add_parser("checkpoint").add_subparsers(
        dest="command", required=True
    )
    checkpoint_list = checkpoint.add_parser("list")
    checkpoint_list.add_argument("run_id")

    budget = groups.add_parser("budget").add_subparsers(dest="command", required=True)
    budget_list = budget.add_parser("list")
    budget_list.add_argument("run_id")
    for name in ("consume", "release"):
        command = budget.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("reservation_id")
        if name == "consume":
            command.add_argument("--cost", type=float, default=0.0)

    capability = groups.add_parser("capability").add_subparsers(
        dest="command", required=True
    )
    capability.add_parser("list")
    policy = groups.add_parser("policy").add_subparsers(dest="command", required=True)
    check = policy.add_parser("check")
    check.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteEventStore(args.db)
    registry = CapabilityRegistry()
    control = ControlPlane(store=store, planner=_NoPlanner(), capabilities=registry)
    try:
        result: Any
        if args.group == "run":
            if args.command == "create":
                goal, risk, budget = _goal(args.goal)
                result = control.create_run(goal, risk_level=risk, budget_limit=budget)
            elif args.command == "inspect":
                result = control.inspect_run(args.run_id)
            elif args.command == "plan":
                result = control.plan_run(args.run_id)
            elif args.command == "step":
                result = control.execute_next(args.run_id)
            elif args.command == "execute":
                result = control.run_until_blocked(args.run_id)
            elif args.command == "pause":
                result = control.pause_run(args.run_id)
            elif args.command == "resume":
                result = control.resume_run(args.run_id)
            else:
                result = control.cancel_run(args.run_id)
        elif args.group == "gate":
            if args.command == "list":
                result = control.list_pending_gates(args.run_id)
            elif args.command == "approve":
                result = control.approve_gate(args.run_id, args.gate_id)
            else:
                result = control.reject_gate(args.run_id, args.gate_id)
        elif args.group == "event":
            result = control.list_events(args.run_id)
        elif args.group == "action":
            if args.command == "list":
                result = control.list_actions(args.run_id)
            else:
                result = control.reconcile_action(
                    args.run_id,
                    args.intent_id,
                    occurred=bool(args.occurred),
                    actual_effects=tuple(args.effect),
                    rollback_ref=args.rollback_ref,
                    precondition_refs=tuple(args.precondition_ref),
                    artifact_refs=tuple(args.artifact_ref),
                    actual_cost_usd=float(args.cost),
                )
        elif args.group == "checkpoint":
            result = control.list_checkpoints(args.run_id)
        elif args.group == "budget":
            if args.command == "list":
                result = control.list_budget_reservations(args.run_id)
            else:
                result = control.resolve_budget_reservation(
                    args.run_id,
                    args.reservation_id,
                    consumed=args.command == "consume",
                    actual_cost_usd=float(getattr(args, "cost", 0.0)),
                    reason=f"operator {args.command}",
                )
        elif args.group == "capability":
            result = registry.list()
        else:
            document = yaml.safe_load(Path(args.path).read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("policy document root must be an object")
            engine = PolicyEngine.from_document(document)
            result = {"valid": True, "rules": [rule.id for rule in engine.rules]}
        _print(result, args.compact)
        return 0
    except (RunBlocked, ValueError, KeyError) as exc:
        _print({"error": str(exc)}, args.compact)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
