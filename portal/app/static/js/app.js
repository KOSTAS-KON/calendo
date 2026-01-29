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
  initResponsiveTables();
  initBottomBarQuickCreate();
});


function initResponsiveTables(){
  // For tables with .js-responsive-table, add data-label attributes from <th> for mobile card layout CSS.
  document.querySelectorAll('table.js-responsive-table').forEach((table) => {
    const headers = Array.from(table.querySelectorAll('thead th')).map(th => (th.textContent || '').trim());
    if (!headers.length) return;

    table.querySelectorAll('tbody tr').forEach((tr) => {
      const cells = Array.from(tr.children).filter(el => el.tagName === 'TD');
      cells.forEach((td, i) => {
        if (!td.getAttribute('data-label') && headers[i]) {
          td.setAttribute('data-label', headers[i]);
        }
      });
    });
  });
}

function initBottomBarQuickCreate(){
  const bb = document.getElementById('bbQuickCreate');
  const open = document.getElementById('openQuickCreate');
  if (!bb || !open) return;
  bb.addEventListener('click', () => open.click());
}

function initMobileNav(){
  const sidebar = document.querySelector('.sidebar');
  const btn = document.getElementById('navToggle');
  const backdrop = document.getElementById('backdrop');
  if (!sidebar || !btn || !backdrop) return;

  function openNav(){
    sidebar.classList.add('open');
    backdrop.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeNav(){
    sidebar.classList.remove('open');
    backdrop.classList.remove('open');
    document.body.style.overflow = '';
  }

  btn.addEventListener('click', () => {
    if (sidebar.classList.contains('open')) closeNav();
    else openNav();
  });
  backdrop.addEventListener('click', closeNav);
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeNav();
  });

  // Close drawer on navigation
  sidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', closeNav));
}

document.addEventListener('DOMContentLoaded', () => {
  initMobileNav();
});
