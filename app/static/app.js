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
})();
