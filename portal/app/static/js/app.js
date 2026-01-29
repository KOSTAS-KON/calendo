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
<<<<<<< HEAD
=======
  initDrawerTriggers();
});

function initDrawerTriggers(){
  document.querySelectorAll("[data-open-drawer]").forEach((btn)=>{
    btn.addEventListener("click",(e)=>{
      e.preventDefault();
      const tplId = btn.getAttribute("data-open-drawer");
      const title = btn.getAttribute("data-drawer-title") || "Edit";
      const subtitle = btn.getAttribute("data-drawer-subtitle") || "";
      const tpl = document.getElementById(tplId);
      if(tpl && window.openDrawer){
        window.openDrawer({ title, subtitle, html: tpl.innerHTML });
        // refresh icons inside drawer
        if(window.lucide && window.lucide.createIcons) window.lucide.createIcons();
      }
    });
  });
}



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
  if (!bb) return;
  bb.addEventListener('click', () => {
    const isMobile = window.matchMedia && window.matchMedia("(max-width: 768px)").matches;
    const menuTpl = document.getElementById("drawerTpl_menu");
    if (isMobile && menuTpl && window.openDrawer){
      window.openDrawer({ title: "Quick create", subtitle: "Choose what to add", html: menuTpl.innerHTML });
      return;
    }
    if (open) open.click();
  });
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


// Drawer + Stepper (mobile)
(function(){
  const backdrop = document.getElementById("drawerBackdrop");
  const drawer = document.getElementById("globalDrawer");
  const body = document.getElementById("drawerBody");
  const titleEl = document.getElementById("drawerTitle");
  const subtitleEl = document.getElementById("drawerSubtitle");
  const closeBtn = document.getElementById("drawerClose");

  function closeDrawer(){
    if(!drawer) return;
    drawer.classList.remove("open");
    backdrop && backdrop.classList.remove("open");
    drawer.setAttribute("aria-hidden","true");
    backdrop && backdrop.setAttribute("aria-hidden","true");
    document.body.style.overflow = "";
    body && (body.innerHTML = "");
  }

  window.openDrawer = function(opts){
    if(!drawer || !body) return;
    const { title, subtitle, html } = opts || {};
    titleEl && (titleEl.textContent = title || "Panel");
    if(subtitle){
      subtitleEl.style.display = "block";
      subtitleEl.textContent = subtitle;
    } else if(subtitleEl){
      subtitleEl.style.display = "none";
      subtitleEl.textContent = "";
    }
    body.innerHTML = html || "";
    // wire steppers inside drawer
    body.querySelectorAll("[data-stepper='true']").forEach(initStepper);
    drawer.classList.add("open");
    backdrop && backdrop.classList.add("open");
    drawer.setAttribute("aria-hidden","false");
    backdrop && backdrop.setAttribute("aria-hidden","false");
    document.body.style.overflow = "hidden";
  };

  function initStepper(stepper){
    const steps = Array.from(stepper.querySelectorAll(".step"));
    const dots = stepper.querySelector(".stepper");
    let idx = 0;

    function renderDots(){
      if(!dots) return;
      dots.innerHTML = "";
      steps.forEach((_, i)=>{
        const d = document.createElement("span");
        d.className = "step-dot" + (i===idx ? " active" : "");
        dots.appendChild(d);
      });
    }

    function show(i){
      idx = Math.max(0, Math.min(i, steps.length-1));
      steps.forEach((s, si)=>s.classList.toggle("active", si===idx));
      renderDots();
      stepper.querySelectorAll("[data-step-next]").forEach(btn=>{
        btn.style.display = (idx < steps.length-1) ? "" : "none";
      });
      stepper.querySelectorAll("[data-step-back]").forEach(btn=>{
        btn.style.display = (idx > 0) ? "" : "none";
      });
      stepper.querySelectorAll("[data-step-submit]").forEach(btn=>{
        btn.style.display = (idx === steps.length-1) ? "" : "none";
      });
    }

    stepper.addEventListener("click",(e)=>{
      const t = e.target;
      if(t && t.closest("[data-step-next]")){ e.preventDefault(); show(idx+1); }
      if(t && t.closest("[data-step-back]")){ e.preventDefault(); show(idx-1); }
    });

    show(0);
  }

  // Close handlers
  closeBtn && closeBtn.addEventListener("click", closeDrawer);
  backdrop && backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown",(e)=>{ if(e.key==="Escape") closeDrawer(); });

  window.closeDrawer = closeDrawer;
})();


