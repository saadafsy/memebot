document.getElementById("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  try {
    const r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json", "X-Memebot": "1"},
      body: JSON.stringify({password: document.getElementById("pw").value}), credentials: "same-origin"});
    if (r.ok) { location.href = "/"; return; }
    const j = await r.json().catch(() => ({}));
    err.textContent = j.detail || "Login failed";
  } catch (e) { err.textContent = "Cannot reach the bot"; }
});
