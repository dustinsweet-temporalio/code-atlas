// Indexing from the UI: the add / sync dialog (folder picker, the "is a sync needed?"
// check), the delete confirmation, and the progress panel above the main view.
//
// app.js creates it once with window.createIndexing(deps):
//   deps.repos()               Map of indexed repositories (repo_id -> /api/repos entry)
//   deps.config()              /api/config (indexing settings and the Temporal UI link)
//   deps.conversationsAbout(id) number of saved conversations about an index
//   deps.onSyncsChanged()      the set of running syncs changed (re-render the buttons)
//   deps.onIndexesChanged()    a sync finished or an index was deleted (reload the indexes)
//   deps.onDeleted(id)         an index was deleted
//   deps.openChat(id)          start a chat about an index
// Syncs are listed by polling /api/syncs: every 2 seconds while one runs, every 15 otherwise.
(() => {
  window.createIndexing = (deps) => {
    const $ = (id) => document.getElementById(id);
    const els = {
      dialog: $("index-dialog"), form: $("index-form"), title: $("ix-title"),
      path: $("ix-path"), browse: $("ix-browse"), browser: $("ix-browser"), up: $("ix-up"), cwd: $("ix-cwd"),
      entries: $("ix-entries"), browserNote: $("ix-browser-note"), use: $("ix-use"), pathStatus: $("ix-path-status"),
      name: $("ix-name"), nameStatus: $("ix-name-status"), cache: $("ix-cache"), db: $("ix-db"), modes: $("ix-mode"),
      check: $("ix-check"), checkResult: $("ix-check-result"), error: $("ix-error"), start: $("ix-start"),
      delDialog: $("delete-dialog"), delForm: $("delete-form"), delName: $("del-name"), delConvs: $("del-convs"),
      delError: $("del-error"), delConfirm: $("del-confirm"), panel: $("sync-panel"),
    };
    const NAME = /^[A-Za-z0-9_-]{1,64}$/;
    const NAME_HINT = els.nameStatus.textContent;

    const getJSON = async (url, options) => {
      const res = await fetch(url, options);
      let body = {};
      try { body = await res.json(); } catch { /* not JSON */ }
      if (!res.ok) {
        const detail = body.error || (Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail);
        throw new Error(detail || `Server returned ${res.status}`);
      }
      return body;
    };
    const post = (url, data) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    const node = (tag, className, text) => {
      const n = document.createElement(tag);
      if (className) n.className = className;
      if (text != null) n.textContent = text;
      return n;
    };
    const setNote = (el, text, tone) => { el.textContent = text; el.className = `field-note ${tone || "muted"}`; };

    // ---- the add / sync dialog ----
    // mode "add": a new index (path and name chosen here); "sync": an existing index (name fixed).
    // folder: the /api/fs answer for the path field, as typed in folderFor (the server resolves ~ and the like).
    const dlg = { mode: "add", repo: null, nameEdited: false, folder: null, folderFor: null, pathSeq: 0, checkSeq: 0, busy: false, browsePath: null };
    let pathTimer = null;

    const name = () => els.name.value.trim();
    const syncMode = () => (dlg.mode === "add" ? "new" : els.form.elements.mode.value);
    // The folder is usable: a git repository with commits.
    const folderOk = () => !!(dlg.folder && dlg.folder.git && !dlg.folder.git.error && dlg.folderFor === els.path.value.trim());

    function nameProblem() {
      const n = name();
      if (!n) return "Enter a name for the index.";
      if (!NAME.test(n)) return "Use only letters, numbers, - and _ (at most 64).";
      if (dlg.mode === "add" && deps.repos().has(n)) return `An index named ${n} already exists. Choose another name, or use Sync on ${n}.`;
      return null;
    }

    function renderName() {
      const problem = name() ? nameProblem() : null;
      if (problem) setNote(els.nameStatus, problem, "bad"); else setNote(els.nameStatus, NAME_HINT);
      const n = NAME.test(name()) ? name() : "<name>";
      const cfg = deps.config();
      els.cache.value = `${(cfg.source_dir || "/data/source").replace(/\/$/, "")}/${n}`;
      els.db.textContent = `${cfg.db_prefix || "semv2"}_${n}`;
      renderButtons();
    }

    function renderButtons() {
      const ready = folderOk() && !nameProblem() && !dlg.busy;
      els.start.disabled = !ready;
      els.check.disabled = !ready;
      els.start.title = ready ? "" : !folderOk() ? "Choose a git repository first" : nameProblem() || "";
    }

    const clearCheck = () => { dlg.checkSeq++; els.checkResult.hidden = true; els.checkResult.textContent = ""; };
    const showError = (text) => { els.error.hidden = !text; els.error.textContent = text || ""; };

    function renderFolderStatus(info, error) {
      if (error) { setNote(els.pathStatus, error, "bad"); return; }
      if (!info) { setNote(els.pathStatus, ""); return; }
      if (!info.git) setNote(els.pathStatus, "No .git folder here, so this folder can't be indexed. Choose the repository's root folder (the one that contains .git).", "bad");
      else if (info.git.error) setNote(els.pathStatus, info.git.error, "bad");
      else {
        const g = info.git;
        const at = g.branch ? `${g.branch} at ${g.short}` : `${g.short} (detached HEAD)`;
        setNote(els.pathStatus, `✓ Git repository · ${at}${g.subject ? ` · ${g.subject}` : ""}`, "good");
      }
    }

    // Look up the typed folder (debounced while typing). Fills the name from the folder in add mode.
    async function inspectPath(immediate) {
      clearTimeout(pathTimer);
      const path = els.path.value.trim();
      dlg.folder = null; clearCheck(); renderButtons();
      if (!path) { renderFolderStatus(null); return; }
      if (!immediate) { setNote(els.pathStatus, "Checking the folder…"); pathTimer = setTimeout(() => inspectPath(true), 350); return; }
      const seq = ++dlg.pathSeq;
      try {
        const info = await getJSON(`/api/fs?path=${encodeURIComponent(path)}`);
        if (seq !== dlg.pathSeq) return;
        useFolderInfo(info, path);
      } catch (e) {
        if (seq !== dlg.pathSeq) return;
        renderFolderStatus(null, e.message);
      }
    }

    function useFolderInfo(info, typed) {
      dlg.folder = info; dlg.folderFor = typed;
      renderFolderStatus(info);
      if (dlg.mode === "add" && !dlg.nameEdited && info.git && !info.git.error) els.name.value = info.suggested_name;
      renderName();
    }

    // Folder picker: subfolders of REPOS_ROOT, git repositories marked.
    async function browseTo(path) {
      els.entries.innerHTML = ""; els.browserNote.textContent = "Loading…"; els.use.disabled = true;
      let info;
      try {
        info = await getJSON(`/api/fs?path=${encodeURIComponent(path || "")}`);
      } catch (e) {
        if (path) return browseTo("");  // a typed path that does not exist: start from the root
        els.browserNote.textContent = e.message;
        return;
      }
      dlg.browsePath = info;
      els.cwd.textContent = info.path; els.cwd.title = info.path;
      els.up.disabled = !info.parent;
      const usable = info.git && !info.git.error;
      els.use.disabled = !usable;
      els.browserNote.textContent = usable ? "This folder is a git repository." : info.git?.error || (info.entries.length ? "Open a folder; git repositories are marked." : "No subfolders here.");
      if (info.truncated) els.browserNote.textContent += " (Only the first folders are listed; type the path instead.)";
      for (const entry of info.entries) {
        const li = node("li");
        const btn = node("button", "entry" + (entry.git ? " git" : ""));
        btn.type = "button";
        btn.append(node("span", "entry-icon", entry.git ? "◆" : "▸"), node("span", "entry-name", entry.name));
        if (entry.git) btn.append(node("span", "badge", "git"));
        btn.title = entry.git ? `${entry.path} (git repository)` : entry.path;
        btn.addEventListener("click", () => browseTo(entry.path));
        btn.addEventListener("dblclick", () => { if (entry.git) pickFolder(entry.path); });
        li.appendChild(btn); els.entries.appendChild(li);
      }
      els.entries.scrollTop = 0;
    }

    function setBrowserOpen(open) {
      els.browser.hidden = !open;
      els.browse.setAttribute("aria-expanded", String(open));
      els.browse.textContent = open ? "Hide folders" : "Browse…";
    }

    async function pickFolder(path) {
      setBrowserOpen(false);
      els.path.value = path;
      await inspectPath(true);
      els.name.focus();
    }

    function openDialog(mode, repo) {
      dlg.mode = mode; dlg.repo = repo; dlg.nameEdited = mode === "sync"; dlg.busy = false; dlg.folder = null;
      els.form.reset();
      showError(""); clearCheck();
      const sync = mode === "sync";
      els.title.textContent = sync ? `Sync ${repo}` : "Add an index";
      els.start.textContent = sync ? "Start sync" : "Start indexing";
      els.modes.hidden = !sync;
      els.name.readOnly = sync;
      els.name.value = sync ? repo : "";
      const known = sync ? deps.repos().get(repo)?.repo_path : "";
      els.path.value = known || "";
      dlg.folderFor = null;
      setBrowserOpen(false);
      renderFolderStatus(null);
      renderName();
      els.dialog.showModal();
      if (known) inspectPath(true);
      else {
        if (sync) setNote(els.pathStatus, "This index does not record where its repository is (it was built by an older version). Choose the folder.", "muted");
        setBrowserOpen(true); browseTo("");
      }
      (sync ? els.form.elements.mode[0] : els.path).focus();
    }

    els.path.addEventListener("input", () => inspectPath(false));
    els.path.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); inspectPath(true); } });
    els.name.addEventListener("input", () => { dlg.nameEdited = true; clearCheck(); renderName(); });
    els.form.addEventListener("change", (e) => { if (e.target.name === "mode") clearCheck(); });
    els.browse.addEventListener("click", () => {
      const open = els.browser.hidden;
      setBrowserOpen(open);
      if (open) browseTo(els.path.value.trim());
    });
    els.up.addEventListener("click", () => { if (dlg.browsePath?.parent) browseTo(dlg.browsePath.parent); });
    els.use.addEventListener("click", () => { if (dlg.browsePath) pickFolder(dlg.browsePath.path); });
    for (const d of [els.dialog, els.delDialog]) {
      d.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => d.close()));
    }
    els.dialog.addEventListener("cancel", (e) => { if (dlg.busy) e.preventDefault(); });  // not while starting

    els.check.addEventListener("click", async () => {
      if (!folderOk() || nameProblem()) return;
      const seq = ++dlg.checkSeq;
      els.check.disabled = true; els.check.textContent = "Checking…";
      els.checkResult.hidden = false; els.checkResult.className = "check-result"; els.checkResult.textContent = "Comparing the repository's files with the index…";
      try {
        const r = await post("/api/sync/check", { repo_id: name(), repo_path: dlg.folder.path, force: syncMode() === "force" });
        if (seq !== dlg.checkSeq) return;
        els.checkResult.className = `check-result ${r.needed ? "warn" : "good"}`;
        els.checkResult.textContent = (r.needed ? "" : "✓ ") + r.message;
      } catch (e) {
        if (seq !== dlg.checkSeq) return;
        els.checkResult.className = "check-result bad";
        els.checkResult.textContent = e.message;
      } finally {
        els.check.textContent = "Check whether a sync is needed";
        renderButtons();
      }
    });

    els.form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (dlg.busy || !folderOk() || nameProblem()) return;
      dlg.busy = true; showError(""); renderButtons();
      const label = els.start.textContent;
      els.start.textContent = "Starting… (preflight checks)";
      try {
        const started = await post("/api/sync", { repo_id: name(), repo_path: dlg.folder.path, mode: syncMode() });
        dlg.busy = false;
        els.dialog.close();
        onStarted(started);
      } catch (err) {
        dlg.busy = false;
        showError(err.message);
        renderButtons();
      } finally {
        els.start.textContent = label;
      }
    });

    // ---- delete ----
    let deleting = null;
    function openDelete(repo) {
      deleting = repo;
      els.delName.textContent = repo;
      const n = deps.conversationsAbout(repo);
      els.delConvs.textContent = n ? `${n} saved ${n === 1 ? "conversation" : "conversations"} about it will stay in the sidebar, but can't be continued.` : "";
      els.delError.hidden = true; els.delConfirm.disabled = false;
      els.delDialog.showModal();
    }
    els.delForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!deleting) return;
      els.delConfirm.disabled = true; els.delConfirm.textContent = "Deleting…";
      try {
        await getJSON(`/api/repos/${encodeURIComponent(deleting)}`, { method: "DELETE" });
        els.delDialog.close();
        deps.onDeleted(deleting);
        deps.onIndexesChanged();
      } catch (err) {
        els.delError.hidden = false; els.delError.textContent = err.message;
        els.delConfirm.disabled = false;
      } finally {
        els.delConfirm.textContent = "Delete index";
      }
    });

    // ---- progress panel ----
    const DISMISSED_KEY = "code-atlas:dismissed-syncs";
    const RECENT_MS = 60 * 60 * 1000;  // closed syncs not watched here are shown for an hour
    const sync = { jobs: [], error: null, timer: null, watched: new Set(), best: new Map(), runningKey: "" };
    let dismissed;
    try { dismissed = new Set(JSON.parse(localStorage.getItem(DISMISSED_KEY)) || []); } catch { dismissed = new Set(); }
    const saveDismissed = () => { try { localStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissed].slice(-50))); } catch { /* ignore */ } };

    const temporalLink = (job) => {
      const cfg = deps.config();
      const base = (cfg.temporal_ui_url || "http://localhost:8233").replace(/\/$/, "");
      const ns = encodeURIComponent(cfg.temporal_namespace || "default");
      return `${base}/namespaces/${ns}/workflows/${encodeURIComponent(job.workflow_id)}${job.run_id ? `/${encodeURIComponent(job.run_id)}/history` : ""}`;
    };
    const money = (v) => (v == null ? "" : `$${v < 0.01 && v > 0 ? v.toFixed(4) : v.toFixed(2)}`);

    function visible(job) {
      if (job.status === "running") return true;
      if (dismissed.has(job.run_id)) return false;
      return sync.watched.has(job.workflow_id) || (job.closed_at && Date.now() - Date.parse(job.closed_at) < RECENT_MS);
    }

    function jobRow(job) {
      const running = job.status === "running";
      const tone = running ? (job.waiting ? "waiting" : "running") : job.result || "error";
      let pct = Math.max(0, Math.min(100, job.percent || 0));
      if (running) { pct = Math.max(pct, sync.best.get(job.workflow_id) || 0); sync.best.set(job.workflow_id, pct); }
      const row = node("div", `sync-job ${tone}`);
      const line = node("div", "sync-line");
      const icon = { ok: "✓", warning: "⚠", error: "✕" }[tone];
      if (icon) line.append(node("span", "sync-icon", icon));
      line.append(node("strong", "sync-name", job.repo_id));
      const desc = node("span", "sync-desc", job.description || (running ? "Starting" : ""));
      desc.title = job.description || "";
      line.append(desc);
      if (running && !job.waiting) line.append(node("span", "sync-pct", `${pct}%`));
      if (!running && job.cost_usd) line.append(node("span", "sync-cost", money(job.cost_usd)));
      const link = node("a", "sync-link", "Temporal ↗");
      link.href = temporalLink(job); link.target = "_blank"; link.rel = "noopener noreferrer";
      link.title = `Open ${job.workflow_id} in the Temporal UI`;
      line.append(link);
      if (!running) {
        if (job.result !== "error" && !job.dry_run && deps.repos().has(job.repo_id)) {
          const open = node("button", "link", "Open chat"); open.type = "button";
          open.addEventListener("click", () => deps.openChat(job.repo_id));
          line.append(open);
        }
        const close = node("button", "icon dismiss", "✕"); close.type = "button";
        close.title = "Dismiss"; close.setAttribute("aria-label", `Dismiss ${job.repo_id}`);
        close.addEventListener("click", () => { dismissed.add(job.run_id); saveDismissed(); renderPanel(); });
        line.append(close);
      }
      const bar = node("div", "bar" + (running && job.waiting ? " indeterminate" : ""));
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-label", `Indexing ${job.repo_id}`);
      bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100");
      if (!(running && job.waiting)) bar.setAttribute("aria-valuenow", String(running ? pct : 100));
      const fill = node("span"); fill.style.width = `${running ? pct : 100}%`;
      bar.append(fill);
      const started = job.started_at ? `Started ${new Date(job.started_at).toLocaleString()}` : "";
      const by = job.origin === "ui" ? "" : " by ./scripts/index.sh";
      row.title = [started + by, running && job.cost_usd ? `Cost so far ${money(job.cost_usd)}` : ""].filter(Boolean).join("\n");
      row.append(line, bar);
      return row;
    }

    function renderPanel() {
      const jobs = sync.jobs.filter(visible);
      els.panel.innerHTML = "";
      jobs.forEach((job) => els.panel.appendChild(jobRow(job)));
      els.panel.hidden = jobs.length === 0;
    }

    async function poll() {
      clearTimeout(sync.timer);
      let finished = false;
      try {
        const { syncs } = await getJSON("/api/syncs");
        sync.jobs = syncs || []; sync.error = null;
        for (const job of sync.jobs) {
          if (job.status === "running") sync.watched.add(job.workflow_id);
          else if (sync.best.has(job.workflow_id)) { sync.best.delete(job.workflow_id); finished = true; }
        }
      } catch (e) {
        sync.error = e.message;  // keep showing the last known state
      }
      renderPanel();
      const key = sync.jobs.filter((j) => j.status === "running").map((j) => j.repo_id).sort().join("\n");
      if (key !== sync.runningKey) { sync.runningKey = key; deps.onSyncsChanged(); }
      if (finished) deps.onIndexesChanged();
      sync.timer = setTimeout(poll, sync.runningKey ? 2000 : 15000);
    }

    // A sync just started: show it at once, take focus to the panel, and poll quickly.
    function onStarted(started) {
      const job = { ...started, status: "running", origin: "ui", percent: 0, description: "Starting", started_at: new Date().toISOString() };
      sync.jobs = [job, ...sync.jobs.filter((j) => j.workflow_id !== job.workflow_id)];
      sync.watched.add(job.workflow_id); sync.best.set(job.workflow_id, 0);
      renderPanel();
      sync.runningKey = ""; deps.onSyncsChanged();
      els.panel.focus({ preventScroll: true });
      els.panel.scrollIntoView({ block: "nearest" });
      clearTimeout(sync.timer); sync.timer = setTimeout(poll, 800);
    }

    const running = (repo) => sync.jobs.find((j) => j.repo_id === repo && j.status === "running") || null;

    return {
      openAdd: () => openDialog("add", null),
      openSync: (repo) => openDialog("sync", repo),
      openDelete,
      running,
      render: renderPanel,
      start: poll,
    };
  };
})();
