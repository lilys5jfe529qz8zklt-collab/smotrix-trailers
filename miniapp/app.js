const tg = window.Telegram?.WebApp;
try { tg?.ready(); tg?.expand(); } catch (_) {}
const initData = tg?.initData || "";
const adminToken = new URLSearchParams(location.search).get("token") || "";

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[ch]));

let quality = 1080;
let timer = null;

function headers(extra = {}) {
  const out = { ...extra };
  if (initData) out["X-Init-Data"] = initData;
  if (adminToken) out["X-Admin-Token"] = adminToken;
  return out;
}

async function api(path, options = {}) {
  const opts = { ...options, headers: headers(options.headers || {}) };
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) throw new Error(payload.error || `Ошибка ${response.status}`);
  return payload;
}

function toast(text) {
  const box = $("toast");
  box.textContent = text;
  box.hidden = false;
  clearTimeout(box._timer);
  box._timer = setTimeout(() => { box.hidden = true; }, 2800);
}

const STATUS = {
  done: "готово", pending: "в очереди", downloading: "качается",
  uploading: "заливается", failed: "ошибка", skipped: "нет в хранилище",
};

function fmtDate(ts) {
  if (!ts) return "";
  return new Date(Number(ts) * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "short" });
}

function renderQuality() {
  $("qualityNow").textContent = `${quality}p`;
  document.querySelectorAll("#qualityChips button").forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.q) === Number(quality));
  });
}

function renderStats(stats) {
  const items = [
    [stats.done || 0, "готово"],
    [(stats.pending || 0) + (stats.downloading || 0) + (stats.uploading || 0), "в работе"],
    [stats.failed || 0, "ошибок"],
  ];
  $("stats").innerHTML = items.map(([value, label]) => `<div class="stat"><b>${value}</b><span>${label}</span></div>`).join("");
}

function renderChannels(channels) {
  $("channelCount").textContent = channels.length;
  $("channels").innerHTML = channels.length
    ? channels.map((row) => `<div class="channel"><b>${esc(row.title || row.url)}</b><span>${esc(row.channel_id || "")}</span></div>`).join("")
    : `<div class="note">Пока один канал из настроек.</div>`;
}

function renderList(items) {
  if (!items.length) {
    $("list").innerHTML = `<div class="empty">Пока пусто</div>`;
    return;
  }
  $("list").innerHTML = items.map((row) => {
    const status = row.status || "pending";
    const retry = ["failed", "skipped"].includes(status)
      ? `<div class="item-actions"><button data-retry="${esc(row.youtube_id)}">Повторить</button></div>` : "";
    return `<div class="item">
      <div class="item-top">
        <div>
          <div class="item-title">${esc(row.title || "Без названия")}${row.year ? ` · ${row.year}` : ""}</div>
          <div class="item-sub">${esc(row.youtube_title || row.youtube_id)}</div>
        </div>
        <span class="badge ${esc(status)}">${esc(STATUS[status] || status)}</span>
      </div>
      <div class="item-sub">${Number(row.quality) || 0}p · ${fmtDate(row.published_at)}</div>
      ${row.error ? `<div class="item-error">${esc(row.error)}</div>` : ""}
      ${retry}
    </div>`;
  }).join("");
}

async function loadState() {
  try {
    const data = await api("/api/admin/state");
    quality = Number(data.quality) || 1080;
    renderQuality();
    renderStats(data.stats || {});
    renderChannels(data.channels || []);
    renderList(data.trailers || []);
    const current = data.current;
    $("current").textContent = current
      ? `${current.status || ""} · ${current.title || current.youtube_id || ""}`.trim()
      : "";
  } catch (error) {
    if (String(error.message).includes("forbidden")) {
      $("list").innerHTML = `<div class="empty">Открой через Telegram: доступ только админу.</div>`;
    } else {
      toast(error.message);
    }
  }
}

document.querySelectorAll("#qualityChips button").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      const data = await api("/api/admin/quality", { method: "POST", body: { value: Number(button.dataset.q) } });
      quality = Number(data.quality);
      renderQuality();
      toast(`По умолчанию ${quality}p`);
    } catch (error) { toast(error.message); }
  });
});

$("addLinks").addEventListener("click", async () => {
  const raw = $("links").value.trim();
  if (!raw) return;
  $("addLinks").disabled = true;
  $("addNote").textContent = "Проверяю ссылки…";
  try {
    const data = await api("/api/admin/link", { method: "POST", body: { urls: raw, quality } });
    const results = data.results || [];
    const ok = results.filter((row) => row.ok).length;
    const bad = results.filter((row) => !row.ok);
    $("addNote").textContent = `В очереди: ${ok}${bad.length ? ` · не взято: ${bad.length}` : ""}`;
    if (bad.length) toast(bad[0].error || "Не удалось");
    $("links").value = bad.map((row) => row.url || "").filter(Boolean).join("\n");
    await loadState();
  } catch (error) {
    $("addNote").textContent = "";
    toast(error.message);
  } finally {
    $("addLinks").disabled = false;
  }
});

$("poll").addEventListener("click", async () => {
  try {
    await api("/api/admin/poll", { method: "POST", body: {} });
    toast("Проверяю каналы");
    setTimeout(loadState, 1500);
  } catch (error) { toast(error.message); }
});

$("addChannel").addEventListener("click", async () => {
  const url = $("channelUrl").value.trim();
  if (!url) return;
  try {
    const data = await api("/api/admin/channel", { method: "POST", body: { url } });
    $("channelUrl").value = "";
    toast(`Канал: ${data.title || data.channel_id}`);
    await loadState();
  } catch (error) { toast(error.message); }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-retry]");
  if (!button) return;
  try {
    await api("/api/admin/retry", { method: "POST", body: { youtube_id: button.dataset.retry } });
    toast("Повторю");
    await loadState();
  } catch (error) { toast(error.message); }
});

loadState();
timer = setInterval(loadState, 6000);
window.addEventListener("beforeunload", () => clearInterval(timer));
