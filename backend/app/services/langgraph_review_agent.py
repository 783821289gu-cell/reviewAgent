"""这里是流程图中每个步骤真正执行的代码。

先记住几个反复出现的变量：

* ``graph_state``：LangGraph 传来的临时工作单。
* ``runtime``：LangGraph 提供的运行环境。
* ``context = runtime.context``：本次任务的工具箱和中间数据。
* ``state = context.state``：从数据库读取的完整任务状态。
* ``payload``：只属于当前一条风险的处理记录。
* ``finding``：风险分析模型给出的结论。
* ``evidence``：证据校验器给出的结果。

建议先读 ``run``，了解一份合同如何启动。然后读 ``analyze_risk``、
``criticize_risk`` 和 ``verify_evidence``，了解一条风险如何产生。

这里的 Analyzer、Planner 和 Critic 不是三个自由聊天的程序。
它们是主控流程在固定位置调用的工具，下一步只能从白名单中选择。
"""

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from threading import Lock
from time import perf_counter
from uuid import uuid4

from langgraph.types import Command, interrupt

from config import settings
from db.errors import RecoveryError
from models.contract import clause_from_dict, contract_document_from_dict
from models.log import StepLog
from models.planner import PlannerAction, PlannerReasonCode
from models.risk import CriticDecision, CriticReasonCode
from models.review import (
    AgentState,
    LLMMode,
    NodeExecutionTimeoutError,
    ReviewPosition,
    ReviewStatus,
    TaskCancelledError,
)
from parsers.pdf_parser import PdfParseError
from providers.llm_provider import (
    LLMOutputInvalidError,
    bind_llm_mode,
    reset_llm_mode,
)
from services.checkpoint_service import ReviewCheckpointManager
from services.log_service import (
    ToolExecutionControl,
    bind_execution_control,
    invoke_tool,
    reset_execution_control,
)
from services.memory_runtime import (
    RelatedMemoryStore,
    bind_memory_store,
    reset_memory_store,
)
from services.planner_service import PlannerOutputInvalidError
from services.review_workflow import (
    ReviewControlState,
    ReviewGraphState,
    build_review_control_graph,
    build_review_graph,
)
from services.review_context_pipeline import (
    ReviewContextPipeline,
    context_work_items,
)
from services.risk_critic import CriticOutputInvalidError
from services.retrieval_runtime import (
    RelatedClauseRetriever,
    bind_related_clause_retriever,
    reset_related_clause_retriever,
)
from services.runtime_log_service import write_runtime_log
from tools.registry import tool_registry

from services.review_agent_support import (
    EXECUTION_INTERRUPTS,
    RECOVERABLE_STATUS_ORDER,
    ReviewAgentSupport,
    _analysis_result_key,
    _append_critic_trace,
    _append_planner_trace,
    _before,
    _context_progress_item,
    _evidence_planner_reason,
    _evidence_verification_summary,
    _finding_progress_item,
    _human_review_progress,
    _invoke_planner,
    _is_low_confidence,
    _manual_review_candidate,
    _manual_recovery_checkpoint,
    _materialize_manual_review_candidates,
    _planner_retry_count,
    _rebuild_review_context,
    _requires_manual_review,
    _restored_analysis_prefix,
    _restored_clause_prefix,
    _restored_evidence_completion_ids,
    _retrieval_is_insufficient,
    _review_context_progress_item,
    _validate_recovery_checkpoint,
)


_COMPATIBILITY_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="review-langgraph",
)
_INITIAL_CONTROL_INPUT = object()


@dataclass
class _ReviewRuntime:
    """一份合同从开始到结束共用的“工具箱”。

    Graph State 适合传少量流程数据。这个工具箱保存体积更大的内容，例如文件
    字节、解析后的合同和数据库状态。节点通过 ``runtime.context`` 取得它。
    """

    # 当前执行这些步骤的 Agent 对象。
    agent: "ReviewOrchestratorAgent"
    # 当前任务编号和原始上传文件。
    task_id: str
    content: bytes
    # 数据库里的完整任务，以及本次执行产生的日志。
    state: AgentState
    logs: list[StepLog]
    # 负责检查取消和单节点超时。
    execution_control: ToolExecutionControl
    # 下列字段按主流程顺序逐步填写。
    document: object | None = None
    contract_type: str = ""
    clauses: list[object] = field(default_factory=list)
    clauses_payload: list[dict] = field(default_factory=list)
    rule_matches: list[dict] = field(default_factory=list)
    review_contexts: list[dict] = field(default_factory=list)
    # 避免同一段文本在一次任务里重复计算向量。
    embedding_cache: dict = field(default_factory=dict)
    # key 是原始顺序，value 是已经完成的风险分支结果。
    branch_results: dict[int, dict] = field(default_factory=dict)


