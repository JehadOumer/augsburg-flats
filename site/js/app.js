/* Static Augsburg Flats gallery — reads data/*.json, prefs via Prefs (Gist) */

const state = {
  listings: [],
  config: null,
  filtered: [],
  map: null,
  markers: null,
  mapReady: false,
  photosMin: 0,
  shortlistMode: "all",
  term: "",
  tenancy: "",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.hidden = true;
  }, 2200);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function scoreClass(score) {
  if (score >= 70) return "high";
  if (score >= 40) return "mid";
  return "low";
}

function fmtPrice(p) {
  if (p == null || p === "") return "Price n/a";
  return `€${Math.round(p)}`;
}

function fmtTransit(l) {
  const m = l?.transit_uni_min;
  if (m == null || m < 0) return null;
  const line = l.transit_uni_summary ? ` · ${l.transit_uni_summary}` : "";
  return `${m} min by metro${line}`;
}

function fmtDist(km) {
  if (km == null) return null;
  return `${Number(km).toFixed(1)} km to uni`;
}

function fmtDistPrimary(l) {
  return fmtTransit(l) || fmtDist(l.distance_uni_km);
}

function allImages(l) {
  const urls = l.image_urls || [];
  return urls.filter((u) => typeof u === "string" && u.startsWith("http"));
}

function photoCount(l) {
  return allImages(l).length;
}

function iconLabelsHtml(l) {
  const bits = [];
  if (l.term_type === "short") {
    bits.push(`<span class="icon-label term-short">Short</span>`);
  } else if (l.term_type === "long") {
    bits.push(`<span class="icon-label term-long">Long</span>`);
  }
  if (l.tenancy_type === "owner") {
    bits.push(`<span class="icon-label tenancy-owner">Owner</span>`);
  } else if (l.tenancy_type === "sublet") {
    bits.push(`<span class="icon-label tenancy-sublet">Sublet</span>`);
  }
  return bits.length ? `<div class="icon-labels">${bits.join("")}</div>` : "";
}

function carouselHtml(images, { idPrefix = "c" } = {}) {
  if (!images.length) {
    return `<div class="carousel placeholder">No photo</div>`;
  }
  const slides = images
    .slice(0, 6)
    .map(
      (src, i) =>
        `<img src="${escapeHtml(src)}" alt="" loading="${i === 0 ? "eager" : "lazy"}" decoding="async" referrerpolicy="no-referrer" data-i="${i}" ${i === 0 ? "" : "hidden"} />`
    )
    .join("");
  const nav =
    images.length > 1
      ? `<div class="carousel-nav">
          <button type="button" data-dir="-1" aria-label="Previous">‹</button>
          <button type="button" data-dir="1" aria-label="Next">›</button>
        </div>
        <div class="carousel-count"><span class="cur">1</span>/${Math.min(images.length, 6)}</div>`
      : "";
  return `<div class="carousel" data-carousel="${escapeHtml(idPrefix)}">${slides}${nav}</div>`;
}

function bindCarousel(root) {
  const imgs = $$("img", root);
  if (imgs.length < 2) return;
  let i = 0;
  const cur = $(".carousel-count .cur", root);
  const show = (n) => {
    i = (n + imgs.length) % imgs.length;
    imgs.forEach((img, idx) => {
      img.hidden = idx !== i;
    });
    if (cur) cur.textContent = String(i + 1);
  };
  root.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-dir]");
    if (!btn) return;
    e.stopPropagation();
    show(i + Number(btn.dataset.dir));
  });
}

function pinColor(l) {
  if (Prefs.isShortlisted(l.id)) return "#7c3aed";
  const s = l.match_score || 0;
  if (s >= 70) return "#059669";
  if (s >= 40) return "#2563eb";
  return "#d97706";
}

