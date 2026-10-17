// Code Atlas chat UI. No build step; libraries are vendored under vendor/.
//
// Every conversation is about one index, chosen in the sidebar before the chat is
// started. The index is sent with every message, so the first message is the
// user's own question, with the repository overview already in context.
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $("messages"), input: $("input"), form: $("composer"), send: $("send"), stop: $("stop"),
    repoSelect: $("repo-select"), repoMeta: $("repo-meta"), convs: $("conversations"), newChat: $("new-chat"),
    status: $("status"), banner: $("banner"), docsStatus: $("docs-status"), docsStatusText: $("docs-status-text"), header: $("chat-header"), chatRepo: $("chat-repo"),
    chatRepoMeta: $("chat-repo-meta"), reportToggle: $("report-toggle"), reportView: $("report-view"),
    reportActions: $("report-actions"), reportSummary: $("report-summary"), reportDetailed: $("report-detailed"),
    reportDownload: $("report-download"), compassOpen: $("compass-open"),
  };

  // ---- storage (per-browser; every access guarded) ----
  const KEY = "code-atlas:conversations:v1";
  const REPO_KEY = "code-atlas:selected-index";
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || []; } catch { return []; } };
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state.conversations)); } catch { /* quota/private mode */ } };
  const loadSelected = () => { try { return localStorage.getItem(REPO_KEY) || ""; } catch { return ""; } };
  const saveSelected = (v) => { try { localStorage.setItem(REPO_KEY, v); } catch { /* ignore */ } };

  const state = {
    conversations: load(), activeId: null, streaming: false, controller: null,
    repos: new Map(), reposLoaded: false, repoIds: "", selected: loadSelected(), view: "chat",
    // The loaded report: {repo, payload, markdown (fetched on first use), mode: "summary" | "detailed"}.
    report: null,
  };

  // ---- clipboard ----
  // navigator.clipboard needs a secure context (https or localhost); the fallback covers
  // the app opened over plain http on another host.
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* fall back */ }
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) throw new Error("copy failed");
  }
  const copyButton = (btn, getText, label = "Copy") => {
    btn.onclick = () => copyText(getText())
      .then(() => { btn.textContent = "Copied"; }, () => { btn.textContent = "Copy failed"; })
      .finally(() => setTimeout(() => (btn.textContent = label), 1200));
  };

  // ---- markdown ----
  marked.setOptions({ gfm: true, breaks: false });
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    // In-page links (the detailed report's process table) stay in the page.
    if (node.tagName === "A" && !(node.getAttribute("href") || "").startsWith("#")) { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
  });
  const render = (el, text) => {
    el.innerHTML = DOMPurify.sanitize(marked.parse(text));
    el.querySelectorAll("pre").forEach((pre) => {
      const code = pre.querySelector("code");
      if (code) hljs.highlightElement(code);
      const btn = document.createElement("button");
      btn.className = "copy"; btn.type = "button"; btn.textContent = "Copy";
      copyButton(btn, () => (code ? code.innerText : pre.innerText));
      pre.appendChild(btn);
    });
  };

  // ---- theme for code blocks ----
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const applyTheme = () => { $("hljs-theme").href = dark.matches ? "vendor/github-dark.min.css" : "vendor/github.min.css"; };
  applyTheme(); dark.addEventListener("change", applyTheme);

  // ---- conversations ----
  const active = () => state.conversations.find((c) => c.id === state.activeId);
  const shorten = (text) => (text.length > 48 ? text.slice(0, 45) + "…" : text);
  const firstQuestion = (c) => c.messages.find((m) => m.role === "user")?.content.trim();
  const retitle = (c) => { const q = firstQuestion(c); c.title = q ? shorten(q) : "New chat"; };

  const newConversation = (repo) => {
    const c = { id: String(Date.now()), v: 2, repo, title: "New chat", messages: [] };
    state.conversations.unshift(c); state.activeId = c.id; save(); return c;
  };

  // Conversations saved before indexes were chosen up front opened with a bare
  // index name as their first message: recover the index from it and drop it,
  // with the model's reply to it, so the title comes from the first real question.
  // Runs once per conversation (v: 2 marks conversations in the current shape).
  state.conversations.forEach((c) => {
    if (c.v === 2) return;
    const first = c.messages[0];
    if (first && first.role === "user" && /^[A-Za-z0-9_-]{1,64}$/.test(first.content.trim())) {
      c.repo = first.content.trim();
      c.messages = c.messages.slice(c.messages[1]?.role === "assistant" ? 2 : 1);
    } else if (!c.repo) {
      c.repo = null;  // unknown: it can be read but not continued
    }
    delete c.titled;
    c.v = 2;
    retitle(c);
  });
  save();

  function renderSidebar() {
    els.convs.innerHTML = "";
    state.conversations.forEach((c) => {
      const li = document.createElement("li");
      li.className = "item conv" + (c.id === state.activeId ? " active" : "");
      li.title = c.repo ? `${c.title}\n${c.repo}` : c.title;
      const text = document.createElement("div"); text.className = "text";
      const title = document.createElement("span"); title.className = "title"; title.textContent = c.title;
      const sub = document.createElement("span"); sub.className = "sub"; sub.textContent = c.repo || "no index";
      text.append(title, sub);
      const del = document.createElement("button"); del.className = "del"; del.textContent = "✕"; del.title = "Delete";
      del.onclick = (e) => {
        e.stopPropagation();
        if (state.streaming) return;
        state.conversations = state.conversations.filter((x) => x.id !== c.id);
        if (state.activeId === c.id) state.activeId = state.conversations[0]?.id ?? null;
        save(); renderAll();
      };
      li.append(text, del);
      li.onclick = () => { if (!state.streaming) { state.activeId = c.id; renderAll(); } };
      els.convs.appendChild(li);
    });
  }

  function renderHeader() {
    const c = active();
    els.header.hidden = !c;
    if (!c) return;
    els.chatRepo.textContent = c.repo || "none recorded";
    els.reportToggle.hidden = !c.repo;
    els.compassOpen.hidden = !c.repo;
    const r = c.repo && state.repos.get(c.repo);
    els.chatRepoMeta.textContent = r ? repoMeta(r)
      : c.repo ? (state.repos.size ? "no longer indexed" : "")
      : "This conversation was started before indexes were chosen up front. Start a new chat to continue.";
  }

  // A message row. Its Copy button copies the message as written (Markdown source for
  // answers), read from bubble.source so a streamed answer can set it when it finishes.
  function addRow(role, text) {
    const row = document.createElement("div"); row.className = "row " + role;
    const bubble = document.createElement("div"); bubble.className = "bubble";
    if (role === "assistant") render(bubble, text); else bubble.textContent = text;
    bubble.source = text;
    const actions = document.createElement("div"); actions.className = "msg-actions";
    const copy = document.createElement("button");
    copy.type = "button"; copy.className = "msg-copy"; copy.textContent = "Copy";
    copy.title = role === "user" ? "Copy this prompt" : "Copy this response as Markdown";
    copyButton(copy, () => bubble.source);
    actions.appendChild(copy);
    row.append(bubble, actions); els.messages.appendChild(row);
    return bubble;
  }

  function renderMessages() {
    els.messages.innerHTML = "";
    const c = active();
    if (!c) {
      els.messages.innerHTML = '<div class="empty"><h1>Ask about your code</h1>' +
        '<p>Select an index on the left, then start a new chat. ' +
        'Answers are built from the semantic index and the mirrored source.</p></div>';
      return;
    }
    if (c.messages.length === 0) {
      const empty = document.createElement("div"); empty.className = "empty";
      empty.innerHTML = "<h1></h1><p>Ask anything about this repository: what it does, where something lives, " +
        "how a part works. Answers are built from the semantic index and the mirrored source.</p>";
      empty.querySelector("h1").textContent = c.repo ? `Ask about ${c.repo}` : "No index recorded";
      els.messages.appendChild(empty);
      return;
    }
    c.messages.forEach((m) => addRow(m.role, m.content));
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function renderComposer() {
    const c = active();
    const usable = !!(c && c.repo) && !state.streaming;
    els.input.disabled = !usable; els.send.disabled = !usable;
    els.input.placeholder = !c ? "Select an index and start a new chat"
      : !c.repo ? "Start a new chat to continue" : `Ask about ${c.repo}…`;
    els.newChat.disabled = !state.selected || state.streaming;
    els.newChat.title = state.selected ? `New chat about ${state.selected}` : "Select an index first";
  }

  const renderAll = () => { showChat(); renderSidebar(); renderHeader(); renderMessages(); renderComposer(); };

  // ---- Temporal report view (the conversation's index) ----
  // Two views of one report: the summary (rendered from the report data) and the
  // detailed report (the full Markdown document the indexer writes). Each downloads
  // as what it is: the summary as a standalone HTML page, the detailed report as .md.
  function showChat() {
    state.view = "chat";
    els.reportView.hidden = true; els.messages.hidden = false; els.form.hidden = false;
    els.reportActions.hidden = true;
    els.reportToggle.textContent = "Temporal report";
  }

  const reportMessage = (text) => {
    els.reportView.innerHTML = "";
    const p = document.createElement("p"); p.className = "muted report"; p.textContent = text;
    els.reportView.appendChild(p);
  };
  // The view still shows this report in this mode (the user may have moved on during a fetch).
  const reportCurrent = (r, mode) => state.view === "report" && state.report === r && r.mode === mode && active()?.repo === r.repo;

  async function showReport() {
    const c = active();
    if (!c || !c.repo) return;
    state.view = "report"; state.report = null;
    els.messages.hidden = true; els.form.hidden = true; els.reportView.hidden = false;
    els.reportActions.hidden = true;
    els.reportToggle.textContent = "Back to chat";
    els.reportView.innerHTML = '<div class="report"><p class="muted">Loading the report…</p></div>';
    try {
      const res = await fetch(`/api/report/${encodeURIComponent(c.repo)}`);
      const body = await res.json();
      if (state.view !== "report" || active() !== c) return;  // the user moved on
      if (!res.ok) {
        els.reportView.innerHTML = "";
        const empty = document.createElement("div"); empty.className = "empty";
        empty.innerHTML = "<h1>No Temporal report yet</h1><p></p>";
        empty.querySelector("p").textContent = `${body.error || ""} It is built when the index is updated with the Temporal lens on, or with ./scripts/report.sh ${c.repo}.`;
        els.reportView.appendChild(empty);
        return;
      }
      state.report = { repo: c.repo, payload: body, markdown: null, mode: "summary" };
      els.reportActions.hidden = false;
      showReportMode("summary");
    } catch (e) {
      reportMessage(`Could not load the report: ${e.message}`);
    }
  }

  async function showReportMode(mode) {
    const r = state.report;
    if (!r) return;
    r.mode = mode;
    els.reportSummary.setAttribute("aria-pressed", String(mode === "summary"));
    els.reportDetailed.setAttribute("aria-pressed", String(mode === "detailed"));
    els.reportDownload.title = mode === "summary" ? "Download the summary as an HTML page" : "Download the detailed report as Markdown";
    els.reportDownload.disabled = mode === "detailed" && !r.markdown;
    if (mode === "summary") {
      window.renderTemporalReport(els.reportView, r.payload, {
        onDiscuss: (name) => {
          showChat(); renderMessages(); renderComposer();
          els.input.value = `Tell me about the Temporal opportunity for "${name}": what would change, and what would we gain?`;
          autosize(); els.input.focus();
        },
      });
      els.reportView.scrollTop = 0;
      return;
    }
    if (!r.markdown) {
      els.reportView.innerHTML = '<div class="report"><p class="muted">Loading the detailed report…</p></div>';
      try {
        const res = await fetch(`/api/report/${encodeURIComponent(r.repo)}/markdown`);
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          if (reportCurrent(r, "detailed")) reportMessage(`Could not load the detailed report: ${body.error || res.statusText}`);
          return;
        }
        r.markdown = await res.text();
      } catch (e) {
        if (reportCurrent(r, "detailed")) reportMessage(`Could not load the detailed report: ${e.message}`);
        return;
      }
      if (!reportCurrent(r, "detailed")) return;
      els.reportDownload.disabled = false;
    }
    renderDetailedReport(r);
  }

  // Heading anchors the way the report renderer writes its links (render.py _anchor).
  const anchor = (text) => [...text.toLowerCase().replace(/ /g, "-")].filter((ch) => /[\p{L}\p{N}-]/u.test(ch)).join("");

  function renderDetailedReport(r) {
    els.reportView.innerHTML = "";
    const wrap = document.createElement("div"); wrap.className = "report md-report";
    if (r.payload.stale) {
      const n = document.createElement("p"); n.className = "notice";
      n.textContent = "The code has changed since this report was built. It will update on the next index run.";
      wrap.appendChild(n);
    }
    const doc = document.createElement("div");
    render(doc, r.markdown);
    // Anchors as data attributes, not ids, so a process name cannot collide with an id in the page.
    doc.querySelectorAll("h1, h2, h3, h4").forEach((hd) => { hd.dataset.anchor = anchor(hd.textContent); });
    doc.addEventListener("click", (e) => {
      const a = e.target.closest('a[href^="#"]');
      if (!a) return;
      e.preventDefault();
      const target = doc.querySelector(`[data-anchor="${CSS.escape(decodeURIComponent(a.getAttribute("href").slice(1)))}"]`);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    wrap.appendChild(doc);
    els.reportView.appendChild(wrap);
    els.reportView.scrollTop = 0;
  }

  const saveFile = (name, type, text) => {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement("a"); a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  async function downloadReport() {
    const r = state.report;
    if (!r) return;
    if (r.mode === "detailed") {
      if (r.markdown) saveFile(`${r.repo}-temporal-report-detailed.md`, "text/markdown;charset=utf-8", r.markdown);
      return;
    }
    // The summary as one self-contained page: the rendered report with the app's styles inlined.
    const report = els.reportView.querySelector(".report");
    if (!report) return;
    let css = "";
    try { css = await (await fetch("style.css")).text(); } catch { /* the page still reads without styles */ }
    const clone = report.cloneNode(true);
    clone.querySelectorAll(".opp-actions").forEach((el) => el.remove());  // buttons that only work in the app
    const title = `Temporal report: ${r.repo}`.replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[ch]);
    saveFile(`${r.repo}-temporal-report.html`, "text/html;charset=utf-8", `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>${css}
body { display: block; height: auto; }
</style>
</head>
<body>
${clone.outerHTML}
</body>
</html>
`);
  }

  // ---- Compass Web (the chat's index in the compose `compass` service) ----
  // The tab is opened on the click itself (so it is not blocked as a popup), then sent to
  // the URL the server looks up; if the lookup fails, the tab says why.
  const compassTitle = els.compassOpen.title;
  async function openCompass() {
    const c = active();
    if (!c || !c.repo) return;
    els.compassOpen.title = compassTitle;
    const tab = window.open("", "_blank");
    if (tab) { tab.opener = null; tab.document.title = "Opening Compass…"; tab.document.body.textContent = "Opening Compass…"; }
    let error;
    try {
      const res = await fetch(`/api/compass/${encodeURIComponent(c.repo)}`);
      const body = await res.json();
      if (res.ok) {
        if (tab) tab.location.replace(body.url); else window.open(body.url, "_blank", "noopener");
        return;
      }
      error = body.error || `Server returned ${res.status}`;
    } catch (e) {
      error = e.message;
    }
    if (tab) { tab.document.title = "Compass unavailable"; tab.document.body.textContent = `Could not open Compass: ${error}`; }
    els.compassOpen.title = `Could not open Compass: ${error}`;
  }

  // ---- chat ----
  function setStreaming(on) {
    state.streaming = on;
    els.send.hidden = on; els.stop.hidden = !on;
    renderComposer();
    if (!on && !els.input.disabled) els.input.focus();
  }

  async function ask(text) {
    text = text.trim();
    const c = active();
    if (!text || state.streaming || !c || !c.repo) return;
    c.messages.push({ role: "user", content: text });
    retitle(c);
    save(); renderAll(); els.input.value = ""; autosize();

    const bubble = addRow("assistant", "");
    bubble.classList.add("typing");
    let answer = ""; let frame = null;
    const flush = () => { frame = null; render(bubble, answer); els.messages.scrollTop = els.messages.scrollHeight; };

    setStreaming(true);
    state.controller = new AbortController();
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_id: c.repo, messages: c.messages }), signal: state.controller.signal,
      });
      if (!res.ok) {
        let detail = `Server returned ${res.status}`;
        try { const body = await res.json(); detail = body.error || JSON.stringify(body.detail) || detail; } catch { /* not JSON */ }
        throw new Error(detail);
      }

      const reader = res.body.getReader(); const decoder = new TextDecoder(); let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const frameText = buf.slice(0, idx); buf = buf.slice(idx + 2);
          if (!frameText.startsWith("data: ")) continue;
          const data = frameText.slice(6);
          if (data === "[DONE]") continue;
          const chunk = JSON.parse(data);
          if (typeof chunk === "string") answer += chunk;
          else if (chunk && chunk.error) answer += `\n\n**Error:** ${chunk.error}`;
          if (!frame) frame = setTimeout(flush, 80);
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") answer += `\n\n**Error:** ${e.message}`;
      else answer += "\n\n_(stopped)_";
    } finally {
      if (frame) clearTimeout(frame);
      bubble.classList.remove("typing"); render(bubble, answer || "_(no response)_");
      bubble.source = answer || "(no response)";
      c.messages.push({ role: "assistant", content: answer || "(no response)" });
      save(); setStreaming(false); renderSidebar();
      els.messages.scrollTop = els.messages.scrollHeight;
    }
  }

  // ---- indexes + health ----
  // Index details: file count plus the knowledge pack the Temporal lens was built with.
  function repoMeta(r) {
    const files = `${Number(r.files).toLocaleString()} files`;
    const mixed = (r.prompt_versions || []).length > 1 ? " · mixed summaries" : "";
    let versions = (r.pack_versions || []).map((v) => v.version);
    if (versions.length === 0 && r.pack_version) versions = [r.pack_version];
    if (!r.lens || r.lens === "off" || versions.length === 0) return files + mixed;
    const pack = `pack ${versions.join(" + ")}`;
    return (r.lens === "partial" ? `${files} · ${pack} (partial)` : `${files} · ${pack}`) + mixed;
  }

  function lensTooltip(r) {
    const prompts = r.prompt_versions || [];
    if (prompts.length > 1) {
      const counts = prompts.map((v) => `${v.version == null ? "older" : `version ${v.version}`}: ${v.files}`).join(", ");
      return `Summaries were written by different prompt versions (${counts}). Re-index with --force to regenerate them all.`;
    }
    if (r.lens === "mixed" || r.lens === "partial") {
      const counts = (r.pack_versions || [])
        .map((v) => `pack ${v.version}: ${v.files} ${v.files === 1 ? "file" : "files"}`).join(", ");
      const why = r.lens === "mixed" ? "Files use different pack versions" : "Some files have no Temporal lens yet";
      return `${why}${counts ? ` (${counts})` : ""}. Re-index with --refresh-lens to update every file.`;
    }
    if (r.lens === "on") return `Temporal lens built with knowledge pack ${r.pack_version}`;
    return "";
  }

  function renderRepoSelect() {
    const sel = els.repoSelect;
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = !state.reposLoaded ? "Loading…" : state.repos.size ? "Select an index…" : "No indexes yet";
    sel.appendChild(placeholder);
    for (const id of state.repos.keys()) {
      const opt = document.createElement("option"); opt.value = id; opt.textContent = id;
      sel.appendChild(opt);
    }
    if (state.reposLoaded && !state.repos.has(state.selected)) state.selected = "";
    sel.value = state.selected;
    const r = state.repos.get(state.selected);
    els.repoMeta.textContent = r ? repoMeta(r)
      : state.reposLoaded && !state.repos.size ? "Index one with the CLI (see README)." : "";
    els.repoMeta.title = r ? lensTooltip(r) : "";
  }

  async function loadRepos() {
    try {
      const { repos } = await (await fetch("/api/repos")).json();
      state.repos = new Map((repos || []).map((r) => [r.repo_id, r]));
    } catch {
      els.repoMeta.textContent = "Could not load indexes.";
      return;
    }
    // Rebuild the options only when the list of indexes changed (including the first
    // load), so the 30-second refresh never disturbs an open dropdown.
    const ids = [...state.repos.keys()].join("\n");
    const changed = !state.reposLoaded || ids !== state.repoIds;
    state.reposLoaded = true; state.repoIds = ids;
    if (changed) renderRepoSelect();
    else {
      const r = state.repos.get(state.selected);
      if (r) { els.repoMeta.textContent = repoMeta(r); els.repoMeta.title = lensTooltip(r); }
    }
    renderHeader(); renderComposer();
  }

  // Temporal Docs MCP light: green when a real docs search works, red with the fix otherwise.
  const DOCS_LABEL = { connected: "connected", unavailable: "unavailable", not_configured: "not signed in" };
  function renderDocsStatus(d) {
    const s = (d && d.status) || "unavailable";
    els.docsStatus.className = `docs-status ${s === "not_configured" ? "" : s}`;
    els.docsStatusText.textContent = `Temporal Docs MCP${s === "connected" ? "" : ` · ${DOCS_LABEL[s] || s}`}`;
    const when = d && d.checked_at ? `\nChecked ${new Date(d.checked_at).toLocaleTimeString()}. Click to check again.` : "";
    els.docsStatus.title = d ? [d.detail, d.fix].filter(Boolean).join("\n") + when : "Could not check the Temporal Docs MCP.";
  }

  let healthSeq = 0;  // only the latest check renders (a slow earlier one must not overwrite it)
  async function loadHealth(recheck = false) {
    const seq = ++healthSeq;
    try {
      const res = await fetch(`/api/health${recheck ? "?recheck=true" : ""}`); const h = await res.json();
      if (seq !== healthSeq) return;
      renderDocsStatus(h.temporal_docs);
      els.status.textContent = `Model: ${h.model}`;
      const problems = [];
      if (!h.api_key_configured) problems.push("ANTHROPIC_API_KEY is not set in .env");
      if (h.database !== "ok") problems.push(`Database ${h.database}`);
      els.banner.hidden = problems.length === 0; els.banner.textContent = problems.join(". ");
    } catch {
      if (seq !== healthSeq) return;
      els.banner.hidden = false; els.banner.textContent = "Cannot reach the server.";
      renderDocsStatus(null);
    }
  }

  // ---- input ----
  const autosize = () => { els.input.style.height = "auto"; els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px"; };
  els.input.addEventListener("input", autosize);
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); els.form.requestSubmit(); }
  });
  els.form.addEventListener("submit", (e) => { e.preventDefault(); ask(els.input.value); });
  els.stop.addEventListener("click", () => state.controller && state.controller.abort());
  els.reportToggle.addEventListener("click", () => {
    if (state.view === "report") { showChat(); renderMessages(); renderComposer(); }
    else if (!state.streaming) showReport();
  });
  els.reportSummary.addEventListener("click", () => { if (state.report?.mode !== "summary") showReportMode("summary"); });
  els.reportDetailed.addEventListener("click", () => { if (state.report?.mode !== "detailed") showReportMode("detailed"); });
  els.reportDownload.addEventListener("click", downloadReport);
  els.compassOpen.addEventListener("click", openCompass);

  els.repoSelect.addEventListener("change", () => {
    state.selected = els.repoSelect.value; saveSelected(state.selected);
    renderRepoSelect();
    // A chat with no messages yet follows the selection; a started chat keeps its index.
    const c = active();
    if (c && c.messages.length === 0 && state.selected) { c.repo = state.selected; save(); renderAll(); }
    else renderComposer();
  });
  els.newChat.addEventListener("click", () => {
    if (state.streaming || !state.selected) return;
    const c = active();
    if (c && c.messages.length === 0) { c.repo = state.selected; save(); }  // reuse an empty chat
    else newConversation(state.selected);
    renderAll(); els.input.focus();
  });

  state.activeId = state.conversations[0]?.id ?? null;
  renderAll(); renderRepoSelect(); loadRepos(); loadHealth();
  setInterval(loadRepos, 30000);
  setInterval(loadHealth, 5 * 60 * 1000);
  els.docsStatus.addEventListener("click", () => {
    els.docsStatus.className = "docs-status checking";
    loadHealth(true);
  });
})();
