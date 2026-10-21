"""巷口 API 与体验页。"""

import asyncio
import re
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .actions import ActionError, VoucherActionWorkflow
from .business import BusinessWorkflow
from .business_state import BusinessStateConflict
from .dianping import DianpingClient, DianpingError, DianpingRejected, DianpingUnauthorized
from .planner import Planner
from .llm import LLMConfig, LLMProvider
from .memory import MemoryPreferences
from .schemas import ChatRequest, ChatResponse
from .session import SessionAccessError
from .workflow import DiscoveryWorkflow


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = DianpingClient(os.getenv("DIANPING_BASE_URL", "http://127.0.0.1:8081"))
    planner = Planner(provider=LLMProvider(LLMConfig.from_env()))
    storage_path = os.getenv("XIANGKOU_DATA_PATH") or str(Path(__file__).parent.parent / "data" / "xiangkou.sqlite3")
    app.state.workflow = DiscoveryWorkflow(
        client, planner, os.getenv("DIANPING_WEB_URL", "http://127.0.0.1:8080"), storage_path)
    app.state.actions = VoucherActionWorkflow(client, app.state.workflow.runs)
    app.state.business = BusinessWorkflow(app.state.workflow, app.state.actions)
    app.state.active_requests = {}
    try:
        yield
    finally:
        try:
            await planner.provider.close()
        finally:
            await client.close()


app = FastAPI(title="巷口", description="Harness + Workflow 本地生活智能决策助手", lifespan=lifespan)
AUTH_COOKIE = "xiangkou_dp_token"


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """参数校验失败统一返回 422 文案。

    FastAPI 默认会把原始输入（可能是二进制）塞进 detail，序列化时可能抛
    UnicodeDecodeError 变成 500；这里只返回安全文案，避免把校验错误变成服务错误。
    """
    return JSONResponse(status_code=422, content={"detail": "请求参数不合法"})

# 上传的探店图片：本地开发读 nginx-html/imgs，容器里读挂进来的 /uploads（与 既有服务 共用卷）
_IMAGES_DIR = Path(os.getenv("XIANGKOU_IMAGES_DIR") or (Path(__file__).parent.parent / "nginx-html" / "imgs"))
if _IMAGES_DIR.is_dir():
    app.mount("/imgs", StaticFiles(directory=str(_IMAGES_DIR)), name="imgs")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """浏览器默认会请求 favicon，返回 204 避免控制台 404 噪音。"""
    return Response(status_code=204)


class CodeRequest(BaseModel):
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class LoginRequest(CodeRequest):
    code: str = Field(pattern=r"^\d{6}$")


class PrepareVoucherRequest(BaseModel):
    run_id: str = Field(min_length=36, max_length=36)
    shop_id: int = Field(gt=0)
    voucher_id: int = Field(gt=0)


class MemoryUpdate(BaseModel):
    consent: Literal[True]
    preferences: MemoryPreferences


def _token(request: Request) -> str:
    token = request.cookies.get(AUTH_COOKIE) or request.headers.get("authorization", "")
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def _check_write_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="写操作来源不匹配")


async def require_user(request: Request) -> dict:
    token = _token(request)
    if not token:
        raise HTTPException(status_code=401, detail="请先连接点评账号")
    try:
        return await app.state.workflow.client.current_user(token)
    except (DianpingUnauthorized, DianpingRejected) as exc:
        raise HTTPException(status_code=401, detail="点评登录已失效，请重新连接") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评登录服务不可用") from exc


@app.post("/api/auth/code")
async def send_code(body: CodeRequest):
    try:
        await app.state.workflow.client.send_login_code(body.email)
    except DianpingRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评登录服务不可用") from exc
    return {"status": "sent"}