function cardHtml(l) {
  const images = allImages(l);
  const sc = scoreClass(l.match_score || 0);
  const isShort = Prefs.isShortlisted(l.id);
  const dist = fmtDistPrimary(l);
  const metaBits = [l.district || l.address, dist].filter(Boolean).join(" · ");
  let sourceTag = l.source || "";
  try {
    sourceTag = new URL(l.url).hostname.replace(/^www\./, "");
  } catch (_) {}

  const tags = [`<span class="tag">${escapeHtml(sourceTag)}</span>`];
  if (isShort) tags.push(`<span class="tag cat-shortlist">shortlist</span>`);
  if (l.furnished) tags.push(`<span class="tag">furnished</span>`);
  if (l.rooms) tags.push(`<span class="tag">${l.rooms} room</span>`);
  if (l.size_sqm) tags.push(`<span class="tag">${l.size_sqm} m²</span>`);

  return `
    <article class="card${l.status === "gone" ? " gone" : ""}${isShort ? " shortlisted" : ""}" data-id="${l.id}">
      <div class="card-media">
        ${carouselHtml(images, { idPrefix: String(l.id) })}
        ${iconLabelsHtml(l)}
        <div class="price-fab">${fmtPrice(l.price)} <span>/ mo</span></div>
        <button type="button" class="hide-fab" data-hide="${l.id}" title="Hide" aria-label="Hide">✕</button>
        <button type="button" class="shortlist-fab${isShort ? " on" : ""}" data-shortlist="${l.id}" title="Shortlist" aria-label="Shortlist">★</button>
        <div class="score-ring ${sc}">${Math.round(l.match_score || 0)}</div>
        ${l.is_new ? `<span class="badge new">NEW</span>` : ""}
      </div>
      <div class="card-body">
        <h3>${escapeHtml(l.title || "Apartment")}</h3>
        <div class="card-meta">${escapeHtml(metaBits || "Augsburg")}</div>
        <div class="tags">${tags.join("")}</div>
      </div>
    </article>`;
}

function filterListings() {
  const q = ($("#fQ").value || "").trim().toLowerCase();
  const priceMax = Number($("#fPriceMax").value) || null;
  const priceMin = Number($("#fPriceMin").value) || null;
  const transitMax = Number($("#fDistMax").value);
  const sept = $("#fSept").checked;
  const showGone = $("#fGone").checked;
  const showHiddenOnly = $("#fShowHidden").checked;
  const sort = $("#fSort").value;

  let items = state.listings.slice();

  items = items.filter((l) => {
    const hid = Prefs.isHidden(l.id);
    if (showHiddenOnly) return hid;
    if (hid) return false;
    return true;
  });

  if (!showGone) items = items.filter((l) => l.status !== "gone");

  if (state.shortlistMode === "only") {
    items = items.filter((l) => Prefs.isShortlisted(l.id));
  } else if (state.shortlistMode === "hide") {
    items = items.filter((l) => !Prefs.isShortlisted(l.id));
  }

  if (state.term) items = items.filter((l) => l.term_type === state.term);
  if (state.tenancy) items = items.filter((l) => l.tenancy_type === state.tenancy);

  if (priceMax != null) items = items.filter((l) => l.price == null || l.price <= priceMax);
  if (priceMin != null) items = items.filter((l) => l.price == null || l.price >= priceMin);

  if (state.photosMin > 0) {
    items = items.filter((l) => photoCount(l) >= state.photosMin);
  }

  if (transitMax < 90) {
    items = items.filter(
      (l) =>
        l.transit_uni_min == null ||
        l.transit_uni_min < 0 ||
        l.transit_uni_min <= transitMax
    );
  }

  if (sept) {
    items = items.filter(
      (l) =>
        !l.available_from ||
        l.available_from === "sofort" ||
        String(l.available_from) <= "2026-09-01"
    );
  }

  if (q) {
    items = items.filter((l) => {
      const blob = [l.title, l.description, l.address, l.district, l.source]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return blob.includes(q);
    });
  }

  const sorters = {
    score: (a, b) => (b.match_score || 0) - (a.match_score || 0),
    price_asc: (a, b) => (a.price ?? 1e9) - (b.price ?? 1e9),
    price_desc: (a, b) => (b.price ?? 0) - (a.price ?? 0),
    transit: (a, b) => {
      const av = a.transit_uni_min == null || a.transit_uni_min < 0 ? 1e9 : a.transit_uni_min;
      const bv = b.transit_uni_min == null || b.transit_uni_min < 0 ? 1e9 : b.transit_uni_min;
      return av - bv;
    },
    distance: (a, b) => (a.distance_uni_km ?? 1e9) - (b.distance_uni_km ?? 1e9),
    newest: (a, b) => String(b.first_seen || "").localeCompare(String(a.first_seen || "")),
    size: (a, b) => (b.size_sqm ?? 0) - (a.size_sqm ?? 0),
  };
  items.sort(sorters[sort] || sorters.score);

  state.filtered = items;
  return items;
}

