(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const terminal = new Set(["succeeded", "partial", "failed", "cancelled"]);
  const labels = {queued: "等待同步", running: "正在同步", succeeded: "同步完成", partial: "部分完成", failed: "同步失败", cancelled: "已停止"};
  let context = null, libraryId = "", timer = null, sequence = 0, data = null, busy = false, requestPending = false;
  function node(tag, text, className = "") { const item = document.createElement(tag); item.textContent = text; item.className = className; return item; }
  function error(message = "") { $("index-error").textContent = message; $("index-error").hidden = !message; }
  async function api(path, body) {
    let token = "";
    try { token = sessionStorage.getItem("dcx-token") || ""; } catch (_) { /* no storage */ }
    const response = await fetch(path, {method: body === undefined ? "GET" : "POST", headers: { ...(token ? {"X-DCX-Token": token} : {}), ...(body === undefined ? {} : {"Content-Type": "application/json"})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(result?.error || `服务请求失败 (${response.status})`);
    return result;
  }
  function render(payload) {
    data = payload;
    const job = payload.jobs.find((item) => !terminal.has(item.status)) || payload.jobs[0];
    const active = job && !terminal.has(job.status);
    $("index-summary").textContent = `${payload.indexed_documents} / ${payload.total_documents} 篇已完整同步 · ${payload.pending_documents} 篇待同步` + (payload.partial_documents ? ` · ${payload.partial_documents} 篇部分可用` : "");
    const counts = payload.counts;
    const stats = [["文本块", counts.text_block, "按语义段落组织"], ["图像", counts.image, "图表或页面"], ["图注／描述", counts.caption_original + counts.caption_generated, `${counts.caption_original} 原图注 · ${counts.caption_generated} 生成`], ["原生图文", counts.image_caption_original + counts.image_caption_generated, "模型原生联合编码"]];
    $("index-coverage").replaceChildren(...stats.map(([name, count, detail]) => { const card = document.createElement("div"); card.append(node("span", name), node("strong", String(count)), node("small", detail)); return card; }));
    const services = [[`${payload.model} · ${payload.dimensions} 维`, payload.embedding_ready && payload.enabled, payload.enabled ? `服务端需要 ${payload.api_key_env}` : "配置中已禁用索引"], [`${payload.joint_protocol === "dashscope" ? "阿里云百炼" : "原生服务"}：${payload.joint_model} · ${payload.joint_dimensions} 维`, payload.joint_ready, `需要服务端 ${payload.joint_key_env}，相关向量尚未就绪。`], [`描述模型：${payload.caption_model}`, payload.caption_ready, `服务端需要 ${payload.caption_key_env}`]];
    $("index-services").replaceChildren(...services.map(([name, ready, reason]) => node("p", `${ready ? "●" : "○"} ${name}${ready ? " · 已配置" : ` · ${reason}`}`, ready ? "" : "unavailable")));
    for (const issue of payload.source_errors || []) $("index-services").appendChild(node("p", `${issue.doc_id}：${issue.error}`, "unavailable"));
    $("index-services").appendChild(node("p", "Gitee 与联合编码使用独立向量空间；查询分别编码后融合排序。"));
    $("index-services").appendChild(node("p", payload.auto_sync ? "已启用新文档自动同步。" : "首次同步后，此文档库新增的文档会自动进入索引任务。"));
    $("index-captions").checked = payload.generate_captions && payload.caption_ready;
    $("index-caption-model").value = payload.caption_model;
    $("index-captions").disabled = active || busy || !payload.caption_ready;
    $("index-caption-model").disabled = active || busy;
    $("index-sync").disabled = active || busy || !payload.enabled || !payload.embedding_ready;
    $("index-rebuild").disabled = $("index-sync").disabled;
    $("index-stop").hidden = !active;
    $("index-stop").disabled = busy;
    $("index-progress").hidden = !job;
    if (job) {
      $("index-job-status").textContent = labels[job.status] || job.status;
      $("index-job-count").textContent = `${job.processed} / ${job.total}`;
      $("index-bar").style.width = `${job.status === "succeeded" ? 100 : Math.round(job.processed / Math.max(job.total, 1) * 100)}%`;
      $("index-job-detail").textContent = [job.current, job.error, ...(job.errors || []).slice(0, 3).map((item) => item.error)].filter(Boolean).join("\n");
    }
    $("index-documents").replaceChildren(...payload.documents.map((doc) => { const row = document.createElement("li"); row.append(node("span", doc.title), node("small", {ready: "已同步", pending: "待同步", partial: "部分可用"}[doc.status])); return row; }));
  }
  async function refresh() {
    if (!$("index-dialog").open || !libraryId || requestPending) return;
    requestPending = true;
    const current = ++sequence, selected = libraryId;
    clearTimeout(timer);
    try {
      const payload = await api(`/api/index/status?library_id=${encodeURIComponent(selected)}`);
      if (selected !== libraryId || current !== sequence) return;
      render(payload);
      error();
      if (payload.jobs.some((job) => !terminal.has(job.status))) timer = setTimeout(refresh, 1800);
    } catch (failure) { error(failure.message); }
    finally { requestPending = false; if ($("index-dialog").open && current !== sequence) setTimeout(refresh, 0); }
  }
  async function open() {
    libraryId = context?.session_context?.library_id || "";
    if (!libraryId) return;
    const library = context.libraries?.find((item) => item.id === libraryId);
    $("index-library").textContent = library?.name || "当前文档库";
    $("index-library").title = library?.root || "";
    $("index-dialog").showModal();
    $("index-dialog").querySelector(".index-body").scrollTop = 0;
    data = null;
    $("index-sync").disabled = true;
    $("index-rebuild").disabled = true;
    $("index-summary").textContent = "正在读取索引状态…";
    await refresh();
  }
  async function sync(force = false) {
    if (!data || busy || $("index-sync").disabled) return;
    const generate = $("index-captions").checked, model = $("index-caption-model").value;
    busy = true; render(data); error();
    try { await api("/api/index/sync", {library_id: libraryId, generate_captions: generate, caption_model: model, force}); }
    catch (failure) { busy = false; render(data); error(failure.message); return; }
    finally { busy = false; }
    await refresh();
  }
  $("index-open").addEventListener("click", open);
  $("index-dismiss").addEventListener("click", () => $("index-dialog").close());
  $("index-dialog").addEventListener("close", () => { clearTimeout(timer); sequence++; });
  $("index-refresh").addEventListener("click", refresh);
  $("index-sync").addEventListener("click", () => sync());
  $("index-rebuild").addEventListener("click", () => sync(true));
  $("index-stop").addEventListener("click", async () => {
    const job = data?.jobs.find((item) => !terminal.has(item.status));
    if (!job || busy) return;
    busy = true;
    try { await api("/api/index/cancel", {library_id: libraryId, job_id: job.id}); }
    catch (failure) { error(failure.message); }
    finally { busy = false; await refresh(); }
  });
  $("index-search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("index-search").disabled) return;
    $("index-search").disabled = true; error();
    try {
      const file = $("index-query-image").files[0];
      if (file && file.size > 5 * 1024 * 1024) throw new Error("查询图片不能超过 5 MB。");
      const image = file ? await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); }) : "";
      const value = $("index-query-kind").value;
      const result = await api("/api/index/search", {library_id: libraryId, text: $("index-query").value.trim(), image, representations: value ? value.split(",") : []});
      $("index-results").replaceChildren(...result.hits.map((hit) => {
        const row = node("div", "", "index-result"), button = node("button", `${hit.title} · 第 ${hit.page} 页 ↗`);
        button.type = "button"; button.addEventListener("click", () => { $("index-dialog").close(); document.dispatchEvent(new CustomEvent("dcx-open-evidence", {detail: hit.locator})); });
        row.append(button, node("p", hit.snippet), node("small", `${hit.matched_representations.join(" · ")} · 排序 ${hit.score.toFixed(3)}`));
        return row;
      }));
      if (result.warnings?.length) error(result.warnings.join(" · "));
      if (!result.hits.length) $("index-results").appendChild(node("p", result.warnings?.join(" · ") || "没有匹配的来源。"));
    } catch (failure) { error(failure.message || "图片读取失败"); }
    finally { $("index-search").disabled = false; }
  });
  window.DcxIndexes = {
    setContext(value) { context = value; if ($("index-dialog").open && libraryId !== value.session_context?.library_id) $("index-dialog").close(); },
    refreshIfOpen: refresh,
  };
})();
