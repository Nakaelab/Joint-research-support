import hashlib
import http.server
import json
import os
import re
import shutil
import sqlite3
import ssl
import tempfile
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DB_PATH = os.path.join(BASE_DIR, 'fukui_matching.db')
BUNDLED_DB_PATH = os.path.join(BASE_DIR, 'api', 'fukui_data.db')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
PORT = int(os.environ.get('PORT', 8000))
_TABLES_ENSURED = False


def resolve_db_path():
    # On Vercel or read-only serverless environments, copy bundled DB to /tmp so SQLite is writable
    if os.environ.get('VERCEL') or not os.path.exists(LOCAL_DB_PATH):
        tmp_db = os.path.join(tempfile.gettempdir(), 'fukui_matching_runtime.db')
        if not os.path.exists(tmp_db) and os.path.exists(BUNDLED_DB_PATH):
            shutil.copyfile(BUNDLED_DB_PATH, tmp_db)
        return tmp_db
    return LOCAL_DB_PATH


def get_db():
    global _TABLES_ENSURED
    db_path = resolve_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if not _TABLES_ENSURED:
        _TABLES_ENSURED = True
        init_tables_on_conn(conn)
    return conn


def hash_pw(pw):
    return hashlib.sha256((pw or '').encode('utf-8')).hexdigest()


def normalize_name(name):
    if not name:
        return ''
    return re.sub(r'[\s　]+', '', name).strip()


def find_researcher_by_name(conn, name, researcher_id=''):
    if researcher_id:
        r = conn.execute('SELECT * FROM researchers WHERE id = ?', (researcher_id,)).fetchone()
        if r:
            return format_researcher_row(r)

    norm = normalize_name(name)
    if not norm:
        return None

    # Exact match or normalized space-insensitive match, prioritizing high priority_score
    rows = conn.execute(
        'SELECT * FROM researchers WHERE REPLACE(REPLACE(name, " ", ""), "　", "") = ? ORDER BY priority_score DESC LIMIT 1',
        (norm,),
    ).fetchall()
    if rows:
        return format_researcher_row(rows[0])

    # Partial match fallback
    rows = conn.execute(
        'SELECT * FROM researchers WHERE REPLACE(REPLACE(name, " ", ""), "　", "") LIKE ? ORDER BY priority_score DESC LIMIT 1',
        (f'%{norm}%',),
    ).fetchall()
    if rows:
        return format_researcher_row(rows[0])
    return None


def enrich_user_with_db_profile(conn, user_dict):
    u = dict(user_dict)
    prof = find_researcher_by_name(conn, u.get('name', ''), u.get('researcher_id', ''))
    if prof:
        u['researcher_id'] = prof['id']
        u['db_matched'] = True
        u['department'] = prof.get('department') or u.get('department') or '福井大学'
        u['dept_short'] = prof.get('dept_short') or '福井大学'
        u['keywords'] = prof.get('keywords', [])
        u['all_keywords'] = prof.get('all_keywords', [])
        u['specialty'] = prof.get('specialty', '')
        u['summary'] = (prof.get('matching_summary') or prof.get('summary') or '')[:400]
        u['evidence_label'] = prof.get('evidence_label', '')
    else:
        u['db_matched'] = False
        u['keywords'] = []
        u['all_keywords'] = []
        u['specialty'] = ''
        u['summary'] = ''
    return u


