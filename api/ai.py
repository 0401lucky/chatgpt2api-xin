from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from api.image_inputs import parse_image_edit_request, read_image_sources
from api.support import require_identity, resolve_image_base_url
from services.content_filter import check_request, request_shape, request_text
from services.editable_file_task_service import (
    EditableFileTaskCleanupError,
    EditableFileTaskInvalidIdError,
    EditableFileTaskNotFoundError,
    EditableFileTaskNotTerminalError,
    editable_file_task_service,
)
from services.log_service import LoggedCall
from services.protocol import (
    anthropic_v1_messages,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
    openai_search,
)
from utils.helper import has_response_image_generation_tool, is_image_chat_request

GIF_GENERATION_TIMEOUT_SECS = 300


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    output_format: str | None = None
    frame_count: object = None
    frame_duration_ms: object = None
    history_disabled: bool = True
    stream: bool | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


class SearchRequest(BaseModel):
    prompt: str = Field(..., min_length=1)


class EditableFileTaskRequest(BaseModel):
    prompt: str = ""
    kind: str = "ppt"
    base64_images: list[str] = Field(default_factory=list)
    client_task_id: str | None = None


TRACE_REQUEST_HEADERS = {
    "x-request-id": "x_request_id",
    "x-newapi-request-id": "x_newapi_request_id",
    "x-oneapi-request-id": "x_oneapi_request_id",
    "x-channel-id": "x_channel_id",
    "x-channel-name": "x_channel_name",
}


