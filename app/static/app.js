// Small helpers, no framework. Live checks: any form with data-check="url" posts
// its fields to that URL on change and shows the returned HTML in [data-check-out].
(function () {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";
  let timer;
  document.querySelectorAll("form[data-check]").forEach((form) => {
    const out = form.querySelector("[data-check-out]") || document.querySelector(form.dataset.checkOut);
    const run = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const body = new FormData(form);
        body.set("_csrf", csrf);
        const r = await fetch(form.dataset.check, { method: "POST", body, headers: { "x-csrf-token": csrf } });
        if (out) out.innerHTML = await r.text();
      }, 250);
    };
    form.addEventListener("input", run);
    form.addEventListener("change", run);
  });
  // Add another row to a multi-row form (OT for several people).
  document.querySelectorAll("[data-add-row]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tbody = document.querySelector(btn.dataset.addRow);
      const last = tbody.querySelector("tr:last-child");
      const clone = last.cloneNode(true);
      clone.querySelectorAll("input").forEach((i) => { if (i.type !== "time") i.value = ""; });
      clone.querySelectorAll("[data-row-out]").forEach((o) => (o.innerHTML = ""));
      tbody.appendChild(clone);
    });
  });
  // Tick or untick all checkboxes in a table.
  document.querySelectorAll("[data-check-all]").forEach((box) => {
    box.addEventListener("change", () => {
      document.querySelectorAll(box.dataset.checkAll).forEach((c) => { if (!c.disabled) c.checked = box.checked; });
    });
  });
  document.querySelectorAll("[data-confirm]").forEach((el) => {
    el.addEventListener("click", (e) => { if (!confirm(el.dataset.confirm)) e.preventDefault(); });
  });
  // Any wide table scrolls inside its own box instead of pushing the page sideways.
  document.querySelectorAll("main table").forEach((t) => {
    if (t.closest(".scroll")) return;
    const box = document.createElement("div");
    box.className = "scroll";
    t.parentNode.insertBefore(box, t);
    box.appendChild(t);
  });
  // Side menu on phones and tablets.
  const setMenu = (open) => {
    document.body.classList.toggle("menu-open", open);
    document.querySelectorAll("[data-menu-open]").forEach((b) => b.setAttribute("aria-expanded", open));
    if (open) document.querySelector(".side a[aria-current]")?.focus();
  };
  document.querySelectorAll("[data-menu-open]").forEach((b) => b.addEventListener("click", () => setMenu(true)));
  document.querySelectorAll("[data-menu-close]").forEach((b) => b.addEventListener("click", () => setMenu(false)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });
  // Messages can be closed; good news closes itself.
  document.querySelectorAll(".flash").forEach((f) => {
    f.querySelector("[data-dismiss]")?.addEventListener("click", () => f.remove());
    if (f.classList.contains("ok")) setTimeout(() => f.remove(), 6000);
  });
  // The row of links under a page title works as tabs: mark the page we are on.
  document.querySelectorAll(".phead > .end.no-print").forEach((row) => {
    const links = [...row.querySelectorAll(":scope > a.btn")];
    const here = location.pathname + location.search;
    let best = null, len = -1;
    links.forEach((a) => {
      const u = new URL(a.href, location.href);
      const p = u.pathname + u.search;
      if (here === p || location.pathname === u.pathname) { if (p.length > len) { best = a; len = p.length + 1000; } }
      else if (location.pathname.startsWith(u.pathname + "/") && u.pathname.length > len) { best = a; len = u.pathname.length; }
    });
    if (best) best.setAttribute("aria-current", "page");
  });
})();
