/* Static Augsburg Flats gallery — reads data/*.json, prefs via Prefs (Gist) */

const state = {
  listings: [],
  config: null,
  filtered: [],
  map: null,
  markers: null,
  mapReady: false,
  focusOverlay: null,
  photosMin: 0,
  shortlistMode: "all",
  term: "",
  tenancy: "",
  /** Empty set = all sources. Otherwise only selected source keys. */
  selectedSources: new Set(),
};

const SOURCE_LABELS = {
  kleinanzeigen: "Kleinanzeigen",
  // Immonet search is hosted on Immowelt — same portal, same URLs.
  immonet: "Immowelt",
  wg_gesucht: "WG-Gesucht",
  immosurf: "Immosurf",
  immowelt: "Immowelt",
  hc24: "HC24",
  immobilienscout24: "ImmoScout24",
  wohnungsboerse: "Wohnungsbörse",
  studentenwerk: "Studentenwerk",
  manual: "Manual",
};

/** Filter dropdown groups: Immowelt + Immonet share one checkbox. */
const SOURCE_FILTER_GROUPS = [
  { key: "immowelt", label: "Immowelt", sources: ["immowelt", "immonet"] },
  { key: "kleinanzeigen", label: "Kleinanzeigen", sources: ["kleinanzeigen"] },
  { key: "wg_gesucht", label: "WG-Gesucht", sources: ["wg_gesucht"] },
  { key: "immosurf", label: "Immosurf", sources: ["immosurf"] },
  { key: "hc24", label: "HC24", sources: ["hc24"] },
  { key: "immobilienscout24", label: "ImmoScout24", sources: ["immobilienscout24"] },
  { key: "wohnungsboerse", label: "Wohnungsbörse", sources: ["wohnungsboerse"] },
  { key: "studentenwerk", label: "Studentenwerk", sources: ["studentenwerk"] },
  { key: "manual", label: "Manual", sources: ["manual"] },
];

const SOURCE_STORAGE_KEY = "augsburg_flats_sources";

function sourceGroupFor(source) {
  const raw = String(source || "").trim();
  return SOURCE_FILTER_GROUPS.find((g) => g.sources.includes(raw)) || null;
}

function sourceLabel(key) {
  const group = SOURCE_FILTER_GROUPS.find((g) => g.key === key);
  if (group) return group.label;
  return SOURCE_LABELS[key] || String(key || "Other").replace(/_/g, " ");
}

