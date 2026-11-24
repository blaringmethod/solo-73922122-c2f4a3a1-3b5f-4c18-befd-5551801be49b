"""FastAPI 入口：声级档案核验 API。

输入方式（所有 CSV 接口都兼容）：
1. multipart/form-data 上传 ``file``（/docs 上有文件选择框），或表单字段 ``csv_text``；
2. 直接发送原始 ``text/csv`` / ``text/plain`` 请求体；
3. 被 HTTP 客户端默认标成 application/x-www-form-urlencoded 的原始 CSV 也能识别；
4. ``application/json``：``{"volume_id": "...", "csv": "..."}`` 或
   ``{"rows": [[...], ...]}``。

为避免 FastAPI 的 Form 注入提前消费请求流，这里统一在处理函数里按 Content-Type
手动解析；OpenAPI 请求体用 ``openapi_extra`` 显式声明，/docs 仍有文件选择框。

同一组路由还镜像在 ``/api`` 与 ``/api/v1`` 前缀下（不重复出现在 /docs）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, demo as demo_module
from . import services
from .csvio import decode_bytes, json_payload_to_csv
from .db import connect, init_db, resolve_db_path
from .errors import AppError, CsvParseError, error_body

logger = logging.getLogger("sound_archive")

DESCRIPTION_MD = """
声级档案分批迁入新仓的**副本核验 / 清退签发**服务。SQLite 保存新仓分片、旧卷明细、
回执与占用份数。

## 核心规则

- CSV 每行 = `仪器号, UTC整数秒, 十分之一分贝整数读数`；三列完全一致才是“相同行”，
  相同行按**份**计（多重集）。
- 提交旧卷时，按整行核对新仓**尚未被已签回执占用**的份数：
  - 足够 → 原子签发**清退回执**并占用对应份数；
  - 不足 → 返回 `shortages`，逐行给出需求 / 总份数 / 已占用 / 可用 / 短缺份数
    （`POST /volumes/{id}/submit` 返回 HTTP 409；若希望“不足也返回 200”，
    请改用 `POST /volumes/{id}/verify`）。
- 同卷号 + 同内容原样重送 → 返回**同一张原回执**，不重复占用；
  同卷号但内容不同 → 409 `VOLUME_CONTENT_MISMATCH`。
- `PUT /shards/{shard_id}` 按片号**整片替换**；若替换后任何整行的全仓份数少于
  全部已签回执所需份数，整笔替换回滚，旧片不变（409 `SHARD_COVERAGE_CONFLICT`）。
- 所有核对、回执、占用份数与分片更新都在同一个 `BEGIN IMMEDIATE`
  事务内提交；失败回滚，保留原状。

## /docs 两分钟演示：两卷争用同一份读数

被争用行 `M-DEMO,1760000000,612` 在新仓最初只有 1 份：

1. `PUT /shards/SH-001`，请求体选 `text/csv`，填两行：
   `M-DEMO,1760000000,612` 与 `M-DEMO,1760000060,588`（也可用 multipart 的
   `csv_text` 字段或 `file` 上传 `examples/shard-SH-001.csv`）。
2. `POST /volumes/VOL-A/submit` 填同样两行 → `outcome=issued`，拿到 `receipt_id`。
3. `POST /volumes/VOL-B/submit` 只填 `M-DEMO,1760000000,612`
   → 409，`shortages[0].short == 1`（总 1、已占用 1、可用 0）。
4. 再次提交第 2 步的甲卷 → `outcome=already_issued`，`receipt_id` 不变。
5. 把甲卷的 `612` 改成 `613` 再提交 → 409 `VOLUME_CONTENT_MISMATCH`。
6. `PUT /shards/SH-001` 换成不含 `...,612` 的内容 → 409 `SHARD_COVERAGE_CONFLICT`；
   随后 `GET /shards/SH-001` 可见旧内容未变。

