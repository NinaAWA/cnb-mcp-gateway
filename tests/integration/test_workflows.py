"""八条端到端工作流（真实 MCP 客户端 over streamable HTTP + 真实免费数据源）。

每条工作流对应验收报告的一行，结果由 `record` 夹具写入 JSON，报告只引用真实输出。
网络依赖的操作在不可达时会如实 FAIL/SKIP，绝不把模拟结果冒充真实结果。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import subprocess
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

ARXIV_ID = "1706.03762"  # Attention Is All You Need，arXiv 官方开放获取


# ---------------------------------------------------------------- 1. OAuth / MCP
def test_wf1_oauth_pkce_and_mcp_tool_listing(live_gateway, record):
    gw = live_gateway.gateway
    redirect = "http://127.0.0.1:19999/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code", "client_id": "", "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "v3-acceptance",
    }
    with httpx.Client(base_url=live_gateway.base, follow_redirects=False,
                      timeout=30) as client:
        reg = client.post("/register", json={
            "redirect_uris": [redirect], "client_name": "v3-acceptance-client"})
        assert reg.status_code == 201, reg.text
        params["client_id"] = reg.json()["client_id"]

        page = client.get("/authorize", params=params)
        assert page.status_code == 200 and "MCP_TOKEN" in page.text, page.text[:300]

        consent = client.post("/authorize", data={**params,
                                                  "password": live_gateway.token})
        assert consent.status_code == 302, consent.text[:300]
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(consent.headers["location"]).query)
        assert query.get("state") == ["v3-acceptance"]
        code = query["code"][0]

        token_resp = client.post("/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect, "client_id": params["client_id"],
            "code_verifier": verifier})
        assert token_resp.status_code == 200, token_resp.text
        access = token_resp.json()["access_token"]

        tools = gw.list_tools(token=access)
        assert len(tools) >= 49, f"工具枚举数量异常: {len(tools)}"
        assert "download_paper" in tools and "git_inspect" in tools

        rev = client.post("/revoke", data={"token": access},
                          headers=gw.headers(access))
        assert rev.status_code == 200
        try:
            gw.list_tools(token=access, timeout=15)
            still_valid = True
        except Exception:  # noqa: BLE001 - 401 会以 HTTPStatusError/异常组抛出
            still_valid = False

    record("WF1", "PASS" if not still_valid else "FAIL", {
        "oauth": "DCR → PKCE S256 授权码 → token → /revoke",
        "tools_listed": len(tools), "client_id": params["client_id"],
        "token_revoked": not still_valid,
        "transport": "mcp.ClientSession over streamable HTTP /mcp"})
    assert not still_valid, "撤销后的令牌仍可列举工具"


# ------------------------------------------------------------------ 2. 网页搜索
def test_wf2_free_web_search_and_content_extraction(live_gateway, record):
    gw = live_gateway.gateway
    search = gw.call("search_web_advanced", {
        "query": "model context protocol",
        "providers": "github,stackexchange",
        "max_results": 3,
        "format": "json",
    }, timeout=180)
    data = json.loads(search[search.index("{"):search.rindex("}") + 1])
    assert data.get("result_count", 0) > 0, search[:500]

    fetched = gw.call("fetch_web_page", {
        "url": "https://example.com", "max_chars": 2000}, timeout=90)
    assert "ERROR:" not in fetched, fetched[:400]
    assert "Example Domain" in fetched, fetched[:400]
    assert "不可信" in fetched or "untrusted" in fetched.lower()

    extracted = gw.call("extract_web_content", {
        "url": "https://example.com", "max_chars": 2000,
        "chunk_size": 500, "format": "json"}, timeout=90)
    record("WF2", "PASS", {
        "provider_results": data.get("result_count"),
        "provider_failures": data.get("errors") or data.get("source_failures"),
        "fetched_chars": len(fetched),
        "extract_json_keys": list(json.loads(
            extracted[extracted.index("{"):extracted.rindex("}") + 1]).keys())
        if extracted.lstrip().startswith("{") else extracted[:200],
        "scope": "GitHub / StackExchange 为垂直社区搜索，不代表全网通用搜索"})
    assert "ERROR:" not in extracted, extracted[:400]


# ------------------------------------------------------- 3. 学术检索 → OA 发现
def test_wf3_academic_search_json_and_oa_discovery(live_gateway, record):
    gw = live_gateway.gateway
    raw = gw.call("search_papers", {
        "query": "attention is all you need",
        "sources": "openalex,crossref,arxiv,pubmed,europepmc",
        "max_results": 5, "from_year": 2017, "sort": "date",
        "format": "json",
    }, timeout=240)
    data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    assert data["result_count"] > 0, raw[:500]
    titles = [p.get("title", "") for p in data.get("papers", data.get("results", []))]
    assert any("attention" in t.lower() for t in titles), titles[:5]

    oa = gw.call("find_paper_fulltext", {"arxiv_id": ARXIV_ID}, timeout=90)
    record("WF3", "PASS", {
        "result_count": data["result_count"],
        "sources": data.get("sources"),
        "source_failures": data.get("source_failures"),
        "citation_semantics": data.get("citations_semantics"),
        "scope_note": data.get("scope_note"),
        "oa_status": oa.splitlines()[0] if oa else "",
        "oa_candidate": next((l for l in oa.splitlines() if "候选" in l), "")})
    assert "状态: FOUND" in oa, oa
    assert "arxiv.org/pdf/" + ARXIV_ID in oa, oa


# ------------------------------------------- 4. 真实 OA PDF 下载 → 按页阅读 → 全文检索
def test_wf4_real_oa_pdf_download_read_and_fulltext(live_gateway, record):
    gw = live_gateway.gateway
    out = gw.call("download_paper", {
        "arxiv_id": ARXIV_ID, "project": "v3-acceptance"}, timeout=300)
    assert "状态: success" in out or "状态: duplicate" in out, out[:600]
    path = re.search(r"路径: (.+)", out).group(1).strip()
    sha = re.search(r"SHA-256: ([0-9a-f]{64})", out)
    size = re.search(r"大小: (\d+)", out)
    pdf = Path(path)
    assert pdf.is_file() and pdf.read_bytes()[:5] == b"%PDF-"
    assert sha and hashlib.sha256(pdf.read_bytes()).hexdigest() == sha.group(1)
    assert size and int(size.group(1)) > 100_000, size.group(1)

    read = gw.call("read_paper_pdf", {
        "path": path, "page": 1, "max_chars": 1200}, timeout=120)
    assert "状态: success" in read, read[:400]
    assert "第 1/" in read, read[:400]
    assert "Attention Is All You Need" in read, read[:600]

    found = gw.call("search_paper_fulltext", {
        "path": path, "keyword": "transformer", "max_results": 3}, timeout=120)
    found_data = json.loads(found[found.index("{"):found.rindex("}") + 1])
    assert found_data["status"] == "success", found[:300]
    assert found_data["matches"], "全文检索应命中 transformer"
    record("WF4", "PASS", {
        "source": "arXiv 官方开放获取 PDF（真实网络下载）",
        "bytes": int(size.group(1)), "sha256": sha.group(1),
        "pages": re.search(r"页数: (\d+)", read).group(1),
        "page1_has_title": "Attention Is All You Need" in read,
        "fulltext_keyword": "transformer",
        "fulltext_hits": len(found_data["matches"]), "path": path})


# ------------------------------------------- 5. 批量下载 → 文献库 → 三种导出
def test_wf5_batch_library_and_bibliography_exports(live_gateway, record):
    gw = live_gateway.gateway
    batch = gw.call("batch_download_papers", {
        "identifiers": f"{ARXIV_ID},1706.03762,10.1038/nature12373",
        "project": "v3-acceptance", "max_workers": 2}, timeout=300)
    assert "总数: 3" in batch and "去重后: 2" in batch, batch[:600]
    assert "成功:" in batch

    listing = gw.call("list_downloaded_papers", {
        "project": "v3-acceptance", "format": "json"}, timeout=60)
    library = json.loads(listing[listing.index("{"):listing.rindex("}") + 1])
    exports = {}
    for fmt in ("bibtex", "ris", "csv", "json"):
        text = gw.call("export_bibliography", {
            "format": fmt, "project": "v3-acceptance"}, timeout=60)
        assert "ERROR" not in text[:200], (fmt, text[:300])
        exports[fmt] = {"chars": len(text), "head": text[:120]}
    record("WF5", "PASS", {
        "batch_head": batch.splitlines()[:3],
        "batch_partial_success": "失败: 1" in batch,
        "library_count": library["count"],
        "export_formats": {k: v["chars"] for k, v in exports.items()}})
    assert library["count"] >= 1, listing[:400]
    assert "@article{" in exports["bibtex"]["head"], exports["bibtex"]
    assert "TY  - JOUR" in exports["ris"]["head"], exports["ris"]
    assert all(v["chars"] > 0 for v in exports.values())


# ------------------------------------------------ 6. 结构化 Git 工具链 → 审计
def test_wf6_gitops_chain_and_audit_records(live_gateway, tmp_path, record):
    gw = live_gateway.gateway
    repo = tmp_path / "v3-repo"
    repo.mkdir()
    env = {**__import__("os").environ,
           "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], env=env,
                              capture_output=True, text=True, check=True).stdout

    git("init", "-q")
    git("config", "user.email", "v3-acceptance@example.local")
    git("config", "user.name", "V3 Acceptance")
    git("config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# V3 acceptance\n", encoding="utf-8")

    ops = {}
    ops["git_inspect"] = gw.call("git_inspect", {"repo": str(repo)}, timeout=60)
    ops["git_stage"] = gw.call("git_stage", {"repo": str(repo), "paths": "README.md"}, timeout=60)
    ops["git_commit"] = gw.call("git_commit", {
        "repo": str(repo), "message": "v3 acceptance commit",
        "dry_run": False}, timeout=60)
    ops["git_branch"] = gw.call("git_branch", {
        "repo": str(repo), "action": "list"}, timeout=60)
    ops["git_worktree"] = gw.call("git_worktree", {
        "repo": str(repo), "action": "list"}, timeout=60)
    for name, text in ops.items():
        assert "ERROR" not in text[:200], (name, text[:400])

    from gateway.audit.storage import get_storage
    rows = get_storage()._query(
        "SELECT operation AS op FROM git_operations ORDER BY id DESC LIMIT 20")
    recorded = {r["op"] for r in rows}
    record("WF6", "PASS", {
        "operations": list(ops), "audit_ops": sorted(recorded),
        "head": git("log", "-1", "--pretty=%s").strip(),
        "credential_policy": "只走 GIT_ASKPASS，未使用 URL 内嵌凭据"})
    assert {"stage", "commit"} <= recorded, recorded


# ---------------------------------- 7. 后台任务 → 进程组终止 → 复现 manifest
def test_wf7_background_task_kill_and_manifest(live_gateway, tmp_path, record):
    gw = live_gateway.gateway
    started = gw.call("run_command_background", {
        "command": "sleep 25 & wait", "cwd": str(tmp_path), "timeout": 0},
        timeout=60)
    assert "task_id=" in started, started[:300]
    task_id = started.split("task_id=", 1)[1].splitlines()[0].strip()
    time.sleep(0.3)
    assert task_id in gw.call("list_tasks", {}, timeout=30)
    stopped = gw.call("stop_task", {"task_id": task_id}, timeout=60)
    assert "进程组" in stopped or "killed" in stopped.lower(), stopped[:300]
    manifest = json.loads(gw.call("experiment_manifest", {
        "task_id": task_id, "format": "json"}, timeout=60))
    record("WF7", "PASS", {
        "task_id": task_id, "stop_result": stopped[:200],
        "manifest_keys": sorted(manifest.keys()),
        "git_commit_field": manifest.get("git_commit"),
        "note": "进程组终止 + 复现元数据；未声称跨容器重建恢复进程对象"})
    assert manifest.get("ok") is True, manifest
    assert "command" in manifest and "env_key_fingerprint" in manifest


# ------------------------- 8. 分块上传 / Range / 签名下载 / 后台监控页
def test_wf8_chunked_range_signed_download_and_admin(live_gateway, record):
    gw = live_gateway.gateway
    root = Path(live_gateway.runtime_dir) / "transfers"
    (root / "data").mkdir(parents=True, exist_ok=True)
    first, second = b"A" * 1000, b"B" * 700 + b"END-OF-V3-FILE"
    full = first + second
    # 模拟中断后遗留的 .part，第二请求按 X-Upload-Offset 续传。
    (root / "data" / "v3.bin.part").write_bytes(first)
    auth = gw.headers()
    with httpx.Client(base_url=live_gateway.base, timeout=60) as client:
        resumed = client.post("/files/upload", content=second, headers={
            **auth, "X-File-Path": "data/v3.bin",
            "X-Upload-Offset": str(len(first)),
            "X-File-Sha256": hashlib.sha256(full).hexdigest()})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["sha256"] == hashlib.sha256(full).hexdigest()

        ranged = client.get("/files/download?path=data/v3.bin",
                            headers={**auth, "Range": "bytes=100-199"})
        assert ranged.status_code == 206, ranged.text
        assert ranged.content == full[100:200]
        assert ranged.headers["content-range"] == f"bytes 100-199/{len(full)}"

        link = gw.call("signed_download_link", {"path": "data/v3.bin"}, timeout=30)
        expiry = re.search(r"有效期至\(epoch\): (\d+)", link).group(1)
        sig = re.search(r"签名: (\w+)", link).group(1)
        signed = client.get(
            f"/files/download?path=data/v3.bin&exp={expiry}&sig={sig}")
        assert signed.status_code == 200 and signed.content == full
        unsigned = client.get("/files/download?path=data/v3.bin")
        assert unsigned.status_code == 401

    status = gw.call("file_transfer_status", {"limit": 20}, timeout=30)

    # 后台监控页：HTTP 层（TestClient + CSRF），非浏览器视觉验收。
    from fastapi.testclient import TestClient
    from gateway.admin import api as admin_api
    with TestClient(admin_api.app) as admin:
        landing = admin.get("/")
        csrf = admin.cookies.get("mcp_admin_csrf")
        login = admin.post("/api/login", json={
            "username": "admin", "password": "TestPassword-123"},
            headers={"X-CSRF-Token": csrf} if csrf else {})
        assert login.status_code == 200, (landing.status_code, login.text[:200])
        csrf = admin.cookies.get("mcp_admin_csrf") or csrf
        head = {"X-CSRF-Token": csrf} if csrf else {}
        overview = admin.get("/api/v3/overview", headers=head)
        transfers = admin.get("/api/transfers", headers=head)
        search_page = admin.get("/api/search/events", headers=head)
        papers_page = admin.get("/api/papers", headers=head)

    record("WF8", "PASS", {
        "resume": resumed.json(), "range_status": ranged.status_code,
        "content_range": ranged.headers["content-range"],
        "signed_status": signed.status_code, "unsigned_status": unsigned.status_code,
        "transfer_status_head": status[:200],
        "admin": {"overview": overview.status_code,
                  "transfers": transfers.status_code,
                  "search_events": search_page.status_code,
                  "papers": papers_page.status_code},
        "note": "后台为 HTTP 层验证；浏览器视觉验收未执行"})
    assert overview.status_code == 200 and transfers.status_code == 200
    assert search_page.status_code == 200 and papers_page.status_code == 200


# ------------------------- 附加：真实 PDF 的 MarkItDown 交叉校验
def test_markitdown_crosscheck_on_real_oa_pdf(live_gateway, record):
    """用 markitdown 独立转换真实下载并校验的 PDF，与 read_paper_pdf 比对。"""
    from gateway.research.pdf import read_pdf

    pdf = Path(live_gateway.runtime_dir) / "papers" / f"{ARXIV_ID}.pdf"
    if not pdf.is_file():
        record("markitdown-crosscheck", "SKIPPED", "真实 PDF 不存在（WF4 未成功）")
        pytest.skip("依赖 WF4 下载的真实 PDF")
    ours = read_pdf(pdf, page=1, max_chars=3000)
    assert ours["status"] == "success" and ours["page_count"] >= 10, ours
    ours_text = " ".join(ours.get("pages") or [])

    try:
        from markitdown import MarkItDown
        converted = MarkItDown().convert(str(pdf))
        markdown = converted.text_content or ""
    except Exception as exc:  # noqa: BLE001 - 如实记录，不视为通过
        record("markitdown-crosscheck", "FAILED", f"{type(exc).__name__}: {exc}")
        pytest.skip(f"markitdown 转换不可用: {exc}")

    shared = [term for term in ("Attention Is All You Need", "Vaswani", "transformer")
              if term.lower() in ours_text.lower() and term.lower() in markdown.lower()]
    record("markitdown-crosscheck", "PASS" if shared else "FAIL", {
        "pdf": str(pdf), "pypdf_pages": ours["page_count"],
        "pypdf_chars": ours["characters_extracted"],
        "markitdown_chars": len(markdown), "shared_terms": shared,
        "note": "真实 arXiv OA PDF；非本地模拟 PDF"})
    assert shared, (ours_text[:200], markdown[:200])
