const state = {
  user: null,
  researchers: [],
  grants: [],
  equipment: [],
  likedKeys: new Set(),
  likedItems: [],
  likesFilter: 'all',
  historyItems: [],
};

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg) {
  const el = document.getElementById('toast-box');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => el.classList.add('hidden'), 2800);
}

async function apiGet(url) {
  const res = await fetch(url);
  return res.json();
}

async function apiPost(url, data) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return res.json();
}

window.addEventListener('DOMContentLoaded', async () => {
  const savedUser = localStorage.getItem('fukui_matching_user');
  if (savedUser) {
    try {
      const parsed = JSON.parse(savedUser);
      if (parsed && parsed.name && parsed.db_matched !== undefined) {
        applyLoggedInUser(parsed);
      } else {
        localStorage.removeItem('fukui_matching_user');
      }
    } catch (e) {
      localStorage.removeItem('fukui_matching_user');
    }
  }
  await refreshStatusAndLikes();
  if (state.user) {
    await runAISearch(true);
  }
});

// ============================================================================
// ログイン・アカウント登録（DB氏名自動照合付き）
// ============================================================================
function switchAuthTab(mode) {
  const isLogin = mode === 'login';
  document.getElementById('tab-btn-login').classList.toggle('active', isLogin);
  document.getElementById('tab-btn-register').classList.toggle('active', !isLogin);
  document.getElementById('form-login').classList.toggle('hidden', !isLogin);
  document.getElementById('form-register').classList.toggle('hidden', isLogin);
  document.getElementById('auth-error-msg').classList.add('hidden');
}

function showAuthError(msg) {
  const el = document.getElementById('auth-error-msg');
  el.textContent = msg;
  el.classList.remove('hidden');
}

let _lookupTimer = null;
function onInputRegisterName(val) {
  document.getElementById('reg-researcher-id').value = '';
  document.getElementById('reg-db-match-banner').classList.add('hidden');
  clearTimeout(_lookupTimer);
  const q = val.trim();
  if (!q) {
    document.getElementById('reg-name-suggestions').classList.add('hidden');
    return;
  }
  _lookupTimer = setTimeout(async () => {
    const res = await apiGet(`/api/auth/lookup-name?name=${encodeURIComponent(q)}`);
    const items = res.items || [];
    const box = document.getElementById('reg-name-suggestions');
    if (!items.length) {
      box.classList.add('hidden');
      return;
    }
    box.classList.remove('hidden');
    box.innerHTML = items
      .map((r) => {
        const kws = (r.keywords || []).slice(0, 3).join('・');
        const encoded = encodeURIComponent(JSON.stringify(r));
        return `
          <div class="suggestion-row" onclick="selectRegisterResearcher('${encoded}')">
            <strong>${escapeHtml(r.name)}</strong>
            <span class="type-badge" style="margin-left:6px;">${escapeHtml(r.dept_short)}</span>
            <div style="color:#64748b;font-size:11.5px;margin-top:2px;">
              ${escapeHtml(r.department)} ${kws ? `／ 専門：${escapeHtml(kws)}` : ''}
            </div>
          </div>
        `;
      })
      .join('');
  }, 180);
}

function selectRegisterResearcher(encoded) {
  const r = JSON.parse(decodeURIComponent(encoded));
  document.getElementById('reg-name').value = r.name;
  document.getElementById('reg-researcher-id').value = r.id;
  document.getElementById('reg-dept').value = r.department || r.dept_short || '福井大学';
  document.getElementById('reg-name-suggestions').classList.add('hidden');

  const banner = document.getElementById('reg-db-match-banner');
  const kws = (r.keywords || []).slice(0, 4).join('・') || r.specialty || '登録済';
  banner.classList.remove('hidden');
  banner.innerHTML = `<strong>✓ 福井大学DBの研究者データと連携しました</strong><br>専門・キーワード：${escapeHtml(kws)}`;
}