也可以直接调用 `POST /demo/two-volumes-contend`（或浏览器打开
`GET /demo/two-volumes-contend`）一键跑完整套争用脚本并查看每一步实测结果。
"""

TAGS_METADATA = [
    {"name": "演示", "description": "两卷争用同一份读数的端到端演示"},
    {"name": "旧卷核验", "description": "提交旧卷、幂等回执、短缺清单"},
    {"name": "回执", "description": "清退回执查询"},
    {"name": "新仓分片", "description": "分片整片替换 / 查询，替换受回执份数保护"},
    {"name": "可用性", "description": "只读核对未占用份数"},
    {"name": "系统", "description": "健康检查、统计、重置"},
]

# /docs 中展示的通用 CSV 请求体（手动声明，因为处理函数直接解析 Request）。
CSV_REQUEST_BODY: dict[str, Any] = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "format": "binary", "description": "CSV 文件"},
                    "csv_text": {"type": "string", "description": "或直接粘贴 CSV 文本"},
                },
            }
        },
        "text/csv": {
            "schema": {"type": "string"},
            "example": "M-DEMO,1760000000,612\nM-DEMO,1760000060,588\n",
        },
        "text/plain": {"schema": {"type": "string"}},
        "application/json": {
            "schema": {"type": "object"},
            "examples": {
                "csv_string": {
                    "summary": "JSON 里放 CSV 字符串",
                    "value": {"csv": "M-DEMO,1760000000,612\n"},
                },
                "rows_array": {
                    "summary": "JSON 行数组",
                    "value": {"rows": [["M-DEMO", 1760000000, 612]]},
                },
            },
        },
    },
}

FORM_TEXT_FIELDS = ("csv_text", "csv", "content", "text")

api_router = APIRouter()


# --------------------------------------------------------------------------- #
# 依赖与输入提取
# --------------------------------------------------------------------------- #


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


ConnDep = Depends(get_conn)


def _form_text(form: Any) -> str | None:
    upload = form.get("file") or form.get("csv") or form.get("upload")
    if upload is not None and hasattr(upload, "read") and getattr(upload, "filename", None):
        # 同步位置无法 await；调用方需先处理 UploadFile。
        return None
    for key in FORM_TEXT_FIELDS:
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


async def extract_csv_text(request: Request) -> tuple[str, str]:
    """从任意常见请求编码中取出 CSV / 可转换内容，返回 (csv_text, source)。"""

    content_type = request.headers.get("content-type", "").lower()

    # multipart：只能用 form 解析器读这一次流。
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file") or form.get("upload") or form.get("csv_file")
        if upload is not None and hasattr(upload, "read") and getattr(upload, "filename", None):
            return decode_bytes(await upload.read()), f"upload:{upload.filename}"
        text = _form_text(form)
        if text is not None:
            return text, "form:csv_text"
        raise CsvParseError("multipart 请求中未找到 file 文件或 csv_text 文本字段")

    # 其余编码先把原始 body 读出来并缓存，避免流被消费后无法重读。
    raw = await request.body()
    if not raw:
        raise CsvParseError(
            "未收到 CSV 内容：可在 /docs 选择 file、填写 csv_text，"
            "或直接发送 text/csv 请求体 / application/json"
        )
    text = decode_bytes(raw)

    if "application/json" in content_type:
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise CsvParseError(f"JSON 请求体无法解析：{exc}") from exc
        return json_payload_to_csv(data), "json"

    if "application/x-www-form-urlencoded" in content_type:
        pairs = dict(parse_qsl(text, keep_blank_values=True))
        for key in FORM_TEXT_FIELDS:
            if isinstance(pairs.get(key), str) and pairs[key].strip():
                return pairs[key], "form"
        # 很多 HTTP 客户端对“直接给字符串 body”默认补 urlencoded 头，
        # 但字节体其实仍是原始 CSV —— 原样使用即可。
        return text, "body"

    return text, "body"


async def extract_manual(request: Request, id_field: str) -> tuple[str | None, str, str]:
    """集合式 POST/PUT：从 query / JSON / 表单中取 id 与 CSV。"""

    content_type = request.headers.get("content-type", "").lower()
    header_mode = request.query_params.get("header", "auto")
    entity_id: Any = request.query_params.get(id_field) or request.query_params.get("id")

    if "multipart/form-data" in content_type:
        form = await request.form()
        entity_id = entity_id or form.get(id_field) or form.get("id")
        header_mode = str(form.get("header") or header_mode)
        upload = form.get("file") or form.get("upload")
        if upload is not None and hasattr(upload, "read") and getattr(upload, "filename", None):
            text = decode_bytes(await upload.read())
        else:
            text = _form_text(form) or ""
        return (str(entity_id) if entity_id else None), text, header_mode

    raw = await request.body()
    text = decode_bytes(raw) if raw else ""

    if "application/json" in content_type and raw:
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise CsvParseError(f"JSON 请求体无法解析：{exc}") from exc
        if isinstance(data, dict):
            entity_id = entity_id or data.get(id_field) or data.get("id")
            header_mode = str(data.get("header", header_mode))
        return (str(entity_id) if entity_id else None), json_payload_to_csv(data), header_mode

    if "application/x-www-form-urlencoded" in content_type and raw:
        pairs = dict(parse_qsl(text, keep_blank_values=True))
        entity_id = entity_id or pairs.get(id_field) or pairs.get("id")
        header_mode = pairs.get("header", header_mode)
        content_value: str | None = None
        for key in FORM_TEXT_FIELDS:
            if pairs.get(key):
                content_value = pairs[key]
                break
        text = content_value if content_value is not None else text

    return (str(entity_id) if entity_id else None), text, str(header_mode)


def map_submission_result(result: dict[str, Any]) -> JSONResponse | dict[str, Any]:
    if result.get("outcome") == "insufficient":
        body = error_body(
            code="INSUFFICIENT_COPIES",
            message=result.get("message", "新仓尚未占用的副本不足"),
            status_code=409,
            details=result,
        )
        return JSONResponse(status_code=409, content=body)
    return result


# --------------------------------------------------------------------------- #
# 系统 / 演示
# --------------------------------------------------------------------------- #


@api_router.get("/", tags=["系统"], summary="服务说明与入口索引")
async def root() -> dict[str, Any]:
    return {
        "service": "sound-level-archive-verification",
        "name_cn": "声级档案核验 API",
        "version": __version__,
        "docs": "/docs",
        "demo": "/demo/two-volumes-contend",
        "health": "/health",
        "endpoints": {
            "put_shard": "PUT /shards/{shard_id}",
            "submit_volume": "POST /volumes/{volume_id}/submit",
            "verify_volume_200_style": "POST /volumes/{volume_id}/verify",
            "recheck_volume": "POST /volumes/{volume_id}/recheck",
            "shards": "GET /shards",
            "volumes": "GET /volumes",
            "receipts": "GET /receipts",
            "availability": "POST /availability",
            "stats": "GET /stats",
        },
    }


@api_router.get("/health", tags=["系统"])
async def health(request: Request, conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    value = conn.execute("SELECT 1").fetchone()[0]
    return {
        "status": "ok" if value == 1 else "degraded",
        "db": "sqlite",
        "db_path": str(request.app.state.db_path),
        "version": __version__,
    }


@api_router.get("/healthz", tags=["系统"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@api_router.get("/stats", tags=["系统"], summary="分片 / 旧卷 / 回执 / 占用份数计数")
async def get_stats(conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.stats(conn)


@api_router.post("/admin/reset", tags=["系统"], summary="清空全部数据（需 ?confirm=true）")
async def reset(
    confirm: str = Query("", description="传 true/yes/reset/1 才执行"),
    conn: sqlite3.Connection = ConnDep,
) -> JSONResponse:
    if confirm.lower() not in {"true", "yes", "reset", "1"}:
        raise AppError(
            "危险操作：清空全部数据需要 ?confirm=true",
            code="CONFIRMATION_REQUIRED",
            status_code=400,
        )
    return JSONResponse(services.reset_all(conn))


@api_router.post(
    "/demo/two-volumes-contend",
    tags=["演示"],
    summary="一键演示：两卷旧档争用同一份读数",
)
async def demo_contend_post(request: Request) -> dict[str, Any]:
    db_path = request.app.state.db_path
    return demo_module.run_contention_demo(lambda: connect(db_path))


@api_router.get(
    "/demo/two-volumes-contend",
    tags=["演示"],
    summary="同 POST：可直接在浏览器地址栏打开的争用演示",
)
async def demo_contend_get(request: Request) -> dict[str, Any]:
    db_path = request.app.state.db_path
    return demo_module.run_contention_demo(lambda: connect(db_path))


# --------------------------------------------------------------------------- #
# 新仓分片
# --------------------------------------------------------------------------- #


@api_router.get("/shards", tags=["新仓分片"], summary="列出全部分片及份数")
async def list_shards(conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.list_shards(conn)


@api_router.post(
    "/shards",
    tags=["新仓分片"],
    summary="集合式放入/替换分片（id 放 JSON/表单/query）",
    openapi_extra={"requestBody": CSV_REQUEST_BODY},
)
@api_router.put("/shards", include_in_schema=False)
async def put_shard_collection(
    request: Request, conn: sqlite3.Connection = ConnDep
) -> dict[str, Any]:
    shard_id, text, header = await extract_manual(request, "shard_id")
    if not shard_id:
        raise CsvParseError("必须提供 shard_id（JSON 字段、表单字段或 ?shard_id=）")
    return services.replace_shard(conn, shard_id, text, header_mode=header)


@api_router.put(
    "/shards/{shard_id}",
    tags=["新仓分片"],
    summary="按片号整片替换（不存在则创建），须保住全部回执份数",
    openapi_extra={"requestBody": CSV_REQUEST_BODY},
)
@api_router.post(
    "/shards/{shard_id}",
    tags=["新仓分片"],
    summary="同 PUT：按片号整片放入/替换",
    include_in_schema=False,
    openapi_extra={"requestBody": CSV_REQUEST_BODY},
)
@api_router.post("/shards/{shard_id}/csv", include_in_schema=False)
@api_router.put("/shards/{shard_id}/csv", include_in_schema=False)
async def put_shard(
    shard_id: str,
    request: Request,
    header: str = Query("auto", description="auto | yes | no"),
    conn: sqlite3.Connection = ConnDep,
) -> dict[str, Any]:
    text, _source = await extract_csv_text(request)
    return services.replace_shard(conn, shard_id, text, header_mode=header)


@api_router.get("/shards/{shard_id}", tags=["新仓分片"], summary="读取分片元数据、频次与 CSV")
async def get_shard(shard_id: str, conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.get_shard(conn, shard_id)


@api_router.get("/shards/{shard_id}/csv", tags=["新仓分片"], summary="以 text/csv 返回分片原文")
async def get_shard_csv(shard_id: str, conn: sqlite3.Connection = ConnDep) -> Response:
    body = services.get_shard(conn, shard_id)
    return Response(
        content=body["csv"],
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{shard_id}.csv"',
            "X-Content-Hash": body["content_hash"],
        },
    )


@api_router.delete(
    "/shards/{shard_id}",
    tags=["新仓分片"],
    summary="删除分片（等价空片替换，仍受回执保护）",
)
async def delete_shard(shard_id: str, conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.delete_shard(conn, shard_id)


# --------------------------------------------------------------------------- #
# 旧卷核验
# --------------------------------------------------------------------------- #


@api_router.get("/volumes", tags=["旧卷核验"], summary="列出已登记旧卷")
async def list_volumes(conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.list_volumes(conn)


@api_router.post(
    "/volumes",
    tags=["旧卷核验"],
    summary="集合式提交旧卷（volume_id 放 JSON/表单/query）",
    openapi_extra={"requestBody": CSV_REQUEST_BODY},
)
async def submit_volume_collection(
    request: Request, conn: sqlite3.Connection = ConnDep
) -> JSONResponse:
    volume_id, text, header = await extract_manual(request, "volume_id")
    if not volume_id:
        raise CsvParseError("必须提供 volume_id（JSON 字段、表单字段或 ?volume_id=）")
    result = services.submit_volume(conn, volume_id, text, header_mode=header)
    return map_submission_result(result)


@api_router.post(
    "/volumes/{volume_id}/submit",
    tags=["旧卷核验"],
    summary="提交旧卷：足够即签发回执，不足返回 409 短缺清单",
    openapi_extra={"requestBody": CSV_REQUEST_BODY},
)
async def submit_volume(
    volume_id: str,
    request: Request,
    header: str = Query("auto", description="auto | yes | no"),
    conn: sqlite3.Connection = ConnDep,
) -> JSONResponse:
    text, _source = await extract_csv_text(request)
    result = services.submit_volume(conn, volume_id, text, header_mode=header)
    return map_submission_result(result)


@api_router.post(
    "/volumes/{volume_id}/verify",
    tags=["旧卷核验"],
    summary="与 /submit 相同，但副本不足时返回 200 + sufficient=false",
    openapi_extra={"requestBody": CSV_REQUEST_BODY},
)
async def verify_volume(
    volume_id: str,
    request: Request,
    header: str = Query("auto"),
    conn: sqlite3.Connection = ConnDep,
) -> dict[str, Any]:
    text, _source = await extract_csv_text(request)
    return services.submit_volume(conn, volume_id, text, header_mode=header)


@api_router.post(
    "/volumes/{volume_id}/check",
    tags=["旧卷核验"],
    summary="只读核对已保存旧卷当前是否足够（不签发、不写入）",
)
async def check_volume(
    volume_id: str, conn: sqlite3.Connection = ConnDep
) -> dict[str, Any]:
    return services.check_stored_volume(conn, volume_id)


@api_router.post(
    "/volumes/{volume_id}/recheck",
    tags=["旧卷核验"],
    summary="用已保存明细重新核验（补副本后重试，足够即签发）",
)
async def recheck_volume(
    volume_id: str, conn: sqlite3.Connection = ConnDep
) -> JSONResponse:
    result = services.recheck_volume(conn, volume_id)
    return map_submission_result(result)


@api_router.get("/volumes/{volume_id}", tags=["旧卷核验"], summary="旧卷状态、明细与回执")
async def get_volume(volume_id: str, conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.get_volume(conn, volume_id)


@api_router.get(
    "/volumes/{volume_id}/receipt",
    tags=["回执"],
    summary="按卷号取清退回执（未签发返回 404）",
)
@api_router.get("/volumes/{volume_id}/receipts", include_in_schema=False)
async def get_volume_receipt(
    volume_id: str, conn: sqlite3.Connection = ConnDep
) -> dict[str, Any]:
    return services.get_receipt_by_volume(conn, volume_id)


# --------------------------------------------------------------------------- #
# 回执 / 可用性
# --------------------------------------------------------------------------- #


@api_router.get("/receipts", tags=["回执"], summary="列出全部已签回执")
async def list_receipts(conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.list_receipts(conn)


@api_router.get("/receipts/{receipt_id}", tags=["回执"], summary="回执详情（含逐行份数）")
async def get_receipt(receipt_id: str, conn: sqlite3.Connection = ConnDep) -> dict[str, Any]:
    return services.get_receipt(conn, receipt_id)


@api_router.post(
    "/availability",
    tags=["可用性"],
    summary="只读核对给定 CSV 每行的总份数 / 已占用 / 未占用",
    openapi_extra={"requestBody": CSV_REQUEST_BODY},
)
async def availability(
    request: Request,
    header: str = Query("auto"),
    conn: sqlite3.Connection = ConnDep,
) -> dict[str, Any]:
    text, _source = await extract_csv_text(request)
    return services.check_availability(conn, text, header_mode=header)


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #


def create_app(db_path: str | Path | None = None) -> FastAPI:
    resolved_path = resolve_db_path(db_path)
    init_db(resolved_path)

    app = FastAPI(
        title="声级档案核验 API",
        description=DESCRIPTION_MD,
        version=__version__,
        openapi_tags=TAGS_METADATA,
        docs_url=None,  # 自定义 /docs，使用本地 Swagger 静态资源
        redoc_url="/redoc",
    )
    app.state.db_path = resolved_path

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        safe_errors = jsonable_encoder(exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "ok": False,
                "code": "REQUEST_VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "detail": safe_errors,
                "errors": safe_errors,
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
        # 写事务上下文已在异常路径 ROLLBACK；这里记录堆栈并返回统一 JSON。
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_body(
                code="INTERNAL_ERROR",
                message=f"服务器内部错误：{type(exc).__name__}: {exc}",
                status_code=500,
            ),
        )

    app.include_router(api_router)
    # 常见前缀兼容；不重复出现在 OpenAPI /docs 中。
    app.include_router(api_router, prefix="/api", include_in_schema=False)
    app.include_router(api_router, prefix="/api/v1", include_in_schema=False)

    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html() -> HTMLResponse:
        # 自带资源、仅依赖 bundle 内确实存在的 apis 预设与 BaseLayout，
        # 不引用本 bundle 未导出的 SwaggerUIStandalonePreset，保证离线可渲染。
        return HTMLResponse(
            """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>声级档案核验 API · Swagger UI</title>
  <link rel="icon" href="/static/favicon.svg"/>
  <link rel="stylesheet" href="/static/swagger-ui.css"/>
  <style>
    .demo-banner{box-sizing:border-box;margin:0;padding:10px 20px;background:#0f766e;
      color:#fff;font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
    .demo-banner a{color:#a7f3d0;margin-right:16px;white-space:nowrap}
  </style>
</head>
<body>
  <div class="demo-banner">
    <b>声级档案核验 API</b> ·
    <a href="/demo/two-volumes-contend" target="_blank">一键演示：两卷争用同一份读数（GET）</a>
    <a href="/openapi.json" target="_blank">openapi.json</a>
    <a href="/health" target="_blank">/health</a>
  </div>
  <div id="swagger-ui"></div>
  <script src="/static/swagger-ui-bundle.js"></script>
  <script>
    window.addEventListener("load", function () {
      window.ui = SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        showExtensions: true,
        showCommonExtensions: true,
        layout: "BaseLayout",
        presets: [SwaggerUIBundle.presets.apis],
        tryItOutEnabled: false
      });
    });
  </script>
</body>
</html>"""
        )

    return app


# 供 uvicorn 使用：app.main:app
app = create_app()
