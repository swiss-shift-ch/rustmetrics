/* RustMetrics — Frontend.
   Watchlist + live server browser + notifications.
   Vanilla JS, no build step, runs in the browser as-is.
*/
(() => {
  "use strict";

  const REFRESH_MS         = 15000;
  const BROWSE_AUTO_FETCH  = true;
  const LS_PREFIX          = "rustmetrics.";

  let serversCache   = [];
  let browseCache    = null;
  let onlineStateMap = new Map();
  let currentDrawer  = null;
  let detailTimer    = null;
  let currentTab     = "watchlist";
  let browseFetched  = false;
  let me             = null;
  // Sort state for detail-drawer player lists (persists across auto-refresh)
  const detailSort = {
    current: "name_asc",       // name_asc | name_desc
    active:  "duration_desc",  // duration_desc | start_desc | name_asc
    past:    "ended_desc",     // ended_desc | duration_desc | name_asc
  };

  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const els = {
    navItems:       $$(".nav-item"),
    views:          $$(".view"),
    servers:        $("#servers"),
    serversEmpty:   $("#servers-empty"),
    addForm:        $("#add-server-form"),
    addHost:        $("#srv-host"),
    addPort:        $("#srv-port"),
    statServers:    $("#stat-servers"),
    statWatched:    $("#stat-watched"),
    statOnline:     $("#stat-online"),
    pollIndicator:  $("#poll-indicator"),
    btnNotif:       $("#btn-notif"),
    footerTs:       $("#footer-ts"),
    userSlot:       $("#user-slot"),
    guestCta:       $("#guest-cta"),
    authOnly:       $("#auth-only"),
    cookieBanner:   $("#cookie-banner"),
    cookieAck:      $("#cookie-ack"),
    settingsGuest:  $("#settings-guest"),
    settingsForm:   $("#settings-form"),
    setDiscordWebhook: $("#set-discord-webhook"),
    setNotifyOnline:   $("#set-notify-online"),
    setNotifyOffline:  $("#set-notify-offline"),
    btnTestWebhook:    $("#btn-test-webhook"),
    settingsStatus:    $("#settings-status"),
    drawer:         $("#detail-drawer"),
    drawerBody:     $("#drawer-body"),
    drawerTitle:    $("#drawer-title"),
    browseForm:     $("#browse-filter"),
    bfQ:            $("#bf-q"),
    bfRegion:       $("#bf-region"),
    bfTier:         $("#bf-tier"),
    bfSort:         $("#bf-sort"),
    bfMinPop:       $("#bf-min-pop"),
    bfRefresh:      $("#bf-refresh"),
    browseLoading:  $("#browse-loading"),
    browseResults:  $("#browse-results"),
    browseStatus:   $("#browse-status"),
  };

  const fmt = {
    relTime(ts) {
      if (!ts) return "—";
      const diff = Math.max(0, Math.floor(Date.now() / 1000) - ts);
      if (diff < 60)    return `${diff}s ago`;
      if (diff < 3600)  return `${Math.floor(diff/60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
      return `${Math.floor(diff/86400)}d ago`;
    },
    duration(sec) {
      if (sec == null) return "—";
      if (sec < 60)    return `${Math.floor(sec)}s`;
      if (sec < 3600)  return `${Math.floor(sec/60)}m`;
      const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
      return m ? `${h}h ${m}m` : `${h}h`;
    },
    timeShort(ts) {
      if (!ts) return "—";
      return new Date(ts * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    },
    escape(s) {
      return String(s ?? "").replace(/[&<>"']/g, c =>
        ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    },
  };

  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json", ...(opts.headers||{}) };
    const r = await fetch(path, { ...opts, headers, credentials: "same-origin" });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      const err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    return r.json();
  }

  // ─── Tabs ─────────────────────────────────────────────────────────────
  function switchTab(name) {
    if (!name) return;
    currentTab = name;
    els.navItems.forEach(t => t.classList.toggle("is-active", t.dataset.tab === name));
    els.views.forEach(v => {
      const active = v.dataset.view === name;
      v.classList.toggle("is-active", active);
      v.hidden = !active;
    });
    if (name === "browse" && !browseFetched && BROWSE_AUTO_FETCH) {
      browseFetched = true;
      loadBrowse();
    }
    try { localStorage.setItem(LS_PREFIX + "tab", name); } catch (e) {}
  }
  els.navItems.forEach(t => {
    if (t.dataset.tab) t.addEventListener("click", () => switchTab(t.dataset.tab));
  });
  document.addEventListener("click", (e) => {
    const goto = e.target.closest?.("[data-go-tab]");
    if (goto) { e.preventDefault(); switchTab(goto.dataset.goTab); }
  });

  // ─── Auth / User ──────────────────────────────────────────────────────
  function isAuthed() { return !!(me && me.authenticated); }

  // Whitelist für externe Steam-URLs (Defense gegen javascript:-Scheme falls Steam-Daten kompromittiert)
  function isSafeSteamUrl(url) {
    if (!url || typeof url !== "string") return false;
    return /^https:\/\/(steamcommunity\.com|[a-z0-9_-]+\.steamstatic\.com|[a-z0-9_-]+\.akamaihd\.net|avatars\.cloudflare\.steamstatic\.com)\//.test(url);
  }

  function renderUserSlot() {
    if (isAuthed()) {
      const safeAvatar  = isSafeSteamUrl(me.avatar_url)  ? me.avatar_url  : null;
      const safeProfile = isSafeSteamUrl(me.profile_url) ? me.profile_url : null;
      const avatar = safeAvatar
        ? `<span class="user-chip-avatar" style="background-image:url('${fmt.escape(safeAvatar)}')"></span>`
        : `<span class="user-chip-avatar placeholder">${fmt.escape((me.display_name||'?').slice(0,1).toUpperCase())}</span>`;
      const profileAttrs = safeProfile
        ? `href="${fmt.escape(safeProfile)}" target="_blank" rel="noopener noreferrer"`
        : `href="#" onclick="return false"`;
      els.userSlot.innerHTML = `
        <a class="user-chip" ${profileAttrs} title="Steam profile">
          ${avatar}
          <span class="user-chip-name">${fmt.escape(me.display_name || me.user_id)}</span>
        </a>
        <a class="btn-logout" href="/auth/logout" title="Log out">Logout</a>
      `;
    } else {
      els.userSlot.innerHTML = `
        <a class="btn-steam" href="/auth/steam/login">
          <span class="steam-icon" aria-hidden="true">⌬</span>
          Sign in with Steam
        </a>
      `;
    }
  }

  function applyAuthState() {
    if (isAuthed()) {
      els.guestCta.hidden = true;
      els.authOnly.hidden = false;
      els.settingsGuest && (els.settingsGuest.hidden = true);
      els.settingsForm  && (els.settingsForm.hidden  = false);
      // Settings-Felder mit /api/me-Werten bestücken
      if (me.settings) {
        if (els.setDiscordWebhook) els.setDiscordWebhook.value = me.settings.discord_webhook || "";
        if (els.setNotifyOnline)   els.setNotifyOnline.checked = !!me.settings.notify_online;
        if (els.setNotifyOffline)  els.setNotifyOffline.checked = !!me.settings.notify_offline;
      }
    } else {
      els.guestCta.hidden = false;
      els.authOnly.hidden = true;
      els.settingsGuest && (els.settingsGuest.hidden = false);
      els.settingsForm  && (els.settingsForm.hidden  = true);
      els.servers.innerHTML = "";
      els.serversEmpty.hidden = true;
      els.statServers.textContent = "0";
      els.statWatched.textContent = "0";
      els.statOnline.textContent  = "0";
    }
    renderUserSlot();
  }

  async function loadMe() {
    try { me = await api("/api/me"); }
    catch (e) { me = { authenticated: false }; }
    applyAuthState();
  }

  // ─── Notifications ────────────────────────────────────────────────────
  const NOTIF_ENABLED_KEY = LS_PREFIX + "notif";

  function notifEnabled() {
    return localStorage.getItem(NOTIF_ENABLED_KEY) === "1"
        && "Notification" in window
        && Notification.permission === "granted";
  }
  function setNotifBtn() {
    if (!("Notification" in window)) { els.btnNotif.style.display = "none"; return; }
    const on = notifEnabled();
    els.btnNotif.classList.toggle("active", on);
    els.btnNotif.querySelector(".lbl").textContent =
      on ? "ON" : (Notification.permission === "denied" ? "Blocked" : "Notif");
  }
  els.btnNotif?.addEventListener("click", async () => {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") {
      const p = await Notification.requestPermission();
      if (p === "granted") localStorage.setItem(NOTIF_ENABLED_KEY, "1");
    } else if (Notification.permission === "granted") {
      localStorage.setItem(NOTIF_ENABLED_KEY, notifEnabled() ? "0" : "1");
    }
    setNotifBtn();
  });
  function notifyPlayerOnline(serverName, playerName) {
    if (!notifEnabled()) return;
    try {
      new Notification("Player online", {
        body: `${playerName} just appeared on ${serverName}.`,
        tag:  `rustmetrics-${serverName}-${playerName}`,
      });
    } catch (e) {}
  }

  // ─── Stats ────────────────────────────────────────────────────────────
  function refreshStats() {
    let totalWatched = 0, totalOnline = 0;
    for (const s of serversCache) {
      for (const w of s.watched_players || []) {
        totalWatched++;
        if (w.online) totalOnline++;
      }
    }
    els.statServers.textContent = serversCache.length;
    els.statWatched.textContent = totalWatched;
    els.statOnline.textContent  = totalOnline;
  }

  // ─── Watchlist cards ──────────────────────────────────────────────────
  function renderTags(s) {
    const tags = [];
    if (s.tier) tags.push(`<span class="tag tier-${s.tier}">${fmt.escape(s.tier)}</span>`);
    for (const t of (s.tags || []).slice(0, 4)) {
      if (t === s.tier) continue;
      tags.push(`<span class="tag">${fmt.escape(t)}</span>`);
    }
    return tags.join("");
  }

  function renderWatchlistSub(s) {
    const anonWarn = s.name_anonymized ? `
      <div class="anon-warn">
        ⚠ This server randomizes player names (Facepunch privacy / anti-stalking).
        Watching individual names won't work here — even BattleMetrics gets the same fake names.
      </div>` : "";
    const items = (s.watched_players || []).map(w => {
      const nameHTML = w.bm_player_id
        ? `<a class="player-name plink" href="/player/${w.bm_player_id}" target="_blank" rel="noopener">${fmt.escape(w.name)}</a>`
        : `<span class="player-name">${fmt.escape(w.name)}</span>`;
      return `
      <div class="watched-player ${w.online ? "is-online" : "is-offline"}" data-watch-id="${w.id}">
        <span class="player-dot ${w.online ? "is-online" : ""}"></span>
        ${nameHTML}
        <span class="player-meta">${w.online ? "ONLINE" : "OFFLINE"}</span>
        <button class="btn-remove" title="Remove" data-remove-watch="${s.id}:${w.id}">×</button>
      </div>
    `;
    }).join("");

    return `
      <div class="watchlist">
        ${anonWarn}
        <div class="watchlist-header">
          <strong>Watched Players</strong>
          <span>${s.watched_players?.length || 0}</span>
        </div>
        ${items || `<div class="watchlist-empty">None yet — add names below.</div>`}
        <form class="add-watch-form" data-add-watch="${s.id}">
          <input type="text" placeholder="Player name (exact, as in game)" required />
          <button type="submit">+ Watch</button>
        </form>
      </div>
    `;
  }

  function renderCard(s) {
    const snap   = s.snapshot;
    const online = snap?.online;
    const pop    = snap?.players_count ?? "—";
    const max    = snap?.max_players ?? "—";
    const pct    = (snap?.players_count && snap?.max_players)
                  ? Math.round(100 * snap.players_count / snap.max_players) : 0;
    const ping   = snap?.ping_ms != null ? `${snap.ping_ms}<span class="sub">ms</span>` : "—";
    const map    = snap?.map || "—";
    const wipe   = s.wipe?.days_until_next != null
                  ? `Wipe in ${s.wipe.days_until_next}d` : "—";
    const updated = snap?.ts ? fmt.relTime(snap.ts) : "—";

    return `
      <article class="server-card ${online ? "" : "is-offline"}" data-server-id="${s.id}">
        <header class="card-header">
          <span class="online-dot ${online ? "" : "is-off"}" title="${online ? "online" : "offline"}"></span>
          <div class="card-header-main">
            <h3 class="card-title" data-open-detail="${s.id}">${fmt.escape(s.name)}</h3>
            <div class="card-host">${fmt.escape(s.host)}:${s.port}</div>
            <div class="card-tags">${renderTags(s)}</div>
          </div>
        </header>
        <div class="card-body">
          <div class="card-stats">
            <div class="card-stat">
              <span class="card-stat-label">Players</span>
              <span class="card-stat-value">${pop}<span class="sub"> / ${max}</span></span>
              <div class="fill-bar"><div class="fill-bar-inner" style="width:${pct}%"></div></div>
            </div>
            <div class="card-stat">
              <span class="card-stat-label">Map</span>
              <span class="card-stat-value" style="font-size:13px;">${fmt.escape(map)}</span>
            </div>
            <div class="card-stat">
              <span class="card-stat-label">Ping</span>
              <span class="card-stat-value">${ping}</span>
            </div>
            <div class="card-stat">
              <span class="card-stat-label">Queue</span>
              <span class="card-stat-value">${s.queued ?? 0}</span>
            </div>
          </div>
          ${renderWatchlistSub(s)}
        </div>
        <footer class="card-footer">
          <span class="last-update">${snap?.error
            ? `<span style="color:var(--red)">${fmt.escape(snap.error)}</span>`
            : `updated ${updated}`}</span>
          <span class="wipe-info">${wipe}</span>
          <button class="btn-small" data-open-detail="${s.id}">Details</button>
          <button class="btn-small danger" data-remove-server="${s.id}" title="Remove">×</button>
        </footer>
      </article>
    `;
  }

  function renderServers() {
    if (!serversCache.length) {
      els.servers.innerHTML = "";
      els.serversEmpty.hidden = false;
    } else {
      els.serversEmpty.hidden = true;
      els.servers.innerHTML = serversCache.map(renderCard).join("");
    }
    refreshStats();
  }

  function diffNotifications() {
    for (const s of serversCache) {
      for (const w of s.watched_players || []) {
        const key  = `${s.id}|${w.name.toLowerCase()}`;
        const prev = onlineStateMap.get(key);
        if (prev === false && w.online) notifyPlayerOnline(s.name, w.name);
        onlineStateMap.set(key, !!w.online);
      }
    }
  }

  async function loadServers() {
    if (!isAuthed()) return;
    try {
      const data = await api("/api/servers");
      serversCache = data;
      renderServers();
      diffNotifications();
      els.pollIndicator.classList.remove("pulse");
      void els.pollIndicator.offsetWidth;
      els.pollIndicator.classList.add("pulse");
      els.footerTs.textContent = "Synced " + new Date().toLocaleTimeString();
    } catch (e) {
      if (e.status === 401) { await loadMe(); return; }
      console.error(e);
      els.footerTs.textContent = "Error: " + e.message;
    }
  }

  els.servers.addEventListener("click", async (e) => {
    const t = e.target;
    const openId = t.closest?.("[data-open-detail]")?.dataset.openDetail;
    if (openId) return openDetail(parseInt(openId, 10));
    const rmServer = t.closest?.("[data-remove-server]")?.dataset.removeServer;
    if (rmServer) {
      if (!confirm("Remove this server from your watchlist? All collected data will be lost.")) return;
      try { await api(`/api/server/${rmServer}`, { method: "DELETE" }); await loadServers(); }
      catch (err) { alert(err.message); }
      return;
    }
    const rmWatch = t.closest?.("[data-remove-watch]")?.dataset.removeWatch;
    if (rmWatch) {
      const [sid, wid] = rmWatch.split(":");
      try { await api(`/api/server/${sid}/watch/${wid}`, { method: "DELETE" }); await loadServers(); }
      catch (err) { alert(err.message); }
    }
  });

  els.servers.addEventListener("submit", async (e) => {
    const form = e.target.closest("[data-add-watch]");
    if (!form) return;
    e.preventDefault();
    const sid = form.dataset.addWatch;
    const input = form.querySelector("input");
    const name = input.value.trim();
    if (!name) return;
    try {
      await api(`/api/server/${sid}/watch`, { method: "POST", body: JSON.stringify({ name }) });
      input.value = "";
      await loadServers();
    } catch (err) { alert(err.message); }
  });

  els.addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const host = els.addHost.value.trim();
    const port = parseInt(els.addPort.value, 10);
    if (!host || !port) return;
    try {
      await api("/api/servers", { method: "POST", body: JSON.stringify({ host, port }) });
      els.addHost.value = ""; els.addPort.value = "";
      await loadServers();
      setTimeout(loadServers, 1500);
    } catch (err) { alert(err.message); }
  });

  // ─── Detail drawer ────────────────────────────────────────────────────
  document.querySelectorAll("[data-close-drawer]").forEach(el =>
    el.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && currentDrawer) closeDrawer();
  });

  // Sort-Dropdown delegated change handler (drawer re-rendert komplett, daher delegation)
  els.drawerBody?.addEventListener("change", (e) => {
    const sel = e.target.closest?.(".sort-select");
    if (!sel) return;
    const target = sel.dataset.sortTarget;
    if (!target || !(target in detailSort)) return;
    detailSort[target] = sel.value;
    refreshDetail();   // re-render mit neuem Sort
  });

  // Quick-Watch (+ Button) im Drawer: ein-Klick Spieler zur Watchlist
  els.drawerBody?.addEventListener("click", async (e) => {
    const btn = e.target.closest?.(".p-quick-watch");
    if (!btn) return;
    e.preventDefault();
    const sid  = btn.dataset.quickWatchSid;
    const name = btn.dataset.quickWatchName;
    if (!sid || !name) return;
    // Optimistic UI: Button sofort disablen, visual feedback
    btn.disabled = true;
    btn.textContent = "…";
    btn.style.opacity = "0.5";
    try {
      await api(`/api/server/${sid}/watch`,
                { method: "POST", body: JSON.stringify({ name }) });
      // Watchlist im Hintergrund neu laden, dann Drawer re-rendern
      await loadServers();
      await refreshDetail();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "+";
      btn.style.opacity = "";
      alert("Couldn't add: " + err.message);
    }
  });

  async function openDetail(sid) {
    currentDrawer = sid;
    els.drawer.hidden = false;
    els.drawerBody.innerHTML = `<p style="color:var(--text-dim)">Loading …</p>`;
    await refreshDetail();
    if (detailTimer) clearInterval(detailTimer);
    detailTimer = setInterval(refreshDetail, 10000);
  }
  function closeDrawer() {
    els.drawer.hidden = true;
    currentDrawer = null;
    if (detailTimer) { clearInterval(detailTimer); detailTimer = null; }
  }
  async function refreshDetail() {
    if (!currentDrawer) return;
    try {
      const d = await api(`/api/server/${currentDrawer}?hours=24`);
      els.drawerTitle.textContent = d.name || `${d.host}:${d.port}`;
      els.drawerBody.innerHTML = renderDetail(d);
    } catch (e) {
      els.drawerBody.innerHTML = `<p style="color:var(--red)">Error: ${fmt.escape(e.message)}</p>`;
    }
  }

  function renderDetail(d) {
    const snap = d.snapshot || {};
    const watched = (serversCache.find(s => s.id === d.id)?.watched_players || []);
    const watchedSet = new Set(watched.map(w => w.name.toLowerCase()));
    // name → bm_id lookup für Verlinkung. Für nicht-gewatchte Spieler haben wir
    // im Frontend keinen direkten Zugriff auf BM-IDs (siehe Backend für Public-Page).
    const watchedBmid = new Map();
    for (const w of watched) {
      if (w.bm_player_id) watchedBmid.set(w.name.toLowerCase(), w.bm_player_id);
    }
    const nameLink = (name) => {
      const bid = watchedBmid.get((name || "").toLowerCase());
      return bid
        ? `<a class="pname plink" href="/player/${bid}" target="_blank" rel="noopener">${fmt.escape(name)}</a>`
        : `<span class="pname">${fmt.escape(name)}</span>`;
    };
    let players = [];
    try { players = JSON.parse(snap.players_json || "[]"); } catch (e) {}

    // ── Currently online (sortable: A-Z, Z-A) ──
    const sortedPlayers = players.slice();
    if (detailSort.current === "name_asc")
      sortedPlayers.sort((a,b) => (a||"").toLowerCase().localeCompare((b||"").toLowerCase()));
    else if (detailSort.current === "name_desc")
      sortedPlayers.sort((a,b) => (b||"").toLowerCase().localeCompare((a||"").toLowerCase()));
    const playerItems = sortedPlayers.map(name => {
      const w = watchedSet.has((name || "").toLowerCase());
      const escName = fmt.escape(name);
      const quickBtn = w
        ? `<span class="p-watched-mark" title="On your watchlist">★</span>`
        : `<button class="p-quick-watch" type="button"
             data-quick-watch-sid="${d.id}"
             data-quick-watch-name="${escName}"
             title="Add ${escName} to your watchlist">+</button>`;
      return `<div class="p ${w ? "is-watched" : ""}">${nameLink(name)}${quickBtn}</div>`;
    }).join("") || `<div style="color:var(--text-dim); padding:6px;">No players online — or the server doesn't expose its list.</div>`;

    // ── Active sessions (sortable: longest active, recently joined, A-Z) ──
    const now = Math.floor(Date.now() / 1000);
    const activeSorted = (d.active_sessions || []).slice();
    if (detailSort.active === "duration_desc")
      activeSorted.sort((a,b) => (now - a.start_ts < now - b.start_ts ? 1 : -1));
    else if (detailSort.active === "start_desc")
      activeSorted.sort((a,b) => b.start_ts - a.start_ts);
    else if (detailSort.active === "name_asc")
      activeSorted.sort((a,b) => a.player_name.toLowerCase().localeCompare(b.player_name.toLowerCase()));
    const activeRows = activeSorted.map(s => {
      const dur = now - s.start_ts;
      return `<div class="sess"><span class="sname">${nameLink(s.player_name)}</span><span class="sstart">since ${fmt.timeShort(s.start_ts)}</span><span class="sdur">${fmt.duration(dur)}</span></div>`;
    }).join("");

    // ── Past sessions (sortable: recently ended, longest, A-Z) ──
    const pastSorted = (d.recent_sessions || []).slice();
    if (detailSort.past === "ended_desc")
      pastSorted.sort((a,b) => b.end_ts - a.end_ts);
    else if (detailSort.past === "duration_desc")
      pastSorted.sort((a,b) => (b.end_ts - b.start_ts) - (a.end_ts - a.start_ts));
    else if (detailSort.past === "name_asc")
      pastSorted.sort((a,b) => a.player_name.toLowerCase().localeCompare(b.player_name.toLowerCase()));
    const closedRows = pastSorted.map(s => {
      const dur = s.end_ts - s.start_ts;
      return `<div class="sess"><span class="sname">${nameLink(s.player_name)}</span><span class="sstart">${fmt.timeShort(s.start_ts)}–${fmt.timeShort(s.end_ts)}</span><span class="sdur">${fmt.duration(dur)}</span></div>`;
    }).join("");

    const sortSelectHTML = (target, options, current) =>
      `<select class="sort-select" data-sort-target="${target}">${
        options.map(([v,l]) => `<option value="${v}"${v===current?" selected":""}>${fmt.escape(l)}</option>`).join("")
      }</select>`;

    const wipeStr = d.wipe?.days_until_next != null
      ? `next wipe in ~${d.wipe.days_until_next} days` : "—";

    return `
      <section class="detail-section">
        <h3>Server</h3>
        <dl class="detail-meta">
          <dt>Status</dt><dd>${snap.online ? '<span style="color:var(--green)">● ONLINE</span>' : '<span style="color:var(--red)">● OFFLINE</span>'}</dd>
          <dt>Players</dt><dd>${snap.players_count ?? "—"} / ${snap.max_players ?? "—"}</dd>
          <dt>Map</dt><dd>${fmt.escape(snap.map ?? "—")}</dd>
          <dt>Ping</dt><dd>${snap.ping_ms ?? "—"} ms</dd>
          <dt>Tier</dt><dd>${fmt.escape(d.tier ?? "—")}</dd>
          <dt>Wipe</dt><dd>${wipeStr}</dd>
          <dt>Address</dt><dd>${fmt.escape(d.host)}:${d.port}</dd>
          <dt>Last sync</dt><dd>${fmt.relTime(snap.ts)}</dd>
        </dl>
      </section>
      <section class="detail-section">
        <h3>Population (last 24h)</h3>
        <div class="chart-wrap">${renderChart(d.history || [])}</div>
      </section>
      <section class="detail-section">
        <h3>Currently online (${players.length})
          ${sortSelectHTML("current", [["name_asc","A → Z"],["name_desc","Z → A"]], detailSort.current)}
        </h3>
        <div class="player-list">${playerItems}</div>
      </section>
      ${activeRows ? `<section class="detail-section">
        <h3>Active sessions (${activeSorted.length})
          ${sortSelectHTML("active", [["duration_desc","Longest active"],["start_desc","Recently joined"],["name_asc","A → Z"]], detailSort.active)}
        </h3>
        <div class="session-list">${activeRows}</div></section>` : ""}
      ${closedRows ? `<section class="detail-section">
        <h3>Past sessions (${pastSorted.length})
          ${sortSelectHTML("past", [["ended_desc","Recently ended"],["duration_desc","Longest"],["name_asc","A → Z"]], detailSort.past)}
        </h3>
        <div class="session-list">${closedRows}</div></section>` : ""}
      <section class="detail-section">
        <h3>Raw keywords</h3>
        <code style="display:block; white-space:pre-wrap; padding:8px;">${fmt.escape(d.keywords_raw || "—")}</code>
      </section>
    `;
  }

  function renderChart(hist) {
    if (!hist.length) return `<div style="color:var(--text-dim); padding:24px; text-align:center;">No data yet — collecting.</div>`;
    const W = 600, H = 120, P = 8;
    const xs = hist.map(r => r.ts);
    const ys = hist.map(r => r.players_count ?? 0);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const yMax = Math.max(1, ...ys);
    const xrange = Math.max(1, xMax - xMin);
    const pts = hist.map(r => {
      const x = P + ((r.ts - xMin) / xrange) * (W - 2*P);
      const y = (H - P) - ((r.players_count ?? 0) / yMax) * (H - 2*P);
      return [x, y];
    });
    const path = pts.map((p,i) => `${i===0?"M":"L"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const areaPath = path + ` L${P + (W-2*P)},${H-P} L${P},${H-P} Z`;
    const gridY = [0.25, 0.5, 0.75].map(f => {
      const y = (H - P) - f * (H - 2*P);
      const val = Math.round(yMax * f);
      return `<line x1="${P}" y1="${y}" x2="${W-P}" y2="${y}" stroke="#2a2a2a" stroke-dasharray="2 4" />
              <text x="${W-P-2}" y="${y-2}" text-anchor="end" fill="#555" font-size="9">${val}</text>`;
    }).join("");
    return `
      <svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
        ${gridY}
        <path d="${areaPath}" fill="rgba(205,65,42,0.18)" />
        <path d="${path}" stroke="#cd412a" stroke-width="1.5" fill="none" />
        ${pts.map(p => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="1.5" fill="#e8c428" />`).join("")}
        <text x="${P}" y="12" fill="#555" font-size="9">${new Date(xMin*1000).toLocaleString()}</text>
        <text x="${W-P}" y="12" text-anchor="end" fill="#555" font-size="9">${new Date(xMax*1000).toLocaleString()}</text>
      </svg>
    `;
  }

  // ─── Server browser ───────────────────────────────────────────────────
  function browseQS(forceRefresh) {
    const params = new URLSearchParams();
    if (els.bfQ.value.trim()) params.set("q",       els.bfQ.value.trim());
    if (els.bfRegion.value)   params.set("region",  els.bfRegion.value);
    if (els.bfTier.value)     params.set("tier",    els.bfTier.value);
    if (els.bfSort.value)     params.set("sort",    els.bfSort.value);
    if (els.bfMinPop.value)   params.set("min_pop", els.bfMinPop.value);
    params.set("limit", "250");
    return "/api/browse?" + params.toString() + (forceRefresh ? "&_=" + Date.now() : "");
  }

  async function loadBrowse(forceRefresh = false) {
    els.browseLoading.hidden = false;
    els.browseStatus.textContent = "loading …";
    try {
      const data = await api(browseQS(forceRefresh));
      browseCache = data;
      renderBrowse(data);
      const ageStr = data.cache_age_sec != null
                   ? ` (cached ${data.cache_age_sec}s ago)`
                   : data.fetch_secs ? ` (fetched in ${data.fetch_secs}s)` : "";
      els.browseStatus.textContent =
        `${data.matched.toLocaleString()} of ${data.total_online.toLocaleString()} online · ` +
        `source: ${data.source || "unknown"}${ageStr}`;
    } catch (e) {
      els.browseResults.innerHTML = `<div class="browse-empty" style="color:var(--red)">Error: ${fmt.escape(e.message)}</div>`;
      els.browseStatus.textContent = "Error loading";
    } finally {
      els.browseLoading.hidden = true;
    }
  }

  function renderBrowse(data) {
    const rows = data.servers || [];
    if (!rows.length) {
      els.browseResults.innerHTML = `<div class="browse-empty">No servers match your filters.</div>`;
      return;
    }
    const header = `
      <div class="browse-row head">
        <div>Server</div><div style="text-align:right;">Pop</div>
        <div>Map</div><div style="text-align:right;">Ping</div>
        <div>Wipe</div><div style="text-align:right;">Action</div>
      </div>`;
    const body = rows.map(s => {
      const tags = [];
      if (s.tier) tags.push(`<span class="tag tier-${s.tier}">${fmt.escape(s.tier)}</span>`);
      for (const t of (s.tags || []).slice(0, 3)) {
        if (t === s.tier) continue;
        tags.push(`<span class="tag">${fmt.escape(t)}</span>`);
      }
      const wipeStr = s.wipe?.days_until_next != null ? `in ${s.wipe.days_until_next}d` : "—";
      const queueStr = s.queued > 0 ? `<span class="queue">+${s.queued}q</span>` : "";
      const btn = s.in_watchlist
        ? `<button class="btn-watch is-watched" disabled>✓ Watching</button>`
        : `<button class="btn-watch" data-add-from-browse="${fmt.escape(s.host)}:${s.port}">+ Watch</button>`;
      return `
        <div class="browse-row ${s.in_watchlist ? "is-watched" : ""}">
          <div class="brow-name">
            <strong>${fmt.escape(s.name)}</strong>
            <div class="brow-tags">${tags.join("")}</div>
          </div>
          <div class="brow-pop">${s.players_count}<span class="max">/${s.max_players}</span>${queueStr}</div>
          <div class="brow-map">${fmt.escape(s.map || "—")}</div>
          <div class="brow-ping">${s.ping_ms ?? "—"}<span style="font-size:10px; color:var(--text-dim);">ms</span></div>
          <div class="brow-wipe">${wipeStr}</div>
          <div class="brow-actions">${btn}</div>
        </div>`;
    }).join("");
    els.browseResults.innerHTML = header + body;
  }

  let filterTimer = null;
  function scheduleFilterReload() {
    if (filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(() => loadBrowse(false), 250);
  }
  [els.bfQ, els.bfMinPop].forEach(el => el?.addEventListener("input", scheduleFilterReload));
  [els.bfRegion, els.bfTier, els.bfSort].forEach(el => el?.addEventListener("change", () => loadBrowse(false)));
  els.bfRefresh.addEventListener("click", () => loadBrowse(true));

  els.browseResults.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-add-from-browse]");
    if (!btn) return;
    const [host, portStr] = btn.dataset.addFromBrowse.split(":");
    const port = parseInt(portStr, 10);
    btn.disabled = true; btn.textContent = "loading …";
    try {
      await api("/api/servers", { method: "POST", body: JSON.stringify({ host, port }) });
      btn.textContent = "✓ Watching";
      btn.classList.add("is-watched");
      if (browseCache) {
        for (const s of browseCache.servers) {
          if (s.host === host && s.port === port) s.in_watchlist = true;
        }
      }
      loadServers();
    } catch (err) {
      alert(err.message);
      btn.disabled = false; btn.textContent = "+ Watch";
    }
  });

  // Guest clicking + Watch in browser → prompt to sign in
  els.browseResults.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-add-from-browse]");
    if (btn && !isAuthed()) {
      e.stopImmediatePropagation();
      e.preventDefault();
      if (confirm("You need to sign in with Steam to watch servers. Sign in now?")) {
        window.location.href = "/auth/steam/login";
      }
    }
  }, true);

  // ─── Settings form ────────────────────────────────────────────────────
  function showSettingsStatus(msg, ok) {
    if (!els.settingsStatus) return;
    els.settingsStatus.textContent = msg;
    els.settingsStatus.className = "settings-status " + (ok ? "ok" : "error");
    if (msg) setTimeout(() => {
      if (els.settingsStatus.textContent === msg) {
        els.settingsStatus.textContent = "";
        els.settingsStatus.className = "settings-status";
      }
    }, 6000);
  }
  els.settingsForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      discord_webhook: els.setDiscordWebhook.value.trim(),
      notify_online:   els.setNotifyOnline.checked,
      notify_offline:  els.setNotifyOffline.checked,
    };
    try {
      await api("/api/me/settings", { method: "POST", body: JSON.stringify(body) });
      showSettingsStatus("✓ Settings saved", true);
      // refresh /api/me damit der State synchron ist
      await loadMe();
    } catch (err) {
      showSettingsStatus("Error: " + err.message, false);
    }
  });
  els.btnTestWebhook?.addEventListener("click", async () => {
    // Vorher speichern, falls der User die URL grad geändert hat
    const body = {
      discord_webhook: els.setDiscordWebhook.value.trim(),
      notify_online:   els.setNotifyOnline.checked,
      notify_offline:  els.setNotifyOffline.checked,
    };
    showSettingsStatus("sending …", true);
    try {
      await api("/api/me/settings", { method: "POST", body: JSON.stringify(body) });
      const r = await api("/api/me/test-webhook", { method: "POST" });
      if (r.ok) showSettingsStatus("✓ Test message sent — check Discord!", true);
      else      showSettingsStatus("Discord rejected: " + r.message, false);
    } catch (err) {
      showSettingsStatus("Error: " + err.message, false);
    }
  });

  // ─── Cookie banner ────────────────────────────────────────────────────
  const COOKIE_ACK_KEY = LS_PREFIX + "cookieAck";
  function setupCookieBanner() {
    try {
      if (localStorage.getItem(COOKIE_ACK_KEY) === "1") return;
      els.cookieBanner.hidden = false;
      els.cookieAck.addEventListener("click", () => {
        try { localStorage.setItem(COOKIE_ACK_KEY, "1"); } catch (e) {}
        els.cookieBanner.hidden = true;
      });
    } catch (e) {}
  }

  // ─── Init ─────────────────────────────────────────────────────────────
  setupCookieBanner();
  setNotifBtn();
  (async () => {
    await loadMe();
    loadServers();
    setInterval(loadServers, REFRESH_MS);
  })();
  try {
    const saved = localStorage.getItem(LS_PREFIX + "tab");
    if (["browse","watchlist","about","settings"].includes(saved)) switchTab(saved);
  } catch (e) {}
})();
