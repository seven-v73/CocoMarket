// Utilitaires communs
const CSRF = document.querySelector('meta[name="csrf"]')?.content || "";

async function api(url, method = "GET", body = null) {
  const opt = { method, headers: { "X-CSRF": CSRF } };
  if (body !== null) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opt); }
  catch (e) { throw new Error("Serveur injoignable."); }
  if (res.status === 401) { location.href = "/login"; throw new Error("Session expirée"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Erreur " + res.status);
  return data;
}

const nf = new Intl.NumberFormat("fr-FR");
const cfa = n => nf.format(Math.round(n || 0)) + " F";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmm = iso => (iso || "").slice(11, 16);

function initTheme() {
  const button = document.getElementById("themeToggle");
  const root = document.documentElement;
  const dark = localStorage.getItem("cocomarket-theme") === "dark";
  root.dataset.theme = dark ? "dark" : "light";
  if (!button) return;
  const sync = active => { button.textContent = active ? "☀ Mode clair" : "☾ Mode sombre"; button.setAttribute("aria-pressed", String(active)); };
  sync(dark);
  button.onclick = () => { const active = root.dataset.theme !== "dark"; root.dataset.theme = active ? "dark" : "light"; localStorage.setItem("cocomarket-theme", active ? "dark" : "light"); sync(active); };
}
initTheme();

function toast(msg, kind = "") {
  let w = document.querySelector(".toast-wrap");
  if (!w) { w = document.createElement("div"); w.className = "toast-wrap"; document.body.appendChild(w); }
  w.setAttribute("aria-live", "polite");
  const t = document.createElement("div");
  t.className = "toast " + kind; t.setAttribute("role", "status"); t.textContent = msg; w.appendChild(t);
  setTimeout(() => t.remove(), 4800);
}

function duration(sec) {
  if (sec == null) return "—";
  if (sec < 1) return "< 1 s";
  if (sec < 90) return Math.round(sec) + " s";
  if (sec < 5400) return Math.round(sec / 60) + " min";
  return (sec / 3600).toFixed(1).replace(".", ",") + " h";
}
