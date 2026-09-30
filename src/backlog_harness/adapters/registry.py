from .codex.adapter import CodexAdapter


class AdapterRegistry:
    def __init__(self, factories=None):
        self.factories = {"codex": CodexAdapter} if factories is None else dict(factories)

    def resolve(self, name):
        if name not in self.factories:
            raise ValueError(f"Unsupported adapter: {name}")
        adapter = self.factories[name]()
        for method in (
            "validate_profile",
            "prepare_telemetry",
            "start_session",
            "resume_session",
            "observe_events",
            "reconcile",
            "request_interrupt",
        ):
            if not callable(getattr(adapter, method, None)):
                raise TypeError(f"Adapter {name} does not implement {method}")
        return adapter
