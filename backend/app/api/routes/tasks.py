import asyncio
import json
import ntpath
from time import monotonic

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from api.dependencies import get_event_store, get_review_agent, get_settings, require_accepting_tasks
from api.errors import ApiError, task_error
from config import Settings
from services.event_service import TERMINAL_STATUSES, ReviewEventStore
from services.review_service import ReviewOrchestratorAgent
from services.task_service import start_review_task


router = APIRouter()
UPLOAD_CHUNK_BYTES = 64 * 1024
SSE_WAIT_SECONDS = 1.0
SSE_KEEP_ALIVE_SECONDS = 10.0
SUPPORTED_MIME_TYPES = {
    "docx": {
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    "pdf": {
        "application/octet-stream",
        "application/pdf",
    },
}
SSE_TERMINAL_STATUS_VALUES = {status.value for status in TERMINAL_STATUSES}


@router.post("/api/tasks", status_code=201)
async def create_task(
    request: Request,
    contract_file: UploadFile | None = File(default=None),
    review_position: str = Form(default=""),
    app_settings: Settings = Depends(get_settings),
    review_agent: ReviewOrchestratorAgent = Depends(get_review_agent),
    _accepting_tasks: None = Depends(require_accepting_tasks),
) -> dict:
    if "multipart/form-data" not in request.headers.get("content-type", "").lower():
        raise task_error("请使用 multipart/form-data 上传合同文件。")
    if contract_file is None:
        raise task_error("请上传 .docx 或 .pdf 合同文件。")

    file_name = _safe_file_name(contract_file.filename)
    file_type = _file_type(file_name)
    _validate_mime_type(file_type, contract_file.content_type)
    content = await _read_upload(contract_file, app_settings.max_upload_bytes)
    _validate_signature(file_type, content)

    try:
        task = start_review_task(
            file_name=file_name,
            content=content,
            review_position_value=review_position,
            review_agent=review_agent,
        )
    except ValueError as exc:
        raise task_error(str(exc)) from exc
    return task.to_dict()


@router.get("/api/tasks/{task_id}")
def get_task(
    task_id: str,
    event_store: ReviewEventStore = Depends(get_event_store),
) -> dict:
    task = event_store.get_task(task_id)
    if task is None:
        raise ApiError(status_code=404, payload={"error": "Task not found"})
    return task.to_dict()


@router.get("/api/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    request: Request,
    event_store: ReviewEventStore = Depends(get_event_store),
) -> StreamingResponse:
    if event_store.get_task(task_id) is None:
        raise ApiError(status_code=404, payload={"error": "Task not found"})

    async def event_stream():
        next_index = 0
        last_keep_alive = monotonic()
        while True:
            if await request.is_disconnected():
                break

            events = await asyncio.to_thread(
                event_store.wait_for_events,
                task_id,
                next_index,
                SSE_WAIT_SECONDS,
            )
            for event in events:
                yield _sse_event(event)
                next_index = event["event_id"] + 1

            if events and events[-1]["status"] in SSE_TERMINAL_STATUS_VALUES:
                break
            if not request.app.state.accepting_tasks:
                break
            if not events and monotonic() - last_keep_alive >= SSE_KEEP_ALIVE_SECONDS:
                yield ": keep-alive\n\n"
                last_keep_alive = monotonic()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _safe_file_name(raw_file_name: str | None) -> str:
    file_name = ntpath.basename(str(raw_file_name or "").strip())
    if not file_name or file_name in {".", ".."}:
        raise task_error("上传文件名无效。")
    if len(file_name) > 255 or any(ord(character) < 32 for character in file_name):
        raise task_error("上传文件名不安全。")
    return file_name


def _file_type(file_name: str) -> str:
    if "." not in file_name:
        raise task_error("仅支持上传 .docx 或 .pdf 文件。")
    file_type = file_name.rsplit(".", 1)[1].lower()
    if file_type not in SUPPORTED_MIME_TYPES:
        raise task_error("仅支持上传 .docx 或 .pdf 文件。")
    return file_type


def _validate_mime_type(file_type: str, content_type: str | None) -> None:
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized_type not in SUPPORTED_MIME_TYPES[file_type]:
        raise task_error("上传文件 MIME 类型与扩展名不匹配。")


async def _read_upload(upload: UploadFile, max_upload_bytes: int) -> bytes:
    content = bytearray()
    try:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_upload_bytes:
                raise task_error(f"上传文件超过大小限制，当前上限为 {max_upload_bytes} 字节。")
    finally:
        await upload.close()

    if not content:
        raise task_error("上传文件为空，无法解析。")
    return bytes(content)


def _validate_signature(file_type: str, content: bytes) -> None:
    if file_type == "pdf" and not content.startswith(b"%PDF-"):
        raise task_error("PDF 文件签名无效。")
    if file_type == "docx" and not content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise task_error("DOCX 文件签名无效。")


def _sse_event(event: dict) -> str:
    body = json.dumps(event, ensure_ascii=False)
    return f"event: review_event\nid: {event['event_id']}\ndata: {body}\n\n"
