/*
 * Экран подключения нативной оболочки NVR Monitor.
 *
 * Логика:
 *  1. При запуске читаем сохранённый адрес сервера.
 *  2. Если он есть — молча пингуем /healthz и, если сервер жив, грузим панель в webview.
 *  3. Если адреса нет или сервер недоступен — показываем форму.
 *
 * Работает и внутри Capacitor (плагин Preferences), и в обычном браузере
 * (localStorage) — чтобы экран можно было отлаживать без сборки apk.
 */
(function () {
  "use strict";

  var KEY = "nvrmon.serverUrl";
  var PING_TIMEOUT_MS = 6000;

  var form = document.getElementById("form");
  var input = document.getElementById("url");
  var btn = document.getElementById("connect");
  var status = document.getElementById("status");
  var forgetBtn = document.getElementById("forget");

  // ── Хранилище: Capacitor Preferences, иначе localStorage ──────────────────
  function prefs() {
    return (window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.Preferences) || null;
  }
  async function loadUrl() {
    var p = prefs();
    if (p) { var r = await p.get({ key: KEY }); return r && r.value; }
    return localStorage.getItem(KEY);
  }
  async function saveUrl(v) {
    var p = prefs();
    if (p) return p.set({ key: KEY, value: v });
    localStorage.setItem(KEY, v);
  }
  async function clearUrl() {
    var p = prefs();
    if (p) return p.remove({ key: KEY });
    localStorage.removeItem(KEY);
  }

  // ── Нормализация введённого адреса ────────────────────────────────────────
  function normalize(raw) {
    var v = (raw || "").trim();
    if (!v) return "";
    if (!/^https?:\/\//i.test(v)) v = "http://" + v; // по умолчанию http (Tailscale без TLS)
    v = v.replace(/\/+$/, "");                        // убрать хвостовой слэш
    return v;
  }

  // ── Проверка доступности сервера: /healthz (публичный, без авторизации) ───
  // Ответ кросс-доменный без CORS-заголовков → читать тело нельзя, но факт
  // успешного ответа (даже opaque) означает, что сервер на связи.
  function ping(base) {
    var ctrl = new AbortController();
    var t = setTimeout(function () { ctrl.abort(); }, PING_TIMEOUT_MS);
    return fetch(base + "/healthz", { mode: "no-cors", cache: "no-store", signal: ctrl.signal })
      .then(function () { clearTimeout(t); return true; })
      .catch(function () { clearTimeout(t); return false; });
  }

  function setStatus(msg, kind, busy) {
    status.className = "status" + (kind ? " " + kind : "");
    status.innerHTML = (busy ? '<span class="spinner"></span>' : "") + (msg || "");
  }

  function go(base) {
    // Помечаем, что в этой сессии уже уходили на панель: если пользователь
    // нажмёт «назад» и вернётся сюда — покажем форму, а не зациклим редирект.
    try { sessionStorage.setItem("nvrmon.navigated", "1"); } catch (e) {}
    // Уходим из локального бандла оболочки в саму панель на сервере.
    window.location.href = base + "/";
  }

  function cameBack() {
    try { return sessionStorage.getItem("nvrmon.navigated") === "1"; } catch (e) { return false; }
  }

  async function tryConnect(base, opts) {
    opts = opts || {};
    btn.disabled = true;
    setStatus("Проверяю связь с сервером…", "busy", true);
    var ok = await ping(base);
    btn.disabled = false;
    if (ok) {
      await saveUrl(base);
      setStatus("Сервер на связи, открываю панель…", "ok");
      go(base);
      return true;
    }
    setStatus(
      "Сервер недоступен. Проверьте, что Tailscale включён на телефоне " +
      "и адрес введён верно.",
      "err"
    );
    if (opts.revealForm) showForm(base);
    return false;
  }

  function showForm(prefill) {
    if (prefill) input.value = prefill;
    forgetBtn.classList.toggle("hidden", !prefill);
    input.focus();
  }

  // ── События ───────────────────────────────────────────────────────────────
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var base = normalize(input.value);
    if (!base) { setStatus("Введите адрес сервера", "err"); return; }
    input.value = base;
    tryConnect(base);
  });

  forgetBtn.addEventListener("click", async function () {
    await clearUrl();
    input.value = "";
    forgetBtn.classList.add("hidden");
    setStatus("");
    input.focus();
  });

  // ── Старт: автоподключение по сохранённому адресу ─────────────────────────
  (async function init() {
    var saved = await loadUrl();
    if (saved && cameBack()) {
      // Вернулись кнопкой «назад» с панели — даём сменить сервер, не редиректим.
      setStatus("Подключение разорвано. Можно сменить сервер или подключиться снова.");
      showForm(saved);
    } else if (saved) {
      input.value = saved;
      forgetBtn.classList.remove("hidden");
      await tryConnect(saved, { revealForm: true });
    } else {
      showForm("");
    }
  })();
})();