function renderGallery() {
  const items = filterListings();
  const gal = $("#gallery");
  gal.innerHTML = items.map(cardHtml).join("") || `<p class="gallery-meta">No listings match.</p>`;
  gal.querySelectorAll(".carousel").forEach(bindCarousel);
  $("#galleryMeta").textContent = `${items.length} listing${items.length === 1 ? "" : "s"} · updated ${
    state.config?.exported_at ? String(state.config.exported_at).slice(0, 16).replace("T", " ") : "—"
  }`;
  updateMap(items);
}

function initMap() {
  if (state.mapReady || typeof L === "undefined") return;
  const uni = state.config?.university || { lat: 48.3345, lon: 10.8974 };
  state.map = L.map("map", { zoomControl: true }).setView([uni.lat, uni.lon], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 19,
  }).addTo(state.map);
  state.markers = L.layerGroup().addTo(state.map);
  L.circleMarker([uni.lat, uni.lon], {
    radius: 9,
    color: "#0f3d32",
    fillColor: "#0f3d32",
    fillOpacity: 0.9,
    weight: 2,
  })
    .bindPopup(`<strong>${escapeHtml(uni.name || "University")}</strong>`)
    .addTo(state.map);
  state.mapReady = true;
}

function updateMap(listings) {
  if (!state.mapReady || !state.markers) return;
  state.markers.clearLayers();
  listings.forEach((l) => {
    if (l.lat == null || l.lon == null) return;
    const marker = L.circleMarker([l.lat, l.lon], {
      radius: 8,
      color: "#fff",
      weight: 2,
      fillColor: pinColor(l),
      fillOpacity: 0.95,
    });
    marker.bindTooltip(
      `<strong>${escapeHtml(l.title || "")}</strong><br/>${fmtPrice(l.price)}`,
      { direction: "top", opacity: 1 }
    );
    marker.on("click", () => openDrawer(l.id));
    state.markers.addLayer(marker);
  });
  setTimeout(() => state.map.invalidateSize(), 80);
}