def init_tables_on_conn(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            name TEXT,
            department TEXT,
            role TEXT,
            password_hash TEXT,
            created_at TEXT,
            researcher_id TEXT DEFAULT ''
        )
    ''')
    try:
        conn.execute("ALTER TABLE users ADD COLUMN researcher_id TEXT DEFAULT ''")
    except Exception:
        pass

    # Seed default demo user linked to a real researcher in DB if not exists
    existing = conn.execute("SELECT email FROM users WHERE email = 'honda@u-fukui.ac.jp'").fetchone()
    if not existing:
        conn.execute(
            'INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                'honda@u-fukui.ac.jp',
                '本田 知己',
                '福井大学 工学系部門 工学領域 機械工学講座 教授',
                '教授',
                hash_pw('fukui2026'),
                datetime.now().strftime('%Y-%m-%d %H:%M'),
                'r_fukui_26',
            ),
        )

    conn.execute('''
        CREATE TABLE IF NOT EXISTS likes (
            key TEXT PRIMARY KEY,
            item_type TEXT,
            item_id TEXT,
            item_json TEXT,
            research_context TEXT,
            created_at TEXT,
            user_email TEXT DEFAULT ''
        )
    ''')
    try:
        conn.execute("ALTER TABLE likes ADD COLUMN user_email TEXT DEFAULT ''")
    except Exception:
        pass

    conn.execute('''
        CREATE TABLE IF NOT EXISTS consultation_history (
            id TEXT PRIMARY KEY,
            research_content TEXT,
            find_researchers INTEGER,
            find_equipment INTEGER,
            user_category TEXT,
            engine TEXT,
            researchers_json TEXT,
            grants_json TEXT,
            equipment_json TEXT,
            created_at TEXT,
            user_email TEXT DEFAULT ''
        )
    ''')
    try:
        conn.execute("ALTER TABLE consultation_history ADD COLUMN user_email TEXT DEFAULT ''")
    except Exception:
        pass

    conn.commit()


def ensure_tables():
    conn = get_db()
    conn.close()


def get_settings():
    conn = get_db()
    rows = conn.execute('SELECT key, value FROM app_settings').fetchall()
    conn.close()
    settings = {r['key']: r['value'] for r in rows}
    env_key = os.environ.get('DEEPSEEK_API_KEY', '')
    if env_key and not settings.get('deepseek_api_key'):
        settings['deepseek_api_key'] = env_key
    return settings


def call_deepseek(system_prompt, user_prompt, temperature=0.3):
    settings = get_settings()
    api_key = settings.get('deepseek_api_key', '').strip()
    if not api_key:
        return None, 'AI推論'

    base_url = settings.get('deepseek_base_url', 'https://api.deepseek.com').rstrip('/')
    if not base_url.endswith('/chat/completions'):
        url = f'{base_url}/chat/completions'
    else:
        url = base_url

    model = settings.get('deepseek_model', 'deepseek-chat') or 'deepseek-chat'

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': temperature,
        'response_format': {'type': 'json_object'},
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=50, context=ctx) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            content = body['choices'][0]['message']['content']
            return json.loads(content), 'DeepSeek AI'
    except Exception:
        return None, 'AI推論'


def extract_terms(text):
    if not text:
        return []
    stop_words = {
        '研究', '開発', '解析', '分析', 'システム', 'による', '用いた',
        '関する', 'および', 'ため', 'こと', 'もの', '分野', '技術', '手法', '応用', '検討', '評価', '支援',
        '福井大学', '一般社団法人', '特定非営利活動法人', '学会', '論文実績', '研究課題'
    }
    tokens = []
    katakana = re.findall(r'[ァ-ヶー]{2,12}', text)
    kanji = re.findall(r'[一-龥]{2,6}', text)
    alnum = re.findall(r'[A-Za-z0-9\-]{2,12}', text)

    for word in katakana + kanji + alnum:
        w = word.lower()
        if w not in stop_words and w not in tokens:
            tokens.append(w)
        if len(word) == 4 and re.match(r'^[一-龥]{4}$', word):
            p1, p2 = word[:2], word[2:]
            for p in (p1, p2):
                if p not in stop_words and p not in tokens:
                    tokens.append(p)
        if 'センサー' in word and 'センサ' not in tokens:
            tokens.append('センサ')
            tokens.append('センサー')
            tokens.append('データ')
    return tokens[:35]


def classify_dept_short(dept_str, spec_str=''):
    combined = f'{dept_str} {spec_str}'
    if any(k in combined for k in ('情報', 'メディア', '知能', 'AI', 'データ', 'コンピュータ', '通信')):
        return '情報系'
    if any(k in combined for k in ('機械', '電気', '電子', '建築', '建設', '材料', '繊維', '応用物理', '化学', '工学', '原子力', '防災')):
        return '工学系'
    if any(k in combined for k in ('医学', '病院', '医療', '看護', '外科', '内科', '小児', 'こころ', '脳', '腫瘍', '臨床')):
        return '医学系'
    if any(k in combined for k in ('教育', '学校', '教科', 'スポーツ', '保健', '社会科', '人文')):
        return '教育・人文系'
    if any(k in combined for k in ('国際', '地域', '経済', '経営')):
        return '国際地域系'
    return '福井大学'


def format_researcher_row(r):
    d = dict(r)
    try:
        kws = json.loads(d.get('keywords_json') or '[]')
    except Exception:
        kws = []
    clean_kws = []
    for k in kws:
        for sub in re.split(r'[,、/／]+', k):
            s = sub.strip()
            if 1 < len(s) <= 18 and s not in clean_kws:
                clean_kws.append(s)
    if not clean_kws and d.get('specialty'):
        for sub in re.split(r'[/／、,]+', d['specialty']):
            s = sub.strip()
            if 1 < len(s) <= 18 and s not in ('研究課題', '論文実績', '福井大学') and s not in clean_kws:
                clean_kws.append(s)
    d['keywords'] = clean_kws[:5]
    d['all_keywords'] = clean_kws[:15]
    d['dept_short'] = classify_dept_short(d.get('department', ''), d.get('specialty', '') + ' ' + ' '.join(clean_kws))
    ev = d.get('evidence', '')
    if d.get('researchmap_url'):
        ev_label = 'researchmap・本人確認記録'
    elif 'pure.flib.u-fukui' in (d.get('profile_url') or ''):
        ev_label = 'Pure・本人確認記録'
    elif '教員公開情報' in ev or '公式' in (d.get('enrollment_status') or ''):
        ev_label = '公式教員一覧・本人確認記録'
    elif 'CiNii' in ev:
        ev_label = 'CiNii / KAKEN連携記録'
    else:
        ev_label = '福井大学研究DB'
    d['evidence_label'] = ev_label
    return d


def search_researchers_db(query='', exclude_id='', exclude_name='', limit=12):
    conn = get_db()
    terms = extract_terms(query)
    rows = conn.execute(
        "SELECT * FROM researchers WHERE matching_target = '対象' OR priority_score >= 40 ORDER BY priority_score DESC, id ASC LIMIT 600"
    ).fetchall()
    conn.close()

    norm_excl_name = normalize_name(exclude_name)
    scored = []
    for r in rows:
        if exclude_id and r['id'] == exclude_id:
            continue
        if norm_excl_name and normalize_name(r['name']) == norm_excl_name:
            continue

        item = format_researcher_row(r)
        kw_text = ' '.join(item.get('all_keywords', [])).lower()
        haystack = ' '.join([
            item.get('name', ''),
            item.get('department', ''),
            item.get('specialty', ''),
            item.get('summary', ''),
            item.get('matching_summary', ''),
            item.get('research_fields', ''),
            kw_text,
        ]).lower()

        score = item.get('priority_score', 0) * 0.1
        for t in terms:
            if t in kw_text:
                score += 45
            elif t in haystack:
                score += 20
        item['match_score'] = round(score, 1)
        scored.append(item)

    scored.sort(key=lambda x: x['match_score'], reverse=True)
    return scored[:limit]


def format_grant_row(g):
    d = dict(g)
    dl = d.get('deadline', '') or d.get('period_text', '')
    m_date = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', dl)
    if m_date:
        y, m, day = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        d['deadline_short'] = f'{m}/{day}'
        d['deadline_full'] = f'{y}/{m:02d}/{day:02d}'
        if not d.get('internal_deadline'):
            im, iday = (m, max(1, day - 12)) if day > 12 else (max(1, m - 1), 28 + day - 12)
            d['internal_deadline_short'] = f'{im}/{iday}'
        else:
            d['internal_deadline_short'] = d['internal_deadline']
        if not d.get('prior_contact_deadline'):
            pm, pday = (m, max(1, day - 19)) if day > 19 else (max(1, m - 1), 28 + day - 19)
            d['prior_contact_short'] = f'{pm}/{pday}'
        else:
            d['prior_contact_short'] = d['prior_contact_deadline']
    else:
        d['deadline_short'] = d.get('period_text') or '随時'
        d['deadline_full'] = d['deadline_short']
        d['internal_deadline_short'] = d.get('internal_deadline') or '要確認'
        d['prior_contact_short'] = '要確認'

    amt = d.get('max_amount_yen', '')
    if amt and str(amt).isdigit() and int(amt) > 0:
        val_man = int(amt) // 10000
        d['amount_display'] = f'上限 {val_man:,}万円（総額）'
    else:
        d['amount_display'] = '助成額：募集要項参照'

    cat = d.get('category', '研究助成')
    cat_clean = re.sub(r'[【】]', ' ', cat).strip().split()[0] if cat else '研究助成'
    d['badge'] = cat_clean[:8]
    return d


def search_grants_db(query='', limit=8):
    conn = get_db()
    rows = conn.execute('SELECT * FROM grants').fetchall()
    conn.close()

    terms = extract_terms(query)
    scored = []
    for r in rows:
        item = format_grant_row(r)
        haystack = ' '.join([
            item.get('title', ''),
            item.get('field', ''),
            item.get('summary', ''),
            item.get('agency', ''),
            item.get('category', ''),
            item.get('search_text', ''),
        ]).lower()

        score = 10
        if '研究' in (item.get('category') or '') or '助成' in (item.get('title') or ''):
            score += 15
        if item.get('deadline', '') >= '2026-10-30':
            score += 20
        for t in terms:
            if t in haystack:
                score += 25
        item['match_score'] = score
        scored.append(item)

    scored.sort(key=lambda x: (x['match_score'], x.get('deadline', '')), reverse=True)
    return scored[:limit]


def search_equipment_db(query='', user_category='学内', limit=8):
    conn = get_db()
    eq_rows = conn.execute('SELECT * FROM equipment').fetchall()
    rate_rows = conn.execute('SELECT * FROM equipment_rates').fetchall()
    conn.close()

    rates_by_eq = {}
    for rt in rate_rows:
        rates_by_eq.setdefault(rt['equipment_id'], []).append(dict(rt))

    terms = extract_terms(query)
    expanded_terms = list(terms)
    synonyms = {
        '表面': ['顕微鏡', 'プローブ', 'x線', '光電子', 'sem', 'tem', 'トライボロジー', '強度', '摩耗', 'レーザー', '分光'],
        'トライボロジー': ['顕微鏡', 'プローブ', '表面', '強度', '光電子', 'x線', 'レーザー'],
        '構造': ['x線', 'nmr', '核磁気共鳴', '単結晶', '質量分析', '電子顕微鏡'],
        '材料': ['顕微鏡', 'x線', '分光', '熱', '強度', '表面', 'icp', '元素', '薄膜', '半導体'],
        '生体': ['顕微鏡', 'レーザー', '質量分析', 'クロマト', '蛍光', '細胞', '遺伝子', 'nmr'],
        '医療': ['顕微鏡', '質量分析', '生体', 'レーザー', '画像', '分光', 'nmr'],
        '健康': ['レーザー', '分光', '顕微鏡', '質量分析', '生体'],
        'がん': ['顕微鏡', 'レーザー', '質量分析', '細胞', '遺伝子', '蛍光'],
        'センサー': ['オシロ', 'ネットワーク', '信号', 'スペクトラム', '半導体', 'プローブ', '表面', 'レーザー', '容量'],
        'センサ': ['オシロ', 'ネットワーク', '信号', 'スペクトラム', '半導体', 'プローブ', '表面', 'レーザー', '容量'],
        '通信': ['ネットワーク', 'アナライザ', '信号', 'アンテナ', 'スペクトラム'],
        '行動': ['センサー', '画像', '計測', '解析', 'レーザー', '三次元', '顕微鏡'],
        '化学': ['nmr', '核磁気共鳴', '質量分析', 'icp', 'クロマト', '赤外', '分光', 'x線'],
        '環境': ['icp', '発光', '質量分析', 'クロマト', '元素', 'x線', '電子顕微鏡'],
    }
    for k, syns in synonyms.items():
        if k in query.lower():
            for s in syns:
                if s not in expanded_terms:
                    expanded_terms.append(s)

    scored = []
    for e in eq_rows:
        item = dict(e)
        eq_rates = rates_by_eq.get(item['id'], [])
        item['rates'] = eq_rates

        matched_rate = None
        for rt in eq_rates:
            if user_category and user_category in (rt.get('user_category') or ''):
                matched_rate = rt
                break
        if not matched_rate and eq_rates:
            matched_rate = eq_rates[0]

        if matched_rate:
            base_yen = matched_rate.get('base_fee_yen', '')
            item['fee_display'] = matched_rate.get('base_raw') or (
                f"{int(base_yen):,}円/{matched_rate.get('base_unit', '回')}" if str(base_yen).isdigit() else '要確認'
            )
            item['rate_condition'] = f"{matched_rate.get('user_category', '')}・{matched_rate.get('method', '通常')}"
        else:
            item['fee_display'] = '別表参照（要確認）'
            item['rate_condition'] = '利用者区分による'

        haystack = ' '.join([
            item.get('name', ''),
            item.get('keywords', ''),
            item.get('spec_samples', ''),
            ' '.join(r.get('method', '') for r in eq_rates),
        ]).lower()

        score = 10
        for t in expanded_terms:
            if t in haystack:
                score += 30 if t in terms else 15

        item['match_score'] = score
        scored.append(item)

    scored.sort(key=lambda x: x['match_score'], reverse=True)
    return scored[:limit]


def generate_local_researchers(candidates, research_content, user_prof=None):
    role_templates = {
        '情報系': ['データ解析・AIモデル開発', '信号処理・アルゴリズム設計'],
        '工学系': ['計測環境の設計・データ収集', 'デバイス評価・構造解析'],
        '医学系': ['生体データ評価・臨床応用検証', '病態解析・医療データ検証'],
        '教育・人文系': ['フィールド実証・行動計測評価', '行動データ分析・実践評価'],
        '福井大学': ['学際領域の知見統合・検証', '共同研究の基盤評価'],
    }
    user_context = ''
    if user_prof and user_prof.get('db_matched'):
        u_kws = '・'.join(user_prof.get('keywords', [])[:2]) or user_prof.get('dept_short', '')
        user_context = f"{user_prof.get('name')}先生のご専門（{u_kws}）と組み合わせることで、"

    out = []
    for idx, c in enumerate(candidates[:5]):
        dept = c.get('dept_short', '福井大学')
        roles = role_templates.get(dept, role_templates['福井大学'])
        role = roles[idx % len(roles)]
        kws_str = '・'.join(c.get('keywords', [])[:3]) or c.get('specialty', '専門分野')[:18]
        item = dict(c)
        item['proposed_role'] = role
        item['ai_reason'] = f"{user_context}「{research_content[:26] or '本研究テーマ'}」に対し、{kws_str}の研究実績を活かして{role}を担当できる有力な共同研究者候補です。"
        out.append(item)
    return out


def generate_local_grants(candidates, researchers, research_content, user_prof=None):
    depts = list({r.get('dept_short', '') for r in researchers if r.get('dept_short')})
    if user_prof and user_prof.get('dept_short') and user_prof['dept_short'] not in depts:
        depts.insert(0, user_prof['dept_short'])

    kws = []
    if user_prof and user_prof.get('keywords'):
        kws.extend(user_prof['keywords'][:2])
    for r in researchers[:3]:
        kws.extend(r.get('keywords', [])[:2])
    kws_label = '・'.join(kws[:4]) if kws else '本研究分野'

    out = []
    for g in candidates[:3]:
        item = dict(g)
        field_label = item.get('field') or '対象分野'
        if len(depts) >= 2:
            reason = f"{'×'.join(depts[:2])}の分野横断連携（{kws_label}）と「{research_content[:24] or '本研究'}」の組み合わせが公募の目的（{field_label}）に合います。"
        else:
            reason = f"「{research_content[:24] or '本研究'}」および{kws_label}の研究内容が、{item.get('agency') or '本公募'}の対象分野（{field_label}）に合致します。"
        item['ai_reason'] = reason
        item['eligibility_warning'] = item.get('eligibility_check_status') or '代表者要件・学内推薦枠・応募資格を原典要項で確認'
        out.append(item)
    return out


def generate_local_equipment(candidates, research_content, user_prof=None):
    u_prefix = ''
    if user_prof and user_prof.get('db_matched') and user_prof.get('keywords'):
        u_prefix = f"{user_prof['name']}先生の研究（{'・'.join(user_prof['keywords'][:2])}）および"

    out = []
    for eq in candidates[:4]:
        item = dict(eq)
        name = item.get('name', '')
        item['proposed_purpose'] = f"「{research_content[:22] or '研究試料・デバイス'}」の精密測定・構造/特性評価"
        item['ai_reason'] = f"{u_prefix}「{research_content[:26] or '本研究テーマ'}」に必要な実験データの取得・検証に「{name}」が活用できます。"
        item['check_notice'] = f"{item.get('usage_status') or '適合試料要確認'} / {item.get('provision_type') or '貸出可否要確認'}（{item.get('operation_status') or '要事前予約確認'}）"
        out.append(item)
    return out


class MatchingHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/api/status':
            conn = get_db()
            likes_count = conn.execute('SELECT COUNT(*) FROM likes').fetchone()[0]
            history_count = conn.execute('SELECT COUNT(*) FROM consultation_history').fetchone()[0]
            conn.close()
            return self.send_json({
                'likes_count': likes_count,
                'history_count': history_count,
            })

        if path == '/api/auth/lookup-name':
            name_q = (qs.get('name', [''])[0]).strip()
            conn = get_db()
            norm = normalize_name(name_q)
            matches = []
            if norm:
                rows = conn.execute(
                    'SELECT * FROM researchers WHERE REPLACE(REPLACE(name, " ", ""), "　", "") LIKE ? ORDER BY priority_score DESC LIMIT 6',
                    (f'%{norm}%',),
                ).fetchall()
                matches = [format_researcher_row(r) for r in rows]
            conn.close()
            return self.send_json({'items': matches})

        if path == '/api/likes':
            conn = get_db()
            rows = conn.execute('SELECT * FROM likes ORDER BY created_at DESC').fetchall()
            conn.close()
            items = []
            for r in rows:
                d = dict(r)
                try:
                    d['item'] = json.loads(d.get('item_json') or '{}')
                except Exception:
                    d['item'] = {}
                items.append(d)
            return self.send_json({'items': items})

        if path == '/api/history':
            conn = get_db()
            rows = conn.execute('SELECT * FROM consultation_history ORDER BY created_at DESC LIMIT 50').fetchall()
            conn.close()
            items = []
            for r in rows:
                d = dict(r)
                for col in ('researchers', 'grants', 'equipment'):
                    try:
                        d[col] = json.loads(d.get(f'{col}_json') or '[]')
                    except Exception:
                        d[col] = []
                items.append(d)
            return self.send_json({'items': items})

        if path.startswith('/api/researchers/'):
            rid = urllib.parse.unquote(path.split('/api/researchers/', 1)[1])
            conn = get_db()
            r = conn.execute('SELECT * FROM researchers WHERE id = ?', (rid,)).fetchone()
            if not r:
                conn.close()
                return self.send_json({'error': 'Not found'}, 404)
            item = format_researcher_row(r)
            item['papers'] = [dict(p) for p in conn.execute('SELECT * FROM papers WHERE researcher_id = ? LIMIT 20', (rid,)).fetchall()]
            item['projects'] = [dict(p) for p in conn.execute('SELECT * FROM projects WHERE researcher_id = ? LIMIT 20', (rid,)).fetchall()]
            conn.close()
            return self.send_json(item)

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_json_body()

        if path == '/api/auth/login':
            email = (body.get('email') or '').strip().lower()
            password = body.get('password') or ''
            if not email or not password:
                return self.send_json({'ok': False, 'error': 'メールアドレスとパスワードを入力してください'}, 400)
            conn = get_db()
            user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            if not user or user['password_hash'] != hash_pw(password):
                conn.close()
                return self.send_json({'ok': False, 'error': 'メールアドレスまたはパスワードが正しくありません'}, 401)
            enriched = enrich_user_with_db_profile(conn, dict(user))
            conn.close()
            return self.send_json({'ok': True, 'user': enriched})

        if path == '/api/auth/register':
            email = (body.get('email') or '').strip().lower()
            name = (body.get('name') or '').strip()
            department = (body.get('department') or '福井大学').strip()
            role = (body.get('role') or '研究者').strip()
            password = body.get('password') or ''
            researcher_id = (body.get('researcher_id') or '').strip()
            if not email or not name or not password:
                return self.send_json({'ok': False, 'error': '氏名・メールアドレス・パスワードを入力してください'}, 400)

            conn = get_db()
            matched_prof = find_researcher_by_name(conn, name, researcher_id)
            if matched_prof:
                researcher_id = matched_prof['id']
                department = matched_prof.get('department') or department

            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            conn.execute(
                'INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?, ?)',
                (email, name, department, role, hash_pw(password), now, researcher_id),
            )
            conn.commit()
            user_row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            enriched = enrich_user_with_db_profile(conn, dict(user_row))
            conn.close()
            return self.send_json({'ok': True, 'user': enriched})

        if path == '/api/likes/toggle':
            item_type = body.get('item_type', '')
            item_id = body.get('item_id', '')
            item_data = body.get('item', {})
            research_context = body.get('research_context', '')
            user_email = body.get('user_email', '')
            key = f'{item_type}:{item_id}'

            conn = get_db()
            existing = conn.execute('SELECT key FROM likes WHERE key = ?', (key,)).fetchone()
            if existing:
                conn.execute('DELETE FROM likes WHERE key = ?', (key,))
                liked = False
            else:
                now = datetime.now().strftime('%Y-%m-%d %H:%M')
                conn.execute(
                    'INSERT OR REPLACE INTO likes VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (key, item_type, item_id, json.dumps(item_data, ensure_ascii=False), research_context, now, user_email),
                )
                liked = True
            conn.commit()
            likes_count = conn.execute('SELECT COUNT(*) FROM likes').fetchone()[0]
            conn.close()
            return self.send_json({'ok': True, 'key': key, 'liked': liked, 'likes_count': likes_count})

        if path == '/api/history/delete':
            hid = body.get('id', '')
            conn = get_db()
            if hid == 'all':
                conn.execute('DELETE FROM consultation_history')
            else:
                conn.execute('DELETE FROM consultation_history WHERE id = ?', (hid,))
            conn.commit()
            history_count = conn.execute('SELECT COUNT(*) FROM consultation_history').fetchone()[0]
            conn.close()
            return self.send_json({'ok': True, 'history_count': history_count})

        if path == '/api/ai/search':
            find_researchers = bool(body.get('find_researchers', True))
            find_equipment = bool(body.get('find_equipment', False))
            research_content = body.get('research_content', '').strip()
            user_category = body.get('user_category', '学内')
            save_history = bool(body.get('save_history', True))
            user_email = body.get('user_email', '')
            user_name = body.get('user_name', '')
            user_researcher_id = body.get('user_researcher_id', '')

            conn = get_db()
            user_prof = find_researcher_by_name(conn, user_name, user_researcher_id)
            conn.close()

            # Combine user's own DB research keywords/specialty with the entered research_content
            user_kw_str = ''
            if user_prof:
                user_kw_str = ' '.join(user_prof.get('all_keywords', [])[:10]) + ' ' + (user_prof.get('specialty') or '')

            combined_search_query = f'{research_content} {user_kw_str}'.strip()

            r_candidates = (
                search_researchers_db(
                    combined_search_query,
                    exclude_id=user_prof['id'] if user_prof else '',
                    exclude_name=user_name,
                    limit=8,
                )
                if find_researchers
                else []
            )
            g_query = combined_search_query + ' ' + ' '.join(' '.join(r.get('keywords', [])) for r in r_candidates[:4])
            g_candidates = search_grants_db(g_query, limit=6) if find_researchers else []
            eq_candidates = search_equipment_db(combined_search_query, user_category=user_category, limit=6) if find_equipment else []

            local_r = generate_local_researchers(r_candidates, research_content, user_prof) if find_researchers else []
            local_g = generate_local_grants(g_candidates, local_r, research_content, user_prof) if find_researchers else []
            local_eq = generate_local_equipment(eq_candidates, research_content, user_prof) if find_equipment else []

            engine_label = 'AI推論'
            settings = get_settings()
            if settings.get('deepseek_api_key'):
                sys_prompt = (
                    'あなたは福井大学の研究連携サポートAIです。ログイン中の研究者自身のDB研究情報（専門・研究キーワード・研究概要）と、'
                    '今回入力された「研究内容」の両方を踏まえて、候補データの中から最適な：\n'
                    '1) 共同研究者5名（ログイン研究者とのシナジーを踏まえた役割案 proposed_role と推薦理由 ai_reason）、\n'
                    '2) 二人以上の共同研究・分野に合う公募最大3件（推薦理由 ai_reason）、\n'
                    '3) この研究に使える貸出機器最大4件（利用目的案 proposed_purpose と推薦理由 ai_reason）\n'
                    'をJSON形式で提案してください。\n'
                    '出力JSON形式: {\n'
                    '  "researchers": [{"id": "...", "proposed_role": "...", "ai_reason": "..."}],\n'
                    '  "grants": [{"id": "...", "ai_reason": "..."}],\n'
                    '  "equipment": [{"id": "...", "proposed_purpose": "...", "ai_reason": "..."}]\n'
                    '}'
                )
                user_prompt = json.dumps({
                    'logged_in_researcher': {
                        'name': user_prof.get('name') if user_prof else user_name,
                        'department': user_prof.get('department') if user_prof else '',
                        'keywords': user_prof.get('keywords') if user_prof else [],
                        'summary': (user_prof.get('summary') or '')[:250] if user_prof else '',
                    },
                    'research_content': research_content,
                    'find_researchers': find_researchers,
                    'find_equipment': find_equipment,
                    'researcher_candidates': [{'id': r['id'], 'name': r['name'], 'dept': r['department'], 'keywords': r['keywords']} for r in r_candidates[:7]],
                    'grant_candidates': [{'id': g['id'], 'title': g['title'], 'field': g['field'], 'agency': g['agency']} for g in g_candidates[:5]],
                    'equipment_candidates': [{'id': e['id'], 'name': e['name'], 'fee': e['fee_display']} for e in eq_candidates[:6]],
                }, ensure_ascii=False)

                ai_res, engine_label = call_deepseek(sys_prompt, user_prompt)
                if ai_res:
                    if find_researchers and isinstance(ai_res.get('researchers'), list):
                        rmap = {r['id']: r for r in local_r}
                        merged_r = []
                        for item in ai_res['researchers']:
                            rid = item.get('id')
                            if rid in rmap:
                                base = dict(rmap[rid])
                                if item.get('proposed_role'):
                                    base['proposed_role'] = item['proposed_role']
                                if item.get('ai_reason'):
                                    base['ai_reason'] = item['ai_reason']
                                merged_r.append(base)
                        if merged_r:
                            local_r = merged_r[:5]

                    if find_researchers and isinstance(ai_res.get('grants'), list):
                        gmap = {g['id']: g for g in local_g}
                        merged_g = []
                        for item in ai_res['grants']:
                            gid = item.get('id')
                            if gid in gmap:
                                base = dict(gmap[gid])
                                if item.get('ai_reason'):
                                    base['ai_reason'] = item['ai_reason']
                                merged_g.append(base)
                        if merged_g:
                            local_g = merged_g[:3]

                    if find_equipment and isinstance(ai_res.get('equipment'), list):
                        emap = {e['id']: e for e in local_eq}
                        merged_e = []
                        for item in ai_res['equipment']:
                            eid = item.get('id')
                            if eid in emap:
                                base = dict(emap[eid])
                                if item.get('proposed_purpose'):
                                    base['proposed_purpose'] = item['proposed_purpose']
                                if item.get('ai_reason'):
                                    base['ai_reason'] = item['ai_reason']
                                merged_e.append(base)
                        if merged_e:
                            local_eq = merged_e[:4]

            conn = get_db()
            if save_history and research_content:
                hid = 'hist_' + uuid.uuid4().hex[:8]
                now = datetime.now().strftime('%Y-%m-%d %H:%M')
                conn.execute(
                    'INSERT INTO consultation_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        hid,
                        research_content,
                        1 if find_researchers else 0,
                        1 if find_equipment else 0,
                        user_category,
                        engine_label,
                        json.dumps(local_r, ensure_ascii=False),
                        json.dumps(local_g, ensure_ascii=False),
                        json.dumps(local_eq, ensure_ascii=False),
                        now,
                        user_email,
                    ),
                )
                conn.commit()
            history_count = conn.execute('SELECT COUNT(*) FROM consultation_history').fetchone()[0]
            conn.close()

            return self.send_json({
                'researchers': local_r,
                'grants': local_g,
                'equipment': local_eq,
                'engine': engine_label,
                'history_count': history_count,
                'user_profile': user_prof,
            })

        return self.send_json({'error': 'Not found'}, 404)


if __name__ == '__main__':
    ensure_tables()
    os.makedirs(STATIC_DIR, exist_ok=True)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), MatchingHandler)
    print(f'Server running on http://127.0.0.1:{PORT}')
    server.serve_forever()
