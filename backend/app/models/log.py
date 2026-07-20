from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StepLog:
    task_id: str
    step_name: str
    tool_name: str
    status: str
    latency_ms: int
    input_summary: str
    output_summary: str
    token_cost_summary: str
    error_message: str
    trace_id: str = ""
    step_id: str = ""
    parent_step_id: str = ""
    retry_index: int = 0
    idempotency_key: str = ""
    trace_summary: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)
