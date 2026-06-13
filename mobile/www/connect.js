/*
 * Лаунчер нативной оболочки NVR Monitor.
 *
 * Поведение:
 *  1. При обычном запуске берём сохранённый адрес (а если его нет — зашитый
 *     адрес по умолчанию) и СРАЗУ открываем панель. Никакого ввода вручную —
 *     приложение ведёт себя как нативное.
 *  2. Экран настроек (форма смены адреса) показывается только когда:
 *      - пользователь вернулся сюда кнопкой «Назад» с панели, ИЛИ
 *      - сервер недоступен.
 *  3. Адрес можно сменить и сбросить на адрес по умолчанию.
 *
 * Работает и внутри Capacitor (плагин Preferences), и в обычном браузере
 * (localStorage) — чтобы экран можно было отлаживать без сборки apk.
 */
(function () {
  "use strict";

  // Зашитый адрес по умолчанию. Сменить — здесь и в README.
  var DEFAULT_SERVER = "http://192.168.11.231:8000";

  var KEY = "nvrmon.serverUrl";
  var PING_TIMEOUT_MS = 6000;

  var form = document.getElementById("form");
  var input = document.getElementById("url");
  var btn = document.getElementById("connect");
  var resetBtn = document.getElementById("reset");
  var status = document.getElementById("status");
  var subtitle = document.getElementById("subtitle");

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
    if (!/^https?:\/\//i.test(v)) v = "http://" + v;
    return v.replace(/\/+$/, "");
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
    // нажмёт «Назад» и вернётся — покажем настройки, а не зациклим редирект.
    try { sessionStorage.setItem("nvrmon.navigated", "1"); } catch (e) {}
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
    subtitle.textContent = "Сервер недоступен";
    setStatus(
      "Не удалось подключиться. Проверьте, что включён VPN (WireGuard) и что " +
      "сервер работает, либо укажите другой адрес.",
      "err"
    );
    if (opts.revealForm) input.focus();
    return false;
  }

  function showSettings(prefill, note) {
    subtitle.textContent = "Настройки подключения";
    if (prefill) input.value = prefill;
    if (note) setStatus(note);
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

  resetBtn.addEventListener("click", async function () {
    await clearUrl();
    input.value = DEFAULT_SERVER;
    setStatus("Адрес сброшен на стандартный.", "ok");
    input.focus();
  });

  // ── Старт ──────────────────────────────────────────────────────────────────
  (async function init() {
    var saved = await loadUrl();
    var target = saved || DEFAULT_SERVER;
    input.value = target;

    if (cameBack()) {
      // Вернулись кнопкой «Назад» с панели — это и есть «экран настроек».
      showSettings(target, "Можно сменить адрес сервера или вернуться на панель кнопкой «Подключиться».");
    } else {
      // Обычный запуск — сразу в панель.
      await tryConnect(target, { revealForm: true });
    }
  })();
})();
