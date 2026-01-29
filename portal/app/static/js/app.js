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



function toast(message, opts){
  const options = opts || {};
  let wrap = document.querySelector('.toast-wrap');
  if (!wrap){
    wrap = document.createElement('div');
    wrap.className = 'toast-wrap';
    document.body.appendChild(wrap);
  }
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<div class="tmsg">${escapeHtml(String(message || ''))}</div>
                  <a class="tbtn" href="javascript:void(0)">OK</a>`;
  const btn = el.querySelector('.tbtn');
  const remove = () => { if (el && el.parentNode) el.parentNode.removeChild(el); };
  btn.addEventListener('click', remove);
  wrap.appendChild(el);
  const ttl = options.ttl_ms || 3200;
  window.setTimeout(remove, ttl);
}

function escapeHtml(s){
  return s.replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function copyOnboarding(tenantSlug){
  try{
    const res = await fetch(`/admin/tenants/${encodeURIComponent(tenantSlug)}/onboarding`, {headers: {'Accept':'application/json'}});
    if (!res.ok){
      toast('Failed to fetch onboarding info');
      return;
    }
    const d = await res.json();
    const text = [
      `Clinic: ${d.clinic || ''}`,
      `Tenant ID: ${d.tenant_id || ''}`,
      `Login URL:`,
      `${d.login_url || ''}`,
      ``,
      `Email: ${d.email || ''}`,
      `Temporary password: ${d.temp_password || '********'}`
    ].join('\n');

    await navigator.clipboard.writeText(text);
    toast('Onboarding info copied');
  }catch(e){
    console.error(e);
    toast('Copy failed (clipboard blocked?)');
  }
}