function enhanceResponsiveTables(){
  const isMobile = window.matchMedia && window.matchMedia("(max-width: 768px)").matches;
  document.querySelectorAll("table.js-responsive-table").forEach(table=>{
    // Fill data-labels once
    const headers = Array.from(table.querySelectorAll("thead th")).map(th => (th.textContent || "").trim());
    table.querySelectorAll("tbody tr").forEach(tr=>{
      Array.from(tr.children).forEach((td, i)=>{
        if(td && td.tagName==="TD"){
          if(!td.getAttribute("data-label")) td.setAttribute("data-label", headers[i] || "");
        }
      });
      if(isMobile){
        // Build action strip from last cell links/buttons
        if(tr.querySelector(".card-actions")) return;
        const last = tr.lastElementChild;
        if(!last) return;
        const candidates = last.querySelectorAll("a,button");
        if(candidates.length===0) return;
        const strip = document.createElement("div");
        strip.className = "card-actions";
        candidates.forEach(el=>{
          const clone = el.cloneNode(true);
          // Normalize styles
          if(clone.tagName==="A") clone.classList.add("btn","btn-sm");
          if(clone.tagName==="BUTTON") clone.classList.add("btn","btn-sm");
          strip.appendChild(clone);
        });
        tr.appendChild(strip);
      }
    });
  });
}
window.addEventListener("resize", ()=>{ enhanceResponsiveTables(); });
document.addEventListener("DOMContentLoaded", ()=>{ enhanceResponsiveTables(); });


function openBillingRowDrawer(btn){
  const id = btn.getAttribute("data-billing-id");
  const action = btn.getAttribute("data-action");
  const paid = btn.getAttribute("data-paid") || "NO";
  const invoice = btn.getAttribute("data-invoice") || "NO";
  const signoff = btn.getAttribute("data-signoff") || "NO";
  const redirect = btn.getAttribute("data-redirect") || "";

  const html = `
  <div data-stepper="true">
    <div class="stepper" aria-hidden="true"></div>

    <form method="post" action="${action}" class="stack">
      <input type="hidden" name="redirect" value="${redirect}">
      <div class="step active">
        <div class="card" style="padding:12px;">
          <div class="grid grid-1 gap-2">
            <div>
              <label class="label">Paid</label>
              <select name="paid" class="input">
                <option value="NO"${paid==="NO"?" selected":""}>NO</option>
                <option value="YES"${paid==="YES"?" selected":""}>YES</option>
              </select>
            </div>
            <div>
              <label class="label">Invoice created</label>
              <select name="invoice_created" class="input">
                <option value="NO"${invoice==="NO"?" selected":""}>NO</option>
                <option value="YES"${invoice==="YES"?" selected":""}>YES</option>
              </select>
            </div>
            <div>
              <label class="label">Parent signed off</label>
              <select name="parent_signed_off" class="input">
                <option value="NO"${signoff==="NO"?" selected":""}>NO</option>
                <option value="YES"${signoff==="YES"?" selected":""}>YES</option>
              </select>
            </div>
          </div>
        </div>

        <div class="step-actions">
          <button class="btn" type="button" data-step-next>Next</button>
        </div>
      </div>

      <div class="step">
        <div class="card" style="padding:12px;">
          <div style="font-weight:800; margin-bottom:8px;">Confirm update</div>
          <div style="color:var(--text-muted); font-size:13px;">Tap “Save” to update the billing status for this child.</div>
        </div>
        <div class="step-actions">
          <button class="btn" type="button" data-step-back>Back</button>
          <button class="btn primary" type="submit" data-step-submit>Save</button>
        </div>
      </div>
    </form>
  </div>
  `;
  window.openDrawer({ title: "Edit billing status", subtitle: "Mobile quick editor", html });
}

document.addEventListener("click",(e)=>{
  const b = e.target && e.target.closest(".js-open-billing-drawer");
  if(b){ e.preventDefault(); openBillingRowDrawer(b); }
});


function openCalendarAddDrawer(kind){
  const tpl = document.getElementById("drawerTpl_"+kind);
  if(!tpl) return;
  const title = tpl.getAttribute("data-title") || "Add";
  const subtitle = tpl.getAttribute("data-subtitle") || "";
  window.openDrawer({ title, subtitle, html: tpl.innerHTML });
}
document.addEventListener("click",(e)=>{
  const t = e.target && e.target.closest("[data-open-calendar-drawer]");
  if(t){
    e.preventDefault();
    openCalendarAddDrawer(t.getAttribute("data-open-calendar-drawer"));
  }
>>>>>>> c6a85c9 (Activation page: redeem codes + show errors + subscription gate)
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
