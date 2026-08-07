/* Shortlist / hide prefs: localStorage + optional private GitHub Gist sync */

const Prefs = (() => {
  const LS_TOKEN = "augsburg_flats_gist_token";
  const LS_GIST = "augsburg_flats_gist_id";
  const LS_DATA = "augsburg_flats_prefs";
  const GIST_FILENAME = "augsburg-flats-prefs.json";

  let shortlisted = new Set();
  let hidden = new Set();
  let saveTimer = null;
  let statusEl = null;

  function setStatus(msg) {
    if (!statusEl) statusEl = document.getElementById("syncStatus");
    if (statusEl) statusEl.textContent = msg || "";
  }

  function loadLocal() {
    try {
      const raw = localStorage.getItem(LS_DATA);
      if (!raw) return;
      const data = JSON.parse(raw);
      shortlisted = new Set((data.shortlisted || []).map(Number));
      hidden = new Set((data.hidden || []).map(Number));
    } catch (_) {
      shortlisted = new Set();
      hidden = new Set();
    }
  }

  function persistLocal() {
    const payload = {
      shortlisted: [...shortlisted],
      hidden: [...hidden],
      updated_at: new Date().toISOString(),
    };
    localStorage.setItem(LS_DATA, JSON.stringify(payload));
    return payload;
  }

  function getCreds() {
    return {
      token: localStorage.getItem(LS_TOKEN) || "",
      gistId: localStorage.getItem(LS_GIST) || "",
    };
  }

  function setCreds({ token, gistId }) {
    if (token != null) {
      if (token) localStorage.setItem(LS_TOKEN, token);
      else localStorage.removeItem(LS_TOKEN);
    }
    if (gistId != null) {
      if (gistId) localStorage.setItem(LS_GIST, gistId);
      else localStorage.removeItem(LS_GIST);
    }
  }

  function clearCreds() {
    localStorage.removeItem(LS_TOKEN);
    localStorage.removeItem(LS_GIST);
  }

  async function gh(path, { method = "GET", body } = {}) {
    const { token } = getCreds();
    if (!token) throw new Error("No GitHub token configured");
    const res = await fetch(`https://api.github.com${path}`, {
      method,
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function applyPayload(data) {
    if (!data || typeof data !== "object") return;
    shortlisted = new Set((data.shortlisted || []).map(Number));
    hidden = new Set((data.hidden || []).map(Number));
    persistLocal();
  }

  async function pull() {
    const { token, gistId } = getCreds();
    if (!token || !gistId) {
      setStatus(token ? "Token set — create/sync a Gist" : "Local only (open ⚙ to sync)");
      return false;
    }
    setStatus("Syncing…");
    try {
      const gist = await gh(`/gists/${gistId}`);
      const file = gist.files?.[GIST_FILENAME] || Object.values(gist.files || {})[0];
      if (file?.content) applyPayload(JSON.parse(file.content));
      setStatus(`Synced · ${shortlisted.size}★ · ${hidden.size} hidden`);
      return true;
    } catch (err) {
      setStatus("Sync failed: " + (err.message || err).toString().slice(0, 80));
      return false;
    }
  }

  async function push() {
    const { token, gistId } = getCreds();
    if (!token) return;
    const payload = persistLocal();
    const body = {
      description: "Augsburg Flats — shortlist & hide prefs",
      files: {
        [GIST_FILENAME]: { content: JSON.stringify(payload, null, 2) },
      },
    };
    try {
      if (!gistId) {
        body.public = false;
        const created = await gh("/gists", { method: "POST", body });
        setCreds({ gistId: created.id });
        setStatus(`Created Gist · ${created.id.slice(0, 8)}…`);
      } else {
        await gh(`/gists/${gistId}`, { method: "PATCH", body });
        setStatus(`Synced · ${shortlisted.size}★ · ${hidden.size} hidden`);
      }
    } catch (err) {
      setStatus("Save failed: " + (err.message || err).toString().slice(0, 80));
    }
  }

  function schedulePush() {
    persistLocal();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      push().catch(console.error);
    }, 600);
  }

  function isShortlisted(id) {
    return shortlisted.has(Number(id));
  }

  function isHidden(id) {
    return hidden.has(Number(id));
  }

  function toggleShortlist(id) {
    id = Number(id);
    if (shortlisted.has(id)) shortlisted.delete(id);
    else shortlisted.add(id);
    schedulePush();
    return shortlisted.has(id);
  }

  function toggleHidden(id) {
    id = Number(id);
    if (hidden.has(id)) hidden.delete(id);
    else hidden.add(id);
    schedulePush();
    return hidden.has(id);
  }

  function setHidden(id, value) {
    id = Number(id);
    if (value) hidden.add(id);
    else hidden.delete(id);
    schedulePush();
  }

  async function saveSettings({ token, gistId }) {
    setCreds({ token: token.trim(), gistId: (gistId || "").trim() });
    const { gistId: id } = getCreds();
    if (!getCreds().token) {
      setStatus("Local only");
      return { ok: true, localOnly: true };
    }
    if (!id) {
      await push();
      return { ok: true, gistId: getCreds().gistId };
    }
    await pull();
    await push();
    return { ok: true, gistId: getCreds().gistId };
  }

  loadLocal();

  return {
    loadLocal,
    pull,
    push,
    getCreds,
    setCreds,
    clearCreds,
    saveSettings,
    isShortlisted,
    isHidden,
    toggleShortlist,
    toggleHidden,
    setHidden,
    setStatus,
    get shortlisted() {
      return shortlisted;
    },
    get hidden() {
      return hidden;
    },
  };
})();
