"""Owner uploads staged by the browser and stored exclusively by account_evidence."""
import base64
import hashlib

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from a_share_research.account.evidence import MAX_BYTES
from a_share_research.tools_client import ToolCallError


def _encode(content: bytes) -> tuple[str, str]:
    return hashlib.sha256(content).hexdigest(), base64.b64encode(content).decode("ascii")


async def _upload(client, file, label):
    row = {"filename": file.filename, "status": "上传失败"}
    if file.size is not None and file.size > MAX_BYTES:
        return row | {"message": "文件超过单份大小上限，请缩小后重新选择。"}
    content = await file.read(MAX_BYTES + 1)
    if not content or len(content) > MAX_BYTES:
        return row | {"message": "文件为空或超过单份大小上限，请重新选择。"}
    sha, encoded = await run_in_threadpool(_encode, content)
    args = {"filename": file.filename, "content_base64": encoded}
    del content
    if label:
        args["label"] = label
    row["sha256"] = sha
    try:
        saved = await client.call("account_evidence", args, request_id=sha)
    except ToolCallError as exc:
        if exc.kind == "unknown":
            return row | {"status": "结果未知", "message": "请先在最近上传中核对文件指纹，再决定是否重传。"}
        return row | {"message": str(exc)}
    if saved.get("sha256") != sha:
        return row | {"status": "结果未知", "message": "回执与原文件的指纹不一致，请核对最近上传。"}
    return row | {"status": "上传成功"}


async def page(request, session, client, render, form=None):
    rows, recent = [], []
    error = recent_error = label = ""
    if client and form is not None:
        label = str(form.get("label", "")).strip()
        files = [file for file in form.getlist("files") if isinstance(file, UploadFile) and file.filename]
        if not files:
            error = "请选择至少一份图片或 PDF。"
        for file in files:
            rows.append(await _upload(client, file, label))
    if client:
        try:
            recent = (await client.call("account_evidence_recent", {}))["evidence"]
        except ToolCallError as exc:
            recent_error = str(exc)
    return render(request, "account_evidence.html", session, enabled=client is not None, max_bytes=MAX_BYTES,
                  max_mib=MAX_BYTES // (1024 * 1024), label=label, rows=rows, recent=recent,
                  error=error, recent_error=recent_error)
