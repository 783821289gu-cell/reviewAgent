from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, payload: dict):
        super().__init__(str(payload.get("message") or payload.get("error") or "API error"))
        self.status_code = status_code
        self.payload = payload


def task_error(message: str, status_code: int = 400) -> ApiError:
    return ApiError(
        status_code=status_code,
        payload={
            "status": "TASK_ERROR",
            "message": message,
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "status": "TASK_ERROR",
                "message": _validation_message(exc),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return JSONResponse(status_code=404, content={"error": "Not found"})
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "请求参数无效。"

    error = errors[0]
    if error.get("type") == "json_invalid":
        return str((error.get("ctx") or {}).get("error") or "请求 JSON 无效。")

    location = ".".join(str(item) for item in error.get("loc", ()) if item != "body")
    message = str(error.get("msg") or "请求参数无效。")
    return f"{location}: {message}" if location else message