class ReviewOrchestratorAgent(ReviewAgentSupport):
    """整个审查流程的总负责人。

    ``_graph`` 负责实际审查。``_control_graph`` 负责人工复核的暂停和继续。
    两张图分开后，Checkpoint 只保存暂停位置，不复制整份合同。
    """

    def __init__(
        self,
        event_store=None,
        *,
        node_timeout_seconds: float = 90.0,
        llm_max_concurrency: int = 2,
        checkpoint_manager: ReviewCheckpointManager | None = None,
        related_clause_retriever: RelatedClauseRetriever | None = None,
        memory_store: RelatedMemoryStore | None = None,
    ):
        init_kwargs = {
            "node_timeout_seconds": node_timeout_seconds,
            "llm_max_concurrency": llm_max_concurrency,
        }
        if event_store is None:
            super().__init__(**init_kwargs)
        else:
            super().__init__(event_store, **init_kwargs)
        self._checkpoint_manager = (
            checkpoint_manager or ReviewCheckpointManager.in_memory()
        )
        self._related_clause_retriever = related_clause_retriever
        self._memory_store = memory_store
        self._context_pipeline = ReviewContextPipeline(self._batch_executor)
        # 把 self 传进去，表示图中的 parse_document 节点会调用
        # self.parse_document，其他同名节点也是如此。
        self._graph = build_review_graph(self, _ReviewRuntime)
        self._control_graph = build_review_control_graph(
            self,
            _ReviewRuntime,
            self._checkpoint_manager.saver,
        )
        self._execution_futures: set[Future] = set()
        self._execution_futures_lock = Lock()

    @property
    def graph(self):
        return self._graph

    @property
    def control_graph(self):
        return self._control_graph

    @property
    def checkpoint_backend(self) -> str:
        return self._checkpoint_manager.backend

    @property
    def memory_store(self) -> RelatedMemoryStore | None:
        return self._memory_store

    def warmup(self) -> None:
        if self._related_clause_retriever is not None:
            self._related_clause_retriever.warmup()

    def close(self) -> None:
        with self._execution_futures_lock:
            active_futures = tuple(self._execution_futures)
        if active_futures:
            wait(active_futures)
        try:
            if self._related_clause_retriever is not None:
                self._related_clause_retriever.close()
        finally:
            try:
                if self._memory_store is not None:
                    self._memory_store.close()
            finally:
                self._checkpoint_manager.close()

    def start(
        self,
        file_name: str,
        file_type: str,
        content: bytes,
        review_position: ReviewPosition,
        llm_mode: LLMMode = LLMMode.LOCAL_STRUCTURED,
    ) -> AgentState:
        state = self.event_store.create_task(
            file_name,
            file_type,
            review_position,
            content=content,
            llm_mode=llm_mode,
        )
        state = self.event_store.update_task(
            state.task_id,
            ReviewStatus.UPLOAD_RECEIVED,
            "上传已接收，开始执行文档解析。",
            step_name="upload_received",
        )
        execution_owner = self._acquire_execution(state.task_id)
        self._submit_execution(
            state.task_id,
            content,
            execution_owner,
        )
        return state

    def recover_pending_tasks(self, async_mode: bool = True) -> list[str]:
        if self.event_store.persistence is None:
            return []
        recovered_task_ids = []
        for state in self.event_store.pending_tasks():
            if state.status == ReviewStatus.CANCEL_REQUESTED:
                self.event_store.finalize_cancel(state.task_id)
                continue
            try:
                execution_owner = self._acquire_execution(state.task_id)
            except RuntimeError:
                continue
            try:
                _validate_recovery_checkpoint(state)
                content = self.event_store.persistence.load_upload(state.task_id)
                self.event_store.record_recovery(state.task_id)
            except RecoveryError as exc:
                self.event_store.update_task(
                    state.task_id,
                    ReviewStatus.NEED_MANUAL_REVIEW,
                    str(exc),
                    step_name="recovery_blocked",
                )
                self.event_store.release_execution(
                    state.task_id,
                    execution_owner,
                )
                continue
            except Exception:
                self.event_store.release_execution(
                    state.task_id,
                    execution_owner,
                )
                raise

            recovered_task_ids.append(state.task_id)
            if async_mode:
                self._submit_execution(
                    state.task_id,
                    content,
                    execution_owner,
                    control_input=None,
                )
            else:
                self.run(
                    state.task_id,
                    content,
                    execution_owner,
                    True,
                    None,
                )
        return recovered_task_ids

    def recover_task(
        self,
        task_id: str,
        *,
        resume_from: ReviewStatus | None,
        operator_action: str,
        reason: str,
    ) -> AgentState:
        if self.event_store.persistence is None:
            raise ValueError("manual recovery requires persistent task storage")
        execution_owner = self._acquire_execution(task_id)
        try:
            state = self.prepare_manual_recovery(
                task_id,
                resume_from=resume_from,
                operator_action=operator_action,
                reason=reason,
            )
            content = self.event_store.persistence.load_upload(task_id)
            self._submit_execution(
                task_id,
                content,
                execution_owner,
                control_input=None,
            )
            return state
        except Exception:
            self.event_store.release_execution(task_id, execution_owner)
            raise

    def prepare_manual_recovery(
        self,
        task_id: str,
        *,
        resume_from: ReviewStatus | None,
        operator_action: str,
        reason: str,
    ) -> AgentState:
        if self.event_store.persistence is None:
            raise ValueError("manual recovery requires persistent task storage")
        current = self.event_store.get_task(task_id)
        if current is None:
            raise ValueError(f"task not found: {task_id}")
        expected = _manual_recovery_checkpoint(current)
        if resume_from is not None and resume_from != expected:
            raise ValueError(
                f"recovery checkpoint must be {expected.value} "
                f"for {current.status.value}"
            )
        candidate = replace(current, status=expected)
        _validate_recovery_checkpoint(candidate)
        self.event_store.persistence.load_upload(task_id)
        return self.event_store.record_manual_recovery(
            task_id,
            resume_from=expected,
            operator_action=operator_action,
            reason=reason,
        )

    def resume_human_review(
        self,
        task_id: str,
        resume_payload: dict,
    ) -> AgentState:
        """用户处理一条风险后，尝试继续之前暂停的流程。

        ``snapshot`` 是 LangGraph 保存的暂停现场。
        如果现场里没有 ``interrupt``，说明流程没有暂停，直接返回即可。
        如果有，就用 ``Command(resume=...)`` 唤醒同一个任务。
        """

        current = self.event_store.get_task(task_id)
        if current is None:
            raise ValueError(f"task not found: {task_id}")
        config = self._control_config(task_id, current)
        snapshot = self._control_graph.get_state(config)
        interrupts = [
            item
            for task in snapshot.tasks
            for item in task.interrupts
        ]
        if not interrupts:
            return current
        execution_owner = self._acquire_execution(task_id)
        try:
            self.run(
                task_id,
                b"",
                execution_owner,
                True,
                Command(resume=dict(resume_payload)),
            )
            resumed = self.event_store.get_task(task_id)
            if resumed is None:
                raise ValueError(f"task not found after resume: {task_id}")
            return resumed
        except Exception:
            self.event_store.release_execution(task_id, execution_owner)
            raise

    def run(
        self,
        task_id: str,
        content: bytes,
        execution_owner: str = "",
        execution_acquired: bool = False,
        control_input=_INITIAL_CONTROL_INPUT,
    ) -> None:
        """一份合同的总执行入口。

        新任务会这样走：

        1. 从数据库取出任务。
        2. 创建本次执行的工具箱 ``runtime``。
        3. 运行合同审查主图。
        4. 有风险待人工处理就暂停，否则结束。

        人工反馈后再次进入这里时，``control_input`` 是 Command。
        这时只继续暂停的控制图，不会重新解析合同。
        """

        # resolved_owner 是本次执行的唯一编号。
        # 同一任务已有执行者时，新的执行请求会直接返回，避免重复调用模型。
        resolved_owner = execution_owner or f"exec_{uuid4().hex}"
        if not execution_acquired and not self.event_store.try_acquire_execution(
            task_id,
            resolved_owner,
        ):
            return
        state = self.event_store.get_task(task_id)
        if state is None:
            self.event_store.release_execution(task_id, resolved_owner)
            return

        execution_control = ToolExecutionControl(
            cancel_check=lambda: self.event_store.raise_if_cancelled(task_id),
            task_started_at=perf_counter(),
            task_timeout_seconds=None,
            node_timeout_seconds=self.node_timeout_seconds,
            execution_retry_index=state.recovery_count,
        )
        runtime = _ReviewRuntime(
            agent=self,
            task_id=task_id,
            content=content,
            state=state,
            logs=[StepLog(**item) for item in (state.logs or [])],
            execution_control=execution_control,
        )
        # bind_* 让深层工具知道当前任务使用哪个模型、检索器和 Memory。
        # 返回的 token 用于 finally 中恢复原值，避免不同任务互相串配置。
        llm_mode_token = bind_llm_mode(state.llm_mode.value)
        control_token = bind_execution_control(execution_control)
        retrieval_token = bind_related_clause_retriever(
            self._related_clause_retriever,
            task_id,
        )
        memory_token = bind_memory_store(self._memory_store)
        write_runtime_log(
            "task_started",
            task_id=task_id,
            trace_id=state.trace_id,
            llm_mode=state.llm_mode.value,
            node_timeout_seconds=self.node_timeout_seconds,
            task_timeout_seconds=None,
            task_timeout_owner="rq",
            llm_max_concurrency=self._batch_width(state),
            recovery_count=state.recovery_count,
            workflow="langgraph",
        )
        try:
            if isinstance(control_input, Command):
                # 这是人工反馈后的调用：从书签位置继续。
                self._stream_control(runtime, control_input)
            else:
                # 这是第一次执行：先记录“正在执行”，再跑完整主图。
                self._stream_control(
                    runtime,
                    self._control_state(runtime.state, "execute"),
                )
                self._execute_business_graph(runtime)
                current = self.event_store.get_task(task_id)
                if current is None:
                    raise ValueError(
                        f"task not found after graph execution: {task_id}"
                    )
                pending_risk_ids = self._pending_risk_ids(current)
                phase = (
                    "human_review"
                    if current.status == ReviewStatus.HUMAN_REVIEW_PENDING
                    and pending_risk_ids
                    else "complete"
                )
                # 仍有风险没处理时，下一行会暂停在 await_human_review。
                self._stream_control(
                    runtime,
                    self._control_state(current, phase),
                )
        except TaskCancelledError:
            self.event_store.finalize_cancel(
                task_id,
                logs=[log.to_dict() for log in self._all_logs(runtime)],
            )
        except NodeExecutionTimeoutError as exc:
            current = self.event_store.get_task(task_id)
            resume_from_status = (
                current.status.value
                if current is not None and current.status in RECOVERABLE_STATUS_ORDER
                else ReviewStatus.UPLOAD_RECEIVED.value
            )
            self._update_terminal_or_cancel(
                task_id,
                ReviewStatus.NODE_TIMEOUT,
                f"Node timeout at {exc.step_name}; manual recovery is required.",
                self._all_logs(runtime),
                step_name="node_timeout",
                last_timeout={
                    "type": "node",
                    "step_name": exc.step_name,
                    "elapsed_seconds": round(exc.elapsed_seconds, 4),
                    "limit_seconds": exc.limit_seconds,
                    "resume_from_status": resume_from_status,
                },
            )
        except RecoveryError as exc:
            self._update_terminal_or_cancel(
                task_id,
                ReviewStatus.NEED_MANUAL_REVIEW,
                str(exc),
                self._all_logs(runtime),
                step_name="recovery_blocked",
            )
        except PdfParseError as exc:
            self._update_terminal_or_cancel(
                task_id,
                ReviewStatus.PARSE_FAILED,
                f"PDF 解析失败：{exc}",
                self._all_logs(runtime),
                step_name="parse_failed",
                tool_name="parse_document",
            )
        except Exception as exc:
            write_runtime_log(
                "langgraph_execution_failed",
                task_id=task_id,
                trace_id=state.trace_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc)[:500],
            )
            if isinstance(control_input, Command):
                raise
            self._record_unexpected_failure(task_id, self._all_logs(runtime))
        finally:
            final_state = self.event_store.get_task(task_id)
            write_runtime_log(
                "task_finished",
                task_id=task_id,
                trace_id=state.trace_id,
                status=(
                    final_state.status.value
                    if final_state is not None
                    else "TASK_MISSING"
                ),
                elapsed_ms=int(
                    (perf_counter() - execution_control.task_started_at) * 1000
                ),
                log_count=(
                    len(final_state.logs or [])
                    if final_state is not None
                    else len(self._all_logs(runtime))
                ),
                workflow="langgraph",
            )
            reset_execution_control(control_token)
            reset_llm_mode(llm_mode_token)
            reset_related_clause_retriever(retrieval_token)
            reset_memory_store(memory_token)
            self.event_store.release_execution(task_id, resolved_owner)

    def checkpoint_phase(
        self,
        graph_state: ReviewControlState,
        runtime,
    ) -> dict:
        """让控制图经过一个可以保存书签的步骤。

        返回空字典表示不修改 State。LangGraph 仍会记录这个步骤已经执行。
        """

        return {}

    def _execute_business_graph(self, context: _ReviewRuntime) -> None:
        """运行合同审查主图，并边运行边接收节点更新。

        ``initial_state`` 是交给第一个节点的初始工作单。
        ``stream_mode="updates"`` 表示每完成一个节点就返回一次变化。
        因此前端不用等整份合同结束，能逐步看到风险处理数量。
        """

        # 主图启动时只需要任务编号、停止标记和空结果列表。
        initial_state: ReviewGraphState = {
            "task_id": context.task_id,
            "terminal": False,
            "risk_results": [],
        }
        for update in self._graph.stream(
            initial_state,
            context=context,
            config={"max_concurrency": self._batch_width(context.state)},
            stream_mode="updates",
        ):
            # update 的 key 是刚完成的节点名。
            # 这里只读取 risk_subgraph，因为它能提供“第几条风险完成”的进度。
            risk_update = update.get("risk_subgraph")
            if not isinstance(risk_update, dict):
                continue
            for result in risk_update.get("risk_results") or []:
                self._record_branch_progress(context, result)

    def await_human_review(
        self,
        graph_state: ReviewControlState,
        runtime,
    ) -> dict:
        """有未处理风险时暂停，等用户在界面上处理。

        ``interrupt`` 类似“保存并退出”。传入的字典告诉外部正在等哪些风险。
        用户提交反馈后，``Command(resume=...)`` 会让这个方法重新执行。
        """

        context = runtime.context
        current = self.event_store.get_task(context.task_id)
        if current is None:
            raise ValueError(f"task not found during human review: {context.task_id}")
        pending_risk_ids = self._pending_risk_ids(current)
        if pending_risk_ids:
            interrupt(
                {
                    "task_id": context.task_id,
                    "phase": "human_review",
                    "pending_risk_ids": pending_risk_ids,
                    "recovery_count": current.recovery_count,
                }
            )
            # 用户反馈已经先写入数据库，所以继续后必须重新读取任务。
            # 不能使用暂停前的 current，否则看不到刚处理的风险。
            current = self.event_store.get_task(context.task_id)
            if current is None:
                raise ValueError(
                    f"task not found after human review resume: {context.task_id}"
                )
            pending_risk_ids = self._pending_risk_ids(current)
        return {
            "phase": "human_review" if pending_risk_ids else "complete",
            "status": current.status.value,
            "pending_risk_ids": pending_risk_ids,
            "recovery_count": current.recovery_count,
        }

    def bootstrap(self, graph_state: ReviewGraphState, runtime) -> dict:
        """主图的起点：确认任务已进入“上传完成”状态。"""

        context = runtime.context
        if context.state.status == ReviewStatus.START:
            context.state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.UPLOAD_RECEIVED,
                "上传已接收，开始执行文档解析。",
                step_name="upload_received",
            )
        return {"terminal": False}

    def parse_document(self, graph_state: ReviewGraphState, runtime) -> dict:
        """把 PDF/DOCX 变成程序可以处理的文档对象。

        ``_before`` 用来判断这一步以前是否完成。
        已完成时直接使用数据库结果，恢复任务不会重复解析文件。
        """

        context = runtime.context
        state = context.state
        if _before(state.status, ReviewStatus.DOCUMENT_PARSED):
            state = self._publish_progress(
                state,
                context.logs,
                stage="document_parse",
                stage_label="解析合同文档",
                completed=0,
                total=1,
                current_item="document",
                message="正在解析合同文档。",
                tool_name="parse_document",
            )
            context.document = invoke_tool(
                context.task_id,
                tool_registry,
                "parse_document",
                {
                    "file_id": context.task_id,
                    "file_name": state.file_name,
                    "file_type": state.file_type,
                    "content": context.content,
                },
                context.logs,
                step_name="document_parse",
                execution_control=context.execution_control,
                node_timeout_seconds=(
                    max(
                        self.node_timeout_seconds,
                        settings.docling_document_timeout_seconds + 30,
                    )
                    if state.file_type == "pdf"
                    else None
                ),
            )
            state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.DOCUMENT_PARSED,
                "文档解析完成。",
                step_name="document_parsed",
                tool_name="parse_document",
                document=context.document.to_dict(),
                logs=[log.to_dict() for log in context.logs],
            )
            context.state = state
        elif state.document:
            context.document = contract_document_from_dict(state.document)
        return {"terminal": False}

    def classify_contract(self, graph_state: ReviewGraphState, runtime) -> dict:
        """判断上传文件是不是目前支持的 NDA 合同。

        不是 NDA 时返回 ``terminal=True``。后面的条款和风险节点就不会执行。
        """

        context = runtime.context
        state = context.state
        if _before(state.status, ReviewStatus.CONTRACT_TYPE_CLASSIFIED):
            if context.document is None:
                raise RecoveryError("持久化文档缺失，无法执行合同类型识别。")
            try:
                state = self._publish_progress(
                    state,
                    context.logs,
                    stage="contract_type_classification",
                    stage_label="识别合同类型",
                    completed=0,
                    total=1,
                    current_item="document",
                    message="正在识别合同类型。",
                    tool_name="classify_contract_type",
                    document=context.document.to_dict(),
                )
                classification = invoke_tool(
                    context.task_id,
                    tool_registry,
                    "classify_contract_type",
                    {"document": context.document},
                    context.logs,
                    step_name="contract_type_classification",
                    execution_control=context.execution_control,
                )
            except EXECUTION_INTERRUPTS:
                raise
            except Exception as exc:
                return self._terminal(
                    context,
                    ReviewStatus.NEED_MANUAL_REVIEW,
                    f"合同类型识别失败，需要人工确认：{exc}",
                    step_name="contract_type_classification_failed",
                    tool_name="classify_contract_type",
                )

            decision = classification["decision"]
            if decision == "UNSUPPORTED_CONTRACT_TYPE":
                return self._terminal(
                    context,
                    ReviewStatus.UNSUPPORTED_CONTRACT_TYPE,
                    "当前文件不是支持的 NDA/保密协议，已停止正式风险审查。",
                    step_name="unsupported_contract_type",
                    tool_name="classify_contract_type",
                    contract_classification=classification,
                    risk_findings=[],
                )
            if decision == "NEED_MANUAL_REVIEW":
                return self._terminal(
                    context,
                    ReviewStatus.NEED_MANUAL_REVIEW,
                    "合同类型特征不足，无法确认是 NDA/保密协议，需要人工复核。",
                    step_name="contract_type_manual_review",
                    tool_name="classify_contract_type",
                    contract_classification=classification,
                    risk_findings=[],
                )
            state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.CONTRACT_TYPE_CLASSIFIED,
                "已确认合同类型为 NDA，开始执行条款结构化。",
                step_name="contract_type_classified",
                tool_name="classify_contract_type",
                contract_classification=classification,
                logs=[log.to_dict() for log in context.logs],
            )
            context.state = state
        else:
            classification = state.contract_classification

        context.contract_type = str(
            (classification or {}).get("contract_type", "")
        )
        if context.contract_type != "NDA":
            raise RecoveryError(
                "持久化合同类型不是 NDA，无法进入 NDA 风险审查链路。"
            )
        return {"terminal": False}

    def structure_clauses(self, graph_state: ReviewGraphState, runtime) -> dict:
        """把整份合同拆成一条条带编号的条款。

        后面的检索、风险和证据都使用同一个 ``clause_id`` 指向原文。
        """

        context = runtime.context
        state = context.state
        if _before(state.status, ReviewStatus.CLAUSES_STRUCTURED):
            if context.document is None:
                raise RecoveryError("持久化文档缺失，无法从条款结构化节点恢复。")
            try:
                clauses = invoke_tool(
                    context.task_id,
                    tool_registry,
                    "extract_clauses",
                    {"document": context.document},
                    context.logs,
                    step_name="clause_structure",
                    execution_control=context.execution_control,
                )
                completed_clauses = _restored_clause_prefix(
                    clauses,
                    state.clauses or [],
                )
                total = len(clauses)
                completed = len(completed_clauses)
                state = self._publish_clause_progress(
                    context,
                    state,
                    clauses,
                    completed_clauses,
                    completed,
                )
                width = self._batch_width(state)
                for batch_start in range(completed, total, width):
                    batch = clauses[batch_start : min(batch_start + width, total)]
                    outcomes = self._invoke_independent_batch(
                        context.task_id,
                        batch,
                        tool_name="extract_key_fields",
                        input_builder=lambda item: {"clause": item},
                        step_name="key_field_extract",
                        execution_control=context.execution_control,
                        parent_step_id=context.logs[-1].step_id if context.logs else "",
                        max_workers=width,
                    )
                    self._merge_batch_logs(context.logs, outcomes)
                    for outcome in outcomes:
                        if outcome.error is not None:
                            raise outcome.error
                        completed_clauses.append(
                            replace(outcome.item, key_fields=outcome.output)
                        )
                        completed += 1
                        state = self._publish_clause_progress(
                            context,
                            state,
                            clauses,
                            completed_clauses,
                            completed,
                        )
            except LLMOutputInvalidError as exc:
                return self._terminal(
                    context,
                    ReviewStatus.LLM_OUTPUT_INVALID,
                    f"关键字段结构化输出无效：{exc}",
                    step_name="llm_output_invalid",
                    tool_name="extract_key_fields",
                    analysis_results=[],
                    risk_findings=[],
                )
            context.clauses = completed_clauses
            context.state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.CLAUSES_STRUCTURED,
                "合同已解析并完成条款结构化，开始检索 NDA Playbook 规则。",
                step_name="clauses_structured",
                tool_name="extract_key_fields",
                clauses=[clause.to_dict() for clause in completed_clauses],
                logs=[log.to_dict() for log in context.logs],
            )
        else:
            context.clauses = [
                clause_from_dict(item) for item in (state.clauses or [])
            ]
        context.clauses_payload = [
            clause.to_dict() for clause in context.clauses
        ]
        return {"terminal": False}

    def retrieve_playbook(self, graph_state: ReviewGraphState, runtime) -> dict:
        """为每条合同条款查找需要遵守的 Playbook 审查规则。"""

        context = runtime.context
        state = context.state
        if _before(state.status, ReviewStatus.PLAYBOOK_RETRIEVED):
            try:
                matches = []
                total = len(context.clauses)
                state = self._publish_progress(
                    state,
                    context.logs,
                    stage="playbook_retrieval",
                    stage_label="检索 Playbook 规则",
                    completed=0,
                    total=total,
                    current_item=(
                        context.clauses[0].clause_id if context.clauses else ""
                    ),
                    message=(
                        f"正在检索 {context.clauses[0].clause_id} 的规则（0/{total}）。"
                        if context.clauses
                        else "没有条款需要检索 Playbook。"
                    ),
                    tool_name="retrieve_playbook_rules",
                    matched_rules=[],
                )
                width = self._batch_width(state)
                for batch_start in range(0, total, width):
                    batch = context.clauses[
                        batch_start : min(batch_start + width, total)
                    ]
                    outcomes = self._invoke_independent_batch(
                        context.task_id,
                        batch,
                        tool_name="retrieve_playbook_rules",
                        input_builder=lambda clause: {
                            "contract_type": context.contract_type,
                            "clause_type": clause.clause_type,
                            "key_fields": clause.key_fields,
                            "review_position": state.review_position.value,
                        },
                        step_name="playbook_retrieval",
                        execution_control=context.execution_control,
                        parent_step_id=(
                            context.logs[-1].step_id if context.logs else ""
                        ),
                        max_workers=width,
                    )
                    self._merge_batch_logs(context.logs, outcomes)
                    for outcome in outcomes:
                        if outcome.error is not None:
                            raise outcome.error
                        clause = outcome.item
                        matches.append(
                            {
                                "clause_id": clause.clause_id,
                                "clause_type": clause.clause_type,
                                "clause_title": clause.title,
                                "matched_rules": outcome.output,
                            }
                        )
                        completed = len(matches)
                        next_item = (
                            context.clauses[completed].clause_id
                            if completed < total
                            else ""
                        )
                        state = self._publish_progress(
                            state,
                            context.logs,
                            stage="playbook_retrieval",
                            stage_label="检索 Playbook 规则",
                            completed=completed,
                            total=total,
                            current_item=next_item,
                            message=(
                                f"正在检索 {next_item} 的规则（{completed}/{total}）。"
                                if next_item
                                else f"Playbook 规则检索完成（{completed}/{total}）。"
                            ),
                            tool_name="retrieve_playbook_rules",
                            matched_rules=matches,
                        )
            except EXECUTION_INTERRUPTS:
                raise
            except Exception as exc:
                return self._terminal(
                    context,
                    ReviewStatus.RETRIEVAL_FAILED,
                    f"Playbook 规则检索失败：{exc}",
                    step_name="playbook_failed",
                    tool_name="retrieve_playbook_rules",
                )
            context.rule_matches = matches
            context.state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.PLAYBOOK_RETRIEVED,
                "NDA Playbook 规则检索完成，开始构建风险分析上下文。",
                step_name="playbook_retrieved",
                tool_name="retrieve_playbook_rules",
                matched_rules=matches,
                logs=[log.to_dict() for log in context.logs],
            )
        else:
            context.rule_matches = list(state.matched_rules or [])
        return {"terminal": False}

    def build_context(self, graph_state: ReviewGraphState, runtime) -> dict:
        """把“当前条款 + 命中规则 + 相关条款 + Memory”装成模型输入。

        每组输入叫一个 ``review_context``。
        完成后再把它们转成 ``risk_items``，交给 Send 分别处理。
        """

        context = runtime.context
        state = context.state
        if _before(state.status, ReviewStatus.CONTEXT_BUILT):
            try:
                contexts = []
                work_items = context_work_items(
                    context.clauses_payload,
                    context.rule_matches,
                )
                total = len(work_items)
                current = (
                    _context_progress_item(
                        work_items[0].clause,
                        work_items[0].rule,
                    )
                    if work_items
                    else ""
                )
                state = self._publish_progress(
                    state,
                    context.logs,
                    stage="context_build",
                    stage_label="构建风险分析上下文",
                    completed=0,
                    total=total,
                    current_item=current,
                    message=(
                        f"正在构建 {current} 的上下文（0/{total}）。"
                        if current
                        else "没有命中规则需要构建上下文。"
                    ),
                    review_contexts=[],
                )
                width = self._batch_width(state)
                for batch_start in range(0, total, width):
                    batch = work_items[
                        batch_start : min(batch_start + width, total)
                    ]
                    outcomes = self._context_pipeline.build_batch(
                        task_id=context.task_id,
                        contract_type=context.contract_type,
                        review_position=state.review_position.value,
                        clauses=context.clauses_payload,
                        work_items=batch,
                        execution_control=context.execution_control,
                        parent_step_id=(
                            context.logs[-1].step_id if context.logs else ""
                        ),
                        embedding_cache=context.embedding_cache,
                        db_path=self.event_store.db_path,
                        max_workers=width,
                    )
                    self._merge_batch_logs(context.logs, outcomes)
                    for outcome in outcomes:
                        if outcome.error is not None:
                            raise outcome.error
                        if outcome.review_context is None:
                            raise RuntimeError(
                                "context pipeline returned no context"
                            )
                        contexts.append(outcome.review_context)
                        completed = len(contexts)
                        next_item = (
                            _context_progress_item(
                                work_items[completed].clause,
                                work_items[completed].rule,
                            )
                            if completed < total
                            else ""
                        )
                        state = self._publish_progress(
                            state,
                            context.logs,
                            stage="context_build",
                            stage_label="构建风险分析上下文",
                            completed=completed,
                            total=total,
                            current_item=next_item,
                            message=(
                                f"正在构建 {next_item} 的上下文（{completed}/{total}）。"
                                if next_item
                                else f"风险分析上下文构建完成（{completed}/{total}）。"
                            ),
                            review_contexts=contexts,
                        )
            except EXECUTION_INTERRUPTS:
                raise
            except Exception as exc:
                return self._terminal(
                    context,
                    ReviewStatus.RETRIEVAL_FAILED,
                    f"上下文构建失败：{exc}",
                    step_name="context_failed",
                )
            context.review_contexts = contexts
            context.state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.CONTEXT_BUILT,
                "风险分析上下文已构建，开始执行结构化风险分析。",
                step_name="context_built",
                review_contexts=contexts,
                logs=[log.to_dict() for log in context.logs],
            )
        else:
            context.review_contexts = list(state.review_contexts or [])

        risk_items = self._build_risk_items(context)
        # 每个 risk_item 都记住原始顺序和以前保存的结果。
        # 恢复任务时，已完成的项可以跳过模型调用。
        total = len(risk_items)
        completed_items = [item for item in risk_items if item["completed"]]
        if completed_items:
            first_pending = next(
                (
                    item["existing_finding"]
                    for item in risk_items
                    if not item["completed"] and item.get("existing_finding")
                ),
                None,
            )
            completed_ids = [
                item["review_context"]["context_id"]
                for item in completed_items
            ]
            context.state = self._publish_progress(
                context.state,
                context.logs,
                stage="evidence_verification",
                stage_label="校验风险与证据",
                completed=len(completed_items),
                total=total,
                current_item=(
                    _finding_progress_item(first_pending)
                    if first_pending is not None
                    else ""
                ),
                message=(
                    f"已恢复证据校验进度（{len(completed_items)}/{total}）。"
                ),
                tool_name="verify_evidence",
                completed_item_ids=completed_ids,
            )
        context.state = self._publish_progress(
            context.state,
            context.logs,
            stage="risk_subgraph",
            stage_label="分析并校验合同风险",
            completed=0,
            total=total,
            current_item=(
                _review_context_progress_item(risk_items[0]["review_context"])
                if risk_items
                else ""
            ),
            message=(
                f"开始并行处理 {total} 个风险上下文。"
                if risk_items
                else "没有风险上下文需要分析。"
            ),
            tool_name="analyze_risk",
        )
        return {"terminal": False, "risk_items": risk_items}

    def prepare_risk(self, graph_state: ReviewGraphState, runtime) -> dict:
        """拿到一条 risk_item，准备它的处理记录并选择下一步。

        * 从没分析过：去 ``analyze_risk``。
        * 已确认没有风险：去 ``finalize_risk``。
        * 有模型结论：去 ``criticize_risk``。
        * 相关条款不足：先去 ``repair_retrieval``。
        """

        context = runtime.context
        item = graph_state["risk_item"]
        # payload 是这一条风险专用的文件夹。
        # 之后的分析、复核和证据节点都在这个字典中继续填写。
        payload = {
            # index 用于并行结束后恢复合同中的原始顺序。
            "index": item["index"],
            # review_context 是模型分析需要的完整输入。
            "review_context": dict(item["review_context"]),
            # finding 是 Analyzer 的结论；恢复时可能已经存在。
            "finding": item.get("existing_finding"),
            "final_finding": item.get("existing_final_finding"),
            # 保存每次证据校验结果，便于解释为什么转人工。
            "evidence_results": list(item.get("existing_evidence_results") or []),
            "logs": [],
            # Planner 或 Critic 不放心时，把风险交给人。
            "planner_requested_human": False,
            "critic_requested_human": False,
            # 每条风险最多重新校验证据 1 次。
            "evidence_retry_count": 0,
            "retrieval_repair_count": _planner_retry_count(
                [item["review_context"]]
            ),
        }
        if item.get("completed"):
            payload["outcome"] = (
                "no_risk"
                if payload["finding"]["review_status"] == "NO_RISK"
                else "existing"
            )
            return {"branch_payload": payload, "branch_route": "finalize_risk"}
        if _retrieval_is_insufficient(
            payload["review_context"],
            context.clauses_payload,
        ):
            try:
                decision = _invoke_planner(
                    context.task_id,
                    payload["logs"],
                    PlannerReasonCode.RETRIEVAL_INSUFFICIENT,
                    ReviewStatus.CONTEXT_BUILT,
                    str(
                        payload["review_context"]["current_clause"]["clause_id"]
                    ),
                    context.clauses_payload,
                    payload["retrieval_repair_count"],
                    "related clause retrieval returned no candidates",
                )
                _append_planner_trace(
                    payload["review_context"],
                    decision,
                    payload["retrieval_repair_count"],
                )
                if decision["action"] != PlannerAction.RETRIEVE_AGAIN.value:
                    self._set_branch_terminal(
                        payload,
                        ReviewStatus.RETRIEVAL_FAILED,
                        "相关条款召回不足，Planner 未授权再次检索。",
                        "planner_retrieval_stopped",
                        "plan_review_action",
                    )
                    return {
                        "branch_payload": payload,
                        "branch_route": "finalize_risk",
                    }
                payload["query_adjustments"] = decision["query_adjustments"]
                payload["repair_reason"] = "initial_retrieval"
                return {
                    "branch_payload": payload,
                    "branch_route": "repair_retrieval",
                }
            except EXECUTION_INTERRUPTS:
                raise
            except Exception as exc:
                self._set_branch_terminal(
                    payload,
                    ReviewStatus.RETRIEVAL_FAILED,
                    f"相关条款修复被拒绝：{exc}",
                    "planner_retrieval_rejected",
                    "plan_review_action",
                )
                return {
                    "branch_payload": payload,
                    "branch_route": "finalize_risk",
                }
        route = (
            "analyze_risk"
            if payload["finding"] is None
            else (
                "finalize_risk"
                if payload["finding"]["review_status"] == "NO_RISK"
                else "criticize_risk"
            )
        )
        return {"branch_payload": payload, "branch_route": route}

    def analyze_risk(self, graph_state: ReviewGraphState, runtime) -> dict:
        """让风险分析模型判断当前条款有没有风险。

        ``invoke_tool("analyze_risk", ...)`` 返回的字典叫 ``finding``。
        它包含风险类型、等级、理由、证据文本和置信度。

        如果置信度太低，Planner 只能选择：再分析一次、交给人、停止。
        """

        context = runtime.context
        payload = graph_state["branch_payload"]
        try:
            finding = invoke_tool(
                context.task_id,
                tool_registry,
                "analyze_risk",
                {"review_context": payload["review_context"]},
                payload["logs"],
                step_name=(
                    "planner_risk_reanalysis"
                    if payload.get("repair_reason")
                    else "risk_analysis"
                ),
                execution_control=context.execution_control,
                parent_step_id=context.logs[-1].step_id if context.logs else "",
            )
            # 防止并行时把 A 条款的模型结果误放到 B 条款。
            if _analysis_result_key(finding) != payload["review_context"]["context_id"]:
                raise ValueError(
                    "risk finding identity does not match review context"
                )
            payload["finding"] = finding
            if finding["review_status"] == "NO_RISK":
                payload["outcome"] = "no_risk"
                return {
                    "branch_payload": payload,
                    "branch_route": "finalize_risk",
                }
            if _is_low_confidence(finding):
                decision = _invoke_planner(
                    context.task_id,
                    payload["logs"],
                    PlannerReasonCode.LOW_CONFIDENCE,
                    ReviewStatus.RISK_ANALYZED,
                    finding["clause_id"],
                    context.clauses_payload,
                    payload["retrieval_repair_count"],
                    "risk confidence is below the deterministic threshold",
                )
                _append_planner_trace(
                    payload["review_context"],
                    decision,
                    payload["retrieval_repair_count"],
                )
                if decision["action"] == PlannerAction.ANALYZE_AGAIN.value:
                    payload["finding"] = invoke_tool(
                        context.task_id,
                        tool_registry,
                        "analyze_risk",
                        {"review_context": payload["review_context"]},
                        payload["logs"],
                        step_name="planner_risk_reanalysis",
                        execution_control=context.execution_control,
                    )
                elif (
                    decision["action"]
                    == PlannerAction.REQUEST_HUMAN_REVIEW.value
                ):
                    payload["planner_requested_human"] = True
                else:
                    self._set_branch_terminal(
                        payload,
                        ReviewStatus.NEED_MANUAL_REVIEW,
                        "低置信度风险被 Planner 终止，需要人工确认。",
                        "planner_low_confidence_stopped",
                        "plan_review_action",
                    )
                    return {
                        "branch_payload": payload,
                        "branch_route": "finalize_risk",
                    }
            return {
                "branch_payload": payload,
                "branch_route": "criticize_risk",
            }
        except EXECUTION_INTERRUPTS:
            raise
        except Exception as exc:
            self._record_invalid_output_planner(context, payload, exc)
            self._set_branch_terminal(
                payload,
                ReviewStatus.LLM_OUTPUT_INVALID,
                f"风险分析结构化输出无效：{exc}",
                "llm_output_invalid",
                "analyze_risk",
            )
            return {"branch_payload": payload, "branch_route": "finalize_risk"}

    def criticize_risk(self, graph_state: ReviewGraphState, runtime) -> dict:
        """让第二个角色 Critic 检查 Analyzer 的结论。

        Analyzer 负责“提出风险”。Critic 负责“挑错”：

        * 理由是否真的来自当前条款。
        * 是否符合命中的 Playbook 规则。
        * 合同文本里是否混入了诱导模型的指令。

        Critic 通过后才自动生成修改建议。没有通过就交给人。
        """

        context = runtime.context
        payload = graph_state["branch_payload"]
        finding = payload["finding"]
        try:
            critic = invoke_tool(
                context.task_id,
                tool_registry,
                "criticize_risk",
                {
                    "finding": finding,
                    "current_clause": payload["review_context"]["current_clause"],
                    "matched_rule": payload["review_context"]["matched_rule"],
                },
                payload["logs"],
                step_name="risk_critique",
                execution_control=context.execution_control,
            )
            _append_critic_trace(payload["review_context"], finding, critic)
            if (
                critic["reason_code"]
                == CriticReasonCode.PROMPT_INJECTION_DETECTED.value
            ):
                self._set_branch_terminal(
                    payload,
                    ReviewStatus.NEED_MANUAL_REVIEW,
                    "Critic 检测到不可信指令文本，已停止该候选进入正式风险流程。",
                    "critic_injection_blocked",
                    "criticize_risk",
                )
                return {
                    "branch_payload": payload,
                    "branch_route": "finalize_risk",
                }
            payload["critic_requested_human"] = (
                critic["decision"] != CriticDecision.PASS.value
            )
            if not payload["critic_requested_human"]:
                finding = dict(finding)
                finding["related_memory"] = list(
                    payload["review_context"].get("related_memory") or []
                )
                revision = invoke_tool(
                    context.task_id,
                    tool_registry,
                    "generate_revision",
                    {
                        "finding": finding,
                        "preferred_position": context.state.review_position.value,
                    },
                    payload["logs"],
                    step_name="revision_generation",
                    execution_control=context.execution_control,
                )
                finding["revision_suggestion"] = revision["revision_suggestion"]
                if revision.get("memory_references"):
                    finding["memory_references"] = revision["memory_references"]
                finding.pop("related_memory", None)
                payload["finding"] = finding
            return {
                "branch_payload": payload,
                "branch_route": "verify_evidence",
            }
        except CriticOutputInvalidError as exc:
            self._record_invalid_output_planner(context, payload, exc)
            self._set_branch_terminal(
                payload,
                ReviewStatus.LLM_OUTPUT_INVALID,
                f"Critic 结构化输出无效：{exc}",
                "critic_output_invalid",
                "criticize_risk",
            )
        except LLMOutputInvalidError as exc:
            self._record_invalid_output_planner(context, payload, exc)
            self._set_branch_terminal(
                payload,
                ReviewStatus.LLM_OUTPUT_INVALID,
                f"修改建议结构化输出无效：{exc}",
                "llm_output_invalid",
                "generate_revision",
            )
        except EXECUTION_INTERRUPTS:
            raise
        except Exception as exc:
            self._set_branch_terminal(
                payload,
                ReviewStatus.HUMAN_REVIEW_PENDING,
                f"证据处理异常，候选已保留并转入人工处理：{exc}",
                "evidence_manual_review_pending",
                "verify_evidence",
            )
        return {"branch_payload": payload, "branch_route": "finalize_risk"}

    def verify_evidence(self, graph_state: ReviewGraphState, runtime) -> dict:
        """确认风险引用的证据确实存在于合同原文。

        验证成功：保存原文位置，然后整理结果。
        验证失败：Planner 可以批准重新检索一次。
        再失败：保留风险，但标记为需要人工补证据。
        """

        context = runtime.context
        payload = graph_state["branch_payload"]
        finding = payload["finding"]
        try:
            evidence = invoke_tool(
                context.task_id,
                tool_registry,
                "verify_evidence",
                {
                    "finding": finding,
                    "clauses": context.clauses_payload,
                    "matched_rule": payload["review_context"]["matched_rule"],
                },
                payload["logs"],
                step_name="evidence_verification",
                execution_control=context.execution_control,
            )
            # evidence["is_valid"] 是证据是否通过的最终布尔值。
            payload["evidence_results"].append(evidence)
            if evidence["is_valid"]:
                if evidence.get("source_location"):
                    finding["evidence_location"] = evidence["source_location"]
                finding["evidence_verification"] = _evidence_verification_summary(
                    evidence
                )
                payload["finding"] = finding
                payload["outcome"] = "verified"
                return {
                    "branch_payload": payload,
                    "branch_route": "finalize_risk",
                }

            decision = _invoke_planner(
                context.task_id,
                payload["logs"],
                _evidence_planner_reason(evidence),
                ReviewStatus.RISK_ANALYZED,
                finding["clause_id"],
                context.clauses_payload,
                payload["retrieval_repair_count"],
                str(evidence["failure_reason"]),
            )
            _append_planner_trace(
                payload["review_context"],
                decision,
                payload["retrieval_repair_count"],
            )
            if (
                decision["action"] == PlannerAction.RETRIEVE_AGAIN.value
                and payload["evidence_retry_count"] < 1
            ):
                payload["query_adjustments"] = decision["query_adjustments"]
                payload["repair_reason"] = "evidence"
                payload["evidence_retry_count"] += 1
                return {
                    "branch_payload": payload,
                    "branch_route": "repair_retrieval",
                }
            payload["finding"] = _manual_review_candidate(finding, evidence)
            payload["planner_requested_human"] = True
            payload["outcome"] = "manual"
        except PlannerOutputInvalidError as exc:
            failure = {
                "is_valid": False,
                "failure_reason": str(exc),
            }
            payload["finding"] = _manual_review_candidate(finding, failure)
            payload["planner_requested_human"] = True
            payload["outcome"] = "manual"
            self._set_branch_terminal(
                payload,
                ReviewStatus.HUMAN_REVIEW_PENDING,
                f"Planner 决策被拒绝，候选已保留并转入人工处理：{exc}",
                "planner_decision_rejected",
                "plan_review_action",
            )
        except EXECUTION_INTERRUPTS:
            raise
        except Exception as exc:
            failure = {
                "is_valid": False,
                "failure_reason": str(exc),
            }
            payload["finding"] = _manual_review_candidate(finding, failure)
            payload["outcome"] = "manual"
        return {"branch_payload": payload, "branch_route": "finalize_risk"}

    def repair_retrieval(self, graph_state: ReviewGraphState, runtime) -> dict:
        """证据不足时换一组检索词，再构建一次模型输入。

        修复成功后回到 ``analyze_risk``。这条路线最多执行一次。
        """

        context = runtime.context
        payload = graph_state["branch_payload"]
        try:
            payload["retrieval_repair_count"] += 1
            payload["review_context"] = _rebuild_review_context(
                context.task_id,
                payload["logs"],
                payload["review_context"],
                context.clauses_payload,
                payload["query_adjustments"],
                {},
                payload["retrieval_repair_count"],
            )
            return {
                "branch_payload": payload,
                "branch_route": "analyze_risk",
            }
        except EXECUTION_INTERRUPTS:
            raise
        except Exception as exc:
            self._set_branch_terminal(
                payload,
                ReviewStatus.RETRIEVAL_FAILED,
                f"相关条款修复被拒绝：{exc}",
                "planner_retrieval_rejected",
                "retrieve_related_clauses",
            )
            return {"branch_payload": payload, "branch_route": "finalize_risk"}

    def finalize_risk(self, graph_state: ReviewGraphState, runtime) -> dict:
        """把当前风险整理成统一格式，交回主图。

        每个并行分支都返回 ``risk_results=[一条结果]``。
        State 中的 ``add`` 会把这些单项列表拼成完整列表。
        """

        payload = graph_state["branch_payload"]
        finding = payload.get("final_finding") or payload.get("finding")
        terminal = payload.get("terminal")
        if (
            finding
            and finding.get("review_status") != "NO_RISK"
            and (
                payload.get("planner_requested_human")
                or payload.get("critic_requested_human")
                or _requires_manual_review(finding)
            )
        ):
            finding = dict(finding)
            finding["review_status"] = "NEED_MANUAL_REVIEW"
        result = {
            "index": payload["index"],
            "review_context": payload["review_context"],
            "analysis_result": payload.get("finding"),
            "risk_finding": (
                finding
                if (
                    not terminal
                    and finding
                    and finding.get("review_status") != "NO_RISK"
                )
                else None
            ),
            "evidence_results": payload["evidence_results"],
            "logs": [log.to_dict() for log in payload["logs"]],
            "outcome": payload.get("outcome", "manual"),
            "terminal": terminal,
        }
        return {"risk_results": [result], "branch_route": "finalize_risk"}

    def aggregate_risks(self, graph_state: ReviewGraphState, runtime) -> dict:
        """等所有风险分支结束，再生成整份合同的风险列表。

        并行任务完成顺序可能是 3、1、2，所以先按 ``index`` 排回 1、2、3。
        然后把模型结论、证据和日志一次写入数据库。
        """

        context = runtime.context
        results = self._ordered_results(
            list(graph_state.get("risk_results") or [])
        )
        if len(results) != len(context.review_contexts):
            raise RuntimeError(
                "risk subgraph did not return exactly one result per context"
            )
        for result in results:
            context.branch_results[int(result["index"])] = result
        logs = self._all_logs(context)
        contexts = [result["review_context"] for result in results]
        analyses = [
            result["analysis_result"]
            for result in results
            if result.get("analysis_result") is not None
        ]
        evidence = [
            item
            for result in results
            for item in result.get("evidence_results") or []
        ]
        findings = [
            result["risk_finding"]
            for result in results
            if result.get("risk_finding") is not None
        ]
        terminal = next(
            (result["terminal"] for result in results if result.get("terminal")),
            None,
        )
        if terminal:
            if terminal["status"] == ReviewStatus.HUMAN_REVIEW_PENDING.value:
                findings = _materialize_manual_review_candidates(
                    analyses,
                    evidence,
                    findings,
                    default_failure_reason=terminal["message"],
                )
            else:
                findings = []
            context.state = self.event_store.update_task(
                context.task_id,
                ReviewStatus(terminal["status"]),
                terminal["message"],
                step_name=terminal["step_name"],
                tool_name=terminal["tool_name"],
                review_contexts=contexts,
                analysis_results=analyses,
                evidence_results=evidence,
                risk_findings=findings,
                progress=(
                    _human_review_progress(findings)
                    if terminal["status"]
                    == ReviewStatus.HUMAN_REVIEW_PENDING.value
                    else context.state.progress
                ),
                logs=[log.to_dict() for log in logs],
            )
            return {"terminal": True}

        context.state = self.event_store.update_task(
            context.task_id,
            ReviewStatus.RISK_ANALYZED,
            "结构化风险分析完成，开始执行证据验证。",
            step_name="risk_analyzed",
            tool_name="analyze_risk",
            review_contexts=contexts,
            analysis_results=analyses,
            evidence_results=evidence,
            logs=[log.to_dict() for log in logs],
        )
        if any(
            finding["review_status"] == "NEED_MANUAL_REVIEW"
            for finding in findings
        ):
            context.state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.HUMAN_REVIEW_PENDING,
                "证据验证完成，存在需要人工复核的风险。",
                step_name="human_review_pending",
                tool_name="verify_evidence",
                review_contexts=contexts,
                analysis_results=analyses,
                evidence_results=evidence,
                risk_findings=findings,
                logs=[log.to_dict() for log in logs],
            )
        else:
            context.state = self.event_store.update_task(
                context.task_id,
                ReviewStatus.EVIDENCE_VERIFIED,
                "证据验证完成，正式风险列表已生成。",
                step_name="evidence_verified",
                tool_name="verify_evidence",
                review_contexts=contexts,
                analysis_results=analyses,
                evidence_results=evidence,
                risk_findings=findings,
                logs=[log.to_dict() for log in logs],
            )
        return {"terminal": True}

    def _build_risk_items(self, context: _ReviewRuntime) -> list[dict]:
        state = context.state
        restored = _restored_analysis_prefix(
            context.review_contexts,
            state.analysis_results or [],
        )
        existing_by_id = {
            _analysis_result_key(finding): finding for finding in restored
        }
        resuming_evidence = (
            str((state.progress or {}).get("stage", ""))
            in {"evidence_verification", "risk_subgraph"}
            and state.recovery_from_status != ReviewStatus.EVIDENCE_MISSING.value
        )
        candidates = [
            finding
            for finding in restored
            if finding.get("review_status") != "NO_RISK"
        ]
        completed_ids = set(
            _restored_evidence_completion_ids(
                state.progress or {} if resuming_evidence else {},
                restored,
                candidates,
            )
        )
        evidence_owner_id = next(
            (
                review_context["context_id"]
                for review_context in context.review_contexts
                if review_context["context_id"] in completed_ids
            ),
            "",
        )
        final_by_id = {
            _analysis_result_key(finding): finding
            for finding in (state.risk_findings or [])
        }
        return [
            {
                "index": index,
                "review_context": review_context,
                "existing_finding": existing_by_id.get(
                    review_context["context_id"]
                ),
                "existing_final_finding": final_by_id.get(
                    review_context["context_id"]
                ),
                "existing_evidence_results": (
                    list(state.evidence_results or [])
                    if review_context["context_id"] == evidence_owner_id
                    else []
                ),
                "completed": review_context["context_id"] in completed_ids,
            }
            for index, review_context in enumerate(context.review_contexts)
        ]

    def _publish_clause_progress(
        self,
        context: _ReviewRuntime,
        state: AgentState,
        clauses: list,
        completed_clauses: list,
        completed: int,
    ) -> AgentState:
        total = len(clauses)
        next_item = clauses[completed].clause_id if completed < total else ""
        return self._publish_progress(
            state,
            context.logs,
            stage="key_field_extract",
            stage_label="提取条款关键字段",
            completed=completed,
            total=total,
            current_item=next_item,
            message=(
                f"正在处理 {next_item}（{completed}/{total}）。"
                if next_item
                else f"条款关键字段提取完成（{completed}/{total}）。"
            ),
            tool_name="extract_key_fields",
            clauses=[item.to_dict() for item in completed_clauses],
        )

    def _record_branch_progress(
        self,
        context: _ReviewRuntime,
        result: dict,
    ) -> None:
        context.branch_results[int(result["index"])] = result
        ordered = self._ordered_results(list(context.branch_results.values()))
        contiguous = []
        for index in range(len(context.review_contexts)):
            result_at_index = context.branch_results.get(index)
            if result_at_index is None:
                break
            contiguous.append(result_at_index)
        completed = len(ordered)
        total = len(context.review_contexts)
        pending = next(
            (
                item
                for index, item in enumerate(context.review_contexts)
                if index not in context.branch_results
            ),
            None,
        )
        next_item = (
            _review_context_progress_item(pending) if pending is not None else ""
        )
        context.state = self._publish_progress(
            context.state,
            self._all_logs(context),
            stage="risk_subgraph",
            stage_label="分析并校验合同风险",
            completed=completed,
            total=total,
            current_item=next_item,
            message=(
                f"正在处理 {next_item}（{completed}/{total}）。"
                if next_item
                else f"风险分析与证据校验完成（{completed}/{total}）。"
            ),
            tool_name="verify_evidence",
            completed_item_ids=[
                str(result["review_context"]["context_id"])
                for result in ordered
            ],
            review_contexts=[
                context.branch_results[index]["review_context"]
                if index in context.branch_results
                else review_context
                for index, review_context in enumerate(context.review_contexts)
            ],
            analysis_results=[
                result["analysis_result"]
                for result in contiguous
                if result.get("analysis_result") is not None
            ],
            evidence_results=[
                evidence
                for result in ordered
                for evidence in result.get("evidence_results") or []
            ],
        )

    def _terminal(
        self,
        context: _ReviewRuntime,
        status: ReviewStatus,
        message: str,
        **payload,
    ) -> dict:
        context.state = self.event_store.update_task(
            context.task_id,
            status,
            message,
            logs=[log.to_dict() for log in context.logs],
            **payload,
        )
        return {"terminal": True}

    def _submit_execution(
        self,
        task_id: str,
        content: bytes,
        execution_owner: str,
        *,
        control_input=_INITIAL_CONTROL_INPUT,
    ) -> None:
        try:
            future = _COMPATIBILITY_EXECUTOR.submit(
                self.run,
                task_id,
                content,
                execution_owner,
                True,
                control_input,
            )
            with self._execution_futures_lock:
                self._execution_futures.add(future)
            future.add_done_callback(self._forget_execution_future)
        except Exception:
            self.event_store.release_execution(task_id, execution_owner)
            raise

    def _forget_execution_future(self, future: Future) -> None:
        with self._execution_futures_lock:
            self._execution_futures.discard(future)

    def _control_config(self, task_id: str, state: AgentState) -> dict:
        """告诉 LangGraph 这次运行属于哪个任务。

        ``thread_id`` 像书签上的书号。暂停和继续必须使用同一个 task_id，
        否则 LangGraph 会把它当成另一份新任务。
        """

        return {
            "configurable": {"thread_id": task_id},
            "max_concurrency": self._batch_width(state),
        }

    def _stream_control(
        self,
        context: _ReviewRuntime,
        control_input,
    ) -> None:
        """运行控制图，直到它结束或停在人工复核处。"""

        for _update in self._control_graph.stream(
            control_input,
            context=context,
            config=self._control_config(context.task_id, context.state),
            stream_mode="updates",
        ):
            pass

    def _control_state(
        self,
        state: AgentState,
        phase: str,
    ) -> ReviewControlState:
        """从完整任务中挑出书签真正需要的 5 个字段。"""

        return {
            "task_id": state.task_id,
            "phase": phase,
            "status": state.status.value,
            "pending_risk_ids": self._pending_risk_ids(state),
            "recovery_count": state.recovery_count,
        }

    @staticmethod
    def _pending_risk_ids(state: AgentState) -> list[str]:
        """找出还没被用户采纳、忽略或修改的风险编号。"""

        return [
            str(risk.get("risk_id", ""))
            for risk in state.risk_findings or []
            if str(risk.get("risk_id", "")).strip()
            and not str(
                (risk.get("feedback") or {}).get("user_action", "")
            ).strip()
        ]

    @staticmethod
    def _set_branch_terminal(
        payload: dict,
        status: ReviewStatus,
        message: str,
        step_name: str,
        tool_name: str,
    ) -> None:
        payload["terminal"] = {
            "status": status.value,
            "message": message,
            "step_name": step_name,
            "tool_name": tool_name,
        }

    @staticmethod
    def _record_invalid_output_planner(
        context: _ReviewRuntime,
        payload: dict,
        error: Exception,
    ) -> None:
        try:
            decision = _invoke_planner(
                context.task_id,
                payload["logs"],
                PlannerReasonCode.STRUCTURED_OUTPUT_INVALID,
                ReviewStatus.RISK_ANALYZED,
                str(
                    payload["review_context"]["current_clause"]["clause_id"]
                ),
                context.clauses_payload,
                payload["retrieval_repair_count"],
                str(error),
            )
            _append_planner_trace(
                payload["review_context"],
                decision,
                payload["retrieval_repair_count"],
            )
        except EXECUTION_INTERRUPTS:
            raise
        except Exception:
            pass

    @staticmethod
    def _ordered_results(results: list[dict]) -> list[dict]:
        by_index = {int(result["index"]): result for result in results}
        return [by_index[index] for index in sorted(by_index)]

    @staticmethod
    def _all_logs(context: _ReviewRuntime) -> list[StepLog]:
        branch_logs = []
        for result in context.branch_results.values():
            branch_logs.extend(
                StepLog(**log) for log in (result.get("logs") or [])
            )
        return list(context.logs) + branch_logs