def attach_trace_headers(call: LoggedCall, request: Request) -> None:
    if not call._trace_image_perf():
        return
    headers: dict[str, str] = {}
    for header, field in TRACE_REQUEST_HEADERS.items():
        value = str(request.headers.get(header) or "").strip()
        if value:
            headers[field] = value[:160]
    if headers:
        existing = call.trace_metadata.get("request_headers")
        if isinstance(existing, dict):
            existing.update(headers)
        else:
            call.trace_metadata["request_headers"] = headers


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def create_router() -> APIRouter:
    router = APIRouter()

    async def submit_editable_file_task(submit, identity, **kwargs):
        try:
            return await run_in_threadpool(submit, identity, **kwargs)
        except EditableFileTaskInvalidIdError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": str(exc)},
            ) from exc

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations/gif")
    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        for field in ("output_format", "frame_count", "frame_duration_ms"):
            if field not in body.model_fields_set:
                payload.pop(field, None)
        gif = request.url.path.endswith("/gif")
        if gif:
            payload["prompt"] = f"请生成一段可循环播放的动画 GIF：{body.prompt}"
            payload["stream"] = False
            payload["image_timeout_secs"] = GIF_GENERATION_TIMEOUT_SECS
        payload["base_url"] = resolve_image_base_url(request)
        endpoint = "/v1/images/generations/gif" if gif else "/v1/images/generations"
        call = LoggedCall(identity, endpoint, body.model, "GIF 动图生成" if gif else "文生图", request_text=body.prompt)
        attach_trace_headers(call, request)
        call.attach_trace_metadata(payload)
        await filter_or_log(call, str(payload["prompt"]))
        return await call.run(openai_v1_image_generations.handle, payload)

    @router.post("/v1/images/edits/gif")
    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        gif = request.url.path.endswith("/gif")
        if gif:
            payload["prompt"] = f"请将参考图片制作成可循环播放的动画 GIF，动作自然连贯：{prompt}"
            payload["stream"] = False
            payload["image_timeout_secs"] = GIF_GENERATION_TIMEOUT_SECS
        endpoint = "/v1/images/edits/gif" if gif else "/v1/images/edits"
        call = LoggedCall(identity, endpoint, model, "参考图 GIF 动图生成" if gif else "图生图", request_text=prompt)
        attach_trace_headers(call, request)
        call.attach_trace_metadata(payload)
        await filter_or_log(call, str(payload["prompt"]))
        payload["images"] = await read_image_sources(image_sources)
        if mask_sources:
            payload["mask"] = await read_image_sources(mask_sources)
        payload["base_url"] = resolve_image_base_url(request)
        return await call.run(openai_v1_image_edit.handle, payload)

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        for key in ("output_format", "frame_count", "frame_duration_ms"):
            if key not in body.model_fields_set:
                payload.pop(key, None)
        payload["base_url"] = resolve_image_base_url(request)
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("prompt"), payload.get("messages"))
        image_chat = is_image_chat_request(payload)
        call = LoggedCall(
            identity,
            "/v1/chat/completions",
            model,
            "聊天生图" if image_chat else "文本生成",
            request_text=request_preview,
            request_shape=request_shape(payload.get("messages")),
            image_request=image_chat,
        )
        attach_trace_headers(call, request)
        call.attach_trace_metadata(payload)
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("input"), payload.get("instructions"))
        image_response = has_response_image_generation_tool(payload)
        call = LoggedCall(
            identity,
            "/v1/responses",
            model,
            "Responses",
            request_text=request_preview,
            request_shape=request_shape(payload.get("input")),
            image_request=image_response,
        )
        call.attach_trace_metadata(payload)
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("system"), payload.get("messages"), payload.get("tools"))
        call = LoggedCall(identity, "/v1/messages", model, "Messages", request_text=request_preview)
        await filter_or_log(call, request_preview)
        return await call.run(anthropic_v1_messages.handle, payload, sse="anthropic")

    @router.post("/v1/search")
    async def search(body: SearchRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        call = LoggedCall(identity, "/v1/search", openai_search.MODEL, "搜索", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await call.run(openai_search.handle, body.model_dump(mode="python"))

    @router.get("/v1/editable-file-tasks")
    async def list_editable_file_tasks(
            ids: str = "",
            limit: int = Query(default=0, ge=0, le=100),
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        task_ids = [item.strip() for item in ids.split(",") if item.strip()]
        return await run_in_threadpool(editable_file_task_service.list_tasks, identity, task_ids, limit=limit)

    @router.post("/v1/editable-file-tasks")
    async def create_editable_file_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        kind = (body.kind or "ppt").strip().lower()
        if kind not in {"ppt", "psd"}:
            raise HTTPException(status_code=400, detail={"error": "kind must be ppt or psd"})
        endpoint = f"/v1/{kind}/generations"
        await filter_or_log(
            LoggedCall(identity, endpoint, "gpt-5-5-thinking", f"{kind.upper()} generation task", request_text=body.prompt),
            body.prompt,
        )
        submit = editable_file_task_service.submit_psd if kind == "psd" else editable_file_task_service.submit_ppt
        return await submit_editable_file_task(
            submit,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    @router.delete("/v1/editable-file-tasks/{task_id}")
    async def delete_editable_file_task(
            task_id: str,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        try:
            return await run_in_threadpool(
                editable_file_task_service.delete_task,
                identity,
                task_id,
            )
        except EditableFileTaskNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error": str(exc)},
            ) from exc
        except EditableFileTaskNotTerminalError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": str(exc)},
            ) from exc
        except EditableFileTaskCleanupError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": str(exc)},
            ) from exc

    @router.get("/files/{file_path:path}")
    async def download_editable_file(file_path: str):
        try:
            path = await run_in_threadpool(
                editable_file_task_service.public_file_path,
                file_path,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"error": "file not found"}) from exc
        return FileResponse(path, filename=path.name)

    @router.post("/v1/ppt/generations")
    async def create_ppt_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/ppt/generations", "gpt-5-5-thinking", "PPT生成任务", request_text=body.prompt), body.prompt)
        return await submit_editable_file_task(
            editable_file_task_service.submit_ppt,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    @router.post("/v1/psd/generations")
    async def create_psd_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/psd/generations", "gpt-5-5-thinking", "PSD生成任务", request_text=body.prompt), body.prompt)
        return await submit_editable_file_task(
            editable_file_task_service.submit_psd,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    return router