function openDrawer(id) {
  const l = state.listings.find((x) => x.id === Number(id));
  if (!l) return;
  const images = allImages(l);
  const isShort = Prefs.isShortlisted(l.id);
  const isHid = Prefs.isHidden(l.id);
  const termLabel =
    l.term_type === "short" ? "Short-term" : l.term_type === "long" ? "Long-term" : null;
  const tenancyLabel =
    l.tenancy_type === "owner" ? "Direct from owner" : l.tenancy_type === "sublet" ? "Sublet" : null;

  const facts = [
    ["Rent / month", l.price != null ? fmtPrice(l.price) : null],
    ["Size", l.size_sqm ? `${l.size_sqm} m²` : null],
    ["Rooms", l.rooms || null],
    ["Available from", l.available_from || null],
    ["Term", termLabel],
    ["Tenancy", tenancyLabel],
    ["By metro", fmtTransit(l)],
    ["To uni", l.distance_uni_km != null ? `${Number(l.distance_uni_km).toFixed(1)} km` : null],
    ["District", l.district || null],
  ].filter(([, v]) => v != null && v !== "");

  const factsHtml = facts
    .map(
      ([k, v]) =>
        `<div class="fact"><span class="fact-k">${k}</span><span class="fact-v">${escapeHtml(String(v))}</span></div>`
    )
    .join("");

  const desc = (l.description || "")
    .split(/\n+/)
    .map((p) => p.trim())
    .filter((p) => p.length > 1)
    .slice(0, 16)
    .map((p) => `<p>${escapeHtml(p)}</p>`)
    .join("");

  $("#drawerContent").innerHTML = `
    ${carouselHtml(images, { idPrefix: `d-${l.id}` })}
    <div class="drawer-head">
      <h2>${escapeHtml(l.title || "")}</h2>
      <div class="drawer-sub">${escapeHtml([l.address, l.district].filter(Boolean).join(" · "))}</div>
      <div class="drawer-pricerow">
        <span class="drawer-price">${fmtPrice(l.price)}<small> / month</small></span>
        <span class="score-chip ${scoreClass(l.match_score || 0)}">match ${Math.round(l.match_score || 0)}</span>
      </div>
    </div>
    <div class="drawer-actions">
      <a class="btn primary" href="${escapeHtml(l.url)}" target="_blank" rel="noopener">Open listing ↗</a>
      <button type="button" class="btn${isShort ? " primary" : ""}" id="drawerShortlist">${isShort ? "★ Shortlisted" : "☆ Shortlist"}</button>
      <button type="button" class="btn ghost" id="drawerHide">${isHid ? "Unhide" : "Hide"}</button>
    </div>
    <div class="facts">${factsHtml}</div>
    ${desc ? `<div class="drawer-section"><div class="section-label">Description</div><div class="drawer-desc">${desc}</div></div>` : ""}
  `;
  $("#drawer").hidden = false;
  $("#drawerBackdrop").hidden = false;
  $("#drawerContent").querySelectorAll(".carousel").forEach(bindCarousel);

  $("#drawerShortlist").onclick = () => {
    Prefs.toggleShortlist(l.id);
    toast(Prefs.isShortlisted(l.id) ? "Shortlisted" : "Removed from shortlist");
    renderGallery();
    openDrawer(l.id);
  };
  $("#drawerHide").onclick = () => {
    Prefs.toggleHidden(l.id);
    toast(Prefs.isHidden(l.id) ? "Hidden" : "Unhidden");
    closeDrawer();
    renderGallery();
  };
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawerBackdrop").hidden = true;
}

function bindPills(rootSel, attr, onPick) {
  $$(`${rootSel} .pill`).forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(`${rootSel} .pill`).forEach((b) => b.classList.toggle("active", b === btn));
      onPick(btn.dataset[attr] ?? "");
      renderGallery();
    });
  });
}

