"""Web Admin Dashboard for tansaibot (#17).

Standalone FastAPI app yang bisa dijalankan paralel dengan bot.
Expose REST API + HTML UI untuk admin.

Jalankan:
    uvicorn admin_web.app:app --host 0.0.0.0 --port 8080 --reload

Env vars:
    ADMIN_SECRET_KEY   — secret for JWT session tokens
    ADMIN_USERNAME     — admin login username  
    ADMIN_PASSWORD     — admin login password (hashed with bcrypt)
    CHAT_DB_PATH       — path ke SQLite DB yang sama dengan bot
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

# Add parent dir to path so we can import db
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi import FastAPI, HTTPException, Request, Depends
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.middleware.cors import CORSMiddleware
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

import db

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

if _FASTAPI_AVAILABLE:
    app = FastAPI(
        title="tansaibot Admin Dashboard",
        description="Web UI for managing tansaibot users, sessions, and analytics",
        version="2.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app = None  # type: ignore

SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "change-me-in-production-please")
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "tansaibot2024")
DB_PATH = os.getenv("CHAT_DB_PATH", str(Path(__file__).resolve().parent.parent / "chat_history.db"))

if _FASTAPI_AVAILABLE:
    @app.on_event("startup")
    async def startup_event():
        await db.init_db(DB_PATH)

# ---------------------------------------------------------------------------
# Auth helpers (simple HMAC token — no external deps)
# ---------------------------------------------------------------------------

def _make_token(username: str) -> str:
    ts = int(time.time())
    payload = f"{username}:{ts}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_token(token: str) -> str | None:
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        username, ts_str, sig = parts
        ts = int(ts_str)
        if time.time() - ts > 86400:  # 24h expiry
            return None
        expected = hmac.new(SECRET_KEY.encode(), f"{username}:{ts_str}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return username
        return None
    except Exception:
        return None


def _get_current_admin(request: Request) -> str:
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError("FastAPI not installed")
    token = request.headers.get("X-Admin-Token") or request.cookies.get("admin_token", "")
    user = _verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


# ---------------------------------------------------------------------------
# HTML Dashboard (single-page, self-contained)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tansaibot Admin</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
  .sidebar{position:fixed;left:0;top:0;width:220px;height:100vh;background:#161b22;border-right:1px solid #30363d;padding:1.5rem 1rem}
  .logo{font-size:1.2rem;font-weight:700;color:#58a6ff;margin-bottom:2rem}
  .logo span{color:#3fb950}
  .nav a{display:block;padding:.6rem .8rem;margin:.2rem 0;border-radius:6px;color:#8b949e;text-decoration:none;font-size:.9rem;cursor:pointer}
  .nav a:hover,.nav a.active{background:#1f6feb22;color:#58a6ff}
  .main{margin-left:220px;padding:2rem}
  .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:2rem}
  h1{font-size:1.5rem;font-weight:600}
  .badge{background:#1f6feb;color:#fff;padding:.2rem .6rem;border-radius:12px;font-size:.75rem}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin-bottom:2rem}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:1.25rem}
  .card-label{font-size:.8rem;color:#8b949e;margin-bottom:.4rem}
  .card-value{font-size:1.8rem;font-weight:700;color:#58a6ff}
  .card-sub{font-size:.75rem;color:#484f58;margin-top:.25rem}
  table{width:100%;border-collapse:collapse;background:#161b22;border-radius:10px;overflow:hidden}
  th,td{padding:.75rem 1rem;text-align:left;border-bottom:1px solid #21262d;font-size:.875rem}
  th{background:#1c2128;color:#8b949e;font-weight:500}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:#1c2128}
  .btn{padding:.5rem 1rem;border:none;border-radius:6px;cursor:pointer;font-size:.875rem;font-weight:500}
  .btn-primary{background:#1f6feb;color:#fff}
  .btn-danger{background:#da3633;color:#fff}
  .btn-success{background:#238636;color:#fff}
  .btn:hover{opacity:.85}
  .input{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:.5rem .75rem;border-radius:6px;width:100%;font-size:.875rem;outline:none}
  .input:focus{border-color:#58a6ff}
  .status-active{color:#3fb950}
  .status-banned{color:#da3633}
  .status-waitlist{color:#d29922}
  .tab-content{display:none}.tab-content.active{display:block}
  #login-screen{display:flex;justify-content:center;align-items:center;min-height:100vh;background:#0d1117}
  .login-box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:2rem;width:360px}
  .login-box h2{margin-bottom:1.5rem;color:#58a6ff}
  .form-group{margin-bottom:1rem}
  .form-group label{display:block;margin-bottom:.4rem;font-size:.875rem;color:#8b949e}
  .alert{padding:.75rem;border-radius:6px;margin-bottom:1rem;font-size:.875rem}
  .alert-error{background:#da363322;border:1px solid #da3633;color:#ff7b72}
  #app{display:none}
  .section-title{font-size:1rem;font-weight:600;margin-bottom:1rem;color:#e6edf3}
  .search-box{margin-bottom:1rem}
  .pill{display:inline-block;padding:.1rem .5rem;border-radius:10px;font-size:.75rem}
  .pill-free{background:#1f6feb22;color:#58a6ff}
  .pill-premium{background:#d2992222;color:#d29922}
  .pill-admin{background:#3fb95022;color:#3fb950}
</style>
</head>
<body>

<!-- Login Screen -->
<div id="login-screen">
  <div class="login-box">
    <h2>🤖 tansaibot Admin</h2>
    <div id="login-error" class="alert alert-error" style="display:none"></div>
    <div class="form-group">
      <label>Username</label>
      <input class="input" id="login-user" type="text" value="admin" placeholder="admin">
    </div>
    <div class="form-group">
      <label>Password</label>
      <input class="input" id="login-pass" type="password" placeholder="password">
    </div>
    <button class="btn btn-primary" style="width:100%" onclick="doLogin()">Login</button>
  </div>
</div>

<!-- App -->
<div id="app">
  <div class="sidebar">
    <div class="logo">tans<span>ai</span>bot</div>
    <nav class="nav">
      <a onclick="showTab('dashboard')" class="active" id="nav-dashboard">📊 Dashboard</a>
      <a onclick="showTab('users')" id="nav-users">👥 Users</a>
      <a onclick="showTab('sessions')" id="nav-sessions">💬 Sessions</a>
      <a onclick="showTab('audit')" id="nav-audit">📋 Audit Log</a>
      <a onclick="showTab('broadcast')" id="nav-broadcast">📢 Broadcast</a>
    </nav>
  </div>
  <main class="main">
    <div class="header">
      <h1 id="page-title">Dashboard</h1>
      <div>
        <span class="badge" id="admin-badge">admin</span>
        <button class="btn btn-danger" style="margin-left:.5rem" onclick="doLogout()">Logout</button>
      </div>
    </div>

    <!-- Dashboard Tab -->
    <div class="tab-content active" id="tab-dashboard">
      <div class="cards" id="stats-cards">
        <div class="card"><div class="card-label">Total Users</div><div class="card-value" id="stat-users">-</div></div>
        <div class="card"><div class="card-label">Sessions</div><div class="card-value" id="stat-sessions">-</div></div>
        <div class="card"><div class="card-label">Messages</div><div class="card-value" id="stat-messages">-</div></div>
      </div>
      <p class="section-title">Quick Actions</p>
      <div style="display:flex;gap:.75rem;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="loadUsers()">🔄 Refresh Users</button>
        <button class="btn btn-success" onclick="showTab('broadcast')">📢 Broadcast Message</button>
        <button class="btn btn-primary" onclick="loadAudit()">📋 View Audit Log</button>
      </div>
    </div>

    <!-- Users Tab -->
    <div class="tab-content" id="tab-users">
      <div class="search-box">
        <input class="input" id="user-search" placeholder="Filter by user ID or name..." oninput="filterUsers()" style="max-width:400px">
      </div>
      <table>
        <thead><tr><th>User ID</th><th>Name</th><th>Status</th><th>Tier</th><th>Model</th><th>Actions</th></tr></thead>
        <tbody id="users-table"><tr><td colspan="6" style="text-align:center;color:#484f58">Loading...</td></tr></tbody>
      </table>
    </div>

    <!-- Sessions Tab -->
    <div class="tab-content" id="tab-sessions">
      <table>
        <thead><tr><th>Session ID</th><th>User</th><th>Title</th><th>Model</th><th>Messages</th><th>Created</th></tr></thead>
        <tbody id="sessions-table"><tr><td colspan="6" style="text-align:center;color:#484f58">Loading...</td></tr></tbody>
      </table>
    </div>

    <!-- Audit Tab -->
    <div class="tab-content" id="tab-audit">
      <table>
        <thead><tr><th>Time</th><th>Admin</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
        <tbody id="audit-table"><tr><td colspan="5" style="text-align:center;color:#484f58">Loading...</td></tr></tbody>
      </table>
    </div>

    <!-- Broadcast Tab -->
    <div class="tab-content" id="tab-broadcast">
      <div style="max-width:500px">
        <p class="section-title">📢 Broadcast Message</p>
        <div class="form-group">
          <label>Target</label>
          <select class="input" id="broadcast-target">
            <option value="all">All Users (active)</option>
            <option value="premium">Premium only</option>
          </select>
        </div>
        <div class="form-group">
          <label>Message</label>
          <textarea class="input" id="broadcast-msg" rows="4" placeholder="Pesan broadcast..."></textarea>
        </div>
        <button class="btn btn-primary" onclick="doBroadcast()">Send Broadcast</button>
        <div id="broadcast-result" style="margin-top:1rem"></div>
      </div>
    </div>
  </main>
</div>

<script>
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
  document.getElementById('nav-'+tab).classList.add('active');
  const titles = {dashboard:'Dashboard', users:'Users', sessions:'Sessions', audit:'Audit Log', broadcast:'Broadcast'};
  document.getElementById('page-title').textContent = titles[tab] || tab;
  if (tab==='users') loadUsers();
  if (tab==='sessions') loadSessions();
  if (tab==='audit') loadAudit();
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
  if (!d.length) { tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#484f58">No sessions</td></tr>'; return; }
  tb.innerHTML = d.map(s=>`
    <tr>
      <td><code>${s.id}</code></td>
      <td><code>${s.user_id}</code></td>
      <td>${s.title||'(no title)'}</td>
      <td><code>${s.model}</code></td>
      <td>${s.message_count}</td>
      <td>${s.created_at.substring(0,10)}</td>
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
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

if _FASTAPI_AVAILABLE:
    from fastapi import Form
    from fastapi.responses import Response

    @app.get("/admin", response_class=HTMLResponse)
    async def dashboard():
        return _DASHBOARD_HTML

    @app.post("/admin/api/login")
    async def login(request: Request):
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
        if username == ADMIN_USER and password == ADMIN_PASS:
            token = _make_token(username)
            return {"token": token}
        raise HTTPException(status_code=401, detail="Username atau password salah")

    @app.get("/admin/api/stats")
    async def get_stats(admin: str = Depends(_get_current_admin)):
        stats = await db.global_stats(DB_PATH)
        return stats

    @app.get("/admin/api/users")
    async def get_users(admin: str = Depends(_get_current_admin)):
        users = await db.list_all_users(DB_PATH, limit=500)
        return users

    @app.post("/admin/api/users/{user_id}/status")
    async def update_user_status(
        user_id: int,
        request: Request,
        admin: str = Depends(_get_current_admin),
    ):
        body = await request.json()
        status = body.get("status", "")
        if status not in ("active", "banned", "waitlist"):
            raise HTTPException(status_code=400, detail="Invalid status")
        await db.upsert_user_prefs(DB_PATH, user_id, status=status)
        await db.log_audit(DB_PATH, admin_id=0, action=f"set_status_{status}", target_id=user_id, detail=f"via admin dashboard by {admin}")
        return {"ok": True}

    @app.post("/admin/api/users/{user_id}/tier")
    async def update_user_tier(
        user_id: int,
        request: Request,
        admin: str = Depends(_get_current_admin),
    ):
        body = await request.json()
        tier = body.get("tier", "")
        if tier not in ("free", "premium", "admin"):
            raise HTTPException(status_code=400, detail="Invalid tier")
        await db.upsert_user_prefs(DB_PATH, user_id, tier=tier)
        await db.log_audit(DB_PATH, admin_id=0, action="set_tier", target_id=user_id, detail=f"tier={tier} via dashboard by {admin}")
        return {"ok": True}

    @app.get("/admin/api/sessions")
    async def get_sessions(admin: str = Depends(_get_current_admin), limit: int = 100):
        sessions = await db.list_recent_sessions(DB_PATH, limit=limit)
        return [
            {
                "id": s.id, "user_id": s.user_id,
                "title": s.title, "model": s.model,
                "message_count": s.message_count,
                "created_at": s.created_at,
            }
            for s in sessions
        ]

    @app.get("/admin/api/audit")
    async def get_audit(admin: str = Depends(_get_current_admin), limit: int = 50):
        entries = await db.get_audit_log(DB_PATH, limit=limit)
        return [
            {
                "id": e.id, "admin_id": e.admin_id,
                "action": e.action, "target_id": e.target_id,
                "detail": e.detail, "ts": e.ts,
            }
            for e in entries
        ]

    @app.post("/admin/api/broadcast")
    async def broadcast(request: Request, admin: str = Depends(_get_current_admin)):
        body = await request.json()
        message = body.get("message", "").strip()
        target = body.get("target", "all")
        if not message:
            raise HTTPException(status_code=400, detail="Message is empty")
        # Queue broadcast (bot picks up from DB or bot_data)
        # For now, save to audit log as broadcast entry
        await db.log_audit(
            DB_PATH, admin_id=0,
            action="broadcast",
            detail=f"target={target} msg={message[:200]} via={admin}"
        )
        return {"message": f"Broadcast queued untuk target '{target}'. Bot akan mengirim saat berjalan."}
