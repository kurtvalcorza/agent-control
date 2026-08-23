"""Public control-plane runtime facade."""

from .controller import ControlPlane as _ControlPlane
from .controller import RunBlocked, RunNotFound
from .errors import CheckpointInvalid
from .events import EventType
from .models import Run, RunState
from .projections import project_run


class ControlPlane(_ControlPlane):
    """Public runtime with fail-closed recovery and terminal transitions."""

    def _complete(self, run: Run) -> Run:
        if run.state != RunState.VERIFYING:
            run = self._transition(run, RunState.VERIFYING)
        run = self._transition(run, RunState.COMPLETED)
        self._append(
            run.id,
            EventType.RUN_COMPLETED,
            {"outcome": "success criteria satisfied"},
        )
        return self.get_run(run.id)

    def cancel_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            return run
        self._append(run.id, EventType.CANCEL_REQUESTED, {})
        run = self._transition(run, RunState.CANCELLING)
        compensation_failed = False
        if self.compensator is not None:
            for action in reversed(self.list_actions(run.id)):
                intent = action["intent"]
                receipt = action["receipt"]
                if receipt is None or not bool(intent["reversible"]):
                    continue
                self._append(
                    run.id,
                    EventType.COMPENSATION_ATTEMPTED,
                    {"intent_id": intent["id"], "receipt_id": receipt["id"]},
                )
                result = self.compensator.compensate(run_id=run.id, action=action)
                kind = (
                    EventType.COMPENSATION_SUCCEEDED
                    if result.success
                    else EventType.COMPENSATION_FAILED
                )
                self._append(
                    run.id,
                    kind,
                    {
                        "intent_id": intent["id"],
                        "detail": result.detail,
                        "metadata": result.metadata or {},
                    },
                )
                compensation_failed = compensation_failed or not result.success
        run = self._transition(self.get_run(run.id), RunState.CANCELLED)
        self._append(
            run.id,
            EventType.RUN_CANCELLED,
            {
                "outcome": (
                    "cancelled_with_compensation_failure"
                    if compensation_failed
                    else "cancelled"
                )
            },
        )
        return self.get_run(run.id)

    def resume_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run.state != RunState.PAUSED:
            raise RunBlocked("run is not paused")
        checkpoints = self.list_checkpoints(run_id)
        if checkpoints:
            checkpoint = checkpoints[-1]
            sequence = int(checkpoint["event_sequence"])
            projected = project_run(self._events(run_id)[:sequence])
            if projected is None:
                raise CheckpointInvalid("checkpoint references an empty projection")
            if self._checkpoint_digest(projected) != checkpoint["projected_state_digest"]:
                raise CheckpointInvalid("checkpoint digest does not match replayed state")

            unresolved = {
                str(item) for item in checkpoint.get("unresolved_action_intents", [])
            }
            records = self._intent_records(run_id)
            started = {
                str(event.payload["intent_id"])
                for event in self._events(run_id)
                if event.type == EventType.ACTION_STARTED
            }
            for intent_id in unresolved:
                record = records.get(intent_id)
                if record is None:
                    raise CheckpointInvalid("checkpoint references unknown action intent")
                if record["receipt"] is not None or record["rejected"]:
                    continue
                if intent_id in started:
                    raise RunBlocked(
                        "unsafe resume: started side effect has no verified receipt"
                    )
                if not record["authorized"]:
                    raise RunBlocked("unsafe resume: unresolved side-effect intent")

        self._append(run.id, EventType.RESUME_REQUESTED, {})
        target = RunState.READY if run.plan is not None else RunState.PLANNING
        run = self._transition(run, target)
        self._append(run.id, EventType.RUN_RESUMED, {"state": target.value})
        return self.get_run(run.id)


__all__ = ["ControlPlane", "RunBlocked", "RunNotFound"]
