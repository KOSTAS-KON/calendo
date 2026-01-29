// Minimal JS (enhancements for the Therapy Archive Portal)

function initTimelineGroups(){
  const root = document.querySelector('[data-component="timeline-groups"]');
  if (!root) return;

  // Fill group counts
  root.querySelectorAll('.t-group').forEach((group) => {
    const countEl = group.querySelector('[data-role="t-group-count"]');
    const count = group.querySelectorAll('.t-item').length;
    if (countEl) countEl.textContent = String(count);
  });

  // Toolbar actions
  document.querySelectorAll('[data-action="timeline-expand"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      root.querySelectorAll('.t-group').forEach((d) => { d.open = true; });
    });
  });
  document.querySelectorAll('[data-action="timeline-collapse"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      root.querySelectorAll('.t-group').forEach((d) => { d.open = false; });
    });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initTimelineGroups();
});


// Global copy buttons (delegated)
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const text = btn.dataset.copy;
  if (!text) return;

  try {
    await navigator.clipboard.writeText(text);
    const old = btn.innerText;
    btn.innerText = "✓ Copied";
    btn.classList.add("success");
    setTimeout(() => {
      btn.innerText = old;
      btn.classList.remove("success");
    }, 1400);
  } catch (err) {
    alert("Copy failed. Please copy manually.");
  }
});
