"""Public control-plane runtime facade."""

from .controller import ControlPlane as _ControlPlane
from .controller import RunBlocked, RunNotFound
from .errors import CheckpointInvalid
from .events import EventType
from .models import Run, RunState
from .projections import project_run


class ControlPlane(_ControlPlane):
    """Public runtime with fail-closed resume checks for started side effects."""

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
