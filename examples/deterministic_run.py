from agent_control_plane import (
    CapabilityDescriptor,
    CapabilityRegistry,
    ControlPlane,
    Goal,
    InProcessCapability,
    Plan,
    PlanNode,
    SQLiteEventStore,
)


class Planner:
    def create_plan(self, *, run_id, goal, version):
        return Plan(
            id=f"plan_{version}",
            run_id=run_id,
            version=version,
            nodes=(
                PlanNode("collect", "collect", ("echo",)),
                PlanNode("analyze", "analyze", ("echo",), dependencies=("collect",)),
                PlanNode(
                    "report",
                    "report",
                    ("echo",),
                    dependencies=("analyze",),
                    contributes_to=(goal.success_criteria[0],),
                ),
            ),
        )

    def revise_plan(self, *, run, reason, version):
        del reason
        return self.create_plan(run_id=run.id, goal=run.goal, version=version)


registry = CapabilityRegistry()
registry.register(
    InProcessCapability(CapabilityDescriptor("echo", "1", "example"), lambda _: True)
)
control = ControlPlane(
    store=SQLiteEventStore(":memory:"),
    planner=Planner(),
    capabilities=registry,
)
run = control.create_run(Goal("complete a three-step run", ("all steps verified",)))
run = control.run_until_blocked(run.id)
print(control.inspect_run(run.id))
