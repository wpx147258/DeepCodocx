(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    data: null,
    busy: false,
    stopping: false,
    turn: null,
    permission: null,
    stick: true,
    controller: null,
    draftKey: "cli-agent-draft",
    toastTimer: null,
    sessions: [],
  };

  // ------------------------------------------------------------------ utils

  function api(path, body) {
    const options = {
      method: body === undefined ? "GET" : "POST",
      headers: {},
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

  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
    $("scrim").hidden = !open;
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

  function addUserMessage(content) {
    const text = renderTextBlocks(content);
    const images = content.filter((block) => block.type === "image");
    if (!text.trim() && !images.length) return;
    const row = h("div", "msg msg-user");
    const body = h("div", "msg-body");
    if (text.trim()) body.appendChild(h("div", "bubble", text));
    for (const image of images) {
      const img = document.createElement("img");
      img.alt = "attached image";
      img.src = `data:${image.media_type};base64,${image.data}`;
      img.style.maxWidth = "260px";
      img.style.borderRadius = "12px";
      img.style.marginTop = "6px";
      body.appendChild(img);
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
    summary.append(icon, h("span", "tool-name", name || "Tool"));
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
    let cards = new Map();
    for (const message of messages) {
      const results = message.content.filter((block) => block.type === "tool_result");
      const text = renderTextBlocks(message.content);
      if (message.role === "assistant") {
        if (!shell) {
          shell = assistantShell();
          cards = new Map();
        }
        renderAssistantContent(shell, message.content, cards);
      } else if (results.length && !text.trim() && shell) {
        for (const result of results) {
          finishToolCard(cards.get(result.tool_use_id), result, null);
        }
      } else {
        shell = null;
        addUserMessage(message.content);
      }
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

  function createTurn(userText) {
    addUserMessage([{ type: "text", text: userText }]);
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
        systemNote(
          `Retrying (${event.attempt}/${event.max_attempts}) — ${event.reason}` +
          (event.partial ? " · output may repeat" : "")
        );
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
    state.busy = true;
    state.stopping = false;
    updateComposer();
    const turn = createTurn(text);
    $("input").value = "";
    saveDraft();
    autoGrow();
    state.stick = true;
    scrollIfSticky(true);

    const controller = new AbortController();
    state.controller = controller;
    let sawDone = false;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
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
      refreshState();
      refreshSessions();
    }
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
    $("perm-scope").textContent = event.scope ? `Always scope: ${event.scope}` : "";
    const highRisk = /high-risk/i.test(event.reason || "");
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
    state.data = data;
    $("connection-dot").className = "brand-dot online";
    $("model-chip").textContent = data.model;
    $("top-sub").textContent = data.cwd || "workspace";
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
    $("ctx-text").textContent = `${formatTokens(used || 0)} / ${formatTokens(window || 0)} tokens · ${Math.round(ratio * 100)}%`;
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

  async function refreshState() {
    try {
      const data = await api("/api/bootstrap");
      applyState(data);
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

  function renderSessions(sessions) {
    state.sessions = sessions || [];
    const list = $("chat-list");
    list.textContent = "";
    if (!state.sessions.length) {
      list.appendChild(h("div", "chat-empty", "No saved chats yet."));
      return;
    }
    for (const session of state.sessions) {
      const button = h("button", `chat-item${session.current ? " current" : ""}`);
      button.type = "button";
      button.title = session.title || "Untitled chat";
      if (session.current) button.setAttribute("aria-current", "true");
      button.appendChild(h("span", "chat-title", session.title || "Untitled chat"));
      const meta = [session.model, formatWhen(session.created_at)].filter(Boolean).join(" · ");
      if (meta) button.appendChild(h("span", "chat-meta", meta));
      button.addEventListener("click", () => openSession(session));
      list.appendChild(button);
    }
  }

  async function refreshSessions() {
    try {
      const data = await api("/api/sessions");
      renderSessions(data.sessions);
    } catch (_) { /* the chat list is best-effort */ }
  }

  async function openSession(session) {
    if (state.busy) { toast("Stop the current turn first."); return; }
    if (session.current) { setSidebar(false); return; }
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
    if (state.data && (state.data.messages || []).length === 0) {
      setSidebar(false);
      toast("This is already a new chat.");
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
    send.disabled = state.busy ? false : !input.value.trim();
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
    try { localStorage.setItem("cli-agent-theme", theme); } catch (_) { /* ignore */ }
  }

  function initialTheme() {
    try {
      const saved = localStorage.getItem("cli-agent-theme");
      if (saved === "light" || saved === "dark") return saved;
    } catch (_) { /* ignore */ }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  // ---------------------------------------------------------------------- init

  function bindEvents() {
    const transcript = $("transcript");
    transcript.addEventListener("scroll", () => { state.stick = nearBottom(); });

    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.busy) { stopTurn(); return; }
      const text = $("input").value.trim();
      if (text) sendMessage(text);
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
    $("compact").addEventListener("click", compactContext);
    $("model-select").addEventListener("change", (event) => changeModel(event.target.value));
    $("mode-toggle").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-auto]");
      if (button) setAuto(button.dataset.auto === "true");
    });
    $("theme-toggle").addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });

    $("menu").addEventListener("click", () => setSidebar(true));
    $("sidebar-close").addEventListener("click", () => setSidebar(false));
    $("scrim").addEventListener("click", () => setSidebar(false));

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
    bindEvents();
    loadDraft();
    updateComposer();
    try {
      const data = await api("/api/bootstrap");
      applyState(data, { render: true });
      await refreshSessions();
    } catch (error) {
      $("connection-dot").className = "brand-dot offline";
      systemNote(`Cannot reach cli-agent: ${error.message}`, "error");
    }
    autoGrow();
    updateComposer();
  }

  init();
})();