function applyLoggedInUser(user) {
  state.user = user;
  localStorage.setItem('fukui_matching_user', JSON.stringify(user));
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('header-user-label').textContent = `👤 ${user.name}（${user.dept_short || user.department || '福井大学'}）`;
  renderMyDbProfileBox(user);
}

function renderMyDbProfileBox(user) {
  const box = document.getElementById('my-db-profile-box');
  if (!user) {
    box.innerHTML = '';
    return;
  }
  if (user.db_matched) {
    const kws = (user.keywords || []).slice(0, 5);
    box.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
        <div>
          <span class="type-badge" style="background:#dcfce7;color:#166534;">✓ DB研究情報と連携中</span>
          <div style="font-size:15.5px;font-weight:800;margin-top:4px;">${escapeHtml(user.name)}</div>
        </div>
        ${
          user.researcher_id
            ? `<button class="outline-action-btn" style="padding:4px 9px;font-size:11.5px;" onclick="openResearcherDetail('${escapeHtml(user.researcher_id)}')">自分のDB情報</button>`
            : ''
        }
      </div>
      <div style="font-size:12px;color:#475569;margin-top:2px;">${escapeHtml(user.department)}</div>
      ${
        kws.length
          ? `<div class="keyword-pills mt-8">${kws.map((k) => `<span class="kw-pill">${escapeHtml(k)}</span>`).join('')}</div>`
          : ''
      }
      <div style="font-size:11.5px;color:#1c56c9;font-weight:700;margin-top:6px;">
        💡 あなたのDB研究実績・専門分野をもとに、相乗効果のある共同研究者・公募・貸出機器をDeepSeekが検索します。
      </div>
    `;
  } else {
    box.innerHTML = `
      <div style="font-size:14px;font-weight:800;">👤 ${escapeHtml(user.name)}（${escapeHtml(user.department)}）</div>
      <div style="font-size:12px;color:#64748b;margin-top:2px;">入力された研究内容をもとにDeepSeekが検索します。</div>
    `;
  }
}

async function handleLogin(e) {
  e.preventDefault();
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value;
  const res = await apiPost('/api/auth/login', { email, password });
  if (!res.ok) {
    showAuthError(res.error || 'ログインに失敗しました');
    return;
  }
  applyLoggedInUser(res.user);
  showToast(`${res.user.name} 先生の研究データと連携しました！`);
  await runAISearch(false);
}

async function handleRegister(e) {
  e.preventDefault();
  const name = document.getElementById('reg-name').value.trim();
  const researcher_id = document.getElementById('reg-researcher-id').value.trim();
  const department = document.getElementById('reg-dept').value.trim();
  const role = document.getElementById('reg-role').value.trim();
  const email = document.getElementById('reg-email').value.trim();
  const password = document.getElementById('reg-password').value;

  const res = await apiPost('/api/auth/register', { name, researcher_id, department, role, email, password });
  if (!res.ok) {
    showAuthError(res.error || '登録に失敗しました');
    return;
  }
  applyLoggedInUser(res.user);
  if (res.user.db_matched && res.user.keywords?.length) {
    document.getElementById('research-input').value = `${res.user.keywords.slice(0, 3).join('・')}を活かした分野横断型共同研究`;
  }
  showToast(`${res.user.name} さんの研究情報をもとにAI提案を開始します！`);
  await runAISearch(false);
}

function handleLogout() {
  state.user = null;
  localStorage.removeItem('fukui_matching_user');
  document.getElementById('login-screen').classList.remove('hidden');
  showToast('ログアウトしました');
}

// ============================================================================
// ステータス・画面切り替え
// ============================================================================
function getLocalLikes() {
  try {
    return JSON.parse(localStorage.getItem('fukui_matching_likes') || '[]');
  } catch (e) {
    return [];
  }
}

function saveLocalLikes(items) {
  try {
    localStorage.setItem('fukui_matching_likes', JSON.stringify(items));
  } catch (e) {}
}

function getLocalHistory() {
  try {
    return JSON.parse(localStorage.getItem('fukui_matching_history') || '[]');
  } catch (e) {
    return [];
  }
}

function saveLocalHistory(items) {
  try {
    localStorage.setItem('fukui_matching_history', JSON.stringify(items.slice(0, 30)));
  } catch (e) {}
}

function mergeByKey(serverItems, localItems, keyField = 'key') {
  const map = new Map();
  for (const item of [...serverItems, ...localItems]) {
    if (item && item[keyField] && !map.has(item[keyField])) {
      map.set(item[keyField], item);
    }
  }
  return Array.from(map.values());
}

async function refreshStatusAndLikes() {
  try {
    const [st, likesRes] = await Promise.all([apiGet('/api/status'), apiGet('/api/likes')]);
    const mergedLikes = mergeByKey(likesRes.items || [], getLocalLikes(), 'key');
    saveLocalLikes(mergedLikes);
    state.likedItems = mergedLikes;
    state.likedKeys = new Set(state.likedItems.map((x) => x.key));
    updateLikesBadges(state.likedItems.length);
    const histCount = Math.max(st.history_count || 0, getLocalHistory().length);
    updateHistoryBadges(histCount);
  } catch (e) {
    console.error(e);
  }
}

function updateLikesBadges(count) {
  document.getElementById('header-likes-count').textContent = count;
  document.getElementById('sidebar-likes-count').textContent = count;
}

function updateHistoryBadges(count) {
  document.getElementById('header-history-count').textContent = count;
  document.getElementById('sidebar-history-count').textContent = count;
}

function switchView(viewName) {
  document.querySelectorAll('.sidebar-nav .nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === viewName);
  });
  document.querySelectorAll('.view-section').forEach((sec) => {
    sec.classList.toggle('active', sec.id === `view-${viewName}`);
  });
  if (viewName === 'likes') {
    loadAndRenderLikes();
  } else if (viewName === 'history') {
    loadAndRenderHistory();
  }
}

function onToggleCheckCard() {
  const chkR = document.getElementById('chk-researchers').checked;
  const chkE = document.getElementById('chk-equipment').checked;
  document.getElementById('card-chk-researchers').classList.toggle('selected', chkR);
  document.getElementById('card-chk-equipment').classList.toggle('selected', chkE);

  document.getElementById('section-researchers').style.display = chkR ? 'block' : 'none';
  document.getElementById('section-grants').style.display = chkR ? 'block' : 'none';
  document.getElementById('section-equipment').style.display = chkE ? 'block' : 'none';

  document.getElementById('jump-researchers').style.display = chkR ? 'inline-flex' : 'none';
  document.getElementById('jump-grants').style.display = chkR ? 'inline-flex' : 'none';
  document.getElementById('jump-equipment').style.display = chkE ? 'inline-flex' : 'none';

  const dualGrid = document.getElementById('results-dual-grid');
  if (!chkR && chkE) {
    dualGrid.classList.add('single-col');
  } else {
    dualGrid.classList.remove('single-col');
  }
}

function setSampleQuery(text) {
  document.getElementById('research-input').value = text;
  runAISearch(false);
}

async function runAISearch(isInitial = false) {
  const chkR = document.getElementById('chk-researchers').checked;
  const chkE = document.getElementById('chk-equipment').checked;
  const researchContent = document.getElementById('research-input').value.trim();
  const userCat = document.getElementById('eq-user-category').value;

  if (!chkR && !chkE) {
    showToast('「共同研究者＋公募を探す」または「貸出機器を探す」にチェックを入れてください');
    return;
  }

  onToggleCheckCard();

  const btn = document.getElementById('btn-run-search');
  btn.disabled = true;
  btn.textContent = 'DeepSeek AIで提案を作成中...';

  try {
    const res = await apiPost('/api/ai/search', {
      find_researchers: chkR,
      find_equipment: chkE,
      research_content: researchContent,
      user_category: userCat,
      save_history: !isInitial,
      user_email: state.user?.email || '',
      user_name: state.user?.name || '',
      user_researcher_id: state.user?.researcher_id || '',
    });

    state.researchers = res.researchers || [];
    state.grants = res.grants || [];
    state.equipment = res.equipment || [];
    if (!isInitial && researchContent) {
      const localHist = getLocalHistory();
      const newEntry = {
        id: 'local_' + Date.now(),
        research_content: researchContent,
        find_researchers: chkR,
        find_equipment: chkE,
        user_category: userCat,
        engine: res.engine || 'AI推論',
        researchers: state.researchers,
        grants: state.grants,
        equipment: state.equipment,
        created_at: new Date().toISOString().slice(0, 16).replace('T', ' '),
      };
      saveLocalHistory([newEntry, ...localHist]);
    }
    const histCount = Math.max(res.history_count || 0, getLocalHistory().length);
    updateHistoryBadges(histCount);

    renderSearchResults();
    if (!isInitial) showToast('あなたの研究データと研究内容をもとにAI提案を更新しました');
  } catch (e) {
    console.error(e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'AIで提案する';
  }
}

function renderSearchResults() {
  const rLen = state.researchers.length;
  const gLen = state.grants.length;
  const eLen = state.equipment.length;

  document.getElementById('count-researchers').textContent = `${rLen}名`;
  document.getElementById('jump-r-count').textContent = `${rLen}名`;
  const rContainer = document.getElementById('list-researchers');
  const letters = ['A', 'B', 'C', 'D', 'E'];
  rContainer.innerHTML = state.researchers
    .map((r, idx) => renderResearcherCard(r, letters[idx] || 'R'))
    .join('');

  document.getElementById('count-grants').textContent = `${gLen}件`;
  document.getElementById('jump-g-count').textContent = `${gLen}件`;
  const gContainer = document.getElementById('list-grants');
  gContainer.innerHTML = state.grants.map((g) => renderGrantCard(g)).join('');

  document.getElementById('count-equipment').textContent = `${eLen}件`;
  document.getElementById('jump-e-count').textContent = `${eLen}件`;
  const eContainer = document.getElementById('list-equipment');
  eContainer.innerHTML = state.equipment.map((eq) => renderEquipmentCard(eq)).join('');
}

function renderResearcherCard(r, badgeLetter = 'A') {
  const key = `researcher:${r.id}`;
  const isLiked = state.likedKeys.has(key);
  const kws = (r.keywords || []).slice(0, 4);
  const encodedItem = encodeURIComponent(JSON.stringify(r));

  return `
    <div class="item-card">
      <div class="card-top-row">
        <div class="r-identity-group">
          <div class="avatar-circle">${escapeHtml(badgeLetter)}</div>
          <div style="flex:1;min-width:160px;">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
              <span class="r-name-link" onclick="openResearcherDetail('${escapeHtml(r.id)}')">${escapeHtml(r.name)}</span>
              <span class="type-badge">${escapeHtml(r.dept_short || '福井大学')}</span>
            </div>
            <div class="dept-line">${escapeHtml(r.department)}</div>
            <div class="keyword-pills">
              ${kws.map((k) => `<span class="kw-pill">${escapeHtml(k)}</span>`).join('')}
            </div>
          </div>
        </div>
        <button class="like-btn ${isLiked ? 'liked' : ''}" data-like-key="${escapeHtml(key)}" onclick="toggleLike('researcher', '${escapeHtml(r.id)}', '${encodedItem}')">
          <span>${isLiked ? '♥ いいね済み' : '♡ いいね'}</span>
        </button>
      </div>

      <div class="role-row">
        <span class="role-label">役割案</span>
        <span class="role-title">${escapeHtml(r.proposed_role || 'データ解析・共同研究')}</span>
      </div>

      <div class="ai-reason-box">
        <span>💡</span>
        <div>
          <span class="ai-reason-label">AIの推薦理由：</span>
          <span>${escapeHtml(r.ai_reason)}</span>
        </div>
      </div>

      <div class="card-footer-bar">
        <div>
          <span>📄 根拠：${escapeHtml(r.evidence_label || '福井大学研究DB')}</span>
          &nbsp;|&nbsp;
          <span>👤 参加意向：未確認</span>
        </div>
        <div class="card-footer-actions">
          <button class="outline-action-btn" onclick="openResearcherDetail('${escapeHtml(r.id)}')">詳細を見る</button>
        </div>
      </div>
    </div>
  `;
}

function renderGrantCard(g) {
  const key = `grant:${g.id}`;
  const isLiked = state.likedKeys.has(key);
  const encodedItem = encodeURIComponent(JSON.stringify(g));
  const fetchedDate = (g.fetched_date || '2026-10-28').slice(0, 10).replace(/-/g, '.');

  return `
    <div class="item-card">
      <div class="card-top-row">
        <div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <h3 style="font-size:17px;font-weight:800;">${escapeHtml(g.title)}</h3>
            <span class="type-badge">${escapeHtml(g.badge || '研究助成')}</span>
          </div>
          <div style="font-size:13px;color:#334155;font-weight:600;margin-top:3px;">
            ${escapeHtml(g.amount_display)} &nbsp;|&nbsp; 配分機関：${escapeHtml(g.agency || g.source || '要確認')}
          </div>
        </div>
        <button class="like-btn ${isLiked ? 'liked' : ''}" data-like-key="${escapeHtml(key)}" onclick="toggleLike('grant', '${escapeHtml(g.id)}', '${encodedItem}')">
          <span>${isLiked ? '♥ いいね済み' : '♡ いいね'}</span>
        </button>
      </div>

      <div class="deadline-3grid">
        <div class="deadline-box">
          <div class="dl-label">事前連絡期限</div>
          <div class="dl-value">${escapeHtml(g.prior_contact_short)}</div>
        </div>
        <div class="deadline-box internal-highlight">
          <div class="dl-label">学内締切</div>
          <div class="dl-value">${escapeHtml(g.internal_deadline_short)}</div>
        </div>
        <div class="deadline-box">
          <div class="dl-label">募集元締切</div>
          <div class="dl-value">${escapeHtml(g.deadline_short)}</div>
        </div>
      </div>

      <div class="ai-reason-box">
        <span>💡</span>
        <div>
          <span class="ai-reason-label">AIの推薦理由：</span>
          <span>${escapeHtml(g.ai_reason)}</span>
        </div>
      </div>

      <div class="warning-callout-box">
        <span class="warning-icon">!</span>
        <div>
          <div class="warning-title">応募条件：要確認</div>
          <div>${escapeHtml(g.eligibility_warning || g.eligibility_check_status || '代表者要件・学内推薦枠を原典で確認してください')}</div>
        </div>
      </div>

      <div class="card-footer-bar">
        <div>
          📄 情報源：${escapeHtml(g.agency || g.source || '公募情報')} （確認 ${escapeHtml(fetchedDate)}）
        </div>
        <div class="card-footer-actions">
          ${
            g.url
              ? `<a class="outline-action-btn" href="${escapeHtml(g.url)}" target="_blank" rel="noopener">原典を見る ↗</a>`
              : ''
          }
        </div>
      </div>
    </div>
  `;
}

function renderEquipmentCard(eq) {
  const key = `equipment:${eq.id}`;
  const isLiked = state.likedKeys.has(key);
  const encodedItem = encodeURIComponent(JSON.stringify(eq));

  return `
    <div class="item-card">
      <div class="card-top-row">
        <div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="type-badge">機器番号 ${escapeHtml(eq.eq_number)}</span>
            <h3 style="font-size:16.5px;font-weight:800;">${escapeHtml(eq.name)}</h3>
          </div>
          <div style="font-size:13px;color:#1c56c9;font-weight:700;margin-top:3px;">
            基本料金：${escapeHtml(eq.fee_display)} （${escapeHtml(eq.rate_condition)}）
          </div>
        </div>
        <button class="like-btn ${isLiked ? 'liked' : ''}" data-like-key="${escapeHtml(key)}" onclick="toggleLike('equipment', '${escapeHtml(eq.id)}', '${encodedItem}')">
          <span>${isLiked ? '♥ いいね済み' : '♡ いいね'}</span>
        </button>
      </div>

      <div class="role-row">
        <span class="role-label">利用目的案</span>
        <span class="role-title" style="font-size:15.5px;">${escapeHtml(eq.proposed_purpose || '研究試料の精密測定・解析')}</span>
      </div>

      <div class="ai-reason-box">
        <span>💡</span>
        <div>
          <span class="ai-reason-label">AIの推薦理由：</span>
          <span>${escapeHtml(eq.ai_reason)}</span>
        </div>
      </div>

      <div class="warning-callout-box">
        <span class="warning-icon">!</span>
        <div>
          <div class="warning-title">利用条件・確認事項</div>
          <div>${escapeHtml(eq.check_notice || '適合試料・予約状況・持ち出し可否は管理部門へ事前確認してください')}</div>
        </div>
      </div>

      <div class="card-footer-bar">
        <div>📄 出典：${escapeHtml(eq.source_doc || '機器利用料一覧（別表）')} (p.${escapeHtml(eq.source_page || '1')})</div>
      </div>
    </div>
  `;
}

// ============================================================================
// いいね機能
// ============================================================================
async function toggleLike(itemType, itemId, encodedItem) {
  const item = JSON.parse(decodeURIComponent(encodedItem));
  const researchContext = document.getElementById('research-input')?.value || '';
  const res = await apiPost('/api/likes/toggle', {
    item_type: itemType,
    item_id: itemId,
    item,
    research_context: researchContext,
    user_email: state.user?.email || '',
  });

  if (res.liked) {
    state.likedKeys.add(res.key);
    const localLikes = getLocalLikes().filter((x) => x.key !== res.key);
    localLikes.unshift({
      key: res.key,
      item_type: itemType,
      item_id: itemId,
      item,
      research_context: researchContext,
      created_at: new Date().toISOString().slice(0, 16).replace('T', ' '),
    });
    saveLocalLikes(localLikes);
    showToast('♥ いいね一覧に保存しました');
  } else {
    state.likedKeys.delete(res.key);
    saveLocalLikes(getLocalLikes().filter((x) => x.key !== res.key));
    showToast('いいねを解除しました');
  }

  updateLikesBadges(Math.max(res.likes_count || 0, getLocalLikes().length));

  document.querySelectorAll(`[data-like-key="${res.key}"]`).forEach((btn) => {
    btn.classList.toggle('liked', res.liked);
    btn.innerHTML = `<span>${res.liked ? '♥ いいね済み' : '♡ いいね'}</span>`;
  });

  if (document.getElementById('view-likes').classList.contains('active')) {
    loadAndRenderLikes();
  }
}

async function loadAndRenderLikes() {
  const res = await apiGet('/api/likes');
  const merged = mergeByKey(res.items || [], getLocalLikes(), 'key');
  saveLocalLikes(merged);
  state.likedItems = merged;
  state.likedKeys = new Set(state.likedItems.map((x) => x.key));
  updateLikesBadges(state.likedItems.length);
  renderLikesView();
}

function filterLikes(filterType) {
  state.likesFilter = filterType;
  document.querySelectorAll('.filter-tabs .filter-tab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.filter === filterType);
  });
  renderLikesView();
}

function renderLikesView() {
  const container = document.getElementById('likes-container');
  const filtered = state.likedItems.filter(
    (entry) => state.likesFilter === 'all' || entry.item_type === state.likesFilter
  );

  if (!filtered.length) {
    container.innerHTML = `
      <div class="panel-card">
        <p class="panel-subcaption">まだ「いいね」した項目がありません。提案された共同研究者・公募・貸出機器の「♡ いいね」ボタンを押すとここに保存されます。</p>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered
    .map((entry) => {
      if (entry.item_type === 'researcher') {
        return renderResearcherCard(entry.item, entry.item.name?.slice(0, 1) || 'R');
      } else if (entry.item_type === 'grant') {
        return renderGrantCard(entry.item);
      } else {
        return renderEquipmentCard(entry.item);
      }
    })
    .join('');
}

// ============================================================================
// 相談結果履歴機能
// ============================================================================
async function loadAndRenderHistory() {
  const res = await apiGet('/api/history');
  const serverItems = res.items || [];
  const localItems = getLocalHistory();
  // Deduplicate by created_at + research_content
  const seen = new Set();
  const merged = [];
  for (const h of [...serverItems, ...localItems]) {
    const sig = `${h.created_at || ''}__${h.research_content || ''}`;
    if (!seen.has(sig)) {
      seen.add(sig);
      merged.push(h);
    }
  }
  state.historyItems = merged;
  updateHistoryBadges(state.historyItems.length);
  const container = document.getElementById('history-container');

  if (!state.historyItems.length) {
    container.innerHTML = `
      <div class="panel-card">
        <p class="panel-subcaption">まだ相談結果履歴がありません。「研究連携を探す」画面で「AIで提案する」を押すと、自動的にここへ履歴が保存されます。</p>
      </div>
    `;
    return;
  }

  container.innerHTML = state.historyItems
    .map((h, idx) => {
      const rNames = (h.researchers || []).map((r) => `${escapeHtml(r.name)}（${escapeHtml(r.dept_short || '福井大学')}）`);
      const gTitles = (h.grants || []).map((g) => `${escapeHtml(g.title)}（締切: ${escapeHtml(g.deadline_short)}）`);
      const eNames = (h.equipment || []).map((e) => `[${escapeHtml(e.eq_number)}] ${escapeHtml(e.name)}`);

      return `
        <div class="panel-card">
          <div class="card-top-row">
            <div>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span class="type-badge">🕒 ${escapeHtml(h.created_at)}</span>
              </div>
              <h3 style="font-size:17.5px;font-weight:800;margin-top:8px;">研究内容：「${escapeHtml(h.research_content)}」</h3>
            </div>
            <div class="card-footer-actions">
              <button class="primary-action-btn" onclick="restoreHistoryItem(${idx})">
                この相談結果を開く
              </button>
              <button class="outline-action-btn" onclick="deleteHistoryItem('${escapeHtml(h.id)}')">
                削除
              </button>
            </div>
          </div>

          <div class="history-summary-grid">
            <div class="history-col-box">
              <div class="history-col-title">👤 提案された共同研究者 (${rNames.length}名)</div>
              ${
                rNames.length
                  ? rNames.map((n) => `<div class="history-mini-item">・${n}</div>`).join('')
                  : `<div class="history-mini-item" style="color:#94a3b8;">対象外</div>`
              }
            </div>
            <div class="history-col-box">
              <div class="history-col-title">📄 提案された公募 (${gTitles.length}件)</div>
              ${
                gTitles.length
                  ? gTitles.map((t) => `<div class="history-mini-item">・${t}</div>`).join('')
                  : `<div class="history-mini-item" style="color:#94a3b8;">対象外</div>`
              }
            </div>
            <div class="history-col-box">
              <div class="history-col-title">🔬 提案された貸出機器 (${eNames.length}件)</div>
              ${
                eNames.length
                  ? eNames.map((n) => `<div class="history-mini-item">・${n}</div>`).join('')
                  : `<div class="history-mini-item" style="color:#94a3b8;">対象外</div>`
              }
            </div>
          </div>
        </div>
      `;
    })
    .join('');
}

function restoreHistoryItem(idx) {
  const h = state.historyItems[idx];
  if (!h) return;
  document.getElementById('research-input').value = h.research_content || '';
  document.getElementById('chk-researchers').checked = Boolean(h.find_researchers);
  document.getElementById('chk-equipment').checked = Boolean(h.find_equipment);
  if (h.user_category) {
    document.getElementById('eq-user-category').value = h.user_category;
  }
  onToggleCheckCard();

  state.researchers = h.researchers || [];
  state.grants = h.grants || [];
  state.equipment = h.equipment || [];

  renderSearchResults();
  switchView('search');
  showToast('過去の相談結果を復元しました');
}

async function deleteHistoryItem(id) {
  const target = state.historyItems.find((x) => x.id === id);
  const res = await apiPost('/api/history/delete', { id });
  if (target) {
    saveLocalHistory(
      getLocalHistory().filter(
        (x) => x.id !== id && !(x.created_at === target.created_at && x.research_content === target.research_content)
      )
    );
  }
  updateHistoryBadges(Math.max(res.history_count || 0, getLocalHistory().length));
  await loadAndRenderHistory();
  showToast('履歴を削除しました');
}

async function clearAllHistory() {
  const res = await apiPost('/api/history/delete', { id: 'all' });
  saveLocalHistory([]);
  updateHistoryBadges(res.history_count || 0);
  await loadAndRenderHistory();
  showToast('相談結果履歴をすべてクリアしました');
}

// ============================================================================
// 研究者詳細モーダル
// ============================================================================
async function openResearcherDetail(rid) {
  const modal = document.getElementById('modal-researcher-detail');
  const body = document.getElementById('modal-r-body');
  modal.classList.remove('hidden');
  body.innerHTML = `<p class="panel-subcaption">読み込み中...</p>`;

  const r = await apiGet(`/api/researchers/${encodeURIComponent(rid)}`);
  document.getElementById('modal-r-title').textContent = `${r.name}（${r.dept_short}）`;

  body.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;">
      <div>
        <div style="font-size:14px;font-weight:700;">所属：${escapeHtml(r.department)}</div>
        <div style="font-size:12.5px;color:#475569;margin-top:3px;">
          在籍確認：${escapeHtml(r.enrollment_status)} &nbsp;|&nbsp; 根拠：${escapeHtml(r.evidence_label)}
        </div>
      </div>
      <div style="display:flex;gap:8px;">
        ${r.profile_url ? `<a class="outline-action-btn" href="${escapeHtml(r.profile_url)}" target="_blank" rel="noopener">公式プロフィール ↗</a>` : ''}
        ${r.researchmap_url ? `<a class="outline-action-btn" href="${escapeHtml(r.researchmap_url)}" target="_blank" rel="noopener">researchmap ↗</a>` : ''}
      </div>
    </div>

    <div class="keyword-pills mt-12">
      ${(r.all_keywords || []).map((k) => `<span class="kw-pill">${escapeHtml(k)}</span>`).join('')}
    </div>

    <h4 style="font-size:14px;font-weight:800;margin:16px 0 8px;">研究課題 (${(r.projects || []).length}件)</h4>
    <div style="max-height:160px;overflow-y:auto;border:1px solid #e2e8f0;border-radius:8px;">
      <table class="data-table">
        <thead><tr><th>開始年</th><th>研究課題名</th><th>配分機関</th></tr></thead>
        <tbody>
          ${
            (r.projects || []).length
              ? r.projects.map((p) => `<tr><td>${escapeHtml(p.start_date)}</td><td>${escapeHtml(p.title)}</td><td>${escapeHtml(p.agency)}</td></tr>`).join('')
              : `<tr><td colspan="3">記載なし</td></tr>`
          }
        </tbody>
      </table>
    </div>

    <h4 style="font-size:14px;font-weight:800;margin:16px 0 8px;">主要論文 (${(r.papers || []).length}件)</h4>
    <div style="max-height:160px;overflow-y:auto;border:1px solid #e2e8f0;border-radius:8px;">
      <table class="data-table">
        <thead><tr><th>年月</th><th>論文名</th><th>掲載誌</th></tr></thead>
        <tbody>
          ${
            (r.papers || []).length
              ? r.papers.map((p) => `<tr><td>${escapeHtml(p.pub_date)}</td><td>${escapeHtml(p.title)}</td><td>${escapeHtml(p.journal)}</td></tr>`).join('')
              : `<tr><td colspan="3">記載なし</td></tr>`
          }
        </tbody>
      </table>
    </div>
  `;
}

function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
}
