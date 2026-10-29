(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function migrateStorage(name, keys) {
    for (const key of keys) {
      try {
        const storage = window[name];
        const legacyKey = key.replace(/^dcx-/, "cli-agent-");
        const legacy = storage.getItem(legacyKey);
        if (legacy === null) continue;
        if (storage.getItem(key) === null) storage.setItem(key, legacy);
        // Remove only after the new value is saved, so cleared drafts stay cleared.
        storage.removeItem(legacyKey);
      } catch (_) { /* storage may be unavailable; retry on the next page load */ }
    }
  }

  migrateStorage("sessionStorage", ["dcx-token"]);
  migrateStorage("localStorage", [
    "dcx-draft", "dcx-theme", "dcx-sidebar-width", "dcx-sidebar-collapsed",
  ]);

  const initialUrl = new URL(window.location.href);
  let accessToken = initialUrl.searchParams.get("token") || "";
  if (accessToken) {
    try { sessionStorage.setItem("dcx-token", accessToken); } catch (_) { /* ignore */ }
    initialUrl.searchParams.delete("token");
    window.history.replaceState({}, "", initialUrl.pathname + initialUrl.search + initialUrl.hash);
  } else {
    try { accessToken = sessionStorage.getItem("dcx-token") || ""; } catch (_) { accessToken = ""; }
  }
  function authHeaders() {
    return accessToken ? { "X-DCX-Token": accessToken } : {};
  }

  const state = {
    data: null,
    busy: false,
    stopping: false,
    turn: null,
    permission: null,
    stick: true,
    controller: null,
    draftKey: "dcx-draft",
    toastTimer: null,
    sessions: [],
    sidebarWidth: 268,
    archivedOpen: false,
    attachments: [],
    editing: null,
    searchQuery: "",
    searchTimer: null,
    rewindResolve: null,
    rewindConflicts: [],
    evidenceRequest: 0,
  };

  // ------------------------------------------------------------------ utils

  function api(path, body) {
    const options = {
      method: body === undefined ? "GET" : "POST",
      headers: { ...authHeaders() },
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(async (res) => {
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error((data && data.error) || `Request failed (${res.status})`);
      }
      return data;
    });
  }

  function h(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatTokens(n) {
    if (!Number.isFinite(n)) return "—";
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}k`;
    return String(n);
  }

  function formatDuration(ms) {
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function truncate(value, n) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > n ? text.slice(0, n - 1) + "…" : text;
  }

  function toast(message, ms = 2600) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { node.hidden = true; }, ms);
  }

  function systemNote(text, kind = "system") {
    if (!text) return;
    const node = h("div", kind === "error" ? "error-note" : "system-note", text);
    $("messages").appendChild(node);
    scrollIfSticky();
  }

  const SIDEBAR_DEFAULT = 268;
  const SIDEBAR_MIN = 220;
  const SIDEBAR_MAX = 460;
  let sidebarDrag = null;

  function applySidebarWidth(width, { persist = false } = {}) {
    const clamped = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(width)));
    document.documentElement.style.setProperty("--sidebar-w", `${clamped}px`);
    state.sidebarWidth = clamped;
    if (persist) {
      try { localStorage.setItem("dcx-sidebar-width", String(clamped)); } catch (_) { /* ignore */ }
    }
  }

  function loadSidebarWidth() {
    try {
      const saved = Number(localStorage.getItem("dcx-sidebar-width"));
      if (Number.isFinite(saved) && saved > 0) applySidebarWidth(saved);
    } catch (_) { /* ignore */ }
  }

  function bindSidebarResizer() {
    const resizer = $("sidebar-resizer");
    resizer.addEventListener("pointerdown", (event) => {
      if (window.matchMedia("(max-width: 900px)").matches) return;
      sidebarDrag = {
        startX: event.clientX,
        startWidth: state.sidebarWidth || SIDEBAR_DEFAULT,
      };
      resizer.setPointerCapture(event.pointerId);
      document.body.classList.add("resizing");
      event.preventDefault();
    });
    resizer.addEventListener("pointermove", (event) => {
      if (!sidebarDrag) return;
      applySidebarWidth(sidebarDrag.startWidth + event.clientX - sidebarDrag.startX);
    });
    const finish = () => {
      if (!sidebarDrag) return;
      sidebarDrag = null;
      document.body.classList.remove("resizing");
      applySidebarWidth(state.sidebarWidth, { persist: true });
    };
    resizer.addEventListener("pointerup", finish);
    resizer.addEventListener("pointercancel", finish);
    resizer.addEventListener("dblclick", () => {
      applySidebarWidth(SIDEBAR_DEFAULT, { persist: true });
    });
    resizer.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const delta = event.key === "ArrowRight" ? 16 : -16;
      applySidebarWidth((state.sidebarWidth || SIDEBAR_DEFAULT) + delta, {
        persist: true,
      });
    });
  }

  function openShortcuts() {
    if (!$("permission").hidden || !$("dialog").hidden) return;
    $("shortcuts").hidden = false;
    document.querySelector(".app").inert = true;
    setTimeout(() => $("shortcuts-close").focus(), 30);
  }

  function closeShortcuts() {
    $("shortcuts").hidden = true;
    document.querySelector(".app").inert = false;
  }

  function toggleShortcuts() {
    if ($("shortcuts").hidden) openShortcuts();
    else closeShortcuts();
  }

  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
    $("scrim").hidden = !open;
  }

  function isNarrow() {
    return window.matchMedia("(max-width: 900px)").matches;
  }

  function setSidebarCollapsed(collapsed, { persist = true, focus = false } = {}) {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    $("sidebar-collapse").setAttribute("aria-expanded", String(!collapsed));
    $("menu").setAttribute("aria-label", collapsed ? "Show sidebar" : "Open sidebar");
    if (persist) {
      try {
        localStorage.setItem("dcx-sidebar-collapsed", collapsed ? "1" : "0");
      } catch (_) { /* ignore */ }
    }
    if (collapsed && focus) $("menu").focus();
  }

  function loadSidebarCollapsed() {
    try {
      if (localStorage.getItem("dcx-sidebar-collapsed") === "1") {
        setSidebarCollapsed(true, { persist: false });
      }
    } catch (_) { /* ignore */ }
  }

  function nearBottom() {
    const box = $("transcript");
    return box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  }

  function scrollIfSticky(force = false) {
    if (!state.stick && !force) return;
    const box = $("transcript");
    box.scrollTop = box.scrollHeight;
  }

  // -------------------------------------------------------------- markdown

  function inline(text) {
    const codes = [];
    let source = String(text).replace(/`([^`\n]+)`/g, (_, code) => {
      codes.push(code);
      return `\u0001${codes.length - 1}\u0001`;
    });
    let out = escapeHtml(source);
    out = out.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    out = out.replace(/\[([^\]]+)\]\((doc:[A-Za-z0-9_.@\/-]+:(?:[A-Za-z0-9_.-]+))\)/g,
      '<a href="#" class="source-link" data-locator="$2">$1 ↗</a>');
    out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/\u0001(\d+)\u0001/g, (_, index) => {
      return `<code>${escapeHtml(codes[Number(index)])}</code>`;
    });
    return out;
  }

  function codeBlock(code, lang, open) {
    const label = lang ? escapeHtml(lang) : "code";
    const suffix = open ? '<span class="tool-status running">streaming</span>' : "";
    return (
      `<div class="code"><div class="code-head"><span>${label}${suffix}</span>` +
      `<button class="copy" type="button">Copy</button></div>` +
      `<pre><code>${escapeHtml(code)}</code></pre></div>`
    );
  }

  function renderMarkdown(source) {
    const text = String(source || "").replace(/\r\n/g, "\n");
    if (!text.trim()) return "";
    const lines = text.split("\n");
    let html = "";
    let index = 0;
    let paragraph = [];
    let list = null;

    const flushParagraph = () => {
      if (!paragraph.length) return;
      html += `<p>${inline(paragraph.join(" "))}</p>`;
      paragraph = [];
    };
    const flushList = () => {
      if (!list) return;
      html += `<${list.type}>${list.items.map((item) => `<li>${inline(item)}</li>`).join("")}</${list.type}>`;
      list = null;
    };
    const flush = () => { flushParagraph(); flushList(); };

    while (index < lines.length) {
      const line = lines[index];

      if (/^```/.test(line)) {
        flush();
        const lang = line.slice(3).trim();
        const code = [];
        index += 1;
        let closed = false;
        while (index < lines.length) {
          if (/^```/.test(lines[index])) { closed = true; index += 1; break; }
          code.push(lines[index]);
          index += 1;
        }
        html += codeBlock(code.join("\n"), lang, !closed);
        continue;
      }

      if (!line.trim()) { flush(); index += 1; continue; }

      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flush();
        const level = heading[1].length;
        html += `<h${level}>${inline(heading[2])}</h${level}>`;
        index += 1;
        continue;
      }

      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        flush();
        html += "<hr>";
        index += 1;
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        flush();
        const quote = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quote.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        html += `<blockquote>${inline(quote.join(" "))}</blockquote>`;
        continue;
      }

      const unordered = line.match(/^\s*[-*+]\s+(.*)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (unordered || ordered) {
        flushParagraph();
        const type = unordered ? "ul" : "ol";
        if (!list || list.type !== type) { flushList(); list = { type, items: [] }; }
        list.items.push((unordered || ordered)[1]);
        index += 1;
        continue;
      }

      if (line.includes("|") && index + 1 < lines.length &&
          /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[index + 1]) &&
          lines[index + 1].includes("-")) {
        flush();
        const cells = (row) => row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
        const header = cells(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(cells(lines[index]));
          index += 1;
        }
        html += "<table><thead><tr>" +
          header.map((cell) => `<th>${inline(cell)}</th>`).join("") +
          "</tr></thead><tbody>" +
          rows.map((row) => "<tr>" + row.map((cell) => `<td>${inline(cell)}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>";
        continue;
      }

      flushList();
      paragraph.push(line.trim());
      index += 1;
    }
    flush();
    return html;
  }

  // ------------------------------------------------------------ message view

  const TOOL_ICONS = {
    Bash: '<path d="M5 7l4 5-4 5M12 17h7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    Read: '<path d="M6 3h8l4 4v14H6z" stroke="currentColor" stroke-width="1.6"/><path d="M14 3v5h5" stroke="currentColor" stroke-width="1.6"/>',
    Write: '<path d="M4 20h4l10-10-4-4L4 16v4Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
    Edit: '<path d="M4 20h4l10-10-4-4L4 16v4Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
    Glob: '<circle cx="11" cy="11" r="6" stroke="currentColor" stroke-width="1.6"/><path d="M16 16l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    Grep: '<circle cx="11" cy="11" r="6" stroke="currentColor" stroke-width="1.6"/><path d="M16 16l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    Remember: '<path d="M12 3l2.4 5.2 5.6.7-4.1 3.9 1 5.6-4.9-2.7-4.9 2.7 1-5.6L4 8.9l5.6-.7Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>',
    load_skill: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9Z" stroke="currentColor" stroke-width="1.5"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5" stroke="currentColor" stroke-width="1.5"/>',
  };

  function toolIcon(name) {
    return TOOL_ICONS[name] || '<path d="M9 3v4M15 3v4M7 7h10v5a5 5 0 0 1-10 0V7Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>';
  }

  function toolPreview(name, input) {
    if (!input) return "";
    if (typeof input.command === "string") return truncate(input.command, 90);
    if (typeof input.path === "string") return truncate(input.path, 90);
    if (typeof input.pattern === "string") return truncate(input.pattern, 90);
    if (typeof input.query === "string") return truncate(input.query, 90);
    return truncate(JSON.stringify(input), 90);
  }

  function avatar() {
    const node = h("span", "avatar");
    node.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M12 3v18M4.2 7.5l15.6 9M19.8 7.5l-15.6 9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
    return node;
  }

  function renderTextBlocks(content) {
    const texts = content.filter((block) => block.type === "text").map((block) => block.text).join("");
    return texts.trim() ? texts : "";
  }

  function addUserMessage(content, messageId = null) {
    const text = renderTextBlocks(content);
    const images = content.filter((block) => block.type === "image");
    if (!text.trim() && !images.length) return;
    const row = h("div", "msg msg-user");
    const body = h("div", "msg-body");
    if (text.trim()) body.appendChild(h("div", "bubble", text));
    for (const image of images) {
      const img = document.createElement("img");
      img.className = "bubble-image";
      img.alt = "attached image";
      img.src = `data:${image.media_type};base64,${image.data}`;
      body.appendChild(img);
    }
    if (messageId) {
      const actions = h("div", "msg-actions");
      const rewind = h("button", "msg-action", "Rewind");
      rewind.type = "button";
      rewind.title = "Rewind files and/or conversation to before this message";
      rewind.addEventListener("click", () => startRewind(messageId));
      actions.appendChild(rewind);
      const edit = h("button", "msg-action", "Edit");
      edit.type = "button";
      edit.title = "Edit and resend";
      edit.addEventListener("click", () => beginEdit(messageId, content));
      actions.appendChild(edit);
      body.appendChild(actions);
    }
    row.appendChild(body);
    $("messages").appendChild(row);
    return row;
  }

  function makeToolCard(toolId, name, input, parent = $("messages")) {
    const card = document.createElement("details");
    card.className = "tool running";
    card.open = true;
    card.dataset.toolId = toolId || "";
    const summary = h("summary");
    const icon = h("span", "tool-icon");
    icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none">${toolIcon(name)}</svg>`;
    summary.append(icon, h("span", "tool-name", name === "CallExternalTool" && input?.tool ? input.tool : name || "Tool"));
    const preview = h("span", "tool-preview", toolPreview(name, input));
    summary.appendChild(preview);
    const time = h("span", "tool-time");
    const status = h("span", "tool-status running", "running");
    summary.append(time, status);
    card.appendChild(summary);

    const body = h("div", "tool-body");
    if (input && Object.keys(input).length) {
      body.appendChild(h("div", "tool-section-label", "Input"));
      body.appendChild(h("pre", "", JSON.stringify(input, null, 2)));
    }
    const output = h("div", "tool-output");
    body.appendChild(output);
    card.appendChild(body);
    parent.appendChild(card);
    return card;
  }

  function toolOutputText(blocks) {
    return (blocks || [])
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n");
  }

  function finishToolCard(card, result, elapsed) {
    if (!card) return;
    const status = card.querySelector(".tool-status");
    status.className = `tool-status ${result.is_error ? "error" : "ok"}`;
    status.textContent = result.is_error ? "error" : "done";
    if (elapsed != null) card.querySelector(".tool-time").textContent = formatDuration(elapsed);
    const output = card.querySelector(".tool-output");
    output.textContent = "";
    const text = toolOutputText(result.content);
    if (text.trim()) {
      output.appendChild(h("div", "tool-section-label", "Output"));
      output.appendChild(h("pre", "", text));
    }
    for (const block of result.content || []) {
      if (block.type === "image") {
        const img = document.createElement("img");
        img.alt = "tool image output";
        img.src = `data:${block.media_type};base64,${block.data}`;
        output.appendChild(img);
      }
    }
    if (card.classList.contains("running")) card.open = false;
    if (!text.trim() && !(result.content || []).some((block) => block.type === "image")) {
      output.appendChild(h("div", "tool-section-label", result.is_error ? "Failed" : "No output"));
    }
    card.classList.remove("running");
    const locators = [...new Set(text.match(/doc:[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/(?:page|unit|asset):[A-Za-z0-9_.-]+/g) || [])];
    for (const locator of locators.slice(0, 8)) {
      const button = h("button", "ghost small source-link", `打开来源 · ${locator.split("/").pop()}`);
      button.type = "button";
      button.dataset.locator = locator;
      output.appendChild(button);
    }
  }

  function addThinking(text, open = true) {
    const template = $("tpl-thinking");
    const node = template.content.firstElementChild.cloneNode(true);
    node.open = open;
    node.querySelector(".thinking-text").textContent = text || "";
    return node;
  }

  function assistantShell() {
    const row = h("div", "msg msg-assistant");
    row.appendChild(avatar());
    const body = h("div", "msg-body");
    const segments = h("div", "segments");
    const meta = h("div", "turn-meta");
    meta.hidden = true;
    body.append(segments, meta);
    row.appendChild(body);
    $("messages").appendChild(row);
    return { row, body, segments, meta };
  }

  function renderAssistantContent(shell, content, cards) {
    for (const block of content) {
      if (block.type === "text") {
        if (!block.text) continue;
        const node = h("div", "md");
        node.innerHTML = renderMarkdown(block.text);
        shell.segments.appendChild(node);
      } else if (block.type === "reasoning") {
        if (!block.text) continue;
        shell.segments.appendChild(addThinking(block.text, false));
      } else if (block.type === "tool_use") {
        const card = makeToolCard(block.id, block.name, block.input, shell.segments);
        card.open = false;
        card.querySelector(".tool-status").className = "tool-status";
        card.querySelector(".tool-status").textContent = "done";
        if (block.id) cards.set(block.id, card);
      }
    }
    return shell;
  }

  function buildMeta(meta) {
    const node = h("div", "turn-meta");
    const usage = meta.usage || {};
    const prompt = (usage.input_tokens || 0) + (usage.cache_read_tokens || 0) + (usage.cache_write_tokens || 0);
    if (prompt || usage.output_tokens) {
      node.appendChild(h("span", "", `in ${formatTokens(prompt)}`));
      node.appendChild(h("span", "sep", "·"));
      node.appendChild(h("span", "", `out ${formatTokens(usage.output_tokens || 0)}`));
      if (usage.cache_read_tokens) {
        node.appendChild(h("span", "sep", "·"));
        node.appendChild(h("span", "", `${Math.round((usage.cache_read_tokens / Math.max(prompt, 1)) * 100)}% cached`));
      }
    }
    if (meta.stopReason && meta.stopReason !== "end_turn") {
      const badge = h("span", "stop-badge", meta.stopReason.replaceAll("_", " "));
      if (meta.stopReason === "error" || meta.stopReason === "refusal") badge.classList.add("bad");
      node.appendChild(badge);
    }
    if (meta.elapsed) {
      node.appendChild(h("span", "sep", "·"));
      node.appendChild(h("span", "", formatDuration(meta.elapsed)));
    }
    node.hidden = false;
    return node;
  }

  function renderHistory(messages) {
    const container = $("messages");
    container.textContent = "";
    // Assistant messages separated only by tool results belong to one visual
    // turn: keep a single avatar and segment list for them.
    let shell = null;
    let lastShell = null;
    let lastRole = null;
    let cards = new Map();
    for (const [index, message] of messages.entries()) {
      const inherited = index < (state.data?.session_context?.fork_boundary || 0);
      const results = message.content.filter((block) => block.type === "tool_result");
      const text = renderTextBlocks(message.content);
      if (message.role === "assistant") {
        if (!shell) {
          shell = assistantShell();
          cards = new Map();
        }
        renderAssistantContent(shell, message.content, cards);
        lastShell = shell;
        lastRole = "assistant";
      } else if (results.length && !text.trim() && shell) {
        for (const result of results) {
          finishToolCard(cards.get(result.tool_use_id), result, null);
        }
      } else {
        shell = null;
        lastShell = null;
        lastRole = "user";
        addUserMessage(message.content, inherited ? null : message.id);
      }
    }
    if (lastRole === "assistant" && lastShell && messages.findLastIndex((message) => message.role === "user") >= (state.data?.session_context?.fork_boundary || 0)) {
      const actions = h("div", "turn-actions");
      const regenerate = h("button", "turn-action", "Regenerate");
      regenerate.type = "button";
      regenerate.title = "Regenerate the last response";
      regenerate.addEventListener("click", regenerateLastTurn);
      actions.appendChild(regenerate);
      lastShell.body.appendChild(actions);
    }
    updateEmptyState();
    updateTitle(messages);
    state.stick = true;
    scrollIfSticky(true);
  }

  // ------------------------------------------------------------------- turn

  function pendingIndicator() {
    const node = h("div", "pending");
    node.setAttribute("aria-label", "Waiting for the model");
    node.append(h("span"), h("span"), h("span"));
    return node;
  }

  function clearPending(turn) {
    if (turn.pending) {
      turn.pending.remove();
      turn.pending = null;
    }
  }

  function createTurn(userText, images = [], { appendUser = true } = {}) {
    if (appendUser && userText && $("chat-title").textContent === "New conversation") {
      $("chat-title").textContent = truncate(userText, 48);
    }
    if (appendUser) {
      const blocks = [];
      if (userText) blocks.push({ type: "text", text: userText });
      for (const image of images) {
        blocks.push({ type: "image", media_type: image.media_type, data: image.data });
      }
      addUserMessage(blocks);
    }
    const shell = assistantShell();
    shell.row.classList.add("streaming");
    const turn = {
      shell,
      userText,
      textSegments: [],
      currentText: null,
      currentReasoning: null,
      currentReasoningText: null,
      cards: new Map(),
      starts: new Map(),
      usage: null,
      stopReason: null,
      elapsed: 0,
      startedAt: performance.now(),
      error: false,
      paintQueued: false,
      pending: null,
    };
    turn.pending = pendingIndicator();
    shell.segments.appendChild(turn.pending);
    state.turn = turn;
    updateEmptyState();
    scrollIfSticky(true);
    return turn;
  }

  function turnTextSegment(turn) {
    if (turn.currentText) return turn.currentText;
    const node = h("div", "md");
    node._raw = "";
    turn.currentText = node;
    turn.textSegments.push(node);
    turn.shell.segments.appendChild(node);
    return node;
  }

  function turnReasoningSegment(turn) {
    if (turn.currentReasoning) return turn.currentReasoningText;
    const node = addThinking("", true);
    turn.currentReasoning = node;
    turn.currentReasoningText = node.querySelector(".thinking-text");
    turn.shell.segments.appendChild(node);
    return turn.currentReasoningText;
  }

  function paintTurn(turn) {
    const nodes = turn.dirtyText ? [turn.dirtyText] : turn.textSegments;
    for (const node of nodes) {
      node.innerHTML = renderMarkdown(node._raw);
      node.classList.remove("is-streaming");
    }
    const live = turn.shell.row.classList.contains("streaming");
    const last = turn.textSegments[turn.textSegments.length - 1];
    if (live && last) last.classList.add("is-streaming");
    turn.dirtyText = null;
    scrollIfSticky();
  }

  function queuePaint(turn) {
    if (turn.paintQueued) return;
    turn.paintQueued = true;
    requestAnimationFrame(() => {
      turn.paintQueued = false;
      paintTurn(turn);
    });
  }

  function finalizeTurn(turn, stopReason) {
    if (!turn) return;
    clearPending(turn);
    turn.elapsed = performance.now() - turn.startedAt;
    turn.stopReason = stopReason || turn.stopReason || "end_turn";
    turn.shell.row.classList.remove("streaming");
    for (const node of turn.textSegments) node.classList.remove("is-streaming");
    for (const thinking of turn.shell.row.querySelectorAll(".thinking[open]")) {
      thinking.open = false;
    }
    const meta = buildMeta({ usage: turn.usage, stopReason: turn.stopReason, elapsed: turn.elapsed });
    turn.shell.meta.replaceWith(meta);
    turn.shell.meta = meta;
    if (!turn.textSegments.length && !turn.cards.size && !turn.error &&
        !turn.shell.segments.querySelector(".thinking")) {
      turn.shell.segments.appendChild(h("div", "md", "No response."));
    }
    state.turn = null;
    scrollIfSticky();
    updateEmptyState();
  }

  function handleEvent(event, turn) {
    switch (event.type) {
      case "turn_start":
        if (event.model) $("model-chip").textContent = event.model;
        break;
      case "text": {
        if (!event.text) break;
        clearPending(turn);
        const node = turnTextSegment(turn);
        node._raw += event.text;
        turn.dirtyText = node;
        if (turn.currentReasoning) turn.currentReasoning.open = false;
        queuePaint(turn);
        break;
      }
      case "reasoning": {
        if (!event.text) break;
        clearPending(turn);
        turn.currentText = null;
        const target = turnReasoningSegment(turn);
        target.textContent += event.text;
        if (state.stick) scrollIfSticky();
        break;
      }
      case "tool_started": {
        clearPending(turn);
        turn.currentText = null;
        turn.currentReasoning = null;
        turn.currentReasoningText = null;
        const card = makeToolCard(event.id, event.name, event.input, turn.shell.segments);
        turn.cards.set(event.id, card);
        turn.starts.set(event.id, performance.now());
        scrollIfSticky();
        break;
      }
      case "tool_finished": {
        clearPending(turn);
        let card = turn.cards.get(event.id);
        const started = turn.starts.get(event.id);
        if (!card) {
          card = makeToolCard(event.id, event.name, {}, turn.shell.segments);
          turn.cards.set(event.id, card);
        }
        finishToolCard(card, event, started ? performance.now() - started : null);
        scrollIfSticky();
        break;
      }
      case "usage":
        turn.usage = event.turn || turn.usage;
        break;
      case "context": {
        if (["compact", "compact_skipped", "compact_failed", "continue"].includes(event.kind)) {
          const detail = event.detail ? ` — ${event.detail}` : "";
          systemNote(`Context ${event.kind.replaceAll("_", " ")}${detail}`);
        }
        break;
      }
      case "notice":
        systemNote(event.text);
        break;
      case "retry":
        if (event.partial) {
          // The host already displayed an abandoned opening. Clear it so the
          // retried attempt is not spliced onto a stale partial answer.
          for (const node of turn.textSegments) {
            node._raw = "";
            node.innerHTML = "";
          }
          turn.textSegments = [];
          turn.currentText = null;
          turn.dirtyText = null;
          if (turn.currentReasoning) turn.currentReasoning.remove();
          turn.currentReasoning = null;
          turn.currentReasoningText = null;
          systemNote(
            `Retrying (${event.attempt}/${event.max_attempts}) — ${event.reason}; ` +
            "the partial response was discarded."
          );
        } else {
          systemNote(
            `Retrying (${event.attempt}/${event.max_attempts}) — ${event.reason}`
          );
        }
        break;
      case "permission_request":
        showPermission(event);
        break;
      case "error":
        clearPending(turn);
        turn.error = true;
        systemNote(event.message, "error");
        break;
      case "cancelled":
        systemNote("Stopped.");
        break;
      case "done":
        if (!turn.usage) turn.usage = event.total || null;
        finalizeTurn(turn, event.stop_reason);
        break;
      default:
        break;
    }
  }

  // ----------------------------------------------------------------- streaming

  function parseSSE(buffer, onEvent) {
    let rest = buffer;
    let split;
    while ((split = rest.indexOf("\n\n")) >= 0) {
      const chunk = rest.slice(0, split);
      rest = rest.slice(split + 2);
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try { onEvent(JSON.parse(payload)); } catch (_) { /* ignore malformed frame */ }
      }
    }
    return rest;
  }

  async function sendMessage(text) {
    if (state.busy) return;
    const images = state.attachments.map((image) => ({
      media_type: image.media_type,
      data: image.data,
    }));
    const editing = state.editing;
    if (editing) {
      state.busy = true;
      updateComposer();
      const ok = await performRewind({ message_id: editing });
      if (!ok) {
        state.busy = false;
        updateComposer();
        return;
      }
      state.busy = false;
      state.editing = null;
      $("editing-bar").hidden = true;
    }
    $("input").value = "";
    clearAttachments();
    saveDraft();
    autoGrow();
    await runChat({ text, images, appendUser: true });
  }

  async function runChat({ text = null, images = [], appendUser = true, continueFlag = false }) {
    if (state.busy) return;
    state.busy = true;
    state.stopping = false;
    updateComposer();
    const turn = createTurn(text, images, { appendUser });
    state.stick = true;
    scrollIfSticky(true);

    const controller = new AbortController();
    state.controller = controller;
    let sawDone = false;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(continueFlag ? { continue: true } : { text, images }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => null);
        throw new Error((payload && payload.error) || `Request failed (${response.status})`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = parseSSE(buffer, (event) => {
          if (event.type === "done") sawDone = true;
          handleEvent(event, turn);
        });
        if (sawDone) break;
      }
      if (!sawDone) {
        systemNote("Connection closed before the turn finished.", "error");
        finalizeTurn(turn, "error");
      }
    } catch (error) {
      if (error.name !== "AbortError") {
        systemNote(error.message || String(error), "error");
        finalizeTurn(turn, "error");
      }
    } finally {
      state.busy = false;
      state.stopping = false;
      state.controller = null;
      if (state.turn === turn) finalizeTurn(turn, turn.stopReason);
      updateComposer();
      $("input").focus();
      hidePermission(true);
      const canonical = sawDone && !["error", "cancelled"].includes(turn.stopReason || "");
      const meta = {
        usage: turn.usage,
        stopReason: turn.stopReason,
        elapsed: turn.elapsed,
      };
      await refreshState({ render: canonical });
      if (canonical) attachLastTurnMeta(meta);
      await refreshSessions();
    }
  }

  function attachLastTurnMeta(meta) {
    const shells = $("messages").querySelectorAll(".msg-assistant");
    const shell = shells[shells.length - 1];
    if (!shell) return;
    const body = shell.querySelector(".msg-body");
    const old = body.querySelector(".turn-meta");
    const next = buildMeta(meta);
    if (old) old.replaceWith(next);
    else body.appendChild(next);
  }

  function closeRewind(value) {
    if (!$("rewind").hidden) {
      $("rewind").hidden = true;
      document.querySelector(".app").inert = false;
    }
    const resolve = state.rewindResolve;
    state.rewindResolve = null;
    if (resolve) resolve(value);
  }

  function addRewindChoice(value, label, detail, { checked = false, disabled = false } = {}) {
    const row = document.createElement("label");
    row.className = `rewind-choice${disabled ? " disabled" : ""}`;
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "rewind-choice";
    input.value = value;
    input.checked = checked;
    input.disabled = disabled;
    const text = h("span", "rewind-choice-text");
    text.appendChild(h("strong", "", label));
    if (detail) text.appendChild(h("em", "", detail));
    row.append(input, text);
    $("rewind-choices").appendChild(row);
  }

  function updateRewindConfirm() {
    const checked = $("rewind-choices").querySelector(
      'input[name="rewind-choice"]:checked'
    );
    const value = checked ? checked.value : "conversation";
    const force = value !== "conversation" && (state.rewindConflicts || []).length > 0;
    const confirm = $("rewind-confirm");
    confirm.textContent = force ? "Restore anyway" : "Continue";
    confirm.classList.toggle("danger", force);
  }

  function renderRewindDiff(pre, text) {
    pre.textContent = "";
    for (const line of text.split("\n")) {
      let kind = "";
      if (line.startsWith("@@")) kind = "hunk";
      else if (line.startsWith("+++") || line.startsWith("---")) kind = "meta";
      else if (line.startsWith("+")) kind = "add";
      else if (line.startsWith("-")) kind = "del";
      pre.appendChild(h("span", kind, `${line}\n`));
    }
  }

  function describeBytes(value) {
    if (value === null || value === undefined) return "absent";
    return `${value} byte${value === 1 ? "" : "s"}`;
  }

  async function toggleRewindDiff(button, pre, target, path) {
    if (!pre.hidden) {
      pre.hidden = true;
      button.textContent = "Diff";
      return;
    }
    if (pre.dataset.loaded === "true") {
      pre.hidden = false;
      button.textContent = "Hide";
      return;
    }
    button.disabled = true;
    button.textContent = "Loading…";
    try {
      const data = await api("/api/rewind/diff", { ...target, path });
      const note = data.warning ? `${data.warning}\n` : "";
      if (data.binary) {
        pre.textContent =
          `${note}Binary file: ${describeBytes(data.current_bytes)} → ` +
          `${describeBytes(data.restored_bytes)}. No textual diff.`;
      } else {
        renderRewindDiff(pre, note + data.diff);
      }
      pre.dataset.loaded = "true";
      pre.hidden = false;
      button.textContent = "Hide";
    } catch (error) {
      pre.textContent = error.message;
      pre.hidden = false;
      button.textContent = "Retry";
    } finally {
      button.disabled = false;
    }
  }

  function askRewind(preview, { mode = "turn", target = {} } = {}) {
    return new Promise((resolve) => {
      state.rewindResolve = resolve;
      state.rewindConflicts = preview.conflicts || [];
      const files = preview.files || [];
      const conflicts = state.rewindConflicts;
      const restorable = preview.restorable !== false;
      const canCode = files.length > 0 && restorable;
      const global = mode === "global";
      const n = files.length;
      const shellOnly = !n && preview.shell_ran;
      const incomplete = !n && (preview.partial === true || (preview.warnings || []).length > 0);

      $("rewind-title").textContent = canCode
        ? global
          ? "Rewind to this message?"
          : "Rewind code changes?"
        : n
          ? "Code cannot be restored"
          : incomplete
            ? "Some changes were not checkpointed"
            : "Rewind conversation?";
      $("rewind-summary").textContent = canCode
        ? `${n} file${n === 1 ? "" : "s"} changed in the turns being rewound.`
        : n
          ? "A checkpoint backup is missing or incomplete, so only the conversation can be rewound."
          : shellOnly
            ? "No file changes were checkpointed. The turns being rewound ran shell commands; their effects are not tracked."
            : "No file changes were checkpointed for this rewind.";

      const list = $("rewind-files");
      list.textContent = "";
      for (const file of files) {
        const row = document.createElement("li");
        row.className = "rewind-file";
        const head = h("div", "rewind-file-head");
        head.appendChild(h("span", "rewind-path", file.path));
        head.appendChild(
          h(
            "span",
            `rewind-status${file.conflict ? " conflict" : ""}`,
            file.conflict ? "conflict" : file.status
          )
        );
        if (file.conflict) {
          const button = h("button", "rewind-diff-toggle", "Diff");
          button.type = "button";
          button.title = "Show what restoring this file would change";
          const pre = h("pre", "rewind-diff");
          pre.hidden = true;
          button.addEventListener("click", () =>
            toggleRewindDiff(button, pre, target, file.path)
          );
          head.appendChild(button);
          row.appendChild(head);
          row.appendChild(pre);
        } else {
          row.appendChild(head);
        }
        list.appendChild(row);
      }
      list.hidden = n === 0;

      const warnings = [...(preview.warnings || [])];
      if (preview.shell_ran) {
        warnings.unshift(
          canCode
            ? "Shell commands ran; only files changed by Write/Edit can be restored."
            : "Shell commands ran; their effects are not tracked."
        );
        if ((preview.commands || []).length) {
          warnings.push(`Shell: ${preview.commands.slice(0, 4).join(" · ")}`);
        }
      }
      if (conflicts.length) {
        warnings.push(
          "Files marked conflict changed after the turn; restoring overwrites those edits. " +
            "Choose Diff to see what would be lost."
        );
      }
      $("rewind-warning").textContent = warnings.join("\n");
      $("rewind-warning").hidden = warnings.length === 0;

      const choices = $("rewind-choices");
      choices.textContent = "";
      const preferConversation = conflicts.length > 0;
      if (canCode && global) {
        addRewindChoice(
          "both",
          "Restore code and conversation",
          "Files go back to before this message; it and everything after it are removed.",
          { checked: !preferConversation }
        );
        addRewindChoice(
          "code",
          "Restore code only",
          "Keep this conversation and restore the files to before this message."
        );
        addRewindChoice(
          "conversation",
          "Restore conversation only",
          "Remove this message and everything after it; leave files as they are.",
          { checked: preferConversation }
        );
      } else if (canCode) {
        addRewindChoice(
          "both",
          "Restore code and continue",
          "Files go back; this and later messages are removed.",
          { checked: !preferConversation }
        );
        addRewindChoice(
          "conversation",
          "Conversation only",
          "Keep files as they are; this and later messages are removed.",
          { checked: preferConversation }
        );
      } else {
        addRewindChoice(
          "conversation",
          global ? "Rewind conversation" : "Skip code restore",
          global
            ? "Remove this message and everything after it."
            : "Keep files as they are and rewind the conversation.",
          { checked: true }
        );
      }
      updateRewindConfirm();
      $("rewind").hidden = false;
      document.querySelector(".app").inert = true;
      setTimeout(() => $("rewind-confirm").focus(), 30);
    });
  }

  async function performRewind(target, { mode = "turn" } = {}) {
    let preview;
    try {
      preview = await api("/api/rewind/preview", target);
    } catch (error) {
      toast(error.message);
      return false;
    }
    let decision = { files: "keep", conversation: "rewind", force: false };
    // Turn flows (edit/regenerate) only need a decision when files changed.
    // The global rewind action always shows the choice, because it is explicit
    // and the user may want to restore code independently of the conversation.
    if (mode === "global" || (preview.files || []).length) {
      decision = await askRewind(preview, { mode, target });
      if (!decision) return false;
    }
    const payload = {
      ...target,
      files: decision.files,
      conversation: decision.conversation,
    };
    if (decision.force) payload.force = true;
    try {
      const data = await api("/api/rewind", payload);
      applyState(data, { render: true });
      const restored = data.rewind?.restore?.restored?.length || 0;
      if (restored) {
        const suffix = decision.conversation === "keep" ? "; conversation kept" : "";
        toast(
          `Restored ${restored} file${restored === 1 ? "" : "s"}${suffix}.`
        );
      } else if (preview.shell_ran && mode === "global") {
        toast("Conversation rewound; shell effects were not reverted.");
      }
      return true;
    } catch (error) {
      toast(error.message);
      return false;
    }
  }

  async function startRewind(messageId) {
    if (state.busy) { toast("Stop the current turn first."); return; }
    await performRewind({ message_id: messageId }, { mode: "global" });
  }

  async function regenerateLastTurn() {
    if (state.busy) { toast("Stop the current turn first."); return; }
    state.busy = true;
    state.stopping = false;
    updateComposer();
    const ok = await performRewind({ last_user: true });
    if (!ok) {
      state.busy = false;
      updateComposer();
      return;
    }
    state.busy = false;
    await runChat({ appendUser: false, continueFlag: true });
  }

  function beginEdit(messageId, content) {
    if (state.busy) { toast("Stop the current turn first."); return; }
    const text = renderTextBlocks(content);
    const images = content.filter((block) => block.type === "image");
    state.editing = messageId;
    $("input").value = text;
    state.attachments = images.map((image) => ({
      media_type: image.media_type,
      data: image.data,
      url: `data:${image.media_type};base64,${image.data}`,
      name: "image",
    }));
    renderAttachments();
    $("editing-bar").hidden = false;
    autoGrow();
    updateComposer();
    $("input").focus();
  }

  function cancelEdit() {
    state.editing = null;
    $("editing-bar").hidden = true;
    $("input").value = "";
    clearAttachments();
    autoGrow();
    updateComposer();
    $("input").focus();
  }

  async function stopTurn() {
    if (!state.busy || state.stopping) return;
    state.stopping = true;
    updateComposer();
    try {
      await api("/api/cancel", {});
    } catch (error) {
      toast(error.message);
    }
    setTimeout(() => {
      if (state.busy && state.controller) {
        state.controller.abort();
      }
    }, 5000);
  }

  // -------------------------------------------------------------- permission

  function showPermission(event) {
    if (state.permission) hidePermission(true);
    state.permission = event;
    $("perm-title").textContent = `Allow ${event.tool}?`;
    $("perm-reason").textContent = event.reason || "This tool needs your approval.";
    $("perm-input").textContent = JSON.stringify(event.input || {}, null, 2);
    const highRisk = /high-risk/i.test(event.reason || "");
    $("perm-scope").textContent = highRisk
      ? "This command always needs a fresh approval."
      : `“Always allow” remembers ${event.tool} for this session.`;
    $("perm-always").hidden = highRisk;
    for (const button of [$("perm-deny"), $("perm-always"), $("perm-allow")]) {
      button.disabled = false;
    }
    $("permission").hidden = false;
    document.body.classList.add("modal-open");
    document.querySelector(".app").inert = true;
    setTimeout(() => $("perm-allow").focus(), 30);
  }

  function hidePermission(markDenied) {
    if (!state.permission) return;
    const request = state.permission;
    state.permission = null;
    $("permission").hidden = true;
    document.body.classList.remove("modal-open");
    document.querySelector(".app").inert = false;
    if (markDenied) {
      api("/api/permission", { id: request.id, decision: "no" }).catch(() => {});
    }
  }

  async function decidePermission(decision) {
    const request = state.permission;
    if (!request) return;
    const buttons = [$("perm-deny"), $("perm-always"), $("perm-allow")];
    buttons.forEach((button) => { button.disabled = true; });
    try {
      await api("/api/permission", { id: request.id, decision });
      if (state.permission && state.permission.id === request.id) {
        hidePermission(false);
      }
    } catch (error) {
      toast(error.message);
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  // ------------------------------------------------------------------ sidebar

  function applyState(data, { render = false } = {}) {
    if (state.data?.session_id && state.data.session_id !== data.session_id) closeEvidence();
    state.data = data;
    applyDocumentMode(data);
    $("connection-dot").className = "brand-dot online";
    $("model-chip").textContent = data.model;
    const selectedLibrary = (data.libraries || []).find((entry) => entry.id === data.session_context?.library_id);
    $("top-sub").textContent = data.mode === "deepdocx" ? `${selectedLibrary?.name || "文档库"} · ${(data.session_context?.documents || []).length ? "已限定文档" : "整个文档库"}` : data.cwd || "workspace";
    $("top-sub").title = data.cwd || "";
    $("version").textContent = `v${data.version}`;
    populateModels(data.models, data);
    updatePermissionMode(data.permission_mode);
    updateContext(data.context_tokens, data.context_window);
    if (render) {
      renderHistory(data.messages);
    } else {
      updateEmptyState();
      updateTitle(data.messages || []);
    }
    if (data.title) $("chat-title").textContent = data.title;
  }

  // ---------------------------------------------------------- document workspace

  function applyDocumentMode(data) {
    const active = data.mode === "deepdocx";
    document.documentElement.dataset.mode = active ? "deepdocx" : "normal";
    document.querySelector(".brand-name").textContent = active ? "DeepDocx" : "dcx";
    $("deepdocx-entry").textContent = active ? "← Agent" : "▧ DeepDocx";
    $("document-navigation").hidden = !active;
    $("input").placeholder = active ? "询问论文、粘贴原文，或上传图片…" : "Message dcx…";
    const empty = $("empty");
    empty.querySelector("h2").textContent = active ? "深入文档，理解每一处细节。" : "What are we building?";
    empty.querySelector("p").textContent = active ? "从文档库定位原文，阅读论文、分析图表，随时查看依据。" : "Ask about the workspace, run commands, or make changes — with approval before anything destructive.";
    const prompts = active ? [
      ["浏览文档库", "有哪些论文与研究主题？", "查看当前文档库，介绍有哪些论文和主要研究主题。"],
      ["解释论文概念", "结合原文与图示理解方法", "从文档库中找一篇关于 VLA 的论文，结合原文和架构图解释它的核心方法。"],
      ["对比研究方法", "找到不同论文中的依据", "在文档库中比较不同论文对记忆机制的设计，引用对应的原文和页码。"],
    ] : [
      ["Tour this codebase", "Entry points, modules, request flow", "Give me a tour of this codebase: entry points, main modules, and how a turn flows."],
      ["Find a rough edge", "Inspect, explain, propose a fix", "Find a bug or rough edge in this project, explain it, and propose a minimal fix."],
      ["Run the tests", "Execute and interpret results", "Run the test suite and summarize any failures with likely causes."],
    ];
    empty.querySelectorAll(".suggestions button").forEach((button, index) => {
      button.querySelector("strong").textContent = prompts[index][0];
      button.querySelector("span").textContent = prompts[index][1];
      button.dataset.prompt = prompts[index][2];
    });
    if (active) {
      const library = (data.libraries || []).find((entry) => entry.id === data.session_context?.library_id);
      $("library-name").textContent = library?.name || "当前文档库";
      refreshDocumentNavigation();
    } else {
      closeEvidence();
    }
  }

  async function refreshDocumentNavigation() {
    const session = state.data?.session_id;
    try {
      const data = await api(`/api/documents?q=${encodeURIComponent($("document-search").value)}`);
      if (session !== state.data?.session_id) return;
      $("document-list").replaceChildren();
      for (const doc of data.documents || []) {
        const button = h("button", "document-item", doc.title);
        button.type = "button";
        button.title = `${doc.page_count} 页 · ${doc.renditions.map((item) => item.rendition).join(", ")}`;
        const rendition = doc.renditions.find((item) => item.rendition !== "basic") || doc.renditions[0];
        button.dataset.locator = `doc:${doc.doc_id}@${rendition.version}/${rendition.rendition}/page:1`;
        $("document-list").appendChild(button);
      }
      if (!(data.documents || []).length) $("document-list").appendChild(h("p", "side-hint", "没有匹配的论文"));
    } catch (error) { toast(error.message); }
  }

  async function loadDocumentScope() {
    const selected = $("deepdocx-library").value;
    $("deepdocx-library-path").textContent = $("deepdocx-library").selectedOptions[0]?.title || "连接文档目录后即可开始。";
    $("deepdocx-scope").replaceChildren();
    if (!selected) return;
    try {
      const data = await api(`/api/documents?library_id=${encodeURIComponent(selected)}`);
      if (selected !== $("deepdocx-library").value) return;
      for (const doc of data.documents || []) {
        const label = h("label", "scope-item");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = doc.doc_id;
        label.append(checkbox, document.createTextNode(doc.title));
        $("deepdocx-scope").appendChild(label);
      }
    } catch (error) { documentError(error.message); }
  }

  function documentError(message) {
    $("deepdocx-error").textContent = message;
    $("deepdocx-error").hidden = !message;
  }

  async function openDocumentDialog() {
    if (state.busy) { toast("请先停止当前任务。"); return; }
    documentError("");
    $("deepdocx-dialog").showModal();
    $("deepdocx-fork-label").hidden = !(state.data?.messages || []).length;
    document.querySelector('input[name="docx-start"][value="new"]').checked = true;
    $("deepdocx-enter").disabled = true;
    $("deepdocx-library").disabled = true;
    $("deepdocx-library").replaceChildren(h("option", "", "正在读取文档库…"));
    try {
      const data = await api("/api/libraries");
      const activeLibrary = state.data?.session_context?.library_id;
      const preferred = data.libraries.some((entry) => entry.id === activeLibrary) ? activeLibrary : data.selected_library_id;
      fillLibraries(data.libraries, preferred);
      if (!data.libraries.length) {
        $("deepdocx-root").closest("details").open = true;
        documentError("未找到可用文档库，请连接下方的文档目录；连接后会自动记住。");
      }
    } catch (error) { documentError(error.message); }
  }

  function fillLibraries(libraries, preferred = "") {
    $("deepdocx-library").replaceChildren();
    for (const entry of libraries) {
      const option = h("option", "", entry.name);
      option.value = entry.id;
      option.title = entry.root;
      $("deepdocx-library").appendChild(option);
    }
    if (libraries.some((entry) => entry.id === preferred)) $("deepdocx-library").value = preferred;
    if (!libraries.length) {
      const placeholder = h("option", "", "未找到文档库，请先连接目录");
      placeholder.value = "";
      $("deepdocx-library").appendChild(placeholder);
    }
    $("deepdocx-library").disabled = !libraries.length;
    $("deepdocx-enter").disabled = !libraries.length;
    loadDocumentScope();
  }

  async function registerLibrary() {
    documentError("");
    $("deepdocx-register").disabled = true;
    try {
      const data = await api("/api/libraries", { root: $("deepdocx-root").value.trim() });
      fillLibraries(data.libraries, data.registered_library_id || data.selected_library_id);
      $("deepdocx-root").closest("details").open = false;
    } catch (error) { documentError(error.message); }
    finally { $("deepdocx-register").disabled = false; }
  }

  async function enterDocumentMode(event) {
    event.preventDefault();
    documentError("");
    $("deepdocx-enter").disabled = true;
    try {
      const data = await api("/api/mode", {
        mode: "deepdocx", library_id: $("deepdocx-library").value,
        fork: document.querySelector('input[name="docx-start"]:checked').value === "fork",
        documents: [...$("deepdocx-scope").querySelectorAll("input:checked")].map((item) => item.value),
      });
      $("deepdocx-dialog").close();
      closeEvidence();
      applyState(data, { render: true });
      await refreshSessions();
      $("input").focus();
    } catch (error) { documentError(error.message); }
    finally { $("deepdocx-enter").disabled = false; }
  }

  function closeEvidence() {
    state.evidenceRequest++;
    $("evidence-panel").hidden = true;
    document.body.classList.remove("evidence-open");
  }

  async function openEvidence(locator) {
    const request = ++state.evidenceRequest;
    $("evidence-panel").hidden = false;
    document.body.classList.add("evidence-open");
    $("evidence-title").textContent = "正在读取来源…";
    $("evidence-text").textContent = "";
    $("evidence-meta").textContent = locator;
    $("evidence-image").hidden = true;
    try {
      const data = await api(`/api/evidence?locator=${encodeURIComponent(locator)}`);
      if (request !== state.evidenceRequest) return;
      const source = data.source || data.evidence?.[0] || {};
      $("evidence-title").textContent = source.title || "文档来源";
      $("evidence-meta").textContent = `第 ${source.page || "—"} 页 · ${source.rendition || ""}${data.truncated ? " · 文本已截断" : ""}`;
      $("evidence-text").textContent = data.text || "";
      const response = await fetch(`/api/evidence?image=1&locator=${encodeURIComponent(data.image_locator)}`, { headers: authHeaders() });
      if (!response.ok) return;
      const blob = await response.blob();
      const reader = new FileReader();
      reader.onload = () => {
        if (request !== state.evidenceRequest) return;
        $("evidence-image").src = reader.result;
        $("evidence-image").hidden = false;
      };
      reader.readAsDataURL(blob);
    } catch (error) {
      if (request === state.evidenceRequest) $("evidence-title").textContent = error.message;
    }
  }

  function populateModels(models, data) {
    const select = $("model-select");
    const current = modelValue(data.provider, data.model);
    select.textContent = "";
    const groups = new Map();
    for (const model of models || []) {
      const value = modelValue(model.provider, model.model);
      let group = groups.get(model.provider);
      if (!group) { group = new Map(); groups.set(model.provider, group); }
      if (!group.has(value)) group.set(value, model.model);
    }
    if (![...groups.values()].some((group) => group.has(current))) {
      let group = groups.get(data.provider);
      if (!group) { group = new Map(); groups.set(data.provider, group); }
      group.set(current, data.model);
    }
    for (const [provider, entries] of groups) {
      const optgroup = document.createElement("optgroup");
      optgroup.label = provider;
      for (const [value, label] of entries) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        option.title = `${provider} · ${label}`;
        optgroup.appendChild(option);
      }
      select.appendChild(optgroup);
    }
    select.value = current;
  }

  function modelValue(provider, model) {
    return `${encodeURIComponent(provider)}/${encodeURIComponent(model)}`;
  }

  function updatePermissionMode(mode) {
    const auto = mode === "bypass";
    for (const button of $("mode-toggle").querySelectorAll("button")) {
      button.classList.toggle("active", (button.dataset.auto === "true") === auto);
    }
    const hints = {
      bypass: "Every tool runs without asking.",
      "accept-edits": "File edits run; other tools ask.",
      default: "Only read-only tools run without asking.",
    };
    $("mode-hint").textContent = hints[mode] || "";
  }

  function updateContext(used, window) {
    const ratio = window > 0 ? Math.min(used / window, 1) : 0;
    const bar = $("ctx-bar");
    bar.style.width = `${Math.max(ratio * 100, used > 0 ? 2 : 0)}%`;
    bar.className = ratio >= 0.9 ? "hot" : ratio >= 0.75 ? "warn" : "";
    const percent =
      used > 0 && ratio < 0.01 ? "<1%" : `${Math.round(ratio * 100)}%`;
    $("ctx-text").textContent = `${formatTokens(used || 0)} / ${formatTokens(window || 0)} tokens · ${percent}`;
  }

  function updateEmptyState() {
    const hasMessages = $("messages").children.length > 0;
    $("empty").hidden = hasMessages;
  }

  function updateTitle(messages) {
    const firstUser = (messages || []).find(
      (message) => message.role === "user" &&
        message.content.some((block) => block.type === "text" && block.text.trim())
    );
    const text = firstUser
      ? firstUser.content.filter((block) => block.type === "text").map((block) => block.text).join(" ")
      : "";
    $("chat-title").textContent = text ? truncate(text, 48) : "New conversation";
  }

  async function refreshState({ render = false } = {}) {
    const box = $("transcript");
    const stick = nearBottom();
    const fromBottom = box.scrollHeight - box.scrollTop;
    try {
      const data = await api("/api/bootstrap");
      applyState(data, { render });
      if (render) {
        if (stick) scrollIfSticky(true);
        else box.scrollTop = box.scrollHeight - fromBottom;
      }
    } catch (_) {
      $("connection-dot").className = "brand-dot offline";
    }
  }

  function formatWhen(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const now = new Date();
    const sameDay = date.toDateString() === now.toDateString();
    return sameDay
      ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : date.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function chatRow(session) {
    const row = h("div", "chat-row");
    const item = h("button", "chat-item");
    item.type = "button";
    item.classList.toggle("current", Boolean(session.current));
    item.classList.toggle("archived", Boolean(session.archived));
    item.title = session.title || "Untitled chat";
    if (session.current) item.setAttribute("aria-current", "true");
    item.appendChild(h("span", "chat-title", session.title || "Untitled chat"));
    if (session.snippet) {
      item.appendChild(h("span", "chat-snippet", session.snippet));
    } else {
      const meta = [session.model, formatWhen(session.created_at)].filter(Boolean).join(" · ");
      if (meta) item.appendChild(h("span", "chat-meta", meta));
    }
    item.addEventListener("click", () => openSession(session));
    item.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      openChatMenu(event.clientX, event.clientY, session);
    });

    const more = h("button", "chat-more", "⋯");
    more.type = "button";
    more.title = "Chat actions";
    more.setAttribute("aria-label", `Actions for ${session.title || "chat"}`);
    more.addEventListener("click", (event) => {
      event.stopPropagation();
      const rect = more.getBoundingClientRect();
      openChatMenu(rect.right - 2, rect.bottom + 4, session);
    });
    row.append(item, more);
    return row;
  }

  function renderSessions(sessions) {
    state.sessions = sessions || [];
    const list = $("chat-list");
    list.textContent = "";
    const active = state.sessions.filter((session) => !session.archived);
    const archived = state.sessions.filter((session) => session.archived);
    if (!active.length && !archived.length) {
      list.appendChild(
        h("div", "chat-empty", state.searchQuery ? "No chats match." : "No saved chats yet.")
      );
      return;
    }
    if (!active.length) {
      list.appendChild(h("div", "chat-empty", "No active chats."));
    }
    for (const session of active) list.appendChild(chatRow(session));
    if (archived.length) {
      const toggle = h(
        "button",
        "archived-toggle",
        `${state.archivedOpen ? "Hide" : "Show"} archived (${archived.length})`
      );
      toggle.type = "button";
      toggle.addEventListener("click", () => {
        state.archivedOpen = !state.archivedOpen;
        renderSessions(state.sessions);
      });
      list.appendChild(toggle);
      if (state.archivedOpen) {
        for (const session of archived) list.appendChild(chatRow(session));
      }
    }
  }

  function downloadFile(name, contents, type) {
    const blob = new Blob([contents], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  function exportChat(format) {
    const data = state.data;
    if (!data || !(data.messages || []).length) {
      toast("This chat is empty.");
      return;
    }
    const slug = (data.title || "dcx-chat")
      .toLowerCase()
      .replace(/[^a-z0-9\u4e00-\u9fff]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 48) || "chat";
    const date = new Date().toISOString().slice(0, 10);
    if (format === "json") {
      const payload = {
        version: data.version,
        session_id: data.session_id,
        title: data.title,
        provider: data.provider,
        model: data.model,
        messages: data.messages,
      };
      downloadFile(`${slug}-${date}.json`, JSON.stringify(payload, null, 2), "application/json");
      toast("Chat exported as JSON.");
      return;
    }
    const lines = [`# ${data.title || "dcx chat"}`, ""];
    const textOf = (content) => content
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("");
    for (const message of data.messages) {
      const text = textOf(message.content);
      const images = message.content.filter((block) => block.type === "image");
      const tools = message.content.filter((block) => block.type === "tool_use");
      const results = message.content.filter((block) => block.type === "tool_result");
      if (message.role === "user" && !text.trim() && !images.length && results.length) {
        for (const result of results) {
          const output = (result.content || [])
            .filter((block) => block.type === "text")
            .map((block) => block.text)
            .join("\n");
          lines.push(`### Tool result${result.is_error ? " (error)" : ""}`, "", "```text", output, "```", "");
        }
        continue;
      }
      if (text.trim()) {
        lines.push(message.role === "assistant" ? "## Assistant" : "## User", "", text.trim(), "");
      }
      for (const image of images) {
        lines.push(`_[image: ${image.media_type}]_`, "");
      }
      for (const tool of tools) {
        lines.push(
          `**Tool: ${tool.name}**`,
          "",
          "```json",
          JSON.stringify(tool.input || {}, null, 2),
          "```",
          ""
        );
      }
      if (message.role === "assistant") {
        const reasoning = message.content
          .filter((block) => block.type === "reasoning")
          .map((block) => block.text)
          .join("\n");
        if (reasoning.trim()) {
          lines.push("<details><summary>Thinking</summary>", "", reasoning.trim(), "", "</details>", "");
        }
      }
    }
    downloadFile(`${slug}-${date}.md`, lines.join("\n"), "text/markdown");
    toast("Chat exported as Markdown.");
  }

  function closeChatMenu() {
    $("chat-menu").hidden = true;
    $("chat-menu").textContent = "";
  }

  function openChatMenu(x, y, session) {
    const menu = $("chat-menu");
    menu.textContent = "";
    const add = (label, handler, { danger = false } = {}) => {
      const button = h("button", danger ? "danger" : "", label);
      button.type = "button";
      button.setAttribute("role", "menuitem");
      button.addEventListener("click", () => {
        closeChatMenu();
        handler();
      });
      menu.appendChild(button);
    };
    if (!session.current) add("Open", () => openSession(session));
    add("Rename", () => openRenameDialog(session));
    if (session.current) {
      add("Export Markdown", () => exportChat("markdown"));
      add("Export JSON", () => exportChat("json"));
    }
    add(session.archived ? "Unarchive" : "Archive", () =>
      setArchived(session, !session.archived)
    );
    add("Copy session id", async () => {
      try {
        await navigator.clipboard.writeText(session.id);
        toast("Session id copied.");
      } catch (_) {
        toast(session.id);
      }
    });
    menu.appendChild(h("div", "menu-sep"));
    add("Delete", () => confirmDelete(session), { danger: true });
    menu.hidden = false;
    menu.style.left = "0px";
    menu.style.top = "0px";
    const rect = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
  }

  let dialogSubmit = null;

  function openDialog(opts) {
    if (!$("permission").hidden) return;
    $("dialog-title").textContent = opts.title;
    $("dialog-body").textContent = opts.body || "";
    $("dialog-body").hidden = !opts.body;
    $("dialog-field").hidden = !opts.field;
    $("dialog-input").value = opts.value || "";
    $("dialog-error").hidden = true;
    const confirm = $("dialog-confirm");
    confirm.textContent = opts.confirmLabel || "Save";
    confirm.classList.toggle("danger", Boolean(opts.danger));
    confirm.disabled = false;
    dialogSubmit = opts.onConfirm;
    $("dialog").hidden = false;
    document.querySelector(".app").inert = true;
    setTimeout(() => {
      if (opts.field) {
        $("dialog-input").focus();
        $("dialog-input").select();
      } else {
        confirm.focus();
      }
    }, 30);
  }

  function closeDialog() {
    $("dialog").hidden = true;
    $("dialog-error").hidden = true;
    dialogSubmit = null;
    document.querySelector(".app").inert = false;
  }

  async function submitDialog() {
    if (!dialogSubmit) return;
    const confirm = $("dialog-confirm");
    const error = $("dialog-error");
    confirm.disabled = true;
    try {
      const message = await dialogSubmit();
      if (message) {
        error.textContent = message;
        error.hidden = false;
        confirm.disabled = false;
        return;
      }
      closeDialog();
    } catch (exc) {
      error.textContent = exc.message || String(exc);
      error.hidden = false;
      confirm.disabled = false;
    }
  }

  function openRenameDialog(session) {
    openDialog({
      title: "Rename chat",
      field: true,
      value: session.title || "",
      confirmLabel: "Save",
      onConfirm: async () => {
        const title = $("dialog-input").value.trim();
        if (!title) return "Enter a name.";
        await api("/api/sessions/rename", { id: session.id, title });
        await refreshSessions();
        if (state.data && state.data.session_id === session.id) {
          await refreshState();
        }
        toast("Chat renamed.");
        return "";
      },
    });
  }

  function confirmDelete(session) {
    const suffix = session.current
      ? " A new empty chat will open after it is removed."
      : "";
    openDialog({
      title: "Delete chat?",
      body: `“${session.title || "This chat"}” and its files will be removed permanently.${suffix}`,
      confirmLabel: "Delete",
      danger: true,
      onConfirm: async () => {
        if (session.current) {
          // The server refuses to delete the open session; move to a fresh one
          // first so any chat, including the current one, can be removed.
          const data = await api("/api/new", {});
          applyState(data, { render: true });
          await refreshSessions();
        }
        await api("/api/sessions/delete", { id: session.id });
        await refreshSessions();
        toast("Chat deleted.");
        return "";
      },
    });
  }

  async function setArchived(session, archived) {
    try {
      if (session.current && archived) {
        const data = await api("/api/new", {});
        applyState(data, { render: true });
        await refreshSessions();
      }
      await api("/api/sessions/archive", { id: session.id, archived });
      await refreshSessions();
      const message = archived
        ? (session.current ? "Chat archived. Started a new chat." : "Chat archived.")
        : "Chat restored.";
      toast(message);
    } catch (error) {
      toast(error.message);
    }
  }

  async function refreshSessions() {
    const requested = state.searchQuery;
    const suffix = requested ? `?q=${encodeURIComponent(requested)}` : "";
    try {
      const data = await api(`/api/sessions${suffix}`);
      if (requested !== state.searchQuery) return;
      renderSessions(data.sessions);
    } catch (_) { /* the chat list is best-effort */ }
  }

  async function openSession(session) {
    if (state.busy) { toast("Stop the current turn first."); return; }
    if (state.editing) cancelEdit();
    if (state.searchQuery) {
      state.searchQuery = "";
      $("chat-search").value = "";
    }
    if (session.current) { setSidebar(false); await refreshSessions(); return; }
    try {
      const data = await api("/api/open", { id: session.id });
      applyState(data, { render: true });
      setSidebar(false);
      await refreshSessions();
      toast("Chat opened.");
    } catch (error) {
      toast(error.message);
    }
  }

  async function newChat() {
    if (state.busy) { toast("Stop the current turn first."); return; }
    if (state.editing) cancelEdit();
    if (state.searchQuery) {
      state.searchQuery = "";
      $("chat-search").value = "";
    }
    if (state.data && (state.data.messages || []).length === 0) {
      setSidebar(false);
      toast("This is already a new chat.");
      await refreshSessions();
      return;
    }
    try {
      const data = await api("/api/new", {});
      applyState(data, { render: true });
      setSidebar(false);
      await refreshSessions();
      toast("Started a new conversation.");
    } catch (error) {
      toast(error.message);
    }
  }

  async function changeModel(value) {
    const [provider, model] = value.split("/").map(decodeURIComponent);
    try {
      const data = await api("/api/model", { provider, model });
      applyState(data);
      toast(`Switched to ${provider} · ${model}`);
    } catch (error) {
      toast(error.message);
      refreshState();
    }
  }

  async function setAuto(enabled) {
    try {
      const data = await api("/api/auto", { enabled });
      updatePermissionMode(data.permission_mode);
      if (state.data) state.data.permission_mode = data.permission_mode;
      toast(enabled ? "Auto mode: tools run without asking." : "Approval mode restored.");
    } catch (error) {
      toast(error.message);
      refreshState();
    }
  }

  async function compactContext() {
    if (state.busy) { toast("Stop the current turn first."); return; }
    const button = $("compact");
    if (button.dataset.busy) return;
    button.dataset.busy = "1";
    button.disabled = true;
    toast("Compacting context…", 120000);
    try {
      const result = await api("/api/compact", {});
      applyState(result.state);
      const noteworthy = (result.events || []).filter((event) =>
        ["compact", "compact_skipped", "compact_failed"].includes(event.kind));
      for (const event of noteworthy) {
        systemNote(`Context ${event.kind.replaceAll("_", " ")}${event.detail ? ` — ${event.detail}` : ""}`);
      }
      toast(
        noteworthy.some((event) => event.kind === "compact")
          ? "Context compaction finished."
          : "Nothing to compact."
      );
    } catch (error) {
      toast(error.message);
    } finally {
      delete button.dataset.busy;
      button.disabled = false;
    }
  }

  // --------------------------------------------------------------- attachments

  const MAX_ATTACHMENTS = 4;
  const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;
  const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

  function renderAttachments() {
    const strip = $("attachments");
    strip.textContent = "";
    strip.hidden = state.attachments.length === 0;
    state.attachments.forEach((image, index) => {
      const item = h("div", "attachment");
      const preview = document.createElement("img");
      preview.src = image.url;
      preview.alt = image.name || "attachment";
      const remove = h("button", "", "×");
      remove.type = "button";
      remove.title = "Remove image";
      remove.setAttribute("aria-label", "Remove image");
      remove.addEventListener("click", () => {
        state.attachments.splice(index, 1);
        renderAttachments();
        updateComposer();
      });
      item.append(preview, remove);
      strip.appendChild(item);
    });
  }

  function clearAttachments() {
    state.attachments = [];
    renderAttachments();
    updateComposer();
  }

  function readImage(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("could not read image"));
      reader.readAsDataURL(file);
    });
  }

  async function addImageFiles(files) {
    if (state.data && state.data.supports_images === false) {
      toast("The active model does not accept image input.");
      return;
    }
    for (const file of Array.from(files || [])) {
      if (state.attachments.length >= MAX_ATTACHMENTS) {
        toast(`At most ${MAX_ATTACHMENTS} images per message.`);
        break;
      }
      if (!IMAGE_TYPES.has(file.type)) {
        toast(`${file.name || "File"}: use PNG, JPEG, GIF, or WebP.`);
        continue;
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        toast(`${file.name || "Image"} is larger than 5 MiB.`);
        continue;
      }
      const currentBytes = state.attachments.reduce(
        (total, image) => total + Math.ceil((image.data.length * 3) / 4),
        0
      );
      if (currentBytes + file.size > 12 * 1024 * 1024) {
        toast("Images are larger than 12 MiB in total.");
        break;
      }
      try {
        const url = await readImage(file);
        const comma = url.indexOf(",");
        if (comma < 0) continue;
        state.attachments.push({
          media_type: file.type,
          data: url.slice(comma + 1),
          url,
          name: file.name || "image",
        });
      } catch (error) {
        toast(error.message || "Could not read image.");
      }
    }
    renderAttachments();
    updateComposer();
  }

  // ------------------------------------------------------------------ composer

  function autoGrow() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
  }

  function updateComposer() {
    const composer = $("composer");
    const send = $("send");
    const input = $("input");
    composer.classList.toggle("busy", state.busy);
    input.disabled = state.busy;
    send.disabled = state.busy ? false : !input.value.trim() && state.attachments.length === 0;
    send.setAttribute("aria-label", state.busy ? (state.stopping ? "Stopping" : "Stop generating") : "Send message");
  }

  function saveDraft() {
    try {
      const value = $("input").value;
      if (value) localStorage.setItem(state.draftKey, value);
      else localStorage.removeItem(state.draftKey);
    } catch (_) { /* storage may be unavailable */ }
  }

  function loadDraft() {
    try {
      const value = localStorage.getItem(state.draftKey);
      if (value) { $("input").value = value; autoGrow(); updateComposer(); }
    } catch (_) { /* ignore */ }
  }

  // -------------------------------------------------------------------- theme

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("dcx-theme", theme); } catch (_) { /* ignore */ }
  }

  function initialTheme() {
    try {
      const saved = localStorage.getItem("dcx-theme");
      if (saved === "light" || saved === "dark") return saved;
    } catch (_) { /* ignore */ }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  // ---------------------------------------------------------------------- init

  function bindEvents() {
    const transcript = $("transcript");
    transcript.addEventListener("scroll", () => { state.stick = nearBottom(); });

    const composer = $("composer");
    const imageInput = $("image-input");
    $("attach").addEventListener("click", () => imageInput.click());
    imageInput.addEventListener("change", () => {
      addImageFiles(imageInput.files);
      imageInput.value = "";
    });
    $("input").addEventListener("paste", (event) => {
      const files = Array.from(event.clipboardData?.files || []).filter((file) =>
        file.type.startsWith("image/"));
      if (files.length) {
        event.preventDefault();
        addImageFiles(files);
      }
    });
    document.addEventListener("paste", (event) => {
      if (event.target === $("input")) return;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
      const files = Array.from(event.clipboardData?.files || []).filter((file) =>
        file.type.startsWith("image/"));
      if (files.length) {
        event.preventDefault();
        addImageFiles(files);
      }
    });
    composer.addEventListener("dragover", (event) => {
      if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
      event.preventDefault();
      composer.classList.add("dragging");
    });
    composer.addEventListener("dragleave", () => composer.classList.remove("dragging"));
    composer.addEventListener("drop", (event) => {
      if (!event.dataTransfer?.files?.length) return;
      event.preventDefault();
      composer.classList.remove("dragging");
      addImageFiles(event.dataTransfer.files);
    });

    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.busy) { stopTurn(); return; }
      const text = $("input").value.trim();
      if (text || state.attachments.length) sendMessage(text);
    });

    $("input").addEventListener("input", () => {
      autoGrow();
      updateComposer();
      saveDraft();
    });

    $("input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!state.busy) $("composer").requestSubmit();
      }
    });

    $("new-chat").addEventListener("click", newChat);
    $("deepdocx-entry").addEventListener("click", async () => {
      if (state.busy) { toast("请先停止当前任务。"); return; }
      if (state.data?.mode !== "deepdocx") { openDocumentDialog(); return; }
      try {
        const data = await api("/api/mode", { mode: "normal" });
        applyState(data, { render: true });
        await refreshSessions();
      } catch (error) { toast(error.message); }
    });
    $("library-name").addEventListener("click", openDocumentDialog);
    $("deepdocx-cancel").addEventListener("click", () => $("deepdocx-dialog").close());
    $("deepdocx-register").addEventListener("click", registerLibrary);
    $("deepdocx-library").addEventListener("change", loadDocumentScope);
    $("deepdocx-form").addEventListener("submit", enterDocumentMode);
    $("document-search").addEventListener("input", refreshDocumentNavigation);
    $("evidence-close").addEventListener("click", closeEvidence);
    document.addEventListener("click", (event) => {
      const source = event.target.closest("[data-locator]");
      if (source) { event.preventDefault(); openEvidence(source.dataset.locator); }
    });
    $("compact").addEventListener("click", compactContext);
    $("model-select").addEventListener("change", (event) => changeModel(event.target.value));
    $("mode-toggle").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-auto]");
      if (button) setAuto(button.dataset.auto === "true");
    });
    $("theme-toggle").addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });
    $("rewind-cancel").addEventListener("click", () => closeRewind(null));
    $("rewind-choices").addEventListener("change", updateRewindConfirm);
    $("rewind-confirm").addEventListener("click", () => {
      const checked = $("rewind-choices").querySelector(
        'input[name="rewind-choice"]:checked'
      );
      const value = checked ? checked.value : "conversation";
      const force = value !== "conversation" && (state.rewindConflicts || []).length > 0;
      if (value === "both") {
        closeRewind({ files: "restore", conversation: "rewind", force });
      } else if (value === "code") {
        closeRewind({ files: "restore", conversation: "keep", force });
      } else {
        closeRewind({ files: "keep", conversation: "rewind", force: false });
      }
    });
    $("rewind").addEventListener("click", (event) => {
      if (event.target === $("rewind")) closeRewind(null);
    });
    $("shortcuts-button").addEventListener("click", openShortcuts);
    $("shortcuts-close").addEventListener("click", closeShortcuts);
    $("shortcuts").addEventListener("click", (event) => {
      if (event.target === $("shortcuts")) closeShortcuts();
    });
    $("chat-search").addEventListener("input", (event) => {
      state.searchQuery = event.target.value.trim();
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(refreshSessions, 220);
    });

    $("menu").addEventListener("click", () => {
      if (isNarrow()) setSidebar(true);
      else setSidebarCollapsed(false);
    });
    $("sidebar-collapse").addEventListener("click", () =>
      setSidebarCollapsed(true, { focus: true })
    );
    $("sidebar-close").addEventListener("click", () => setSidebar(false));
    $("scrim").addEventListener("click", () => setSidebar(false));
    window.addEventListener("resize", () => {
      if (!isNarrow()) setSidebar(false);
    });

    $("messages").addEventListener("click", async (event) => {
      const copy = event.target.closest(".copy");
      if (copy) {
        const code = copy.closest(".code")?.querySelector("code");
        if (!code) return;
        try {
          await navigator.clipboard.writeText(code.textContent || "");
          copy.textContent = "Copied";
          setTimeout(() => { copy.textContent = "Copy"; }, 1400);
        } catch (_) { toast("Could not copy."); }
        return;
      }
      const suggestion = event.target.closest(".suggestions button");
      if (suggestion) {
        $("input").value = suggestion.dataset.prompt || "";
        autoGrow();
        updateComposer();
        $("input").focus();
      }
    });

    $("empty").addEventListener("click", (event) => {
      const suggestion = event.target.closest(".suggestions button");
      if (!suggestion) return;
      $("input").value = suggestion.dataset.prompt || "";
      autoGrow();
      updateComposer();
      $("input").focus();
    });

    $("notice-close").addEventListener("click", () => { $("notice-bar").hidden = true; });
    $("editing-cancel").addEventListener("click", cancelEdit);

    $("dialog-cancel").addEventListener("click", closeDialog);
    $("dialog-confirm").addEventListener("click", submitDialog);
    $("dialog-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitDialog();
      }
    });
    bindSidebarResizer();

    document.addEventListener("pointerdown", (event) => {
      if ($("chat-menu").hidden) return;
      if (!event.target.closest("#chat-menu")) closeChatMenu();
    });
    window.addEventListener("resize", closeChatMenu);
    window.addEventListener("wheel", closeChatMenu, { passive: true });
    window.addEventListener("touchmove", closeChatMenu, { passive: true });

    $("perm-allow").addEventListener("click", () => decidePermission("yes"));
    $("perm-always").addEventListener("click", () => decidePermission("always"));
    $("perm-deny").addEventListener("click", () => decidePermission("no"));

    document.addEventListener("keydown", (event) => {
      if (!$("permission").hidden) {
        if (event.key === "Escape") { event.preventDefault(); decidePermission("no"); }
        if (event.key === "Enter" && event.target.tagName !== "BUTTON") {
          event.preventDefault();
          decidePermission("yes");
        }
        return;
      }
      if (!$("dialog").hidden) {
        if (event.key === "Escape") { event.preventDefault(); closeDialog(); }
        return;
      }
      if (!$("chat-menu").hidden) {
        if (event.key === "Escape") { event.preventDefault(); closeChatMenu(); }
        return;
      }
      if (!$("rewind").hidden) {
        if (event.key === "Escape") { event.preventDefault(); closeRewind(null); }
        return;
      }
      if (!$("shortcuts").hidden) {
        if (event.key === "Escape") { event.preventDefault(); closeShortcuts(); }
        return;
      }
      const command = event.metaKey || event.ctrlKey;
      if (command && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (isNarrow()) setSidebar(true);
        else if (document.body.classList.contains("sidebar-collapsed")) {
          setSidebarCollapsed(false);
        }
        $("chat-search").focus();
        return;
      }
      if (command && event.key.toLowerCase() === "b") {
        event.preventDefault();
        if (isNarrow()) setSidebar(!document.body.classList.contains("sidebar-open"));
        else {
          setSidebarCollapsed(
            !document.body.classList.contains("sidebar-collapsed"),
            { focus: true }
          );
        }
        return;
      }
      if (event.altKey && !command && event.key.toLowerCase() === "n") {
        event.preventDefault();
        newChat();
        return;
      }
      if (command && (event.key === "/" || event.key === "?")) {
        event.preventDefault();
        toggleShortcuts();
        return;
      }
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) ||
        event.target.isContentEditable;
      if (event.key === "?" && !typing) {
        event.preventDefault();
        toggleShortcuts();
        return;
      }
      if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
        event.preventDefault();
        setSidebar(false);
        return;
      }
      if (event.key === "Escape" && state.busy) { event.preventDefault(); stopTurn(); }
    });
  }

  async function init() {
    applyTheme(initialTheme());
    loadSidebarWidth();
    loadSidebarCollapsed();
    bindEvents();
    loadDraft();
    updateComposer();
    try {
      const data = await api("/api/bootstrap");
      applyState(data, { render: true });
      await refreshSessions();
    } catch (error) {
      $("connection-dot").className = "brand-dot offline";
      if (/unauthorized/i.test(error.message)) {
        systemNote(
          "Unauthorized: open the URL printed by the server; it includes the access token.",
          "error"
        );
      } else {
        systemNote(`Cannot reach dcx: ${error.message}`, "error");
      }
    }
    autoGrow();
    updateComposer();
  }

  init();
})();
