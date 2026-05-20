let TOKEN = localStorage.getItem('admin_token') || '';
let allUsers = [];

async function api(path, opts={}) {
  const res = await fetch('/admin/api' + path, {
    ...opts,
    headers: {'X-Admin-Token': TOKEN, 'Content-Type': 'application/json', ...(opts.headers||{})},
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  if (res.status === 401) { doLogout(); return null; }
  return res.json();
}

async function doLogin() {
  const u = document.getElementById('login-user').value;
  const p = document.getElementById('login-pass').value;
  const r = await fetch('/admin/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username:u, password:p})});
  const d = await r.json();
  if (d.token) {
    TOKEN = d.token;
    localStorage.setItem('admin_token', TOKEN);
    document.getElementById('login-screen').style.display='none';
    document.getElementById('app').style.display='block';
    document.getElementById('admin-badge').textContent = u;
    loadDashboard();
  } else {
    const err = document.getElementById('login-error');
    err.textContent = d.detail || 'Login gagal';
    err.style.display = 'block';
  }
}

function doLogout() {
  TOKEN = '';
  localStorage.removeItem('admin_token');
  document.getElementById('app').style.display='none';
  document.getElementById('login-screen').style.display='flex';
}

function showTab(tab) {
  document.querySelectorAll('.tab-content').forEach(e=>e.classList.remove('active'));
  document.querySelectorAll('.nav a').forEach(e=>e.classList.remove('active'));
  document.getElementById('tab-'+tab).classList.add('active');
  const navEl = document.getElementById('nav-'+tab);
  if(navEl) navEl.classList.add('active');
  const titles = {dashboard:'Dashboard',analytics:'Analytics',users:'Users',sessions:'Sessions',settings:'Settings',health:'System Health',audit:'Audit Log',broadcast:'Broadcast'};
  document.getElementById('page-title').textContent = titles[tab] || tab;
  if (tab==='users') loadUsers();
  if (tab==='sessions') loadSessions();
  if (tab==='audit') loadAudit();
  if (tab==='analytics') loadAnalytics();
  if (tab==='settings') loadSettings();
  if (tab==='health') loadHealth();
}

async function loadDashboard() {
  const d = await api('/stats');
  if (!d) return;
  document.getElementById('stat-users').textContent = d.users;
  document.getElementById('stat-sessions').textContent = d.sessions;
  document.getElementById('stat-messages').textContent = d.messages;
  if (TOKEN) { document.getElementById('app').style.display='block'; document.getElementById('login-screen').style.display='none'; }
}

async function loadUsers() {
  const d = await api('/users');
  if (!d) return;
  allUsers = d;
  renderUsers(d);
}

function renderUsers(users) {
  const tb = document.getElementById('users-table');
  if (!users.length) { tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#484f58">No users</td></tr>'; return; }
  tb.innerHTML = users.map(u => `
    <tr>
      <td><code>${u.user_id}</code></td>
      <td>${u.display_name||'-'}</td>
      <td class="status-${u.status}">${u.status}</td>
      <td><span class="pill pill-${u.tier||'free'}">${u.tier||'free'}</span></td>
      <td><code>${u.default_model||'-'}</code></td>
      <td style="display:flex;gap:.4rem;flex-wrap:wrap">
        ${u.status==='waitlist'?`<button class="btn btn-success" onclick="setStatus(${u.user_id},'active')">Approve</button>`:''}
        ${u.status!=='banned'?`<button class="btn btn-danger" onclick="setStatus(${u.user_id},'banned')">Ban</button>`:`<button class="btn btn-primary" onclick="setStatus(${u.user_id},'active')">Unban</button>`}
        <button class="btn btn-primary" onclick="setTier(${u.user_id})">Tier</button>
        <button class="btn btn-primary" onclick="editUserSettings(${u.user_id})">⚙️</button>
      </td>
    </tr>
  `).join('');
}

function filterUsers() {
  const q = document.getElementById('user-search').value.toLowerCase();
  renderUsers(allUsers.filter(u => String(u.user_id).includes(q) || (u.display_name||'').toLowerCase().includes(q)));
}

async function setStatus(uid, status) {
  const r = await api('/users/'+uid+'/status', {method:'POST', body:{status}});
  if (r) loadUsers();
}

async function setTier(uid) {
  const tier = prompt('Set tier (free/premium/admin):');
  if (!tier) return;
  const r = await api('/users/'+uid+'/tier', {method:'POST', body:{tier}});
  if (r) loadUsers();
}

async function loadSessions() {
  const d = await api('/sessions');
  if (!d) return;
  const tb = document.getElementById('sessions-table');
  if (!d.length) { tb.innerHTML='<tr><td colspan="7" style="text-align:center;color:#484f58">No sessions</td></tr>'; return; }
  tb.innerHTML = d.map(s=>`
    <tr>
      <td><code>${s.id}</code></td>
      <td><code>${s.user_id}</code></td>
      <td>${s.title||'(no title)'}</td>
      <td><code>${s.model}</code></td>
      <td>${s.message_count}</td>
      <td>${s.created_at.substring(0,10)}</td>
      <td><button class="btn btn-primary" onclick="viewChat(${s.id},'${(s.title||'Chat').replace(/'/g,"\'")}')">💬 View</button></td>
    </tr>
  `).join('');
}

async function loadAudit() {
  const d = await api('/audit');
  if (!d) return;
  const tb = document.getElementById('audit-table');
  if (!d.length) { tb.innerHTML='<tr><td colspan="5" style="text-align:center;color:#484f58">No entries</td></tr>'; return; }
  tb.innerHTML = d.map(e=>`
    <tr>
      <td>${e.ts.substring(0,16)}</td>
      <td><code>${e.admin_id}</code></td>
      <td><b>${e.action}</b></td>
      <td>${e.target_id||'-'}</td>
      <td>${e.detail}</td>
    </tr>
  `).join('');
}

async function doBroadcast() {
  const msg = document.getElementById('broadcast-msg').value;
  const target = document.getElementById('broadcast-target').value;
  if (!msg.trim()) { alert('Pesan tidak boleh kosong'); return; }
  const r = await api('/broadcast', {method:'POST', body:{message:msg, target}});
  const el = document.getElementById('broadcast-result');
  if (r) {
    el.innerHTML = `<div class="alert" style="background:#3fb95022;border:1px solid #3fb950;color:#3fb950">✅ ${r.message}</div>`;
  }
}

// Auto-login if token exists
window.onload = () => {
  if (TOKEN) loadDashboard();
};

// --- Analytics ---
let dailyChart = null;
async function loadAnalytics() {
  const d = await api('/analytics');
  if (!d) return;
  const tb = document.getElementById('analytics-models');
  if (!d.by_model.length) { tb.innerHTML='<tr><td colspan="4" style="text-align:center;color:#484f58">No data yet</td></tr>'; }
  else { tb.innerHTML = d.by_model.map(m=>`<tr><td><code>${m.model}</code></td><td>${m.count.toLocaleString()}</td><td>${m.tokens_in.toLocaleString()}</td><td>${m.tokens_out.toLocaleString()}</td></tr>`).join(''); }
  // Chart
  const ctx = document.getElementById('chart-daily');
  if (dailyChart) dailyChart.destroy();
  if (typeof Chart !== 'undefined' && d.by_day.length) {
    dailyChart = new Chart(ctx, {
      type:'bar',
      data:{labels:d.by_day.map(x=>x.day.substring(5)),datasets:[{label:'Messages',data:d.by_day.map(x=>x.count),backgroundColor:'#1f6feb',borderRadius:4}]},
      options:{responsive:true,maintainAspectRatio:false,scales:{x:{ticks:{color:'#8b949e'},grid:{display:false}},y:{ticks:{color:'#8b949e'},grid:{color:'#21262d'}}},plugins:{legend:{display:false}}}
    });
  }
}

// --- Chat Viewer ---
async function viewChat(sid, title) {
  document.getElementById('chat-modal-title').textContent = 'Session #'+sid+': '+title;
  document.getElementById('chat-modal-body').innerHTML = '<div style="text-align:center;color:#8b949e">Loading...</div>';
  document.getElementById('chat-modal').classList.add('show');
  const msgs = await api('/sessions/'+sid+'/messages');
  if (!msgs || !msgs.length) { document.getElementById('chat-modal-body').innerHTML='<div style="text-align:center;color:#484f58">No messages</div>'; return; }
  document.getElementById('chat-modal-body').innerHTML = msgs.map(m=>{const safe=m.content.replace(/</g,'&lt;').replace(/\n/g,'<br>');return '<div class="chat-msg '+m.role+'"><div class="chat-role">'+m.role+'</div><div>'+safe+'</div></div>';}).join('');
}
function closeChatModal() { document.getElementById('chat-modal').classList.remove('show'); }

// --- Settings Editor ---
let currentSettings = {};
async function loadSettings() {
  const d = await api('/settings');
  if (!d) return;
  currentSettings = d.settings || {};
  const form = document.getElementById('settings-form');
  const keys = Object.keys(currentSettings);
  if (!keys.length) { form.innerHTML='<div style="color:#484f58">No .env file found</div>'; return; }
  form.innerHTML = keys.map(k=>`<div class="form-group"><label>${k}</label><input class="input" id="setting-${k}" value="${currentSettings[k]||''}"></div>`).join('');
  document.getElementById('settings-result').innerHTML='';
}
async function saveSettings() {
  const changes = {};
  Object.keys(currentSettings).forEach(k=>{ const v=document.getElementById('setting-'+k); if(v) changes[k]=v.value; });
  const r = await api('/settings',{method:'POST',body:{changes}});
  const el = document.getElementById('settings-result');
  if (r && r.ok) { el.innerHTML='<div class="alert" style="background:#3fb95022;border:1px solid #3fb950;color:#3fb950">✅ '+r.message+'</div>'; }
  else { el.innerHTML='<div class="alert alert-error">Error saving</div>'; }
}

// --- System Health ---
async function loadHealth() {
  const d = await api('/health');
  if (!d || d.error) { document.getElementById('health-cpu').textContent='N/A'; return; }
  document.getElementById('health-cpu').innerHTML=d.cpu_percent+'%<div class="progress-bar"><div class="progress-fill" style="width:'+d.cpu_percent+'%;background:'+(_color(d.cpu_percent))+'"></div></div>';
  document.getElementById('health-ram').innerHTML=d.ram_percent+'%<div class="progress-bar"><div class="progress-fill" style="width:'+d.ram_percent+'%;background:'+(_color(d.ram_percent))+'"></div></div>';
  document.getElementById('health-ram-sub').textContent=d.ram_used_gb+'GB / '+d.ram_total_gb+'GB';
  document.getElementById('health-disk').innerHTML=d.disk_percent+'%<div class="progress-bar"><div class="progress-fill" style="width:'+d.disk_percent+'%;background:'+(_color(d.disk_percent))+'"></div></div>';
  document.getElementById('health-disk-sub').textContent=d.disk_used_gb+'GB / '+d.disk_total_gb+'GB';
  const h=Math.floor(d.uptime_seconds/3600), m=Math.floor((d.uptime_seconds%3600)/60);
  document.getElementById('health-uptime').textContent=h+'h '+m+'m';
}
function _color(pct){return pct>80?'#da3633':pct>50?'#d29922':'#3fb950';}

// --- User Settings Modal ---
async function editUserSettings(uid) {
  const body = document.getElementById('user-settings-body');
  body.innerHTML = `
    <div class="form-group"><label>Custom System Prompt</label><textarea class="input" id="us-prompt" rows="4" placeholder="System prompt khusus..."></textarea></div>
    <div class="form-group"><label>Rate Limit / Minute (0=default)</label><input class="input" id="us-rlm" type="number" value="0"></div>
    <div class="form-group"><label>Rate Limit / Day (0=default)</label><input class="input" id="us-rld" type="number" value="0"></div>
    <button class="btn btn-primary" onclick="saveUserSettings(${uid})">Save</button>
    <div id="us-result" style="margin-top:.5rem"></div>
  `;
  document.getElementById('user-settings-modal').classList.add('show');
}
function closeUserSettingsModal() { document.getElementById('user-settings-modal').classList.remove('show'); }
async function saveUserSettings(uid) {
  const r = await api('/users/'+uid+'/settings', {method:'POST', body:{
    custom_system_prompt: document.getElementById('us-prompt').value,
    rate_limit_minute: document.getElementById('us-rlm').value,
    rate_limit_day: document.getElementById('us-rld').value
  }});
  const el = document.getElementById('us-result');
  if (r && r.ok) el.innerHTML='<span style="color:#3fb950">✅ Saved</span>';
  else el.innerHTML='<span style="color:#da3633">Error</span>';
}
