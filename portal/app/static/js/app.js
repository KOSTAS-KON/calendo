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
