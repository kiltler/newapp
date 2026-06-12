// Живые оповещения в браузере: тосты + звук + системные уведомления.
// Работает на всех страницах, наследующих base.html. Без интернета/Telegram.
(function () {
  let lastId = null;
  let audioCtx = null;

  function ensureAudio() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === "suspended") audioCtx.resume();
    } catch (e) {}
  }
  // Браузеры требуют жест пользователя для звука — ловим первый клик.
  document.addEventListener("click", ensureAudio, { once: true });

  function tone(freq, when, dur, gain) {
    if (!audioCtx) return;
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.type = "square"; o.frequency.value = freq; g.gain.value = gain || 0.18;
    o.start(audioCtx.currentTime + when);
    o.stop(audioCtx.currentTime + when + dur);
  }
  function beep(severity) {
    ensureAudio();
    if (severity === "critical") { tone(880, 0, 0.18); tone(620, 0.22, 0.3); }
    else { tone(760, 0, 0.18); }
  }

  function notifyOS(e) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const n = new Notification(e.severity === "critical" ? "🔴 Авария NVR" : "⚠️ Внимание", {
      body: e.message, tag: "nvr-" + e.id,
    });
    if (e.device_id) n.onclick = () => { window.focus(); location.href = "/devices/" + e.device_id; };
  }

  function toast(e) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const resolved = e.type.endsWith("_resolved");
    const cls = resolved ? "ok" : (e.severity === "critical" ? "err" : "warn");
    const el = document.createElement("div");
    el.className = "toast " + cls;
    el.innerHTML = `<span class="toast-x">✕</span>${(resolved ? "✅ " : "") + e.message}`;
    if (e.device_id) { el.style.cursor = "pointer"; el.onclick = (ev) => {
      if (ev.target.classList.contains("toast-x")) { el.remove(); return; }
      location.href = "/devices/" + e.device_id;
    }; }
    el.querySelector(".toast-x").onclick = () => el.remove();
    box.appendChild(el);
    setTimeout(() => el.remove(), resolved ? 8000 : 20000);
  }

  async function poll() {
    try {
      const r = await fetch("/api/alerts/recent?after_id=" + (lastId ?? 0));
      if (!r.ok) return;
      const items = await r.json();
      if (lastId === null) { // первый заход — только запоминаем планку, без спама
        lastId = items.length ? items[items.length - 1].id : 0;
        return;
      }
      for (const e of items) {
        lastId = Math.max(lastId, e.id);
        toast(e);
        if (!e.type.endsWith("_resolved")) { beep(e.severity); notifyOS(e); }
      }
    } catch (e) {}
  }

  // Колокольчик в шапке — включить системные уведомления Windows
  document.addEventListener("DOMContentLoaded", () => {
    const bell = document.getElementById("bell");
    if (bell && "Notification" in window) {
      const sync = () => { bell.textContent = Notification.permission === "granted" ? "🔔" : "🔕"; };
      sync();
      bell.onclick = (e) => { e.preventDefault(); Notification.requestPermission().then(sync); ensureAudio(); };
    }
  });

  poll();
  setInterval(poll, 8000);
})();