function listingSourceTag(l) {
  const group = sourceGroupFor(l.source);
  if (group) return group.label;
  return sourceLabel(l.source);
}

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
  const sourceTag = listingSourceTag(l);

  const tags = [`<span class="tag">${escapeHtml(sourceTag)}</span>`];
  if (isShort) tags.push(`<span class="tag cat-shortlist">shortlist</span>`);
  if (l.furnished) tags.push(`<span class="tag">furnished</span>`);
  if (l.rooms) tags.push(`<span class="tag">${l.rooms} room</span>`);
  if (l.size_sqm) tags.push(`<span class="tag">${l.size_sqm} m²</span>`);

  const mapIcon = `
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z"/>
      <circle cx="12" cy="10" r="3"/>
    </svg>`;

  return `
    <article class="card${l.status === "gone" ? " gone" : ""}${isShort ? " shortlisted" : ""}" data-id="${l.id}">
      <div class="card-media">
        ${carouselHtml(images, { idPrefix: String(l.id) })}
        ${iconLabelsHtml(l)}
        <div class="price-fab">${fmtPrice(l.price)} <span>/ mo</span></div>
        <button type="button" class="hide-fab" data-hide="${l.id}" title="Hide" aria-label="Hide">✕</button>
        <button type="button" class="shortlist-fab${isShort ? " on" : ""}" data-shortlist="${l.id}" title="Shortlist" aria-label="Shortlist">★</button>
        <button type="button" class="map-fab map-jump" data-map-id="${l.id}" ${l.lat == null || l.lon == null ? "disabled" : ""} title="Show on map" aria-label="Show on map">${mapIcon}</button>
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

  if (state.selectedSources.size > 0) {
    items = items.filter((l) => {
      const group = sourceGroupFor(l.source);
      const key = group ? group.key : String(l.source || "");
      return state.selectedSources.has(key);
    });
  }

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

function clearFocusOverlay() {
  if (state.focusOverlay && state.map) {
    state.map.removeLayer(state.focusOverlay);
  }
  state.focusOverlay = null;
}

function ensureMapVisible() {
  if (document.body.classList.contains("map-hidden")) {
    document.body.classList.remove("map-hidden");
    $("#btnMap")?.setAttribute("aria-pressed", "true");
    initMap();
    updateMap(state.filtered);
    resizeMapSoon();
  }
}

function focusListingOnMap(listing) {
  ensureMapVisible();
  if (!state.map || listing?.lat == null || listing?.lon == null) {
    toast("No map location for this listing");
    return;
  }
  const uni = state.config?.university;
  if (!uni?.lat || !uni?.lon) {
    state.map.flyTo([listing.lat, listing.lon], 15, { duration: 0.85 });
    return;
  }

  clearFocusOverlay();

  const listingLatLng = L.latLng(listing.lat, listing.lon);
  const uniLatLng = L.latLng(uni.lat, uni.lon);
  const distKm =
    listing.distance_uni_km != null
      ? Number(listing.distance_uni_km)
      : uniLatLng.distanceTo(listingLatLng) / 1000;
  const distLabel =
    distKm < 1 ? `${Math.round(distKm * 1000)} m` : `${distKm.toFixed(1)} km`;

  const group = L.layerGroup();
  group.addLayer(
    L.polyline([uniLatLng, listingLatLng], {
      color: "#1f6f5b",
      weight: 3,
      opacity: 0.85,
      dashArray: "7 7",
    })
  );

  const mid = L.latLng((uni.lat + listing.lat) / 2, (uni.lon + listing.lon) / 2);
  group.addLayer(
    L.marker(mid, {
      interactive: false,
      icon: L.divIcon({
        className: "dist-label-wrap",
        html: `<div class="dist-label">${escapeHtml(distLabel)}</div>`,
        iconSize: [0, 0],
        iconAnchor: [0, 0],
      }),
    })
  );

  group.addLayer(
    L.circleMarker(listingLatLng, {
      radius: 13,
      color: "#1f6f5b",
      weight: 3,
      fillColor: "#ffffff",
      fillOpacity: 0.95,
    })
  );
  group.addTo(state.map);
  state.focusOverlay = group;

  state.map.flyToBounds(L.latLngBounds([uniLatLng, listingLatLng]), {
    padding: [56, 56],
    maxZoom: 15,
    duration: 0.55,
    easeLinearity: 0.35,
  });

  state.markers?.eachLayer((layer) => {
    if (layer._listingId === listing.id) layer.openTooltip?.();
  });

  document.querySelector(".map-panel")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function tooltipHtml(l) {
  const images = allImages(l);
  const sc = scoreClass(l.match_score || 0);
  const dist = fmtDistPrimary(l);
  const metaBits = [l.district || l.address, dist].filter(Boolean).join(" · ");
  const tags = [];
  if (l.furnished) tags.push("furnished");
  if (l.balcony) tags.push("balcony");
  if (l.rooms) tags.push(`${l.rooms} rm`);
  if (l.size_sqm) tags.push(`${l.size_sqm} m²`);
  if (l.term_type === "short") tags.push("short-term");
  else if (l.term_type === "long") tags.push("long-term");
  if (l.tenancy_type === "owner") tags.push("owner");
  else if (l.tenancy_type === "sublet") tags.push("sublet");
  if (Prefs.isShortlisted(l.id)) tags.unshift("shortlist");

  let sourceTag = listingSourceTag(l);

  const hero = images[0]
    ? `<div class="tip-hero"><img src="${escapeHtml(images[0])}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" />
         ${images.length > 1 ? `<span class="tip-photos">${images.length} photos</span>` : ""}
         <span class="tip-price-fab">${fmtPrice(l.price)}</span>
         <span class="tip-score ${sc}">${Math.round(l.match_score || 0)}</span>
       </div>`
    : `<div class="tip-hero tip-empty"><span class="tip-price-fab">${fmtPrice(l.price)}</span>No photo</div>`;

  return `
    <div class="tip-card">
      ${hero}
      <div class="tip-body">
        <div class="tip-title">${escapeHtml(l.title || "Apartment")}</div>
        <div class="tip-meta">${escapeHtml(metaBits || "Augsburg")}</div>
        <div class="tip-tags">
          <span class="tip-tag">${escapeHtml(sourceTag)}</span>
          ${tags
            .slice(0, 4)
            .map((t) => `<span class="tip-tag${t === "shortlist" ? " cat-shortlist" : ""}">${escapeHtml(t)}</span>`)
            .join("")}
        </div>
      </div>
    </div>`;
}

function initMap() {
  if (state.mapReady || typeof L === "undefined") return;
  const uni = state.config?.university || { lat: 48.3345, lon: 10.8974, name: "University of Augsburg" };
  state.map = L.map("map", { zoomControl: true }).setView([uni.lat, uni.lon], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 19,
  }).addTo(state.map);
  state.markers = L.layerGroup().addTo(state.map);
  L.circleMarker([uni.lat, uni.lon], {
    radius: 9,
    color: "#1f6f5b",
    fillColor: "#1f6f5b",
    fillOpacity: 0.9,
    weight: 2,
  })
    .bindPopup(`<strong>${escapeHtml(uni.name || "University")}</strong>`)
    .addTo(state.map);
  state.mapReady = true;
}

function updateMap(listings) {
  if (!state.mapReady || !state.markers) return;
  clearFocusOverlay();
  state.markers.clearLayers();
  listings.forEach((l) => {
    if (l.lat == null || l.lon == null) return;
    const approx = l.geo_precision && l.geo_precision !== "exact";
    const marker = L.circleMarker([l.lat, l.lon], {
      radius: approx ? 7 : 8,
      color: approx ? "#8b8b8b" : "#ffffff",
      weight: 2,
      fillColor: pinColor(l),
      fillOpacity: approx ? 0.8 : 0.95,
      dashArray: approx ? "2 2" : null,
    });
    marker._listingId = l.id;
    marker.bindTooltip(tooltipHtml(l), {
      direction: "top",
      offset: [0, -10],
      opacity: 1,
      sticky: false,
      className: "listing-tip",
    });
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
      <button type="button" class="btn" id="drawerMap" ${l.lat == null || l.lon == null ? "disabled" : ""}>Show on map</button>
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
  $("#drawerMap").onclick = () => {
    closeDrawer();
    focusListingOnMap(l);
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

function availableSources() {
  const counts = new Map();
  for (const l of state.listings) {
    if (l.status === "gone") continue;
    const group = sourceGroupFor(l.source);
    const key = group ? group.key : String(l.source || "").trim();
    if (!key) continue;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return SOURCE_FILTER_GROUPS.filter((g) => counts.has(g.key))
    .map((g) => [g.key, counts.get(g.key)])
    .concat(
      [...counts.entries()]
        .filter(([k]) => !SOURCE_FILTER_GROUPS.some((g) => g.key === k))
        .sort((a, b) => sourceLabel(a[0]).localeCompare(sourceLabel(b[0])))
    );
}

function persistSelectedSources() {
  try {
    localStorage.setItem(SOURCE_STORAGE_KEY, JSON.stringify([...state.selectedSources]));
  } catch (_) {}
}

function normalizeSourceSelection(keys) {
  const out = new Set();
  for (const raw of keys) {
    const key = String(raw || "").trim();
    if (!key) continue;
    // Migrate old separate "immonet" selections into Immowelt group.
    if (key === "immonet") {
      out.add("immowelt");
      continue;
    }
    const group = sourceGroupFor(key);
    out.add(group ? group.key : key);
  }
  return out;
}

function loadSelectedSources() {
  try {
    const raw = localStorage.getItem(SOURCE_STORAGE_KEY);
    if (!raw) return;
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) {
      state.selectedSources = normalizeSourceSelection(arr);
    }
  } catch (_) {}
}

function sourceSummaryText() {
  if (state.selectedSources.size === 0) return "All";
  const labels = [...state.selectedSources].map(sourceLabel);
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return labels.join(" · ");
  return `${labels.length} selected`;
}

function syncSourceDropdown() {
  const allMode = state.selectedSources.size === 0;
  const allCb = $("#sourceAll");
  if (allCb) allCb.checked = allMode;
  $$("#sourceDdOptions input[type=checkbox]").forEach((cb) => {
    cb.checked = !allMode && state.selectedSources.has(cb.value);
  });
  const summary = $("#sourceDdSummary");
  if (summary) summary.textContent = sourceSummaryText();
  const btn = $("#sourceDdBtn");
  if (btn) btn.classList.toggle("has-filter", !allMode);
}

function setSourceDropdownOpen(open) {
  const panel = $("#sourceDdPanel");
  const btn = $("#sourceDdBtn");
  if (!panel || !btn) return;
  panel.hidden = !open;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
}

function buildSourceDropdown() {
  const root = $("#sourceDdOptions");
  if (!root) return;
  const sources = availableSources();
  const known = new Set(sources.map(([k]) => k));
  for (const key of [...state.selectedSources]) {
    if (!known.has(key)) state.selectedSources.delete(key);
  }
  root.innerHTML = sources
    .map(
      ([key, n]) => `
      <label class="filter-dd-option">
        <input type="checkbox" value="${escapeHtml(key)}" />
        <span>${escapeHtml(sourceLabel(key))}</span>
        <span class="opt-count">${n}</span>
      </label>`
    )
    .join("");

  const applyFromChecks = () => {
    const checked = $$("#sourceDdOptions input[type=checkbox]:checked").map((cb) => cb.value);
    if (!checked.length || checked.length >= known.size) {
      state.selectedSources.clear();
    } else {
      state.selectedSources = normalizeSourceSelection(checked);
    }
    persistSelectedSources();
    syncSourceDropdown();
    renderGallery();
  };

  $("#sourceAll")?.addEventListener("change", () => {
    state.selectedSources.clear();
    persistSelectedSources();
    syncSourceDropdown();
    renderGallery();
  });

  root.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", applyFromChecks);
  });

  const btn = $("#sourceDdBtn");
  btn?.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = btn.getAttribute("aria-expanded") !== "true";
    setSourceDropdownOpen(open);
  });

  $("#sourceDdPanel")?.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => setSourceDropdownOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setSourceDropdownOpen(false);
  });

  syncSourceDropdown();
}

function resizeMapSoon() {
  setTimeout(() => state.map?.invalidateSize(), 120);
  setTimeout(() => state.map?.invalidateSize(), 320);
}

function bindUi() {
  const setFiltersCollapsed = (collapsed) => {
    document.body.classList.toggle("filters-collapsed", collapsed);
    const btn = $("#btnFoldControls");
    if (!btn) return;
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    btn.title = collapsed ? "Show filters" : "Hide filters";
    try {
      localStorage.setItem("augsburg_flats_filters_collapsed", collapsed ? "1" : "0");
    } catch (_) {}
    resizeMapSoon();
  };

  $("#btnFoldControls")?.addEventListener("click", () => {
    setFiltersCollapsed(!document.body.classList.contains("filters-collapsed"));
  });
  try {
    if (localStorage.getItem("augsburg_flats_filters_collapsed") === "1") {
      setFiltersCollapsed(true);
    }
  } catch (_) {}

  $("#btnMap")?.addEventListener("click", () => {
    const hidden = document.body.classList.toggle("map-hidden");
    $("#btnMap")?.setAttribute("aria-pressed", hidden ? "false" : "true");
    if (!hidden) {
      initMap();
      updateMap(state.filtered);
    }
    resizeMapSoon();
  });

  $$(".grid-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".grid-btn").forEach((b) => b.classList.toggle("active", b === btn));
      const cols = btn.dataset.cols || "2";
      $("#gallery")?.classList.toggle("cols-2", cols === "2");
      $("#gallery")?.classList.toggle("cols-3", cols === "3");
    });
  });

  $("#drawerClose")?.addEventListener("click", closeDrawer);
  $("#drawerBackdrop")?.addEventListener("click", closeDrawer);

  const syncSelectFilters = () => {
    state.photosMin = Number($("#fPhotos")?.value) || 0;
    state.shortlistMode = $("#fShortlist")?.value || "all";
    state.term = $("#fTerm")?.value || "";
    state.tenancy = $("#fTenancy")?.value || "";
  };
  ["fPhotos", "fShortlist", "fTerm", "fTenancy"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", () => {
      syncSelectFilters();
      renderGallery();
    });
  });
  syncSelectFilters();

  const syncDist = () => {
    const el = $("#fDistMax");
    if (!el) return;
    const v = Number(el.value);
    const label = $("#fDistMaxLabel");
    if (label) label.textContent = v >= 90 ? "Any" : `${v} min`;
  };
  $("#fDistMax")?.addEventListener("input", () => {
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

  window.addEventListener("resize", resizeMapSoon);

  $("#gallery")?.addEventListener("click", (e) => {
    const mapBtn = e.target.closest(".map-jump");
    if (mapBtn) {
      e.stopPropagation();
      if (mapBtn.disabled) {
        toast("No map location for this listing");
        return;
      }
      const id = Number(mapBtn.dataset.mapId);
      const listing = state.listings.find((x) => x.id === id);
      if (listing) focusListingOnMap(listing);
      return;
    }
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

  $("#btnSettings")?.addEventListener("click", () => {
    const creds = Prefs.getCreds();
    const token = $("#gistToken");
    const gist = $("#gistId");
    const msg = $("#settingsMsg");
    if (token) token.value = creds.token;
    if (gist) gist.value = creds.gistId;
    if (msg) msg.hidden = true;
    $("#settingsDialog")?.showModal();
  });

  $("#btnClearPrefs")?.addEventListener("click", () => {
    Prefs.clearCreds();
    if ($("#gistToken")) $("#gistToken").value = "";
    if ($("#gistId")) $("#gistId").value = "";
    Prefs.setStatus("Local only");
    const msg = $("#settingsMsg");
    if (msg) {
      msg.hidden = false;
      msg.textContent = "Token cleared on this device.";
    }
  });

  $("#settingsForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitter = e.submitter;
    if (submitter?.value === "cancel") {
      $("#settingsDialog")?.close();
      return;
    }
    const msg = $("#settingsMsg");
    if (msg) {
      msg.hidden = false;
      msg.textContent = "Saving…";
    }
    try {
      const result = await Prefs.saveSettings({
        token: $("#gistToken")?.value || "",
        gistId: $("#gistId")?.value || "",
      });
      if ($("#gistId")) $("#gistId").value = Prefs.getCreds().gistId;
      if (msg) {
        msg.textContent = result.localOnly
          ? "Saved as local-only."
          : `Synced. Gist ID: ${result.gistId}`;
      }
      renderGallery();
    } catch (err) {
      if (msg) msg.textContent = "Failed: " + (err.message || err);
    }
  });
}

async function main() {
  try {
    bindUi();
  } catch (err) {
    console.error("bindUi failed", err);
  }
  Prefs.setStatus();
  try {
    const [listingsRes, configRes] = await Promise.all([
      fetch("data/listings.json", { cache: "no-store" }),
      fetch("data/config.json", { cache: "no-store" }),
    ]);
    if (!listingsRes.ok) throw new Error("Could not load listings.json");
    const payload = await listingsRes.json();
    state.config = configRes.ok ? await configRes.json() : { exported_at: payload.exported_at };
    state.listings = payload.listings || payload || [];
    loadSelectedSources();
    buildSourceDropdown();
    await Prefs.pull();
    initMap();
    renderGallery();
    resizeMapSoon();
  } catch (err) {
    const meta = $("#galleryMeta");
    if (meta) meta.textContent = "Failed to load data: " + err.message;
    console.error(err);
  }
}

main();
