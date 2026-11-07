"""Git 硬防护：禁止删除 Git 仓库与分支，同时保留正常的提交 / 切换 / 推送。

设计目标与边界
--------------
用户允许 agent 拥有很高的执行权限，但要求「绝不能删除 git 仓库与分支」。
本模块提供三层本地防护：

1. ``check_command``：在执行任何 shell 命令前，对命令文本做分段、引号感知的
   解析，识别 git 删除类子命令、对 ``.git`` / 仓库目录的删除与重定向写入、
   以及用解释器 / 编码管道混淆的删除意图。
2. ``check_argv``：容器内所有 ``git`` 调用都会经过 ``docker/git-guard-wrapper.py``
   （镜像把真实 git 换成 wrapper），因此脚本内、Python subprocess 内、
   后台任务内的 git 命令同样受检；未知子命令（可能是别名）一律拒绝。
3. ``check_write_path``：文件类工具不得直接改写 ``.git`` 内部的 refs / config /
   hooks，也不得覆盖系统凭据文件。

补充提供 ``register_git_tools``：把常用 Git 操作做成参数化（shell=False）的
窄接口，让 agent 有一条不需要拼 shell 字符串就能提交、切分支、推送的路径。

重要边界：本模块防的是误操作与被外部内容诱导的提示注入，不是对抗持有
容器 root 的攻击者（root 可直接操作文件、替换二进制）。要真正不可逆地满足
「不允许删除仓库与分支」，必须同时在远端 Git 服务开启分支保护/禁止删库，
并使用最小权限凭据与独立备份。见 README「Git 删除保护」章节。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "Decision", "ALLOW", "check_command", "check_argv", "check_write_path",
    "run_git", "register_git_tools", "GitGuardError",
]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


ALLOW = Decision(True, "")


class GitGuardError(Exception):
    """结构化 Git 工具的参数未通过硬防护。"""


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


def deny_message(decision: Decision) -> str:
    return (
        "ERROR: 该操作已被 Git 删除保护硬禁止（不可通过 confirm 绕过）：\n"
        f"  {decision.reason}\n"
        "如需删除分支/仓库，请在远端 Git 平台由管理员手动执行；"
        "日常操作请改用 git_* 结构化工具。"
    )


# ---------------------------------------------------------------------------
# git 参数解析
# ---------------------------------------------------------------------------

# 已知子命令白名单：未知子命令通常意味着用户自定义 alias，而 alias 可以展开成
# `branch -D` 之类的删除命令，git wrapper 只能看到展开前的 argv，故一律拒绝。
KNOWN_GIT_SUBCOMMANDS = frozenset({
    "add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch",
    "bundle", "cat-file", "check-attr", "check-ignore", "check-mailmap",
    "check-ref-format", "checkout", "checkout-index", "cherry", "cherry-pick",
    "clean", "clone", "column", "commit", "commit-tree", "config",
    "count-objects", "credential", "describe", "diagnose", "diff", "diff-files",
    "diff-index", "diff-tree", "fetch", "fetch-pack", "filter-branch",
    "fmt-merge-msg", "for-each-ref", "format-patch", "fsck", "gc", "grep",
    "hash-object", "help", "hook", "init", "lfs", "log", "ls-files", "ls-remote",
    "ls-tree", "maintenance", "merge", "merge-base", "merge-file",
    "merge-index", "merge-one-file", "merge-tree", "mergetool", "mktag",
    "mktree", "multi-pack-index", "mv", "name-rev", "notes", "p4",
    "pack-objects", "pack-refs", "prune", "prune-packed", "pull", "push",
    "read-tree", "rebase", "reflog", "remote", "repack", "replace",
    "request-pull", "reset", "restore", "rev-list", "rev-parse", "revert",
    "rm", "shortlog", "show", "show-branch", "show-index", "show-ref",
    "sparse-checkout", "stage", "stash", "status", "stripspace", "submodule",
    "symbolic-ref", "switch", "tag", "unpack-file", "unpack-objects",
    "update-index", "update-ref", "update-server-info", "var", "verify-commit",
    "verify-pack", "verify-tag", "version", "whatchanged", "worktree",
    "write-tree",
})

# 仓库 pre-push 钩子会调用 `git lfs pre-push`。只放行不会删除 LFS 对象、
# 不重写历史的子操作；prune / uninstall / migrate 等一律拒绝，与 Git 删除硬防护一致。
LFS_SAFE_SUBCOMMANDS = frozenset({
    "clean", "env", "fetch", "filter-process", "fsck", "logs", "ls-files",
    "pointer", "post-checkout", "post-commit", "post-merge", "pre-push",
    "push", "smudge", "status", "track", "version",
})

_GLOBAL_NO_VALUE = frozenset({
    "--version", "--help", "--no-pager", "-p", "--paginate", "--bare",
    "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
    "--icase-pathspecs", "--no-optional-locks", "--no-replace-objects",
    "--no-lazy-fetch",
})
_GLOBAL_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
    "--config-env", "--attr-source",
})
_GLOBAL_QUERY = frozenset({"--exec-path", "--html-path", "--man-path", "--info-path"})

_ALIAS_RE = re.compile(r"alias\.", re.IGNORECASE)
_DELETE_FLAG_RE = re.compile(
    r"(--delete\b|--mirror\b|--prune\b|--move\b|(?:^|\s)-(?:d|D|m|M)\b)", re.IGNORECASE
)


def _extra_subcommands() -> frozenset[str]:
    raw = os.environ.get("MCP_GIT_EXTRA_SUBCOMMANDS", "")
    return frozenset(x.strip().lower() for x in raw.replace("|", ",").split(",") if x.strip())


def _split_git_argv(argv: list[str]) -> tuple[str, list[str]]:
    """剥离 git 全局选项，返回 (子命令, 子命令参数)。"""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _GLOBAL_NO_VALUE:
            i += 1
            continue
        if token in _GLOBAL_QUERY:
            return token, []
        if token in _GLOBAL_WITH_VALUE:
            i += 2
            continue
        if token.startswith("--exec-path="):
            return "__exec_path_override__", []
        if any(token.startswith(prefix + "=") for prefix in _GLOBAL_WITH_VALUE):
            i += 1
            continue
        if token.startswith("-c") and token != "-c":
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token.lower(), argv[i + 1:]
    return "", []


def _has_delete_refspec(args: Iterable[str]) -> bool:
    """git push 的 `:branch` / `branch:` / `+branch` 都能删除或强推远端 ref。"""
    for token in args:
        if token.startswith("-") or token.startswith("+"):
            continue
        if ":" not in token:
            continue
        left, right = token.split(":", 1)
        if not left or not right or left.startswith("+"):
            return True
    return False


def check_argv(argv: list[str]) -> Decision:
    """检查一次 git 调用的参数（不含可执行文件名 ``git`` 本身）。"""
    if not argv:
        return ALLOW

    joined = " ".join(argv)
    # alias 可把 `git del -D main` 展开成 `git branch -D main`，wrapper 看不到展开结果；
    # 因此禁止任何写入 alias 的调用（读取类 --get/--list 不影响）。
    if _ALIAS_RE.search(joined) and not any(a.startswith(("--get", "--list", "-l")) for a in argv):
        return _deny("禁止定义或修改 git alias（可隐藏分支删除命令）")

    sub, args = _split_git_argv(argv)
    if sub == "__exec_path_override__":
        return _deny("禁止用 --exec-path 替换 git 子命令实现")
    if not sub or sub.startswith("--"):
        return ALLOW
    if sub not in KNOWN_GIT_SUBCOMMANDS | _extra_subcommands():
        return _deny(f"未知 git 子命令 {sub!r}（可能是可展开成删除命令的 alias）")

    if sub == "lfs":
        # 取首个非选项参数作为 LFS 子操作。`git lfs pre-push origin <url>` 的远端
        # URL 常以 .git 结尾，属于合法参数，不按危险 token 误杀。
        lfs_sub = next((a.lower() for a in args if a and not a.startswith("-")), "")
        if lfs_sub not in LFS_SAFE_SUBCOMMANDS:
            return _deny(f"git lfs {lfs_sub or '(空子操作)'} 不在允许的安全子操作内")
        lfs_flat = " ".join(args)
        if _DELETE_FLAG_RE.search(lfs_flat) or re.search(
                r"--(?:delete|force|prune|mirror|remove|uninstall)\b", lfs_flat, re.IGNORECASE):
            return _deny("git lfs 的删除 / 强制类参数（与 Git 删除硬防护冲突）")
        return ALLOW

    flat = " ".join(args)

    if sub == "branch" and _DELETE_FLAG_RE.search(flat):
        return _deny("git branch 的删除 / 重命名 / 强制操作")
    if sub == "push":
        if _DELETE_FLAG_RE.search(flat):
            return _deny("git push --delete / --mirror / --prune 等可删除远端 ref 的操作")
        if _has_delete_refspec(args):
            return _deny("git push 的删除型 refspec（:branch / branch: / +branch）")
        if any(a.startswith("+") for a in args):
            return _deny("git push 的强制 refspec")
    if sub == "update-ref":
        return _deny("git update-ref 可直接删除任意 ref（含分支）")
    if sub in ("fetch", "pull") and re.search(r"--prune\b|(?:^|\s)-p\b", flat, re.IGNORECASE):
        return _deny(f"git {sub} --prune 会删除本地跟踪分支")
    if sub == "remote" and re.search(r"(?:^|\s)(?:remove|rm|prune)\b", flat, re.IGNORECASE):
        return _deny("git remote remove / prune 会删除远端或跟踪分支配置")
    if sub == "worktree" and re.search(r"(?:^|\s)(?:remove|prune)\b", flat, re.IGNORECASE):
        return _deny("git worktree remove / prune 会删除工作树及其分支引用")
    if sub == "tag" and _DELETE_FLAG_RE.search(flat):
        return _deny("git tag 的删除操作")
    if sub == "symbolic-ref" and _DELETE_FLAG_RE.search(flat):
        return _deny("git symbolic-ref --delete")
    if sub == "config" and re.search(r"--unset-all\b|--remove-section\b", flat, re.IGNORECASE):
        if "alias." in flat.lower():
            return _deny("禁止通过 git config 隐藏/清除分支删除 alias")
    return ALLOW


# ---------------------------------------------------------------------------
# shell 命令文本解析
# ---------------------------------------------------------------------------

_GIT_BIN_RE = re.compile(r"(?:^|/)(?:git|git\.real)$")
_FS_COMMANDS = frozenset({"rm", "rmdir", "unlink", "shred", "mv", "find", "truncate", "dd"})
_WRAPPERS = frozenset({"sudo", "env", "command", "nice", "nohup", "time", "exec", "builtin"})
_INTERPRETER_RE = re.compile(
    r"^(?:ba|da|z|k|fi|r)?sh|python[0-9.]*|perl|ruby|node|php|lua$", re.IGNORECASE
)
_INTERPRETER_FLAG_RE = re.compile(r"^-[a-z]*[ce]$|^--command$|^-[a-z]*e$", re.IGNORECASE)
_DANGEROUS_TOKEN_RE = re.compile(
    r"git\s+(?:-[-a-zA-Z =./]+\s+)*(?:branch\b[^;\n]*-(?:d|D|m|M)\b|"
    r"push\b[^;\n]*(?:--delete|--mirror|--prune|:\s*\S+\s*$)|"
    r"update-ref\b|worktree\s+(?:remove|prune)\b|remote\s+(?:remove|prune)\b|"
    r"symbolic-ref\b[^\n]*--delete)|"
    r"refs/heads/|\.git(?:/|\b)|--delete\b|--mirror\b|update-ref\b",
    re.IGNORECASE,
)
_DECODER_RE = re.compile(r"\bbase64\b|\bxxd\b[^;\n]*-r\b|\bopenssl\b[^;\n]*\benc\b", re.IGNORECASE)
_GIT_DOT_RE = re.compile(r"(?<![\w.-])\.git(?:/|\b)", re.IGNORECASE)

# 解释器 -c/-e 里的 Python/Perl 代码不是 shell 命令，需要单独识别「git + 删除」
# 或「删 .git 目录」的意图（例如 subprocess.run(['git','branch','-D','main'])）。
_INTERPRETER_DANGER_RE = re.compile(
    r"\bgit\b.{0,160}?(?:--delete|-D\b|-d\b|:refs/|refs/heads|update-ref|"
    r"worktree.{0,20}?remove|branch.{0,20}?-[dDmM])"
    r"|\.git\b.{0,120}?(?:rmtree|remove|unlink|rm\s+-[a-zA-Z]*r)"
    r"|(?:rmtree|remove|unlink|shutil|os\.system)\b.{0,120}?\.git\b",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_WRITE_RE = re.compile(r">{1,2}\s*['\"]?[^\s;|>&]+\.(?:sh|bash|py|pl|rb|js)\b", re.IGNORECASE)
_SCRIPT_RUN_RE = re.compile(
    r"\b(?:ba|da|z|k|r)?sh\s+[^\s;|]+\.(?:sh|bash)\b"
    r"|\bpython[0-9.]*\s+[^\s;|]+\.py\b|\bchmod\s+\+x\b", re.IGNORECASE,
)


def _shell_segments(command: str) -> list[str]:
    """引号感知地把一条 shell 命令拆成多个子命令片段。"""
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                quote = ""
            i += 1
            continue
        if ch in ("'", '"', "`"):
            if ch == "`":
                segments.append("".join(buf))
                buf = []
            else:
                quote = ch
                buf.append(ch)
            i += 1
            continue
        if command.startswith("$(", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";&|\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    while tokens:
        head = tokens[0]
        if "=" in head and not head.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            tokens = tokens[1:]
            continue
        if Path(head).name in _WRAPPERS:
            tokens = tokens[1:]
            continue
        break
    return tokens


def _is_git_bin(token: str) -> bool:
    return bool(_GIT_BIN_RE.search(Path(token).name)) or Path(token).name in ("git", "git.real")


def _protected_workspace(cwd: str) -> Path:
    return Path(os.environ.get("MCP_WORKDIR", cwd or "/workspace")).resolve()


def _target_is_repository(raw: str, cwd: str) -> bool:
    target = raw.strip().strip("'\"")
    if not target or target.startswith("-"):
        return False
    if target in (".", "./", "*", "./*", "$PWD", "${PWD}", "..", "../"):
        return _workspace_has_git(_protected_workspace(cwd))
    try:
        path = Path(os.path.expandvars(target)).expanduser()
        resolved = (path if path.is_absolute() else Path(cwd) / path).resolve()
    except (OSError, RuntimeError):
        return False
    if resolved.name == ".git" or ".git" in resolved.parts:
        return True
    if (resolved / ".git").exists():
        return True
    workspace = _protected_workspace(cwd)
    if resolved == workspace and _workspace_has_git(workspace):
        return True
    return False


def _workspace_has_git(root: Path) -> bool:
    """工作区内是否存在任何 git 仓库（限制递归深度，避免大目录扫描）。"""
    try:
        if (root / ".git").exists():
            return True
        for child in root.iterdir():
            if child.is_dir() and (child / ".git").exists():
                return True
    except OSError:
        return False
    return False


def _check_fs_segment(tokens: list[str], cwd: str) -> Decision:
    if not tokens:
        return ALLOW
    cmd = Path(tokens[0]).name.lower()
    if cmd not in _FS_COMMANDS:
        return ALLOW
    flat = " ".join(tokens)
    if _GIT_DOT_RE.search(flat):
        return _deny("命令涉及删除/移动 .git 目录或其中的引用")
    if cmd in ("rm", "mv", "rmdir", "shred", "find", "truncate"):
        for token in tokens[1:]:
            if _target_is_repository(token, cwd):
                return _deny(f"命令目标包含 Git 仓库: {token}")
    return ALLOW


def _check_segment(segment: str, cwd: str, depth: int = 0) -> Decision:
    tokens = _strip_wrappers(_tokens(segment))
    if not tokens:
        return ALLOW

    # 重定向到 .git 内部（写 refs / config / hooks）
    if re.search(r">{1,2}\s*[^\s;|]*\.git(/|\b)", segment, re.IGNORECASE):
        return _deny("禁止重定向写入 .git 内部的引用 / 配置 / hooks")

    fs_decision = _check_fs_segment(tokens, cwd)
    if not fs_decision.allowed:
        return fs_decision

    cmd = Path(tokens[0]).name
    if _is_git_bin(cmd):
        decision = check_argv(tokens[1:])
        if not decision.allowed:
            return decision

    # 解释器 -c/-e：递归检查被嵌入的命令文本，并拦截混淆后的删除意图
    if _INTERPRETER_RE.match(cmd) and depth < 3:
        for index, token in enumerate(tokens[1:], start=1):
            if _INTERPRETER_FLAG_RE.match(token) and index + 1 < len(tokens):
                inner = check_command(tokens[index + 1], cwd=cwd, depth=depth + 1)
                if not inner.allowed:
                    return inner
            elif (_DANGEROUS_TOKEN_RE.search(token)
                  and re.search(r"\bgit\b|\.git\b|refs/heads", token, re.IGNORECASE)):
                return _deny("解释器中嵌入了 Git 删除类命令（混淆执行被禁止）")
    elif _DANGEROUS_TOKEN_RE.search(" ".join(tokens)) and cmd not in ("grep", "rg", "echo", "cat"):
        # 非解释器但被拆进子命令的写法（如 $(git branch -D main)）也覆盖
        if any(_is_git_bin(t) for t in tokens[1:]):
            return _deny("子命令中包含 Git 删除类操作")
    return ALLOW


def check_command(command: str, cwd: str = "", *, depth: int = 0) -> Decision:
    """在执行 shell 命令前调用；拒绝任何删除仓库/分支的意图。"""
    if depth > 3:
        return ALLOW
    text = (command or "").strip()
    if not text:
        return ALLOW
    base = os.path.abspath(cwd or os.environ.get("MCP_WORKDIR", "/workspace"))
    segments = _shell_segments(text)

    # 编码/解码后管道给 shell：无法静态解析内容，直接拒绝这类混淆组合
    if _DECODER_RE.search(text) and re.search(r"\|\s*(?:ba|da|z|k|r)?sh\b|\bexec\s+sh\b", text, re.IGNORECASE):
        return _deny("禁止以编码解码 + shell 管道方式执行不可解析的命令")

    # 解释器内嵌代码（python -c / perl -e / node -e ...）中的删除意图
    if re.search(r"\b(?:python[0-9.]*|perl|ruby|node|php|lua)\b[^;\n]*\s-[a-z]*[ce]\b",
                 text, re.IGNORECASE) and _INTERPRETER_DANGER_RE.search(text):
        return _deny("解释器中嵌入了 Git 删除类代码（混淆执行被禁止）")

    # 把删除命令写进脚本再执行：git wrapper 可拦 git 子命令，但脚本也可能
    # 直接删除 .git 目录，因此在命令层面拒绝这种组合。
    if (_SCRIPT_WRITE_RE.search(text) and _SCRIPT_RUN_RE.search(text)
            and _DANGEROUS_TOKEN_RE.search(text)):
        return _deny("禁止把 Git 删除命令写入脚本后执行")

    for segment in segments:
        decision = _check_segment(segment, base, depth)
        if not decision.allowed:
            return decision
    return ALLOW


# ---------------------------------------------------------------------------
# 文件工具的路径约束
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PREFIXES = (
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/root/.ssh",
    "/app/.env", "/proc/", "/sys/", "/dev/",
)


def check_write_path(path: str) -> Decision:
    """文件写入工具统一调用：禁止直接改写 .git 与系统凭据。"""
    try:
        resolved = Path(os.path.expandvars(path)).expanduser().resolve()
    except (OSError, RuntimeError):
        return _deny(f"无法解析路径: {path}")
    parts = {p.lower() for p in resolved.parts}
    if ".git" in parts:
        return _deny("禁止通过文件工具改写 .git 内部的 refs / config / hooks")
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if str(resolved).lower().startswith(prefix.lower()):
            return _deny(f"禁止写入受保护的系统路径: {prefix}")
    return ALLOW


# ---------------------------------------------------------------------------
# 结构化 git 工具（shell=False 的正规通道）
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,200}$")


def _check_ref(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not _REF_RE.match(value) or value.startswith("-") or ":" in value or value.startswith("+"):
        raise GitGuardError(f"{label} 不是合法的 ref/remote 名称: {value!r}")
    return value


def _check_cwd(cwd: str, workdir: str) -> str:
    root = Path(workdir).resolve()
    target = Path(cwd or workdir).resolve()
    if target != root and root not in target.parents:
        raise GitGuardError(f"cwd 必须位于 MCP_WORKDIR ({root}) 之内: {target}")
    return str(target)


def run_git(args: list[str], *, cwd: str, timeout: int = 60) -> str:
    """参数化执行 git；先过 check_argv，再由容器 wrapper 二次校验。"""
    decision = check_argv(args)
    if not decision.allowed:
        raise GitGuardError(decision.reason)
    proc = subprocess.run(
        ["git", *args], shell=False, cwd=cwd, capture_output=True,
        text=True, timeout=timeout, check=False,
    )
    return f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"


def register_git_tools(mcp: Any, workdir: str, audit: Callable[[str], None]) -> None:
    """把常用 Git 操作注册为 MCP 工具；不依赖 shell，删除类操作无对应接口。"""
    root = str(Path(workdir).resolve())

    def _run(args: list[str], cwd: str) -> str:
        try:
            target = _check_cwd(cwd, root)
            out = run_git(args, cwd=target)
        except GitGuardError as exc:
            audit(f"GIT DENIED argv={args!r} reason={exc}")
            return deny_message(_deny(str(exc)))
        except (OSError, subprocess.SubprocessError) as exc:
            return f"ERROR: git 执行失败: {exc}"
        audit(f"GIT OK argv={args!r} cwd={target}")
        return out

    @mcp.tool()
    def git_status(cwd: str = workdir) -> str:
        """查看 Git 仓库状态（只读）。Args: cwd: 仓库路径，默认工作目录。"""
        return _run(["status", "--short", "--branch"], cwd)

    @mcp.tool()
    def git_current_branch(cwd: str = workdir) -> str:
        """查看当前分支名（只读）。Args: cwd: 仓库路径。"""
        return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)

    @mcp.tool()
    def git_log(cwd: str = workdir, n: int = 10) -> str:
        """查看最近若干条提交（只读）。Args: cwd: 仓库路径；n: 条数，1-100。"""
        return _run(["log", "--oneline", "--decorate", "-n", str(max(1, min(100, int(n))))], cwd)

    @mcp.tool()
    def git_diff(cwd: str = workdir, staged: bool = False) -> str:
        """查看工作区/暂存区差异（只读）。Args: cwd: 仓库路径；staged: 看已暂存改动。"""
        return _run(["diff", "--staged"] if staged else ["diff"], cwd)

    @mcp.tool()
    def git_branch_list(cwd: str = workdir) -> str:
        """列出本地与远端分支（只读，不提供删除接口）。Args: cwd: 仓库路径。"""
        return _run(["branch", "-a", "--no-color"], cwd)

    @mcp.tool()
    def git_branch_create(branch: str, cwd: str = workdir, start_point: str = "") -> str:
        """创建新分支（不允许删除或重命名分支）。Args: branch: 新分支名；cwd: 仓库路径；start_point: 可选起点。"""
        try:
            name = _check_ref(branch, "branch")
            base = _check_ref(start_point, "start_point")
        except GitGuardError as exc:
            return deny_message(_deny(str(exc)))
        return _run(["branch", name, *([base] if base else [])], cwd)

    @mcp.tool()
    def git_switch(branch: str, cwd: str = workdir, create: bool = False) -> str:
        """切换分支（等价于 git switch / git checkout -b，不会删除任何分支）。
        Args: branch: 目标分支名；cwd: 仓库路径；create: 分支不存在时是否创建。
        """
        try:
            name = _check_ref(branch, "branch")
        except GitGuardError as exc:
            return deny_message(_deny(str(exc)))
        return _run(["switch", "-c", name] if create else ["switch", name], cwd)

    @mcp.tool()
    def git_add(paths: str, cwd: str = workdir) -> str:
        """暂存文件改动。Args: paths: 以空格分隔的路径（支持 . 表示全部）；cwd: 仓库路径。"""
        items = [p for p in paths.split() if p]
        if not items:
            return "ERROR: 请提供至少一个路径"
        for item in items:
            if item.startswith("-") or ":" in item:
                return deny_message(_deny(f"非法路径参数: {item!r}"))
        return _run(["add", "--", *items], cwd)

    @mcp.tool()
    def git_commit(message: str, cwd: str = workdir) -> str:
        """提交已暂存的改动。Args: message: 提交信息；cwd: 仓库路径。"""
        if not message.strip():
            return "ERROR: 提交信息不能为空"
        return _run(["commit", "-m", message], cwd)

    @mcp.tool()
    def git_fetch(cwd: str = workdir, remote: str = "origin") -> str:
        """从远端拉取引用（禁止 --prune，避免删除跟踪分支）。Args: cwd: 仓库路径；remote: 远端名。"""
        try:
            name = _check_ref(remote, "remote")
        except GitGuardError as exc:
            return deny_message(_deny(str(exc)))
        return _run(["fetch", name] if name else ["fetch"], cwd)

    @mcp.tool()
    def git_pull(cwd: str = workdir, remote: str = "origin", branch: str = "") -> str:
        """拉取并合并远端更新（禁止 --prune）。Args: cwd: 仓库路径；remote: 远端名；branch: 可选分支名。"""
        try:
            name = _check_ref(remote, "remote")
            ref = _check_ref(branch, "branch")
        except GitGuardError as exc:
            return deny_message(_deny(str(exc)))
        return _run(["pull", *([name] if name else []), *([ref] if ref else [])], cwd)

    @mcp.tool()
    def git_push(cwd: str = workdir, remote: str = "origin", branch: str = "") -> str:
        """推送分支到远端。严禁 --force / --delete / 删除型 refspec；如需强推请走管理员手动流程。
        Args: cwd: 仓库路径；remote: 远端名；branch: 要推送的本地分支名，留空则推送当前分支。
        """
        try:
            name = _check_ref(remote, "remote")
            ref = _check_ref(branch, "branch")
        except GitGuardError as exc:
            return deny_message(_deny(str(exc)))
        return _run(["push", *([name] if name else []), *([ref] if ref else [])], cwd)

    @mcp.tool()
    def git_remote_list(cwd: str = workdir) -> str:
        """列出远端（只读）。Args: cwd: 仓库路径。"""
        return _run(["remote", "-v"], cwd)
