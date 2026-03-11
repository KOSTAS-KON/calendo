function initTimelineGroups() {
  const root = document.querySelector('[data-component="timeline-groups"]');
  if (!root) return;

  root.querySelectorAll('.t-group').forEach((group) => {
    const countEl = group.querySelector('[data-role="t-group-count"]');
    const count = group.querySelectorAll('.t-item').length;
    if (countEl) countEl.textContent = String(count);
  });

  document.querySelectorAll('[data-action="timeline-expand"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      root.querySelectorAll('.t-group').forEach((group) => {
        group.open = true;
      });
    });
  });

  document.querySelectorAll('[data-action="timeline-collapse"]').forEach((btn) => {
    btn.addEventListener('click', () => {
      root.querySelectorAll('.t-group').forEach((group) => {
        group.open = false;
      });
    });
  });
}

function initSidebarToggle() {
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  const openers = document.querySelectorAll('[data-action="toggle-sidebar"]');
  const closers = document.querySelectorAll('[data-action="close-sidebar"]');

  if (!sidebar) return;

  function openSidebar() {
    document.body.classList.add('sidebar-open');
    sidebar.classList.add('open');
    if (backdrop) backdrop.classList.add('open');
  }

  function closeSidebar() {
    document.body.classList.remove('sidebar-open');
    sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('open');
  }

  function toggleSidebar() {
    if (sidebar.classList.contains('open')) {
      closeSidebar();
    } else {
      openSidebar();
    }
  }

  openers.forEach((btn) => btn.addEventListener('click', toggleSidebar));
  closers.forEach((btn) => btn.addEventListener('click', closeSidebar));

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeSidebar();
  });
}

function initCopyButtons() {
  document.addEventListener('click', async (event) => {
    const btn = event.target.closest('.copy-btn');
    if (!btn) return;

    const text = btn.getAttribute('data-copy') || '';
    if (!text) return;

    try {
      await navigator.clipboard.writeText(text);
      const original = btn.textContent;
      btn.textContent = '✓ Copied';
      btn.classList.add('success');
      setTimeout(() => {
        btn.textContent = original;
        btn.classList.remove('success');
      }, 1400);
    } catch (error) {
      const fallback = document.createElement('textarea');
      fallback.value = text;
      document.body.appendChild(fallback);
      fallback.select();
      document.execCommand('copy');
      fallback.remove();
    }
  });
}

function initActiveNav() {
  const currentPath = window.location.pathname;
  document.querySelectorAll('.sidebar-link, .bottom-nav .item').forEach((link) => {
    const href = link.getAttribute('href') || '';
    if (!href || href.startsWith('http')) return;
    const cleanHref = href.split('?')[0];
    if (cleanHref !== '/' && currentPath.startsWith(cleanHref)) {
      link.classList.add('is-active');
    }
  });
}

function initToasts() {
  document.querySelectorAll('[data-toast]').forEach((toast) => {
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(-6px)';
    }, 2600);
    setTimeout(() => toast.remove(), 3200);
  });
}

function initResponsiveTables() {
  const tables = document.querySelectorAll('table.js-responsive-table[data-cards="true"]');
  tables.forEach((table) => {
    if (table.dataset.cardsBuilt === '1') return;

    const heads = Array.from(table.querySelectorAll('thead th')).map((th) => (th.textContent || '').trim());
    const rows = Array.from(table.querySelectorAll('tbody tr'));
    const wrap = document.createElement('div');
    wrap.className = 'table-cards-view';

    rows.forEach((rowEl) => {
      const card = document.createElement('div');
      card.className = 'tcard';
      const cells = Array.from(rowEl.children);
      cells.forEach((cell, idx) => {
        const row = document.createElement('div');
        row.className = 'row';

        const key = document.createElement('div');
        key.className = 'k';
        key.textContent = heads[idx] || `Field ${idx + 1}`;

        const value = document.createElement('div');
        value.className = 'v';
        value.innerHTML = cell.innerHTML;

        row.appendChild(key);
        row.appendChild(value);
        card.appendChild(row);
      });
      wrap.appendChild(card);
    });

    table.insertAdjacentElement('afterend', wrap);
    table.dataset.cardsBuilt = '1';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initTimelineGroups();
  initSidebarToggle();
  initCopyButtons();
  initActiveNav();
  initToasts();
  initResponsiveTables();
});
