"""从统一采集到的调用元数据派生 Git / 实验 / 文件结构化信息。

当前仓库没有专用 git_commit / git_push / start_experiment 工具：Git 操作通过
run_command 发起，实验通过 run_command_background 发起。本模块在**统一采集层**
解析这些调用，产出可查询的 git_operations / experiment_runs 记录，避免改动
任何现有工具。解析结果属于「尽力提取」，字段缺失时留空而非猜测。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_TASK_RE = re.compile(r"task_id\s*[=:]\s*([A-Za-z0-9_-]{4,64})")
_EXIT_RE = re.compile(r"(?:^|\n)exit(?:_code)?[=:]\s*(-?\d+)")
_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed")
_CD_RE = re.compile(r"(?:^|&&|\|)\s*cd\s+([^\s;&|]+)")
_BRANCH_RE = re.compile(r"(?:git\s+branch\s+--show-current|On branch\s+)([^\s\n]+)")
_MSG_RE = re.compile(r"""-m\s+(?:"([^"]*)"|'([^']*)'|(\S+))""")


def result_text(result: Any) -> str:
    """把 FastMCP ToolResult 归一化为纯文本，供脱敏与解析。"""
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        elif isinstance(block, dict) and "text" in block:
            parts.append(str(block["text"]))
        else:
            parts.append(str(block))
    return "\n".join(parts)


def extract_exit_code(text: str) -> int | None:
    match = _EXIT_RE.search(text or "")
    return int(match.group(1)) if match else None


def derive_status(tool_name: str, text: str, exit_code: int | None,
                  is_error: bool = False) -> str:
    """把工具返回的文本映射为统一终态。"""
    lowered = (text or "").lower()
    body = text or ""
    # 确认门的提示文案在 REST 与 MCP 工具里略有差异，统一识别为「未执行的拒绝」。
    confirm_markers = ("需确认", "需明确确认", "请带上 confirm", "请带 confirm")
    if tool_name.startswith("run_command") and any(m in body for m in confirm_markers):
        return "DENIED"
    if any(m in body for m in ("禁止", "不允许", "被拒绝")):
        return "DENIED"
    if "timed out" in lowered or "超时" in (text or "") or "killed-timeout" in lowered:
        return "TIMEOUT"
    if is_error or (text or "").startswith("ERROR:") or (exit_code not in (None, 0)):
        return "FAILED"
    return "SUCCESS"


def _repo_from_command(command: str, cwd: str) -> tuple[str, str]:
    match = _CD_RE.search(command or "")
    path = (match.group(1) if match else cwd) or ""
    name = path.rstrip("/").rsplit("/", 1)[-1] if path else ""
    return path, name


