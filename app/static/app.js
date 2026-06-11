// Минимальный клиент дашборда: добавление/тест/удаление/опрос устройств.
const $ = (sel) => document.querySelector(sel);

function formData() {
  const f = $("#device-form");
  return {
    name: f.name.value,
    address: f.address.value || null,
    host: f.host.value,
    http_port: parseInt(f.http_port.value || "80", 10),
    username: f.username.value,
    password: f.password.value,
    api_type: f.api_type.value,
    timeout: parseFloat(f.timeout.value || "15"),
  };
}

document.addEventListener("DOMContentLoaded", () => {
  const modal = $("#modal");
  const openBtn = $("#add-device-btn");
  if (openBtn) openBtn.onclick = (e) => { e.preventDefault(); modal.classList.remove("hidden"); };
  const cancel = $("#cancel-btn");
  if (cancel) cancel.onclick = () => modal.classList.add("hidden");

  const testBtn = $("#test-btn");
  if (testBtn) testBtn.onclick = async () => {
    const out = $("#test-result");
    out.textContent = "Проверка соединения…";
    const d = formData();
    const resp = await fetch("/api/devices/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(d),
    });
    const r = await resp.json();
    out.textContent = (r.ok ? "✅ OK\n" : "❌ Ошибка\n") +
      `тип: ${r.api_type} (${r.auth_scheme})\n` +
      (r.model ? `модель: ${r.model}\nпрошивка: ${r.firmware || "-"}\n` : "") +
      `возможности: ${JSON.stringify(r.capabilities)}\n${r.detail}`;
    if (r.ok && r.api_type) $("#device-form").api_type.value = r.api_type;
  };

  const form = $("#device-form");
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    const resp = await fetch("/api/devices", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formData()),
    });
    if (resp.ok) location.reload();
    else $("#test-result").textContent = "Ошибка сохранения: " + (await resp.text());
  };
});

async function pollNow(id) {
  await fetch(`/api/devices/${id}/poll`, { method: "POST" });
  location.reload();
}
async function recheck(id) {
  const r = await fetch(`/api/devices/${id}/recheck`, { method: "POST" });
  if (r.ok) location.reload();
  else alert("Не удалось перепроверить: " + (await r.text()));
}
async function syncTime(id) {
  const r = await fetch(`/api/devices/${id}/sync-time`, { method: "POST" });
  if (r.ok) { alert("Время синхронизировано"); location.reload(); }
  else alert("Ошибка: " + (await r.text()));
}
async function rebootDevice(id) {
  if (!confirm("Перезагрузить регистратор? Запись и просмотр прервутся на пару минут.")) return;
  const r = await fetch(`/api/devices/${id}/reboot`, { method: "POST" });
  alert(r.ok ? "Команда перезагрузки отправлена" : "Ошибка: " + (await r.text()));
}
function showSnap(deviceId, channelId, btn) {
  const cell = btn.parentElement;
  const url = `/api/devices/${deviceId}/channels/${channelId}/snapshot?t=` + Date.now();
  const img = document.createElement("img");
  img.className = "snap";
  img.src = url;
  img.onerror = () => { cell.innerHTML = "<span class='muted'>нет кадра</span>"; };
  img.onclick = () => window.open(url, "_blank");
  cell.innerHTML = "";
  cell.appendChild(img);
}
async function archiveCheck(id) {
  const r = await fetch(`/api/devices/${id}/archive-check`, { method: "POST" });
  const j = await r.json();
  alert("Проверка архива за " + j.day + " выполнена");
  location.reload();
}
async function deleteDevice(id) {
  if (!confirm("Удалить устройство?")) return;
  await fetch(`/api/devices/${id}`, { method: "DELETE" });
  location.href = "/";
}
