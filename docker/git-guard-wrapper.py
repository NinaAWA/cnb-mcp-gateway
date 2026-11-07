#!/usr/bin/env python3
"""容器内所有 `git` 调用的强制入口（真实 git 已移到 /usr/lib/git-core/git.real）。

镜像把 /usr/bin/git 替换为本脚本，因此 shell 命令、脚本文件、Python
subprocess、后台任务里的 git 调用都会先经过 gateway.git_guard.check_argv。
防护模块不可导入时 fail-closed（拒绝执行），不会因为缺少依赖而放行。
"""

from __future__ import annotations

import os
import sys

REAL_GIT = "/usr/lib/git-core/git.real"
GUARD_ROOT = os.environ.get("MCP_GIT_GUARD_ROOT", "/app")

sys.path.insert(0, GUARD_ROOT)
try:
    from gateway.git_guard import check_argv, deny_message
except Exception as exc:  # fail-closed：防护不可用时不执行任何 git 命令
    print(f"[git-guard] 防护模块不可用，拒绝执行 git: {exc!r}", file=sys.stderr)
    raise SystemExit(128)

decision = check_argv(sys.argv[1:])
if not decision.allowed:
    print(deny_message(decision), file=sys.stderr)
    print(f"[git-guard] argv={sys.argv[1:]!r}", file=sys.stderr)
    raise SystemExit(128)

# 这些环境变量可注入 alias / 替换子命令实现，从而绕过 argv 检查。
for key in list(os.environ):
    if key.startswith("GIT_CONFIG_KEY_") or key in (
        "GIT_CONFIG_COUNT", "GIT_CONFIG", "GIT_EXEC_PATH", "GIT_CONFIG_PARAMETERS",
    ):
        os.environ.pop(key, None)

os.execv(REAL_GIT, ["git", *sys.argv[1:]])
