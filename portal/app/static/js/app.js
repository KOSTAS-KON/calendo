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


function initSidebarToggle(){
  const btns = document.querySelectorAll('[data-action="toggle-sidebar"]');
  const closeEls = document.querySelectorAll('[data-action="close-sidebar"]');

  function close(){ document.body.classList.remove('sidebar-open'); }
  function toggle(){ document.body.classList.toggle('sidebar-open'); }

  btns.forEach(b => b.addEventListener('click', toggle));
  closeEls.forEach(el => el.addEventListener('click', close));

  // close on ESC
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });
}

function initCopyButtons(){
  document.querySelectorAll('.copy-btn[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const text = btn.getAttribute('data-copy') || '';
      try{
        await navigator.clipboard.writeText(text);
        const old = btn.textContent;
        btn.textContent = '✅ Copied';
        setTimeout(() => { btn.textContent = old; }, 1200);
      }catch(e){
        // fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
    });
  });
}

function initActiveNav(){
  const path = window.location.pathname;
  document.querySelectorAll('.sideitem, .bn-item').forEach((a) => {
    const href = a.getAttribute('href') || '';
    if (!href || href.startsWith('http')) return;
    // Basic match (ignores query string)
    const hrefPath = href.split('?')[0];
    if (hrefPath !== '/' && path.startsWith(hrefPath)){
      a.classList.add('is-active');
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initSidebarToggle();
  initCopyButtons();
  initActiveNav();
});


function initToasts(){
  document.querySelectorAll('[data-toast]').forEach((el) => {
    // auto-hide
    setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(-6px)'; }, 2600);
    setTimeout(() => { el.remove(); }, 3200);
  });
}

// Convert tables to cards on mobile (reads <th> labels)
function initResponsiveTables(){
  const tables = document.querySelectorAll('table.js-responsive-table[data-cards="true"]');
  tables.forEach((table) => {
    // avoid duplicating
    if (table.dataset.cardsBuilt === '1') return;
    const heads = Array.from(table.querySelectorAll('thead th')).map(th => (th.textContent || '').trim());
    const rows = Array.from(table.querySelectorAll('tbody tr'));
    const wrap = document.createElement('div');
    wrap.className = 'table-cards-view';
    rows.forEach((tr) => {
      const card = document.createElement('div');
      card.className = 'tcard';
      const tds = Array.from(tr.children);
      tds.forEach((td, idx) => {
        const label = heads[idx] || ('Field ' + (idx+1));
        const row = document.createElement('div');
        row.className = 'row';
        const k = document.createElement('div');
        k.className = 'k';
        k.textContent = label;
        const v = document.createElement('div');
        v.className = 'v';
        // preserve inner HTML for buttons/forms
        v.innerHTML = td.innerHTML;
        row.appendChild(k);
        row.appendChild(v);
        card.appendChild(row);
      });
      wrap.appendChild(card);
    });
    table.insertAdjacentElement('afterend', wrap);
    table.dataset.cardsBuilt = '1';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initSidebarToggle();
  initToasts();
  initResponsiveTables();
});