@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response):
    try:
        token = await app.state.workflow.client.login(body.email, body.code)
        user = await app.state.workflow.client.current_user(token)
    except (DianpingRejected, DianpingUnauthorized) as exc:
        raise HTTPException(status_code=401, detail="邮箱或验证码无效") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评登录服务不可用") from exc
    response.set_cookie(AUTH_COOKIE, token, max_age=30*86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")
    return {"user": user}


@app.post("/api/auth/guest-login")
async def guest_login(request: Request, response: Response):
    """游客登录，免验证码；复用点评客户端，不写死后端地址（容器里是 http://go-api:8081）。"""
    try:
        token = await app.state.workflow.client.guest_login()
        user = await app.state.workflow.client.current_user(token)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"游客登录服务暂不可用：{exc}") from exc
    response.set_cookie(AUTH_COOKIE, token, max_age=30*86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")
    return {"user": user}


@app.get("/api/auth/me")
async def me(user: dict = Depends(require_user)):
    return {"user": user}


@app.get("/api/memory")
async def get_memory(user: dict = Depends(require_user)):
    profile = await app.state.workflow.memory.get(user["id"])
    return {"enabled": profile is not None,
            "preferences": profile.model_dump() if profile else None}


@app.put("/api/memory")
async def save_memory(body: MemoryUpdate, request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    await app.state.workflow.memory.set(user["id"], body.preferences)
    return {"enabled": True, "preferences": body.preferences.model_dump()}


@app.delete("/api/memory")
async def delete_memory(request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    await app.state.workflow.memory.delete(user["id"])
    return {"enabled": False, "preferences": None}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = _token(request)
    revoked = False
    if token:
        try:
            await app.state.workflow.client.logout(token)
            revoked = True
        except DianpingError:
            pass
    response.delete_cookie(AUTH_COOKIE, path="/", samesite="lax")
    return {"status": "disconnected", "token_revoked": revoked}


@app.post("/api/actions/vouchers/prepare")
async def prepare_voucher(body: PrepareVoucherRequest, request: Request,
                          user: dict = Depends(require_user)):
    _check_write_origin(request)
    try:
        return await app.state.actions.prepare(user["id"], body.run_id, body.shop_id, body.voucher_id)
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评券服务不可用") from exc


@app.post("/api/actions/{action_id}/confirm")
async def confirm_action(action_id: str, request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    try:
        return await app.state.actions.confirm(action_id, user["id"], _token(request))
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DianpingUnauthorized as exc:
        raise HTTPException(status_code=401, detail="点评登录已失效，请重新连接") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评订单服务不可用") from exc


@app.get("/api/actions/recent")
async def recent_actions(user: dict = Depends(require_user)):
    return {"actions": await app.state.actions.list_recent(user["id"])}


@app.post("/api/actions/{action_id}/cancel-order")
async def cancel_order(action_id: str, request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    try:
        return await app.state.actions.cancel_order(action_id, user["id"], _token(request))
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DianpingUnauthorized as exc:
        raise HTTPException(status_code=401, detail="点评登录已失效，请重新连接") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评订单服务不可用") from exc


@app.get("/api/actions/{action_id}")
async def action_status(action_id: str, request: Request, user: dict = Depends(require_user)):
    try:
        return await app.state.actions.status(action_id, user["id"], _token(request))
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DianpingUnauthorized as exc:
        raise HTTPException(status_code=401, detail="点评登录已失效，请重新连接") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评订单服务不可用") from exc


@app.get("/")
async def home():
    root = Path(__file__).parent.parent
    page = root / "frontend" / "dist" / "index.html"
    return FileResponse(page if page.exists() else root / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/app.js")
async def app_script():
    return FileResponse(Path(__file__).parent.parent / "static" / "app.js", media_type="application/go-apiscript",
                        headers={"Cache-Control": "no-store"})


@app.get("/conversation.js")
async def conversation_script():
    return FileResponse(Path(__file__).parent.parent / "static" / "conversation.js", media_type="application/go-apiscript",
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    provider = app.state.workflow.planner.provider
    return {"status": "ok", "service": "巷口", "llm": {
        "provider": provider.config.provider, "model": provider.config.model or None,
        "mode": "model" if provider.enabled else "rules",
    }}


@app.get("/ready")
async def ready():
    database = app.state.workflow.sessions.database
    if database and not database.check():
        raise HTTPException(status_code=503, detail="状态数据库校验失败")
    try:
        types = await app.state.workflow.client.list_types()
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评 既有服务 服务不可用") from exc
    return {"status": "ready", "shop_types": len(types), "state_storage": "ok" if database else "memory"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request, user: dict = Depends(require_user)):
    if not body.request_id:
        return await _run_chat(body, user["id"], token=_token(request))
    active = app.state.active_requests
    if body.request_id in active:
        raise HTTPException(status_code=409, detail="请求 ID 正在使用")
    task = asyncio.create_task(_run_chat(body, user["id"], token=_token(request)))
    active[body.request_id] = (user["id"], task)
    try:
        return await task
    finally:
        active.pop(body.request_id, None)


# 统一记录一轮消息及完成/失败/取消状态；内部业务入口只负责执行和返回结果。
async def _run_chat(body: ChatRequest, user_id: int, on_event=None, token=""):
    store = getattr(app.state.workflow, "conversations", None)
    if store is None:
        return await _execute_chat(body, user_id, on_event, token)
    try:
        if body.session_id:
            await app.state.workflow.sessions.require_owned(body.session_id, user_id)
        else:
            state = await app.state.workflow.sessions.load(None, user_id)
            body = body.model_copy(update={"session_id": state.id})
        # 先落用户消息与回答占位，让断开或取消也能保留本轮的状态。
        turn_id = await store.begin(body.session_id, user_id, body.message)
    except SessionAccessError as exc:
        raise HTTPException(status_code=404, detail="会话不存在或已过期") from exc
    if on_event:
        on_event({"event": "session_started", "session_id": body.session_id})
    try:
        result = await _execute_chat(body, user_id, on_event, token)
        await store.complete(turn_id, result.answer, run_id=result.run_id)
        return result
    except asyncio.CancelledError:
        await store.complete(turn_id, "本轮已取消，可以继续发送消息。", "cancelled")
        raise
    except Exception:
        await store.complete(turn_id, "本轮请求失败，请重试。", "error")
        raise


async def _execute_chat(body: ChatRequest, user_id: int, on_event=None, token=""):
    try:
        from .dialogue import route_message

        # 先处理订单/选券业务，未命中再找店；沿用入口预算和父 span，不能为子阶段重置额度。
        async def execute_workflow(entry=None):
            business = BusinessWorkflow(app.state.workflow, app.state.actions,
                                        run_context=entry.context if entry else None,
                                        parent_span_id=entry.run_id if entry else None) if hasattr(app.state, "actions") else None
            result = await business.run(body, user_id, token, on_event) if business else None
            if result is not None:
                return result
            if business and business.prelude_harness:
                prelude = business.prelude_harness
                result = await app.state.workflow.run(body, owner_user_id=user_id, on_event=on_event,
                                                     run_context=prelude.context, parent_span_id=prelude.run_id)
                return await business._join(result, prelude, user_id)
            if entry:
                return await app.state.workflow.run(body, owner_user_id=user_id, on_event=on_event,
                                                    run_context=entry.context, parent_span_id=entry.run_id)
            return await app.state.workflow.run(body, owner_user_id=user_id, on_event=on_event)

        if hasattr(app.state.workflow, "sessions") and hasattr(app.state.workflow, "runs"):
            result, _ = await route_message(body, app.state.workflow, user_id, on_event,
                                            execute_workflow=execute_workflow)
            if result is not None:
                return result
        return await execute_workflow()
    except BusinessStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SessionAccessError as exc:
        raise HTTPException(status_code=404, detail="会话不存在或已过期") from exc
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DianpingUnauthorized as exc:
        raise HTTPException(status_code=401, detail="点评登录已失效") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="点评业务接口暂时不可用") from exc


@app.delete("/api/requests/{request_id}")
async def cancel_request(request_id: str, request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    active = app.state.active_requests.get(request_id)
    if active is None or active[0] != user["id"]:
        raise HTTPException(status_code=404, detail="运行中的任务不存在")
    active[1].cancel()
    return {"status": "cancelling"}


@app.post("/api/chat/stream")
async def chat_stream(body: ChatRequest, request: Request, user: dict = Depends(require_user)):
    if not body.request_id:
        raise HTTPException(status_code=422, detail="流式请求需要 request_id")
    active = app.state.active_requests
    if body.request_id in active:
        raise HTTPException(status_code=409, detail="请求 ID 正在使用")
    # SSE 发送阶段事件与最终结果；进度队列有界，防止慢客户端无限积压。
    events: asyncio.Queue = asyncio.Queue(maxsize=100)
    wake = asyncio.Event()
    # 最终结果单独保存，即使进度队列满了，也能发送结束、取消或错误消息。
    terminal = asyncio.get_running_loop().create_future()
    dropped_events = 0

    def push(item: dict):
        nonlocal dropped_events
        # 拥塞时丢弃进度并累计数量，不阻塞业务执行；最终消息不走这个队列。
        if events.full():
            dropped_events += 1
            return
        events.put_nowait({"type": "event", "data": item})
        wake.set()

    def finish(item: dict):
        # A terminal message must never wait for a progress queue consumer.
        if not terminal.done():
            terminal.set_result({**item, "dropped_events": dropped_events})
        wake.set()

    async def worker():
        try:
            result = await _run_chat(body, user["id"], on_event=push, token=_token(request))
            finish({"type": "result", "data": result.model_dump()})
        except asyncio.CancelledError:
            finish({"type": "cancelled"})
        except HTTPException as exc:
            finish({"type": "error", "detail": exc.detail, "status": exc.status_code})
        except Exception:
            finish({"type": "error", "detail": "运行失败，请稍后重试", "status": 500})
        finally:
            active.pop(body.request_id, None)

    task = asyncio.create_task(worker())
    active[body.request_id] = (user["id"], task)

    def task_finished(finished):
        # Includes cancellation before the worker coroutine starts.
        if finished.cancelled():
            finish({"type": "cancelled"})
        elif finished.exception() is not None:
            finish({"type": "error", "detail": "运行失败，请稍后重试", "status": 500})
        if active.get(body.request_id, (None, None))[1] is finished:
            active.pop(body.request_id, None)

    task.add_done_callback(task_finished)

    async def stream():
        try:
            while True:
                wake.clear()
                if not events.empty():
                    item = events.get_nowait()
                elif terminal.done():
                    item = terminal.result()
                else:
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                    continue
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                if item["type"] in ("result", "cancelled", "error"):
                    break
        # 流关闭时取消仍在运行的任务，并限定清理等待时间；这不回滚已完成外部写入。
        finally:
            if not task.done():
                task.cancel()
            # Bound cleanup even if an upstream adapter delays cancellation.
            done, _ = await asyncio.wait({task}, timeout=2)
            if task not in done:
                task.cancel()
            elif not task.cancelled():
                task.exception()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/sessions")
async def list_sessions(user: dict = Depends(require_user)):
    sessions = await app.state.workflow.sessions.list_recent(user["id"], limit=100)
    # 过滤掉早期版本留下的非 UUID 会话 id，避免点进去 404。
    sessions = [item for item in sessions
                if re.fullmatch(r"[0-9a-fA-F-]{36}", str(item.get("session_id", "")))]
    store = getattr(app.state.workflow, "conversations", None)
    if store:
        for session in sessions:
            try:
                messages, _ = await store.read(session["session_id"], user["id"])
            except SessionAccessError:
                continue
            session["title"] = messages[0]["text"][:40] if messages else "、".join(session["constraints"]) or "历史计划"
            session["message_count"] = len(messages)
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, user: dict = Depends(require_user)):
    try:
        messages, run_id = await app.state.workflow.conversations.read(session_id, user["id"])
    except SessionAccessError as exc:
        raise HTTPException(status_code=404, detail="会话不存在或已过期") from exc
    record = await app.state.workflow.runs.get(run_id, user["id"]) if run_id else None
    last_response = record.get("response") if record else None
    last_task = None
    if last_response and last_response.get("response_kind") == "conversation":
        task = await app.state.workflow.runs.latest_for_session(session_id, user["id"], display=True)
        last_task = task.get("response") if task else None
    # Rehydrate display cards from owned run records; never expose draft/action tokens.
    card_fields = ("run_id", "recommendation_run_id", "response_kind", "status", "answer",
                   "shops", "itineraries", "confirmed_constraints", "memory_applied",
                   "candidate_decisions", "candidate_summary", "relaxations", "personal_orders", "business_choices")
    for message in messages:
        if message["role"] != "assistant" or not message.get("run_id"):
            continue
        saved = await app.state.workflow.runs.get(message["run_id"], user["id"])
        payload = saved.get("response", {}) if saved else {}
        if saved and saved.get("session_id") == session_id:
            message["result"] = {key: payload[key] for key in card_fields if key in payload}
            # Old transcripts contained duplicated summaries; show the original model reply.
            message["text"] = payload.get("answer", message["text"])
    return {"session_id": session_id, "messages": messages,
            "last_response": last_response, "last_task_response": last_task}


@app.get("/api/tasks")
async def tasks(user: dict = Depends(require_user)):
    return {"tasks": app.state.workflow.checkpoints.recent(user["id"])}


@app.get("/api/my/orders")
async def my_orders(request: Request, page: int = 1, user: dict = Depends(require_user)):
    return await _personal_page(request, page, user, False)


@app.get("/api/my/vouchers")
async def my_vouchers(request: Request, page: int = 1, user: dict = Depends(require_user)):
    return await _personal_page(request, page, user, True)


async def _personal_page(request, page, user, held_only):
    if not 1 <= page <= 10000:
        raise HTTPException(status_code=422, detail="分页参数无效")
    try:
        return await app.state.workflow.client.my_orders(_token(request), held_only, page)
    except DianpingUnauthorized as exc:
        raise HTTPException(status_code=401, detail="点评登录已失效") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=503, detail="个人券订单服务不可用") from exc


@app.post("/api/tasks/{run_id}/resume", response_model=ChatResponse)
async def resume_task(run_id: str, request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    try:
        result = await app.state.workflow.resume(run_id, user["id"], token=_token(request))
    except SessionAccessError as exc:
        _record_resume(run_id, user["id"], False)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DianpingUnauthorized as exc:
        _record_resume(run_id, user["id"], False)
        raise HTTPException(status_code=401, detail='点评登录已失效') from exc
    except DianpingError as exc:
        _record_resume(run_id, user["id"], False)
        raise HTTPException(status_code=503, detail='个人券订单服务不可用') from exc
    _record_resume(run_id, user["id"], True)
    return result


def _record_resume(run_id: str, user_id: int, ok: bool) -> None:
    """记录一次任务恢复尝试，用于统计恢复成功率（无数据库时静默跳过）。"""
    import time
    runs = getattr(getattr(app.state, "workflow", None), "runs", None)
    database = getattr(runs, "database", None)
    if not database:
        return
    try:
        with database.connect() as conn:
            conn.execute("INSERT INTO resume_events (run_id, owner_user_id, ok, created_at) VALUES (?,?,?,?)",
                         (run_id, user_id, 1 if ok else 0, time.time()))
    except Exception:
        pass


@app.get("/api/actions/{action_id}/preview")
async def restore_draft(action_id: str, user: dict = Depends(require_user)):
    try:
        return await app.state.actions.preview(action_id, user["id"])
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(require_user)):
    record = await app.state.workflow.runs.get(run_id, user["id"])
    if record is None:
        raise HTTPException(status_code=404, detail="运行记录不存在或已过期")
    return record


@app.delete("/api/sessions/{session_id}")
async def clear_session(session_id: str, request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    if not await app.state.workflow.sessions.clear(session_id, user["id"]):
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    app.state.workflow.business_states.clear(user["id"], session_id)
    if hasattr(app.state.workflow, "conversations"):
        app.state.workflow.conversations.clear(session_id)
    return {"status": "cleared"}


class MockPaymentRequest(BaseModel):
    expected_pay_cents: int = Field(gt=0, le=100000000)
    confirmed: Literal[True]


@app.get("/api/my/orders/{order_id}")
async def my_order_detail(order_id: str, request: Request, user: dict = Depends(require_user)):
    if not order_id.isdigit() or len(order_id) > 19:
        raise HTTPException(status_code=400, detail="订单号无效")
    try:
        return await app.state.workflow.client.get_voucher_order(order_id, _token(request))
    except DianpingUnauthorized as exc:
        raise HTTPException(status_code=401, detail="请重新登录") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/my/orders/{order_id}/pay/mock")
async def mock_payment(order_id: str, body: MockPaymentRequest, request: Request, user: dict = Depends(require_user)):
    _check_write_origin(request)
    if not order_id.isdigit() or len(order_id) > 19:
        raise HTTPException(status_code=400, detail="订单号无效")
    try:
        await app.state.workflow.client.pay_mock_order(order_id, body.expected_pay_cents, _token(request))
        detail = await app.state.workflow.client.get_voucher_order(order_id, _token(request))
        return {**detail, "payment_mode": "mock", "notice": "本地模拟支付成功，未扣真实资金。"}
    except DianpingUnauthorized as exc:
        raise HTTPException(status_code=401, detail="请重新登录") from exc
    except DianpingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# Explicit business routes for the unified Vue frontend.
from .web_api import register_web_api
register_web_api(app, require_user, _token, _check_write_origin)


@app.get("/legacy")
async def legacy_home():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/web/{asset_path:path}")
async def web_asset(asset_path: str):
    root = (Path(__file__).parent.parent / "frontend" / "dist").resolve()
    target = (root / (asset_path or "index.html")).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "页面资源不存在，请先构建前端")
    return FileResponse(target, headers={"Cache-Control": "no-store"} if target.name == "index.html" else {})
