(() => {
  "use strict";

  const MAX_SOURCE = 20000;
  const MIN_ZOOM = .05;
  const MAX_ZOOM = 8;
  let queue = Promise.resolve();
  let sequence = 0;
  let viewerBlock = null;
  let zoom = 1;
  let fitted = true;
  let size = { width: 1, height: 1 };
  let drag = null;
  const $ = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }

  function markup(source) {
    return `<div class="code mermaid-block" data-mermaid-state="pending" data-mermaid-view="diagram">` +
      `<div class="code-head"><div class="mermaid-switcher" role="group" aria-label="Mermaid view">` +
      `<button type="button" data-diagram-view="diagram" aria-pressed="true" disabled>Diagram</button>` +
      `<button type="button" data-diagram-view="source" aria-pressed="false">Mermaid</button></div>` +
      `<div class="mermaid-actions"><button class="mermaid-action mermaid-expand" type="button" disabled title="Expand diagram">Expand ↗</button>` +
      `<button class="mermaid-action mermaid-copy" type="button">Copy</button></div></div>` +
      `<pre class="mermaid-source"><code>${escapeHtml(source)}</code></pre>` +
      `<button class="mermaid-canvas" type="button" aria-label="Expand Mermaid diagram" title="Click to expand diagram" hidden></button></div>`;
  }

  function setView(block, view) {
    block.dataset.mermaidView = view;
    const rendered = block.dataset.mermaidState === "done";
    const canvas = block.querySelector(".mermaid-canvas");
    const available = Boolean(canvas.querySelector("svg"));
    block.querySelector(".mermaid-source").hidden = available && view === "diagram";
    canvas.hidden = !available || view !== "diagram";
    canvas.setAttribute("aria-busy", String(block.dataset.mermaidState === "rendering"));
    for (const button of block.querySelectorAll("[data-diagram-view]")) {
      button.setAttribute("aria-pressed", String(button.dataset.diagramView === view));
      if (button.dataset.diagramView === "diagram") button.disabled = !available;
    }
    block.querySelector(".mermaid-expand").disabled = !rendered;
  }

  function theme() {
    const style = getComputedStyle(document.documentElement);
    const color = (name) => style.getPropertyValue(name).trim();
    const ink = color("--ink");
    const border = color("--line-strong");
    const surface = color("--surface");
    const soft = color("--accent-soft");
    const muted = color("--muted");
    return {
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      maxTextSize: MAX_SOURCE,
      theme: "base",
      fontFamily: color("--sans"),
      flowchart: { htmlLabels: false, useMaxWidth: true },
      themeVariables: {
        darkMode: document.documentElement.dataset.theme === "dark",
        fontFamily: color("--sans"), fontSize: "14px",
        background: surface, textColor: ink,
        primaryColor: soft, primaryTextColor: ink, primaryBorderColor: border,
        secondaryColor: color("--surface-2"), secondaryTextColor: ink, secondaryBorderColor: border,
        tertiaryColor: color("--bg-soft"), tertiaryTextColor: ink, tertiaryBorderColor: border,
        mainBkg: soft, nodeBorder: border, lineColor: muted,
        clusterBkg: surface, clusterBorder: border, edgeLabelBackground: surface,
        titleColor: ink, nodeTextColor: ink,
        actorBkg: soft, actorBorder: border, actorTextColor: ink, actorLineColor: muted,
        signalColor: muted, signalTextColor: ink,
        labelBoxBkgColor: surface, labelBoxBorderColor: border, labelTextColor: ink,
        loopTextColor: ink, noteBkgColor: color("--bg-soft"), noteBorderColor: border,
        noteTextColor: ink, activationBkgColor: soft, activationBorderColor: border,
      },
    };
  }

  function fail(block, message) {
    block.dataset.mermaidState = "error";
    block.querySelector(".mermaid-canvas").replaceChildren();
    setView(block, "source");
    let note = block.querySelector(".mermaid-error");
    if (!note) {
      note = document.createElement("div");
      note.className = "mermaid-error";
      note.setAttribute("role", "status");
      block.appendChild(note);
    }
    note.textContent = message;
  }

  async function render(block, source, revision) {
    if (!block.isConnected || block._diagramRevision !== revision) return;
    try {
      window.mermaid.initialize(theme());
      const { svg } = await window.mermaid.render(`dcx-mermaid-${++sequence}`, source);
      // Theme changes and transcript replacement can invalidate in-flight work.
      if (!block.isConnected || block._diagramRevision !== revision) return;
      block.querySelector(".mermaid-canvas").innerHTML = svg;
      block.dataset.mermaidState = "done";
      setView(block, block.dataset.mermaidView);
      if (viewerBlock === block) updateViewer(false);
    } catch (_) {
      if (block.isConnected && block._diagramRevision === revision) {
        fail(block, "Diagram could not be rendered. Showing the Mermaid source instead.");
      }
    }
  }

  function hydrate(root) {
    if (!root?.querySelectorAll) return;
    for (const block of root.querySelectorAll(".mermaid-block[data-mermaid-state='pending']")) {
      const source = block.querySelector(".mermaid-source code").textContent || "";
      if (!source.trim()) { fail(block, "Empty Mermaid diagram."); continue; }
      if (source.length > MAX_SOURCE) {
        fail(block, "Diagram is too large to render. Showing the Mermaid source instead.");
        continue;
      }
      if (!window.mermaid) { fail(block, "Diagram renderer unavailable. Showing the source instead."); continue; }
      block.dataset.mermaidState = "rendering";
      setView(block, block.dataset.mermaidView);
      const revision = block._diagramRevision = (block._diagramRevision || 0) + 1;
      queue = queue.then(() => render(block, source, revision)).catch(() => {});
    }
  }

  function refresh() {
    for (const block of document.querySelectorAll(".mermaid-block")) {
      block.dataset.mermaidState = "pending";
      block.querySelector(".mermaid-error")?.remove();
      setView(block, block.dataset.mermaidView);
    }
    hydrate(document);
  }

  function setZoom(next, center = true) {
    fitted = false;
    const viewport = $("diagram-viewport");
    const x = (viewport.scrollLeft + viewport.clientWidth / 2) / Math.max(1, viewport.scrollWidth);
    const y = (viewport.scrollTop + viewport.clientHeight / 2) / Math.max(1, viewport.scrollHeight);
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, next));
    const image = $("diagram-image");
    image.style.width = `${size.width * zoom}px`;
    image.style.height = `${size.height * zoom}px`;
    $("diagram-zoom-value").textContent = `${Math.round(zoom * 100)}%`;
    $("diagram-zoom-out").disabled = zoom <= MIN_ZOOM;
    $("diagram-zoom-in").disabled = zoom >= MAX_ZOOM;
    viewport.scrollLeft = (center ? x : .5) * viewport.scrollWidth - viewport.clientWidth / 2;
    viewport.scrollTop = (center ? y : .5) * viewport.scrollHeight - viewport.clientHeight / 2;
  }

  function fit() {
    const viewport = $("diagram-viewport");
    setZoom(Math.min(2, (viewport.clientWidth - 48) / size.width, (viewport.clientHeight - 48) / size.height), false);
    fitted = true;
  }

  function updateViewer(reset = true) {
    const svg = viewerBlock?.querySelector(".mermaid-canvas svg");
    if (!svg || !$("diagram-viewer").open) return;
    const box = svg.viewBox.baseVal;
    const bounds = svg.getBoundingClientRect();
    size = { width: box.width || bounds.width || 600, height: box.height || bounds.height || 400 };
    // A separate SVG image keeps IDs isolated and remains sharp at every zoom.
    const image = $("diagram-image");
    const standalone = svg.cloneNode(true);
    standalone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    standalone.setAttribute("width", String(size.width));
    standalone.setAttribute("height", String(size.height));
    standalone.style.maxWidth = "none";
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(standalone))}`;
    if (reset || fitted) fit();
    else setZoom(zoom);
  }

  function open(block, trigger) {
    if (block.dataset.mermaidState !== "done") return;
    const dialog = $("diagram-viewer");
    viewerBlock = block;
    dialog._returnFocus = trigger;
    dialog.showModal();
    updateViewer();
  }

  function close() {
    if ($("diagram-viewer").open) $("diagram-viewer").close();
  }

  async function copy(block, button) {
    const source = block?.querySelector(".mermaid-source code");
    if (!source) return;
    try {
      await navigator.clipboard.writeText(source.textContent || "");
      button.textContent = "Copied";
    } catch (_) {
      button.textContent = "Copy failed";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1400);
  }

  document.addEventListener("click", (event) => {
    const block = event.target.closest(".mermaid-block");
    if (!block) return;
    const tab = event.target.closest("[data-diagram-view]");
    if (tab) { setView(block, tab.dataset.diagramView); return; }
    const expand = event.target.closest(".mermaid-canvas, .mermaid-expand");
    if (expand) { event.preventDefault(); open(block, expand); return; }
    const button = event.target.closest(".mermaid-copy");
    if (button) copy(block, button);
  });

  const dialog = $("diagram-viewer");
  const viewport = $("diagram-viewport");
  $("diagram-close").addEventListener("click", close);
  $("diagram-fit").addEventListener("click", fit);
  $("diagram-actual").addEventListener("click", () => setZoom(1));
  $("diagram-zoom-in").addEventListener("click", () => setZoom(zoom * 1.25));
  $("diagram-zoom-out").addEventListener("click", () => setZoom(zoom / 1.25));
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
  });
  dialog.addEventListener("close", () => {
    viewerBlock = null;
    drag = null;
    viewport.classList.remove("dragging");
    $("diagram-image").removeAttribute("src");
    if (dialog._returnFocus?.isConnected) dialog._returnFocus.focus({ preventScroll: true });
  });
  viewport.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" || event.button !== 0) return;
    event.preventDefault();
    drag = { x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("dragging");
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!drag) return;
    viewport.scrollLeft = drag.left + drag.x - event.clientX;
    viewport.scrollTop = drag.top + drag.y - event.clientY;
  });
  viewport.addEventListener("lostpointercapture", () => {
    drag = null;
    viewport.classList.remove("dragging");
  });
  new ResizeObserver(() => {
    if (!dialog.open) return;
    if (fitted) fit();
    else setZoom(zoom);
  }).observe(viewport);

  window.DcxDiagrams = { markup, hydrate, refresh, close };
})();