def enrich(record: dict[str, Any], arguments: dict[str, Any],
           result: str) -> dict[str, Any]:
    """返回要合并进 tool_calls 的派生字段 + 需要写入子表的事件列表。"""
    tool = record.get("tool_name", "")
    out: dict[str, Any] = {"sub_events": []}
    text = result or ""
    args = arguments or {}

    exit_code = extract_exit_code(text)
    if exit_code is not None:
        out["exit_code"] = exit_code

    task_match = _TASK_RE.search(text)
    if task_match:
        out["task_id"] = task_match.group(1)

    if tool in ("read_file", "write_file", "search_files", "grep_content", "read_image"):
        out["file_path"] = str(args.get("path") or args.get("directory") or "")

    if tool == "run_command" or tool == "run_command_rest":
        command = str(args.get("command") or "")
        cwd = str(args.get("cwd") or "")
        if re.search(r"\bgit\b", command):
            repo_path, repo_name = _repo_from_command(command, cwd)
            out["git_repo"] = repo_name or repo_path
            operation = "other"
            if re.search(r"git\s+commit\b", command):
                operation = "commit"
            elif re.search(r"git\s+push\b", command):
                operation = "push"
            elif re.search(r"git\s+(status|log|diff|branch|fetch|pull|clone|checkout|reset)\b", command):
                operation = command.split("git", 1)[1].strip().split()[0]
            branch_match = _BRANCH_RE.search(text)
            sha_match = _SHA_RE.search(text)
            msg_match = _MSG_RE.search(command)
            remote_parts = re.search(r"git\s+push(?:\s+([^\s;]+))?(?:\s+([^\s;]+))?", command)
            out["sub_events"].append(("git", {
                "request_id": record.get("request_id", ""),
                "trace_id": record.get("trace_id", ""),
                "repo_path": repo_path,
                "repo_name": repo_name,
                "branch": branch_match.group(1) if branch_match else "",
                "operation": operation,
                "files_changed": int(_FILES_RE.search(text).group(1)) if _FILES_RE.search(text) else 0,
                "commit_sha": sha_match.group(0) if sha_match and operation == "commit" else "",
                "commit_message": next((g for g in msg_match.groups() if g), "") if msg_match else "",
                "push_remote": remote_parts.group(1) if remote_parts and remote_parts.group(1) else "",
                "push_branch": remote_parts.group(2) if remote_parts and remote_parts.group(2) else "",
                "client_id": record.get("client_id", ""),
                "status": record.get("status", ""),
                "error_message": record.get("error_message", ""),
            }))

    # 结构化 git_* 工具同样进 Git 记录页（此前只有 run_command 里的 git 才入库）。
    if tool.startswith("git_") and tool != "git_guard":
        cwd = str(args.get("cwd") or "")
        repo_path, repo_name = _repo_from_command("", cwd)
        operation = tool[len("git_"):]
        is_push = operation == "push"
        out["git_repo"] = repo_name or repo_path
        out["sub_events"].append(("git", {
            "request_id": record.get("request_id", ""),
            "trace_id": record.get("trace_id", ""),
            "repo_path": repo_path,
            "repo_name": repo_name,
            "branch": str(args.get("branch") or "") if is_push else "",
            "operation": operation,
            "files_changed": int(_FILES_RE.search(text).group(1)) if _FILES_RE.search(text) else 0,
            "commit_sha": (_SHA_RE.search(text).group(0)
                           if operation == "commit" and _SHA_RE.search(text) else ""),
            "commit_message": str(args.get("message") or ""),
            "push_remote": str(args.get("remote") or "") if is_push else "",
            "push_branch": str(args.get("branch") or "") if is_push else "",
            "client_id": record.get("client_id", ""),
            "status": record.get("status", ""),
            "error_message": record.get("error_message", ""),
        }))

    # 传输的真实大小/摘要由 gateway.files 直接写 file_transfers 表（含失败记录），
    # 这里只补上通用的 file_path 派生字段。
    if tool in ("upload_file", "download_file", "file_info", "file_checksum"):
        out["file_path"] = str(args.get("path") or "")

    if tool == "run_command_background":
        command = str(args.get("command") or "")
        cwd = str(args.get("cwd") or "")
        task_id = out.get("task_id", "")
        out["experiment_id"] = task_id or f"exp_{record.get('request_id', '')[-8:]}"
        out["sub_events"].append(("experiment", {
            "experiment_id": out["experiment_id"],
            "task_id": task_id,
            "request_id": record.get("request_id", ""),
            "trace_id": record.get("trace_id", ""),
            "name": (command.strip().split()[0] if command.strip() else "task").rsplit("/", 1)[-1],
            "project_path": cwd,
            "command": command,
            "status": "launched",   # 启动调用成功 != 实际训练结果
            "log_path": (f"{os.environ.get('MCP_TASK_LOG_DIR', '/tmp/mcp_tasks')}/{task_id}.log"
                         if task_id else ""),
            "detail": "启动工具调用已完成；训练任务结果由独立生命周期事件记录",
        }))

    if "⚠️ 需确认" in text or args.get("confirm") is False:
        out["approval_id"] = f"apr_{record.get('request_id', '')[-10:]}"

    return out


def safe_repo_label(value: str) -> str:
    """把远端 URL 归一化为不含凭据的展示名。"""
    if "://" in value:
        parsed = urlparse(value)
        return f"{parsed.hostname or ''}{parsed.path}".rstrip("/")
    return value