function bindUi() {
  $("#btnFilters").addEventListener("click", () => {
    const panel = $("#filtersPanel");
    const open = panel.hidden;
    panel.hidden = !open;
    $("#btnFilters").setAttribute("aria-expanded", open ? "true" : "false");
  });

  $("#btnMap").addEventListener("click", () => {
    const sheet = $("#mapSheet");
    const open = sheet.hidden;
    sheet.hidden = !open;
    $("#btnMap").setAttribute("aria-pressed", open ? "true" : "false");
    if (open) {
      initMap();
      updateMap(state.filtered);
      setTimeout(() => state.map?.invalidateSize(), 100);
    }
  });
  $("#btnCloseMap").addEventListener("click", () => {
    $("#mapSheet").hidden = true;
    $("#btnMap").setAttribute("aria-pressed", "false");
  });

  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawerBackdrop").addEventListener("click", closeDrawer);

  bindPills("#photoPills", "photos", (v) => {
    state.photosMin = Number(v) || 0;
  });
  bindPills("#shortlistPills", "shortlist", (v) => {
    state.shortlistMode = v || "all";
  });
  bindPills("#termPills", "term", (v) => {
    state.term = v || "";
  });
  bindPills("#tenancyPills", "tenancy", (v) => {
    state.tenancy = v || "";
  });

  const syncDist = () => {
    const v = Number($("#fDistMax").value);
    $("#fDistMaxLabel").textContent = v >= 90 ? "Any" : `${v} min`;
  };
  $("#fDistMax").addEventListener("input", () => {
    syncDist();
    renderGallery();
  });
  syncDist();

  ["fQ", "fPriceMax", "fPriceMin", "fSort", "fSept", "fGone", "fShowHidden"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const evt = el.type === "search" || el.type === "number" ? "input" : "change";
    let t;
    el.addEventListener(evt, () => {
      clearTimeout(t);
      t = setTimeout(renderGallery, id === "fQ" ? 280 : 40);
    });
  });

  $("#gallery").addEventListener("click", (e) => {
    const shortBtn = e.target.closest("[data-shortlist]");
    if (shortBtn) {
      e.stopPropagation();
      const id = Number(shortBtn.dataset.shortlist);
      Prefs.toggleShortlist(id);
      toast(Prefs.isShortlisted(id) ? "Shortlisted" : "Removed");
      renderGallery();
      return;
    }
    const hideBtn = e.target.closest("[data-hide]");
    if (hideBtn) {
      e.stopPropagation();
      const id = Number(hideBtn.dataset.hide);
      Prefs.setHidden(id, true);
      toast("Hidden");
      renderGallery();
      return;
    }
    if (e.target.closest("[data-dir]")) return;
    const card = e.target.closest(".card");
    if (card) openDrawer(card.dataset.id);
  });

  $("#btnSettings").addEventListener("click", () => {
    const creds = Prefs.getCreds();
    $("#gistToken").value = creds.token;
    $("#gistId").value = creds.gistId;
    $("#settingsMsg").hidden = true;
    $("#settingsDialog").showModal();
  });

  $("#btnClearPrefs").addEventListener("click", () => {
    Prefs.clearCreds();
    $("#gistToken").value = "";
    $("#gistId").value = "";
    Prefs.setStatus("Local only");
    $("#settingsMsg").hidden = false;
    $("#settingsMsg").textContent = "Token cleared on this device.";
  });

  $("#settingsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitter = e.submitter;
    if (submitter?.value === "cancel") {
      $("#settingsDialog").close();
      return;
    }
    const msg = $("#settingsMsg");
    msg.hidden = false;
    msg.textContent = "Saving…";
    try {
      const result = await Prefs.saveSettings({
        token: $("#gistToken").value,
        gistId: $("#gistId").value,
      });
      $("#gistId").value = Prefs.getCreds().gistId;
      msg.textContent = result.localOnly
        ? "Saved as local-only."
        : `Synced. Gist ID: ${result.gistId}`;
      renderGallery();
    } catch (err) {
      msg.textContent = "Failed: " + (err.message || err);
    }
  });
}

async function main() {
  bindUi();
  Prefs.setStatus();
  try {
    const [listingsRes, configRes] = await Promise.all([
      fetch("data/listings.json"),
      fetch("data/config.json"),
    ]);
    if (!listingsRes.ok) throw new Error("Could not load listings.json");
    const payload = await listingsRes.json();
    state.config = configRes.ok ? await configRes.json() : { exported_at: payload.exported_at };
    state.listings = payload.listings || payload || [];
    await Prefs.pull();
    renderGallery();
  } catch (err) {
    $("#galleryMeta").textContent = "Failed to load data: " + err.message;
    console.error(err);
  }
}

main();
