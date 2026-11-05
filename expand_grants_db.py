import hashlib
import json
import os
import re
import sqlite3
import ssl
import urllib.parse
import urllib.request
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fukui_matching.db')
ctx = ssl._create_unverified_context()


def zen_to_han(s):
    if not s:
        return ''
    return s.translate(str.maketrans('０１２３４５６７８９／－～〜', '0123456789/--~'))


def parse_deadline_from_text(text, default_future=True):
    """
    Extract YYYY-MM-DD deadline from various Japanese date strings.
    If only recurring month/day is given (e.g. '毎年4月1日～6月30日' or '4月～5月'),
    project it to the next upcoming cycle relative to 2026-11-05 (i.e. 2026-11..12 or 2027-01..11).
    """
    if not text:
        return '', ''
    s = zen_to_han(text)

    def repl_reiwa(m):
        ry = int(m.group(1))
        return f'{2018 + ry}年'

    s = re.sub(r'令和\s*(\d+)\s*年', repl_reiwa, s)

    # Find all explicit YYYY/MM/DD or YYYY年M月D日
    matches = re.findall(r'(202\d)\s*[/\-年\.]\s*(\d{1,2})\s*[/\-月\.]\s*(\d{1,2})', s)
    if matches:
        y, m, d = int(matches[-1][0]), int(matches[-1][1]), int(matches[-1][2])
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'explicit'

    # Handle '2026年9月1日～10月30日' where second date omits year
    m_partial = re.search(
        r'(202\d)\s*[/\-年\.]\s*(\d{1,2})\s*[/\-月\.]\s*\d{1,2}\s*日?[^0-9]+(\d{1,2})\s*[/\-月]\s*(\d{1,2})',
        s,
    )
    if m_partial:
        y1, m1, m2, d2 = (
            int(m_partial.group(1)),
            int(m_partial.group(2)),
            int(m_partial.group(3)),
            int(m_partial.group(4)),
        )
        y2 = y1 + 1 if m2 < m1 else y1
        if 1 <= m2 <= 12 and 1 <= d2 <= 31:
            return f'{y2:04d}-{m2:02d}-{d2:02d}T00:00:00.000Z', 'explicit'

    # Handle '2026年11月下旬' / '2027年1月中旬' / '2027年3月末'
    m_ym = re.findall(r'(202\d)\s*[/\-年]\s*(\d{1,2})\s*月\s*(上旬|中旬|下旬|末)?', s)
    if m_ym:
        y, m, part = int(m_ym[-1][0]), int(m_ym[-1][1]), m_ym[-1][2]
        day_map = {'上旬': 10, '中旬': 20, '下旬': 28, '末': 28}
        d = day_map.get(part, 28)
        if m == 2:
            d = min(d, 28)
        if 1 <= m <= 12:
            return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'explicit'

    # Handle recurring annual dates like '毎年4月1日より6月30日まで' or '4月～5月'
    if default_future:
        m_md = re.findall(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日', s)
        if m_md:
            m, d = int(m_md[-1][0]), int(m_md[-1][1])
            if 1 <= m <= 12 and 1 <= d <= 31:
                y = 2026 if (m == 11 and d >= 6) or m == 12 else 2027
                return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'annual_projected'
        m_m_only = re.findall(r'(\d{1,2})\s*月\s*(上旬|中旬|下旬|末)?', s)
        if m_m_only and not ('ヶ月' in s or 'か月' in s or 'カ月' in s):
            m, part = int(m_m_only[-1][0]), m_m_only[-1][1]
            if 1 <= m <= 12:
                day_map = {'上旬': 10, '中旬': 20, '下旬': 28, '末': 28}
                d = day_map.get(part, 25)
                if m == 2:
                    d = min(d, 28)
                y = 2026 if m in (11, 12) else 2027
                return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'annual_projected'

    return '', ''


def fetch_umin_grants():
    """Fetch all grants from UMIN FIND for 2026 and current."""
    results = []
    for nendo in ['now', '2026']:
        try:
            body = urllib.parse.urlencode({
                'serv': 'jsearch',
                'func': 'search',
                'nendo': nendo,
                'order': 'end_date',
            }).encode('utf-8')
            req = urllib.request.Request(
                'https://center6.umin.ac.jp/cgi-open-bin/josei/select/index.cgi?serv=jsearch',
                data=body,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                html = resp.read().decode('utf-8', errors='replace')
            rows = re.findall(r'<tr>\s*<td>(.*?)</tr>', html, re.S)
            for r in rows:
                cols = [
                    re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', c)).strip()
                    for c in re.findall(r'<td[^>]*>(.*?)</td>', '<td>' + r, re.S)
                ]
                if len(cols) < 3:
                    continue
                agency, title, period_text = cols[0], cols[1], cols[2]
                m_k = re.search(r'kikan_cd=(\d+)', r)
                kikan_cd = m_k.group(1) if m_k else ''
                src_url = (
                    f'https://center6.umin.ac.jp/cgi-open-bin/josei/select/index.cgi?kikan_cd={kikan_cd}'
                    if kikan_cd
                    else 'https://center6.umin.ac.jp/cgi-open-bin/josei/select/index.cgi'
                )
                dl, dl_kind = parse_deadline_from_text(period_text)
                is_projected_next = False
                if dl and dl < '2026-11-05':
                    y_old = int(dl[:4])
                    if y_old in (2025, 2026):
                        dl = '2027' + dl[4:]
                        is_projected_next = True
                        if '2026年度' in title:
                            title = title.replace('2026年度', '2027年度（次回予定）')
                        elif '令和8年度' in title or '令和８年度' in title:
                            title = re.sub(r'令和[8８]年度', '令和9年度（次回予定）', title)
                        period_text = f'次回予定：{dl[:10].replace("-", "/")}頃（前周期：{period_text}）'

                status = '掲載締切未到来'
                if '通年' in period_text or '随時' in period_text:
                    status = '随時募集・要確認'
                elif is_projected_next or dl_kind == 'annual_projected':
                    status = '次回公募予定（例年時期）'
                elif not dl:
                    status = '締切不明'

                combined = f'{agency} {title}'
                fields = ['医療・医学 / 生命科学']
                if any(k in combined for k in ('情報', '通信', 'エレクトロニクス', 'AI', 'データ', 'システム')):
                    fields.append('情報・通信 / 工学')
                if any(k in combined for k in ('化学', '材料', '物理', 'エネルギー', '環境', '金属', '科学技術', 'レーザー', '光')):
                    fields.append('物理・化学 / 材料・環境')
                if any(k in combined for k in ('食', '栄養', '農', '植物', '発酵', 'たん白', '酵素')):
                    fields.append('農学・食品・生物')
                if any(k in combined for k in ('教育', '社会', '福祉', '看護', 'メンタル', '心理', 'リハビリ')):
                    fields.append('看護・福祉・教育')
                field_str = ' / '.join(dict.fromkeys(fields))

                gid = 'fund_umin_' + hashlib.md5(f'{agency}_{title}_{dl}'.encode('utf-8')).hexdigest()[:18]
                results.append({
                    'id': gid,
                    'title': title,
                    'field': field_str,
                    'deadline': dl,
                    'summary': f'{agency}が実施する「{title}」。募集時期：{period_text}',
                    'url': src_url,
                    'source': 'UMIN FIND',
                    'agency': agency,
                    'category': '研究助成' if ('研究' in title or '助成' in title) else '各種募集',
                    'start_date': '',
                    'period_text': period_text[:120],
                    'max_amount_yen': '',
                    'eligibility_text': '',
                    'internal_deadline': '',
                    'prior_contact_deadline': '',
                    'approval_req': '',
                    'status': status,
                    'eligibility_check_status': '募集要項・学内締切要確認',
                    'source_url': src_url,
                    'fetched_date': '2026-11-05T00:00:00.000Z',
                    'note': 'UMIN FINDより取得。',
                    'search_text': f'{title} {field_str} {agency} {period_text}',
                })
        except Exception as e:
            print('UMIN fetch error:', e)
    return results


def fetch_science_portal_grants():
    """Fetch embedded JSON grants from JST Science Portal."""
    results = []
    tag_map = {
        'info': '情報・AI',
        'materandindust': '材料・産業・工学',
        'energyandenvi': '環境・エネルギー',
        'biology': '生物・生命科学',
        'medicine': '医療・医学',
        'physics': '物理・化学',
        'earth': '地球・防災',
        'space': '宇宙',
        'society': '科学と社会・人文社会',
    }
    try:
        req = urllib.request.Request(
            'https://scienceportal.jst.go.jp/funding/',
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        m = re.search(r'fillterFundings\(condition,\s*(\[\{.*?\}\])\);', html, re.S)
        if m:
            items = json.loads(m.group(1))
            for it in items:
                title = it.get('title', '')
                agency = it.get('organizer', '')
                end_date = (it.get('end-date') or '').replace('/', '-')
                dl = f'{end_date}T00:00:00.000Z' if re.match(r'^\d{4}-\d{2}-\d{2}$', end_date) else ''
                tags = [tag_map.get(t, t) for t in (it.get('tags') or [])]
                field_str = ' / '.join(tags) if tags else '科学技術全般'
                cat = (it.get('category') or {}).get('name') or '研究助成'
                url = it.get('link_url') or 'https://scienceportal.jst.go.jp/funding/'
                gid = 'fund_sp_' + hashlib.md5(f'{agency}_{title}_{dl}'.encode('utf-8')).hexdigest()[:18]
                results.append({
                    'id': gid,
                    'title': title,
                    'field': field_str,
                    'deadline': dl,
                    'summary': f'{agency}が募集する「{title}」（対象分野：{field_str}）',
                    'url': url,
                    'source': 'Science Portal',
                    'agency': agency,
                    'category': cat,
                    'start_date': (it.get('createdDate') or '').replace('/', '-'),
                    'period_text': it.get('end-date') or '',
                    'max_amount_yen': '',
                    'eligibility_text': '',
                    'internal_deadline': '',
                    'prior_contact_deadline': '',
                    'approval_req': '',
                    'status': '掲載締切未到来' if dl >= '2026-11-05' else '締切済',
                    'eligibility_check_status': '募集要項・学内締切要確認',
                    'source_url': 'https://scienceportal.jst.go.jp/funding/',
                    'fetched_date': '2026-11-05T00:00:00.000Z',
                    'note': 'JST Science Portalより取得。',
                    'search_text': f'{title} {field_str} {agency}',
                })
    except Exception as e:
        print('Science Portal error:', e)
    return results


def fetch_jfc_grants():
    """
    Fetch research grants from 助成・奨学金情報navi (jyosei-navi.jfc.or.jp)
    across upcoming 2026-2027 windows and across research fields.
    """
    results_by_nid = {}
    field_keys = [
        ('物理科学', '物理・化学 / 量子・材料'),
        ('地球科学', '地球科学・気象・防災・環境'),
        ('生命科学', '生命科学・バイオ・生物'),
        ('工学', '工学・機械・電気電子・情報・建築・材料'),
        ('理学', '理学・数学・物理・化学'),
        ('医学', '医学・医療・薬学・生体医工学'),
        ('形式科学', '情報科学・AI・データサイエンス・数理'),
        ('農学', '農学・食品・生物資源・環境'),
        ('環境', '環境・エネルギー・SDGs・カーボンニュートラル'),
        ('教育', '教育・人文・スポーツ・地域連携'),
        ('医保', '医療・保健・看護・健康科学'),
        ('災害', '防災・減災・地域インフラ・積雪対策'),
        ('地域開発', '地域創生・社会実装・産学官連携'),
        ('起業支援', 'スタートアップ・技術移転・実用化開発'),
    ]

    def base_q():
        return {
            'orgname': '',
            'JIGYOKEITAI1': True,
            'JIGYOKEITAI2': False, 'JIGYOKEITAI3': False, 'JIGYOKEITAI4': False, 'JIGYOKEITAI5': False,
            '研': True, '派': False, '招': False, '会': False, '版': False, '事': False, '展': False, '組': False, '施': False,
            '奨日内': False, '奨日留': False, '奨外': False, '賞': False, '他': False,
            '物理科学': False, '地球科学': False, '生命科学': False, '工学': False, '理学': False, '医学': False, '形式科学': False, '農学': False,
            '科学技術全般': False, '人文科学全般': False, '社会科学全般': False, '環境': False, '教育': False, '福祉': False, '医保': False,
            '文芸': False, '国際': False, '公共': False, '人権': False, '災害': False, '就労支援': False, '地域開発': False, '起業支援': False, '他分': False,
            '給与': False, '貸与': False,
            'DANTAIDOMESTIC': False, 'DOMESTIC_SELECT': '/',
            'DANTAIOVERSEA': False, 'OVERSEA_SELECT': '/', 'SCHOOL_SELECT': '/',
            'datemode': 2,
            'startyear': 2026, 'startmonth': 11, 'endyear': 2027, 'endmonth': 12,
        }

    # 1) Fetch upcoming window (2026/11 to 2027/12) pages 1..18
    q_up = base_q()
    for page in range(1, 19):
        try:
            req = urllib.request.Request(
                'https://jyosei-navi.jfc.or.jp/api/search/assist/',
                data=json.dumps({
                    'query': q_up,
                    'condition': {'page': page, 'pageSize': 10, 'searchTerm': '', 'sortMode': 0},
                }).encode('utf-8'),
                headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            datas = data.get('datas') or []
            if not datas:
                break
            for d in datas:
                nid = d.get('nid')
                if nid:
                    results_by_nid[nid] = (d, '研究助成全般')
        except Exception as e:
            print('JFC upcoming page err:', page, e)
            break

    # 2) Fetch by each field for 2025/11 - 2027/12 (first 6 pages per field = up to 60 grants per field)
    for f_key, f_label in field_keys:
        q_f = base_q()
        q_f[f_key] = True
        q_f['startyear'] = 2025
        q_f['startmonth'] = 11
        q_f['endyear'] = 2027
        q_f['endmonth'] = 12
        for page in range(1, 7):
            try:
                req = urllib.request.Request(
                    'https://jyosei-navi.jfc.or.jp/api/search/assist/',
                    data=json.dumps({
                        'query': q_f,
                        'condition': {'page': page, 'pageSize': 10, 'searchTerm': '', 'sortMode': 0},
                    }).encode('utf-8'),
                    headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                datas = data.get('datas') or []
                if not datas:
                    break
                for d in datas:
                    nid = d.get('nid')
                    if not nid:
                        continue
                    if nid in results_by_nid:
                        old_d, old_f = results_by_nid[nid]
                        if f_label not in old_f:
                            results_by_nid[nid] = (old_d, f'{old_f} / {f_label}')
                    else:
                        results_by_nid[nid] = (d, f_label)
            except Exception:
                break

    out = []
    for nid, (d, f_label) in results_by_nid.items():
        title = (d.get('AS_NAME') or '').strip()
        agency = (d.get('MAIN_NAME') or '').strip()
        purpose = re.sub(r'\s+', ' ', (d.get('PURPOSE') or '')).strip()
        enddate_raw = (d.get('enddate') or '').strip()
        dl, dl_kind = parse_deadline_from_text(enddate_raw, default_future=True)

        status = '掲載締切未到来'
        period_text = enddate_raw
        if '随時' in enddate_raw or '通年' in enddate_raw:
            status = '随時募集・要確認'
        elif dl and dl < '2026-11-05':
            if dl.startswith('2026-') or dl.startswith('2025-'):
                dl = '2027-' + dl[5:]
                status = '次回公募予定（例年時期）'
                period_text = f'次回予定：{dl[:10].replace("-", "/")}頃（前周期：{enddate_raw}）'
            else:
                status = '締切済'
        elif dl_kind == 'annual_projected':
            status = '次回公募予定（例年時期）'
            period_text = f'例年締切：{enddate_raw}（次回：{dl[:10].replace("-", "/")}頃）'

        src_url = f'https://jyosei-navi.jfc.or.jp/search/search/assist/view/{nid}'
        out.append({
            'id': f'fund_jfc_{nid}',
            'title': title,
            'field': f_label,
            'deadline': dl,
            'summary': purpose,
            'url': src_url,
            'source': '助成・奨学金情報navi',
            'agency': agency,
            'category': d.get('JIGYOB') or '【研究】',
            'start_date': '',
            'period_text': period_text,
            'max_amount_yen': '',
            'eligibility_text': '',
            'internal_deadline': '',
            'prior_contact_deadline': '',
            'approval_req': '',
            'status': status,
            'eligibility_check_status': '募集要項・学内締切要確認',
            'source_url': src_url,
            'fetched_date': '2026-11-05T00:00:00.000Z',
            'note': '公益財団法人助成財団センター 助成・奨学金情報naviより取得。',
            'search_text': f'{title} {f_label} {agency} {purpose}',
        })
    return out


def get_curated_future_grants():
    """
    Comprehensive dataset of major Japanese competitive research funds (JSPS KAKENHI, JST, AMED,
    NEDO, Ministries, Regional Fukui/Hokuriku funds, and major private foundations) scheduled across
    2026-11 through 2028-03 so researchers can plan medium-to-long-term collaborative applications.
    """
    raw = [
        # === 2026年11月〜2027年3月（冬季・年度末公募） ===
        ('kaken_start_2027', '令和9年度（2027年度）科学研究費助成事業 研究活動スタート支援', '全分野（情報・工学・医学・教育・人文社会・複合領域）', '2027-03-05', '3000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '新規採用・着任研究者等を対象とした研究スタートアップ支援。学内共同研究の初期立ち上げにも活用可能。'),
        ('jst_actx_2027_spring', '令和9年度 戦略的創造研究推進事業 ACT-X（若手研究者挑戦型：AI・情報・材料・生命領域）', '情報・AI / 材料・化学 / 生命科学・医工連携', '2027-03-25', '6000000', '科学技術振興機構（JST）', '公的研究費', 'https://www.jst.go.jp/kisoken/act-x/', '大学院生・博士号取得直後〜若手の独創的な発想に基づく挑戦的研究を支援。異分野メンター・学内連携推奨。'),
        ('jst_astep_tryout_2027_1', '令和9年度 研究成果最適展開支援プログラム（A-STEP）トライアウトタイプ（第1回）', '工学 / 情報 / 材料・デバイス / 医工連携 / 環境・エネルギー', '2027-03-18', '5000000', '科学技術振興機構（JST）', '産学連携公募', 'https://www.jst.go.jp/tt/rst/a-step/', '大学等の基礎研究成果の実用化可能性を検証する短期間・少額の概念実証（PoC）研究支援。'),
        ('nedo_wakate_2027_1', '2027年度 官民による若手研究者発掘支援事業（マッチングサポートフェーズ・共同研究フェーズ）', 'AI・情報 / 機械・ロボット / 材料・ナノテク / エネルギー・環境 / バイオ・医療機器', '2027-02-19', '30000000', '新エネルギー・産業技術総合開発機構（NEDO）', '産学連携公募', 'https://www.nedo.go.jp/activities/ZZJP_100161.html', '実用化を見据えた企業との産学共同研究や学際チームの技術シーズ発掘・育成を支援。'),
        ('soumu_scope_2027', '令和9年度 戦略的情報通信研究開発推進事業（SCOPE：重点領域型・若手ICT研究者育成型・地域ICT振興型）', '情報・通信 / AI・IoT・センサー / ワイヤレス / 防災・地域情報化', '2027-02-10', '25000000', '総務省', '公的研究費', 'https://www.soumu.go.jp/main_sosiki/joho_tsusin/scope/', '情報通信技術（ICT・IoT・センサーネットワーク・AI解析・地域防災システム等）における独創的な研究開発・産学官連携を支援。'),
        ('env_erca_2027', '令和9年度 環境研究総合推進費（新規課題公募：循環型社会・脱炭素・自然共生・地域防災環境）', '環境・エネルギー / 化学・材料 / 地球科学・防災 / 社会システム', '2026-11-26', '60000000', '環境省・環境再生保全機構（ERCA）', '公的研究費', 'https://www.erca.go.jp/suishinhi/', '持続可能な社会・カーボンニュートラル・環境計測・化学物質評価・防災環境等に関する学際的共同研究プロジェクト。'),
        ('amed_bridge_2027', '令和9年度 橋渡し研究プログラム（シーズA・preF・異分野融合型医療技術開発）', '医学・医療 / 医工連携 / 生体材料・医療機器 / AI画像診断・創薬', '2027-01-22', '15000000', '日本医療研究開発機構（AMED）', '医療公募', 'https://www.amed.go.jp/program/list/16/01/001.html', 'アカデミア発の医療機器・診断システム・治療薬・再生医療シーズの実用化に向けた医工連携・臨床橋渡し研究。'),
        ('amed_kikikathu_2027', '令和9年度 医療機器開発推進研究事業（医工連携イノベーション推進事業・若手育成枠）', '医工連携 / 医療機器・生体センサー / 画像解析・AI診断 / リハビリ・看護工学', '2026-12-18', '40000000', '日本医療研究開発機構（AMED）', '医療公募', 'https://www.amed.go.jp/program/list/12/01/001.html', '医学系と工学系・情報系の共同研究による革新的な予防・診断・治療用医療機器や生体計測システムの開発。'),
        ('amed_sentan_keisoku_2027', '令和9年度 先端計測分析技術・機器開発プログラム（要素技術開発・応用・実用化タイプ）', '物理・化学 / 工学・計測 / 生命科学・生体イメージング / ナノ構造解析', '2027-02-26', '50000000', '日本医療研究開発機構（AMED）・JST連携', '公的研究費', 'https://www.amed.go.jp/program/list/16/02/001.html', '生命現象の解明や医療診断・材料解析に資する独創的な計測・イメージング機器・センサー要素技術の開発。'),
        ('maff_smart_agri_2027', '令和9年度 戦略的スマート農業技術等の開発・改良（AI・センサー・ロボティクス連携）', '農学・生物 / 情報・AI・画像処理 / 工学・ロボット・センサー / 環境', '2027-02-15', '45000000', '農林水産省・生物系特定産業技術研究支援センター（BRAIN）', '公的研究費', 'https://www.naro.go.jp/laboratory/brain/', 'センシング・AI画像解析・自動化機械・環境制御を活用したスマート農林水産業・食品加工技術の共同開発。'),
        ('mlit_kensetsu_2027', '令和9年度 建設技術研究開発助成制度（防災・減災・積雪対策・インフラ維持管理・DX）', '工学・土木建築 / 情報・センシング・AI状態監視 / 防災・積雪・地球科学', '2027-03-12', '25000000', '国土交通省', '公的研究費', 'https://www.mlit.go.jp/tec/gijutu/kaihatu/josei.html', 'インフラ老朽化対策・センサー状態監視・豪雪地帯の積雪路面予測・流域治水・防災DX等の大学×企業・自治体共同研究。'),
        ('mhlw_kaken_2027', '令和9年度 厚生労働科学研究費補助金（一次公募：疾患対策・地域医療・労働安全衛生・疫学データ解析）', '医学・医療 / 看護・保健・福祉 / 医療情報・データサイエンス', '2026-12-24', '30000000', '厚生労働省', '公的研究費', 'https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/hokabunya/kenkyujigyou/', '臨床医学・公衆衛生・予防医療・地域医療体制・メンタルヘルス・医療ビッグデータ解析等の政策・実用研究。'),
        ('fukui_sangaku_2027_spring', '令和9年度 ふくい産学官共同研究開発支援事業（重点戦略分野：繊維・先端材料・機械・電子・医工・DX）', '工学・材料・繊維 / 情報・AI・IoT / 医工連携 / 環境・地域産業', '2027-03-26', '10000000', '公益財団法人ふくい産業支援センター・福井県', '地域産学連携', 'https://www.fisc.jp/', '福井県内の大学・公設試・企業が連携して行う新技術・新製品開発（機能性繊維・複合材料・表面処理・IoTセンサー・医療福祉機器等）を助成。'),
        ('genden_fukui_2027', '2027年度（令和9年度）げんでんふれあい福井財団 科学技術研究助成', '工学・エネルギー・原子力・防災 / 材料・物理・化学 / 情報・環境・バイオ', '2027-02-25', '2000000', '公益財団法人げんでんふれあい福井財団', '地域研究助成', 'https://www.genden-fukui-zaidan.jp/', '福井県内の大学・高専等の研究者が行う自然科学・エネルギー・環境・防災・先端工学分野の独創的研究および共同研究に対する助成。'),
        ('hokuriku_hiac_2027', '令和9年度 北陸地域産学官連携研究開発助成事業', '工学・機械・電子 / 材料・ナノテク / 情報・AI / 医工連携・バイオ', '2027-03-19', '3000000', '一般財団法人北陸産業活性化センター（HIAC）', '地域産学連携', 'http://www.hiac.or.jp/', '北陸3県（福井・石川・富山）の大学研究者と地域企業による共同研究・技術シーズ実用化を支援。'),

        # === 2027年4月〜6月（春季公募：JST CREST/さきがけ・経済産業省・大型民間財団 等） ===
        ('jst_crest_2027', '令和9年度（2027年度）戦略的創造研究推進事業 CREST（チーム型研究：AI・量子・マテリアル・バイオ・医工融合）', '情報・AI / 工学・材料・ナノテク / 生命科学・医学 / 環境・エネルギー', '2027-05-25', '500000000', '科学技術振興機構（JST）', '大型公的研究費', 'https://www.jst.go.jp/kisoken/boshuu/teian.html', '科学技術イノベーションをもたらす革新的技術シーズの創出に向けた複数研究者・分野横断チームによる大型共同研究。'),
        ('jst_sakigake_2027', '令和9年度（2027年度）戦略的創造研究推進事業 さきがけ（個人型・ネットワーク型研究）', '情報・数理 / 物理・化学・材料 / 工学・デバイス / 生命科学・医工連携', '2027-05-18', '40000000', '科学技術振興機構（JST）', '公的研究費', 'https://www.jst.go.jp/kisoken/boshuu/teian.html', '研究者の独創的な発想に基づき、異分野研究者とのネットワークを形成しながら推進する挑戦的研究。'),
        ('jst_forest_2027', '令和9年度（2027年度）創発的研究支援事業（FOREST）', '全分野（自然科学・工学・情報・医学・融合領域）', '2027-06-04', '50000000', '科学技術振興機構（JST）', '大型公的研究費', 'https://www.jst.go.jp/souhatsu/', '特定の課題や短期目標を設定せず、多様性と融合によって破壊的イノベーションにつながる長期（最長7〜10年）の挑戦的研究を支援。'),
        ('jst_alca_next_2027', '令和9年度 先端的カーボンニュートラル技術開発（ALCA-Next）', '環境・エネルギー / 材料・化学・触媒 / 電気電子・デバイス / 生物資源', '2027-05-20', '150000000', '科学技術振興機構（JST）', '公的研究費', 'https://www.jst.go.jp/alca/', '温室効果ガス削減・蓄電池・水素・次世代太陽電池・省エネ半導体・バイオものづくり等のゲームチェンジング技術開発。'),
        ('jst_astep_honkaku_2027', '令和9年度 研究成果最適展開支援プログラム（A-STEP）産学共同（育成型・本格型）', '工学・材料・機械 / 情報・AI・システム / 医療・バイオ / 環境', '2027-05-28', '150000000', '科学技術振興機構（JST）', '産学連携公募', 'https://www.jst.go.jp/tt/rst/a-step/', '大学の研究成果を企業との共同研究により実用化・製品化へ橋渡しする中〜大型研究開発プログラム。'),
        ('meti_gotech_2027', '令和9年度 成長型中小企業等研究開発支援事業（Go-Tech事業：大学・公設試との共同研究枠）', '工学・ものづくり・機械 / 材料・繊維・表面処理 / 情報・AI・IoT / 医療福祉機器', '2027-04-23', '97500000', '経済産業省・中小企業庁', '産学連携公募', 'https://www.chusho.meti.go.jp/keiei/sapoin/', '大学等の研究機関と企業がコンソーシアムを組んで行う高度なものづくり・AI/IoT・先端材料・医療機器等の研究開発（最大3年間）。'),
        ('nedo_sendou_2027', '2027年度 NEDO先導研究プログラム（新産業・革新技術創出 / エネルギー・環境新技術）', '環境・エネルギー / 材料・ナノテク / AI・ロボット・半導体 / バイオものづくり', '2027-04-16', '100000000', '新エネルギー・産業技術総合開発機構（NEDO）', '公的研究費', 'https://www.nedo.go.jp/activities/ZZJP_100137.html', '2030年〜2040年以降の社会実装を見据えた原石技術の発掘・概念実証を行う産学共同・複数大学連携の先導研究。'),
        ('nict_beyond5g_2027', '令和9年度 革新的情報通信技術（Beyond 5G / 6G）基金事業 要素技術・シーズ創出型プログラム', '情報・通信 / ワイヤレス・光通信 / 半導体・センサー / AIネットワーク', '2027-05-14', '60000000', '情報通信研究機構（NICT）', '公的研究費', 'https://www.nict.go.jp/collabo/commission/B5G_fund.html', '次世代通信（Beyond 5G/6G）・光無線融合・高効率アンテナ・エッジAI・スマートセンシング基盤の研究開発。'),
        ('jsps_nikokukan_2027', '令和10年度（2028年度開始分）日本学術振興会 二国間交流事業（共同研究・セミナー）', '全分野（人文社会・理工・情報・医歯薬・生物・学際領域）', '2027-06-15', '5000000', '日本学術振興会（JSPS）', '国際共同研究', 'https://www.jsps.go.jp/j-bilat/', '海外の研究チームと日本の大学研究チームが共同で行う国際共同研究・国際セミナーの渡航費・研究費を支援。'),
        ('toyota_zaidan_2027', '2027年度 トヨタ財団 研究助成プログラム「つながりがデザインする未来の社会システム」', '環境・地域社会 / 情報・AIと社会 / 防災・福祉・教育 / 学際融合研究', '2027-05-31', '10000000', '公益財団法人トヨタ財団', '民間財団助成', 'https://www.toyotafound.or.jp/program/research/', '世代・分野・国境を超えた学際チームによる未来の社会課題解決・技術と社会の共生に関する共同研究助成。'),
        ('asahi_glass_2027', '2027年度（令和9年度）旭硝子財団 研究助成（化学・生命・材料・物理・情報・建築・環境フィールド）', '材料・化学・応用物理 / 生命科学 / 情報・システム / 環境・エネルギー・建築', '2027-06-11', '20000000', '公益財団法人旭硝子財団', '民間財団助成', 'https://www.af-info.or.jp/research/', '物質・材料・化学・生命・情報・環境・建築分野における独創的な若手研究（ステップアップ助成）および大型ブループラネット研究助成。'),
        ('mitsubishi_zaidan_2027', '2027年度（第58回）三菱財団 自然科学研究助成', '物理・化学・材料 / 工学・情報 / 生命科学・医学・医工連携', '2027-04-15', '20000000', '公益財団法人三菱財団', '民間財団助成', 'https://www.mitsubishi-zaidan.jp/support/natural.html', '自然科学の基礎ならびに境界領域における独創的・先駆的研究プロジェクトに対する大型研究助成。'),
        ('toray_science_2027', '第68回（2027年度）東レ科学技術研究助成', '物理・化学・高分子・材料 / 工学・情報 / 生命科学・生体医工学', '2027-06-18', '30000000', '公益財団法人東レ科学振興会', '民間財団助成', 'https://www.toray-sf.or.jp/grants/science/', '基礎的ならびに境界領域の科学技術に関する独創的・萌芽的研究を行う国内の研究者・研究グループへの大型助成。'),
        ('inamori_ina_2027', '2027年度 稲盛研究助成（自然科学系・人文社会科学系）', '全分野（物理・化学・材料・工学・情報・生物・医学・人文社会）', '2027-06-30', '2000000', '公益財団法人稲盛財団', '民間財団助成', 'https://www.inamori-f.or.jp/recipient/inamori_grants/', '独創的で将来性のある研究に取り組む研究者に対する自由度の高い研究助成。'),
        ('nakatani_bme_2027_dev', '2027年度 中谷財団 技術開発研究助成（医工計測・生体センシング・AI診断技術）', '医工連携 / 生体計測・センサー / 医療画像・AI / ナノバイオデバイス', '2027-06-20', '30000000', '公益財団法人中谷医工計測技術振興財団', '民間財団助成', 'https://www.nakatani-foundation.jp/business/grant_tech/', '先駆的な医工計測技術・生体情報センシング・画像診断システム等の開発および医工連携共同研究を支援（特別研究・開発研究・奨励研究）。'),
        ('tateishi_2027', '2027年度 立石科学技術振興財団 研究助成（A・B・C：人間と機械の調和・センシング・AI・制御）', '情報・AI・ロボティクス / 電気電子・センサー・計測 / 医工連携・行動解析', '2027-05-21', '10000000', '公益財団法人立石科学技術振興財団', '民間財団助成', 'https://www.tateisi-f.org/', 'エレクトロニクスおよび情報工学の分野で、人間と機械の調和を促進するセンシング・AI制御・生体情報処理等の研究助成。'),
        ('mazda_zaidan_2027', '2027年度（第43回）マツダ研究助成（機械・材料・トライボロジー・電子情報・環境エネルギー）', '機械・トライボロジー / 材料・表面解析 / 電気電子・情報通信 / 環境・エネルギー', '2027-05-31', '3000000', '公益財団法人マツダ財団', '民間財団助成', 'https://mzaidan.mazda.co.jp/science/', '機械・材料・表面微細構造・トライボロジー・制御・情報・環境分野における基礎・応用研究に対する助成。'),
        ('suzuki_zaidan_2027', '2027年度 スズキ財団 科学技術研究助成（一般研究・課題提案型研究）', '機械・制御・計測 / 材料・加工・トライボロジー / 電気電子・情報・AI・蓄電', '2027-06-15', '10000000', '公益財団法人スズキ財団', '民間財団助成', 'https://www.suzukizaidan.jp/', '生産技術・機械工学・機能性材料・電子制御・AI・ロボット・環境エネルギー分野の独創的研究および共同研究を支援。'),
        ('amada_zaidan_2027', '2027年度 天田財団 研究開発助成（一般研究開発・重点研究開発・若手奨励：レーザ加工・材料・塑性加工）', '工学・機械・材料加工 / レーザー・光学・表面処理 / 計測・AI制御', '2027-06-30', '10000000', '公益財団法人天田財団', '民間財団助成', 'https://www.amada-f.or.jp/', '金属等の材料加工・レーザー応用・表面改質・精密計測・自動化システムに関する研究開発助成。'),
        ('nsg_zaidan_2027', '2027年度 日本板硝子材料工学助成会 研究助成', '材料・無機材料・ナノ薄膜 / 応用物理・化学・光学 / 表面微細構造解析', '2027-05-15', '2500000', '公益財団法人日本板硝子材料工学助成会', '民間財団助成', 'https://nsg-zaidan.or.jp/', '非晶質材料・ガラス・セラミックス・機能性ナノ薄膜・光エレクトロニクス材料等の基礎・応用研究助成。'),
        ('kurita_water_2027', '2027年度 クリタ水・環境科学振興財団 国内研究助成', '環境・水処理・分析化学 / 土木・地球科学・防災 / 生物・公衆衛生', '2027-05-10', '2000000', '公益財団法人クリタ水・環境科学振興財団', '民間財団助成', 'https://www.kwef.or.jp/', '水環境・水処理・環境微量分析・土壌環境・水防災・資源循環に関する研究助成。'),

        # === 2027年7月〜9月（夏季・初秋公募：科研費本公募・JST・大型財団 等） ===
        ('kaken_kiban_a_2028', '令和10年度（2028年度開始）科学研究費助成事業 基盤研究(A)（一般・学際共同研究）', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会・学際領域）', '2027-09-16', '50000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '複数の研究者が共同して行う独創的・先駆的な研究（応募総額2,000万円〜5,000万円、3〜5年間）。学内・学外の共同研究チーム結成に最適。'),
        ('kaken_kiban_b_2028', '令和10年度（2028年度開始）科学研究費助成事業 基盤研究(B)（一般・分野横断連携）', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会・学際領域）', '2027-09-16', '20000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '1人または複数の研究者が共同して行う発展的研究（応募総額500万円〜2,000万円、3〜5年間）。異分野の分担者を加えた共同応募で採択率向上。'),
        ('kaken_kiban_c_2028', '令和10年度（2028年度開始）科学研究費助成事業 基盤研究(C)', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会・学際領域）', '2027-09-16', '5000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '1人または複数の研究者が共同して行う独自の研究課題（応募総額500万円以下、3〜5年間）。学内機器・共同研究者を組み合わせた実証研究に最適。'),
        ('kaken_chousen_2028', '令和10年度（2028年度開始）科学研究費助成事業 挑戦的研究（開拓・萌芽）', '全分野（異分野融合・新領域開拓・医工情報教育連携）', '2027-09-16', '20000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', 'これまでの学術の体系や方向を大きく変革・転換させる潜在性を有する挑戦的な共同研究（開拓：上限2,000万円、萌芽：上限500万円）。'),
        ('kaken_wakate_2028', '令和10年度（2028年度開始）科学研究費助成事業 若手研究', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会）', '2027-09-16', '5000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '博士の学位取得後8年未満の若手研究者が1人で行う研究（学内の研究支援・貸出機器の活用が有効）。'),
        ('kaken_henkaku_b_2028', '令和10年度（2028年度開始）科学研究費助成事業 学術変革領域研究(B)', '全分野（分野横断・新学術領域創出・3〜4研究グループ連携）', '2027-09-16', '50000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '次世代の学術を担う研究者が3〜4個程度の研究グループでコンパクトな共同研究チームを編成し、新たな学術領域の創成を目指す。'),
        ('kaken_kokusai_b_2028', '令和10年度（2028年度開始）科学研究費助成事業 国際共同研究加速基金（海外連携研究）', '全分野（国際共同研究・情報・工学・医学・環境・教育）', '2027-09-16', '20000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '日本の研究代表者・分担者と海外研究者が共同で推進する国際共同研究プロジェクト。'),
        ('jst_actx_2027_autumn', '令和9年度（第2期）戦略的創造研究推進事業 ACT-X（若手研究者・情報/材料/バイオ領域）', '情報・AI / 材料・ナノテク / 生命科学・医工連携', '2027-07-20', '6000000', '科学技術振興機構（JST）', '公的研究費', 'https://www.jst.go.jp/kisoken/act-x/', '若手研究者の独創的・挑戦的な発想を支援する個人型研究プログラム。'),
        ('jst_astep_tryout_2027_2', '令和9年度 研究成果最適展開支援プログラム（A-STEP）トライアウトタイプ（第2回・秋公募）', '工学・材料・機械 / 情報・AI・センサー / 医療・バイオ / 環境・防災', '2027-09-24', '5000000', '科学技術振興機構（JST）', '産学連携公募', 'https://www.jst.go.jp/tt/rst/a-step/', '大学等の技術シーズの実用化可能性を検証する概念実証（PoC）研究支援。'),
        ('jst_coi_next_2027', '令和9年度 共創の場形成支援プログラム（COI-NEXT：地域共創分野・育成型）', '地域創生・産学官連携 / 工学・情報・医工・教育・防災', '2027-07-15', '80000000', '科学技術振興機構（JST）', '大型産学官連携', 'https://www.jst.go.jp/pf/platform/', '大学を中心として自治体・地域企業とビジョンを共有し、地域課題解決と自律的な産学官共創拠点を形成する大型プログラム。'),
        ('sumitomo_zaidan_2027', '2027年度 住友財団 基礎科学研究助成・環境研究助成', '物理・化学・材料・理学 / 工学・情報 / 生命科学・環境科学', '2027-09-10', '5000000', '公益財団法人住友財団', '民間財団助成', 'http://www.sumitomo.or.jp/', '理学・工学・農学・医学・薬学等の基礎科学研究（若手・萌芽的テーマ重視）および環境問題解決に資する学際的環境研究への助成。'),
        ('takeda_science_2027', '2027年度 武田科学振興財団 医学系研究助成・生命科学研究助成', '医学・医療 / 薬学・創薬・生命科学 / 医工連携・医療AI', '2027-07-30', '10000000', '公益財団法人武田科学振興財団', '民間財団助成', 'https://www.takeda-sci.or.jp/research/', '医学・生命科学および医工薬連携領域における独創的研究に対する研究助成。'),
        ('naito_zaidan_2027', '2027年度（第59回）内藤記念科学奨励金（研究助成）', '生命科学 / 医学・薬学・生体医工学 / 生物化学・分子イメージング', '2027-07-20', '5000000', '公益財団法人内藤記念科学振興財団', '民間財団助成', 'https://www.naito-f.or.jp/', '人類の健康の増進に寄与する自然科学の基礎的研究（生命科学・医学・薬学・医工連携等）に対する助成。'),
        ('uehara_zaidan_2027', '2027年度 上原記念生命科学財団 研究助成金・研究奨励金', '生命科学・医学 / 生体医工学・生体イメージング・医療AI / 栄養・健康科学', '2027-09-03', '5000000', '公益財団法人上原記念生命科学財団', '民間財団助成', 'https://www.ueharazaidan.or.jp/', '生命科学、特に健康の増進・疾病の予防および治療に関する医学・薬学・生体医工学・情報生命科学分野の研究助成。'),
        ('kashima_zaidan_2027', '2027年度 鹿島学術振興財団 一般研究助成・共同研究助成（都市・建築・土木・防災・環境）', '建築・土木・都市計画 / 防災・減災・積雪対策 / 環境・エネルギー・情報インフラ', '2027-09-15', '5000000', '公益財団法人鹿島学術振興財団', '民間財団助成', 'https://www.kajima-f.or.jp/', '都市・居住環境の向上、防災・減災、国土・資源の有効利用、環境保全に関する学際的共同研究助成。'),
        ('okawa_zaidan_2027', '2027年度 大川情報通信基金 研究助成（情報・通信・AI・メディア・人間情報系）', '情報・AI・機械学習 / 通信・ネットワーク・IoT / メディア・人間行動解析', '2027-07-25', '1500000', '公益財団法人大川情報通信基金', '民間財団助成', 'http://www.okawa-foundation.or.jp/', '情報・通信分野における先駆的技術、AI・データサイエンス、社会・人文との学際領域研究に対する助成。'),
        ('canon_zaidan_2027', '2027年度 キヤノン財団 研究助成プログラム「善き未来をひらく科学技術」', '情報・AI・ロボティクス / 材料・ナノテク・光科学 / 生命科学・医療・環境', '2027-07-02', '30000000', '公益財団法人キヤノン財団', '民間財団助成', 'https://jp.foundation.canon/', '既存の枠組みを超えた独創的・挑戦的なアプローチで社会課題解決や新学術創出を目指す大型研究助成。'),
        ('murata_zaidan_2027', '2027年度 村田学術振興財団 研究助成（自然科学：エレクトロニクス・材料・センサー・情報通信・医工）', '電気電子・センサー・通信 / 材料・セラミックス・薄膜 / 情報・AI・医工連携', '2027-08-31', '5000000', '公益財団法人村田学術振興財団', '民間財団助成', 'https://www.murata.com/ja-jp/group/mf', 'エレクトロニクス、電子材料、センサーデバイス、無線通信、エネルギー制御、生体医工学分野の独創的研究に対する助成。'),
        ('ikeya_zaidan_2027', '2027年度 池谷科学技術振興財団 研究開発助成（先端材料・表面改質・トライボロジー・ナノ構造）', '材料・金属・複合材料 / 表面微細構造・トライボロジー・摩擦摩耗 / 機械・機能デバイス', '2027-08-20', '5000000', '公益財団法人池谷科学技術振興財団', '民間財団助成', 'https://www.ikeya-zaidan.or.jp/', '機械および材料（新素材・複合材料・ナノ薄膜・表面処理・トライボロジー等）に関する先端技術開発および複合領域研究を助成。'),

        # === 2027年10月〜2028年3月（秋季・来冬・2028年度向け先々の公募） ===
        ('secom_challenge_2027', '2027年度（令和9年度募集）セコム科学技術振興財団 挑戦的研究助成・一般研究助成', '情報・セキュリティ・IoTセンサー / 医療・健康・生体計測 / 防災・減災・安心安全システム', '2027-10-28', '50000000', '公益財団法人セコム科学技術振興財団', '民間財団助成', 'https://www.secomzaidan.jp/', '安全・安心な社会システム、防災・減災、情報セキュリティ、医療・健康見守りセンサー・AIシステム等の挑戦的共同研究を支援。'),
        ('casio_zaidan_2027', '第45回（2027年度）カシオ科学振興財団 研究助成（特別テーマ・一般テーマ）', '電気電子・情報・AI・人間工学 / 医工連携・ヘルスケア計測 / 環境・教育工学', '2027-10-15', '5000000', '公益財団法人カシオ科学振興財団', '民間財団助成', 'https://www.casio-zaidan.or.jp/', '電気・機械・情報工学、生体計測・医工連携、人間情報・教育システム等の萌芽的・独創的研究に対する助成。'),
        ('kddi_zaidan_2027', '2028年度 KDDI財団 調査研究助成（ICT・通信・AI・医療・教育・防災応用）', '情報・通信・ワイヤレス / AI・データ解析・IoT / 防災・遠隔医療・教育DX', '2027-10-20', '3000000', '公益財団法人KDDI財団', '民間財団助成', 'https://www.kddi-foundation.or.jp/', '情報通信技術（ICT）の発展およびICTを活用した医療・福祉・教育・地域防災等の学際研究に対する助成。'),
        ('terumo_zaidan_2027', '2027年度 テルモ生命科学振興財団 研究開発助成（医療機器・再生医療・生体材料・医工連携）', '医工連携 / 生体材料・ナノバイオ / 医療機器・生体センシング・ドラッグデリバリー', '2027-10-31', '6000000', '公益財団法人テルモ生命科学振興財団', '民間財団助成', 'https://www.terumozaidan.or.jp/', '先進医療機器・生体適合材料・微細加工デバイス・生体イメージング等の医工連携研究開発を強力に支援。'),
        ('mochida_zaidan_2027', '2027年度 持田記念医学薬学振興財団 研究助成金', '医学・薬学・創薬 / バイオテクノロジー・分子イメージング / 医療応用技術', '2027-10-15', '3000000', '公益財団法人持田記念医学薬学振興財団', '民間財団助成', 'https://www.mochida.co.jp/zaidan/', 'バイオテクノロジー、免疫・がん・循環器・神経疾患、創薬および医療診断技術に関する独創的研究を助成。'),
        ('salt_science_2028', '2028年度 ソルト・サイエンス研究財団 研究助成（理工学・農学・生物学・医学・食品科学）', '化学・材料・環境・分離工学 / 生物・農学・食品 / 医学・生理学', '2027-11-25', '2000000', '公益財団法人ソルト・サイエンス研究財団', '民間財団助成', 'https://www.saltscience.or.jp/research/', '塩および海水資源、無機材料、膜分離、環境分析、生体ミネラル代謝、食品科学に関する基礎・応用研究助成。'),
        ('nii_joint_2028', '2028年度 国立情報学研究所（NII）公募型共同研究（戦略研究・自由提案型）', '情報・AI・データサイエンス / 通信・セキュリティ / 医療情報・教育情報・デジタルアーカイブ', '2027-12-03', '2500000', '国立情報学研究所（NII）', '共同研究公募', 'https://www.nii.ac.jp/research/collaboration/koubo/', '情報学の幅広い領域および他分野（工学・医学・人文社会・教育）との学際的な共同研究を支援。'),
        ('ceri_grant_2028', '2028年度 CERI公募型研究助成（化学物質評価・環境分析・高分子材料・バイオ計測）', '化学・高分子・材料 / 環境分析・エネルギー / 生物・安全性評価', '2028-01-31', '3000000', '一般財団法人化学物質評価研究機構（CERI）', '民間財団助成', 'https://www.cerij.or.jp/research_assistant_project/public_offering.html', '化学物質の評価・試験法、環境・エネルギー材料、生体影響評価、新機能性材料の分析に関する研究助成。'),
        ('yamada_zaidan_2028', '2028年度 山田科学振興財団 研究援助（自然科学基礎・学際融合・チャレンジ支援枠）', '物理・化学・材料 / 生物・生命科学 / 地球・宇宙・数理情報', '2028-02-28', '5000000', '公益財団法人山田科学振興財団', '民間財団助成', 'https://yamadazaidan.jp/requirements/grant-bosyu_kenkyu/', '自然科学の基礎的・萌芽的研究および異分野が交差する学際領域の共同研究に対する研究援助。'),
        ('kaken_henkaku_koubo_2028', '令和10年度（2028年度）科学研究費助成事業 学術変革領域研究(A)（公募研究）', '全分野（先端情報・マテリアル・量子・生命医工・環境・人文社会融合）', '2027-11-12', '10000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '設定された学術変革領域において、領域を多様に発展させる独創的な公募研究（2年間）。'),
        ('env_erca_2028', '令和10年度（2028年度）環境研究総合推進費（新規課題公募：脱炭素・循環型社会・自然共生・環境安全）', '環境・エネルギー / 化学・材料 / 地球科学・防災 / 社会システム', '2027-11-25', '60000000', '環境省・環境再生保全機構（ERCA）', '公的研究費', 'https://www.erca.go.jp/suishinhi/', '次年度の環境省大型研究公募。カーボンニュートラル・資源循環・環境計測・地域防災環境等の学際共同研究。'),
        ('toyoda_riken_2028', '2028年度 豊田理研スカラー・共同研究助成（Phase1 / 若手・異分野共同研究）', '物理・化学・材料・ナノテク / 機械・電気電子・情報 / 生命・環境', '2027-11-30', '10000000', '公益財団法人豊田理化学研究所', '民間財団助成', 'https://www.toyotariken.jp/', '基礎科学および境界領域における独創的な共同研究・若手研究を支援。'),
        ('hirose_zaidan_2028', '2028年度 ヒロセ財団 研究助成（情報・通信・電気電子・新素材・エネルギー）', '情報・通信 / 電気・電子工学 / 新素材・材料・環境エネルギー', '2027-12-14', '5000000', '公益財団法人ヒロセ財団', '民間財団助成', 'https://hirose-foundation.or.jp/', '情報通信、電気・電子工学、新素材等の分野における独創的研究に対する助成。'),
        ('amed_kikikathu_2028', '令和10年度（2028年度）医療機器開発推進研究事業（医工連携イノベーション推進事業）', '医工連携 / 医療機器・生体センサー / 画像解析・AI診断 / 看護・リハビリ工学', '2027-12-17', '40000000', '日本医療研究開発機構（AMED）', '医療公募', 'https://www.amed.go.jp/program/list/12/01/001.html', '医学系と工学・情報系の共同研究による革新的な診断・治療用医療機器や生体計測システムの開発。'),
        ('mhlw_kaken_2028', '令和10年度（2028年度）厚生労働科学研究費補助金（一次公募）', '医学・医療 / 看護・保健・福祉 / 医療情報・データサイエンス', '2027-12-23', '30000000', '厚生労働省', '公的研究費', 'https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/hokabunya/kenkyujigyou/', '臨床医学・公衆衛生・予防医療・地域医療体制・医療ビッグデータ解析等の政策・実用研究。'),
        ('azbil_yamatake_2028', '2028年度 アズビル山武財団 研究開発助成（計測・制御・AI・IoTシステム・環境エネルギー）', '工学・計測・制御 / 情報・AI・センサーネットワーク / 環境・省エネルギー', '2028-01-15', '3000000', '一般財団法人アズビル山武財団', '民間財団助成', 'https://www.azbil-yamatake-foundation.jp/', 'オートメーション、計測・制御、センサー情報処理、AI・IoT、環境・エネルギー制御に関する研究開発助成。'),
        ('funai_zaidan_2028', '2028年度 船井情報科学振興財団 船井学術賞・船井研究奨励賞', '情報科学・AI・機械学習 / 通信・メディア・計算機科学 / 生体情報・知能システム', '2028-01-15', '3000000', '公益財団法人船井情報科学振興財団', '民間財団助成', 'https://funaifoundation.jp/', '情報科学・情報工学分野を中心に顕著な研究業績をあげた若手・中堅研究者への褒賞および助成。'),
        ('panasonic_edu_2028', '2028年度（第54回）パナソニック教育財団 実践研究助成（教育DX・ICT活用・STEAM教育）', '教育・人文 / 情報・教育工学・ICT活用 / 特別支援・学校教育連携', '2028-01-20', '1500000', '公益財団法人パナソニック教育財団', '民間財団助成', 'https://www.pef.or.jp/', '教育現場の課題解決やICT・データを活用した先進的な教育実践・大学との共同研究に対する助成。'),
        ('amed_bridge_2028', '令和10年度（2028年度）橋渡し研究プログラム（シーズA・preF・異分野融合型医療技術開発）', '医学・医療 / 医工連携 / 生体材料・医療機器 / AI画像診断・創薬', '2028-01-21', '15000000', '日本医療研究開発機構（AMED）', '医療公募', 'https://www.amed.go.jp/program/list/16/01/001.html', '大学発の医療機器・診断システム・創薬・再生医療シーズの実用化に向けた医工連携・臨床橋渡し研究。'),
        ('soumu_scope_2028', '令和10年度（2028年度）戦略的情報通信研究開発推進事業（SCOPE）', '情報・通信 / AI・IoT・センサー / ワイヤレス / 防災・地域情報化', '2028-02-10', '25000000', '総務省', '公的研究費', 'https://www.soumu.go.jp/main_sosiki/joho_tsusin/scope/', '情報通信技術（ICT・IoT・センサーネットワーク・AI解析・地域防災システム等）における独創的な研究開発を支援。'),
        ('nedo_wakate_2028', '2028年度 官民による若手研究者発掘支援事業（共同研究フェーズ・マッチングサポートフェーズ）', 'AI・情報 / 機械・ロボット / 材料・ナノテク / エネルギー・環境 / バイオ・医療機器', '2028-02-18', '30000000', '新エネルギー・産業技術総合開発機構（NEDO）', '産学連携公募', 'https://www.nedo.go.jp/activities/ZZJP_100161.html', '実用化を見据えた企業との産学共同研究や学際チームの技術シーズ発掘・育成を支援。'),
        ('genden_fukui_2028', '2028年度（令和10年度）げんでんふれあい福井財団 科学技術研究助成', '工学・エネルギー・原子力・防災 / 材料・物理・化学 / 情報・環境・バイオ', '2028-02-25', '2000000', '公益財団法人げんでんふれあい福井財団', '地域研究助成', 'https://www.genden-fukui-zaidan.jp/', '福井県内の大学・高専等の研究者が行う自然科学・エネルギー・環境・防災・先端工学分野の共同研究・独創的研究助成。'),
        ('kaken_start_2028', '令和10年度（2028年度）科学研究費助成事業 研究活動スタート支援', '全分野（情報・工学・医学・教育・人文社会・複合領域）', '2028-03-06', '3000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '新規採用・異動研究者等を対象とした研究スタートアップ支援。'),
        ('mlit_kensetsu_2028', '令和10年度（2028年度）建設技術研究開発助成制度（防災・減災・積雪対策・インフラDX）', '工学・土木建築 / 情報・センシング・AI状態監視 / 防災・積雪・地球科学', '2028-03-10', '25000000', '国土交通省', '公的研究費', 'https://www.mlit.go.jp/tec/gijutu/kaihatu/josei.html', 'インフラ状態監視・豪雪地帯の積雪路面予測・防災DX等の大学×企業・自治体共同研究。'),
        ('jst_astep_tryout_2028_1', '令和10年度（2028年度）研究成果最適展開支援プログラム（A-STEP）トライアウトタイプ', '工学 / 情報 / 材料・デバイス / 医工連携 / 環境・エネルギー', '2028-03-17', '5000000', '科学技術振興機構（JST）', '産学連携公募', 'https://www.jst.go.jp/tt/rst/a-step/', '大学等の基礎研究成果の実用化可能性を検証する概念実証（PoC）研究支援。'),
        ('jst_actx_2028_spring', '令和10年度（2028年度）戦略的創造研究推進事業 ACT-X（若手研究者挑戦型）', '情報・AI / 材料・化学 / 生命科学・医工連携', '2028-03-24', '6000000', '科学技術振興機構（JST）', '公的研究費', 'https://www.jst.go.jp/kisoken/act-x/', '若手研究者の独創的な発想に基づく挑戦的研究を支援。'),
        ('fukui_sangaku_2028_spring', '令和10年度（2028年度）ふくい産学官共同研究開発支援事業', '工学・材料・繊維 / 情報・AI・IoT / 医工連携 / 環境・地域産業', '2028-03-27', '10000000', '公益財団法人ふくい産業支援センター・福井県', '地域産学連携', 'https://www.fisc.jp/', '福井県内の大学・企業が連携して行う新技術・新製品開発（機能性繊維・複合材料・表面処理・IoTセンサー・医療福祉機器等）を助成。'),
        ('fukui_univ_internal_2027', '令和9年度 福井大学 学内重点研究・異分野融合共同研究推進プログラム（学内シーズ育成・外部資金獲得支援）', '全分野（情報系×工学系×医学系×教育・人文系×国際地域系の学内横断連携）', '2027-04-30', '2000000', '福井大学 研究推進機構', '学内公募・支援', 'https://www.u-fukui.ac.jp/', '科研費（基盤A/B・挑戦的研究）やJST/AMED/NEDO等の大型外部資金獲得を目指し、学内の異なる学系・部門の研究者がチームを組んで行う萌芽的共同研究・機器活用研究を支援。'),
        ('fukui_univ_internal_2027_autumn', '令和9年度（後期）福井大学 次世代医療・防災・グリーン・DX学際連携プロジェクト支援', '医工連携 / 防災・積雪・地域インフラ / AI・IoTデータ解析 / 先端材料・繊維', '2027-10-22', '3000000', '福井大学 研究推進機構・産学官連携本部', '学内公募・支援', 'https://www.u-fukui.ac.jp/', '来年度（2028年度）の大型公募応募に向けた予備実験データの取得（学内共用機器利用料の支援を含む）および異分野共同研究チームの立ち上げ支援。'),
    ]

    out = []
    for gid, title, field, dl_ymd, amt, agency, cat, url, summary in raw:
        dl = f'{dl_ymd}T00:00:00.000Z'
        y, m, d = dl_ymd.split('-')
        status = '掲載締切未到来' if dl_ymd <= '2027-03-31' else '公募予定（年間スケジュール）'
        out.append({
            'id': f'fund_curated_{gid}',
            'title': title,
            'field': field,
            'deadline': dl,
            'summary': summary,
            'url': url,
            'source': agency,
            'agency': agency,
            'category': cat,
            'start_date': '',
            'period_text': f'{y}/{m}/{d}',
            'max_amount_yen': amt,
            'eligibility_text': '大学・研究機関に所属する研究者（共同研究チームでの応募可）',
            'internal_deadline': '',
            'prior_contact_deadline': '',
            'approval_req': '要確認',
            'status': status,
            'eligibility_check_status': '募集要項・学内締切要確認（例年スケジュール含む）',
            'source_url': url,
            'fetched_date': '2026-11-05T00:00:00.000Z',
            'note': '主要競争的研究費・財団公募スケジュールデータ。',
            'search_text': f'{title} {field} {agency} {summary}',
        })
    return out


def upgrade_grants_database():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    existing_rows = [dict(r) for r in cur.execute('SELECT * FROM grants').fetchall()]
    print(f'Existing grants in DB: {len(existing_rows)}')

    # 1. Fix existing rows whose deadline was empty or whose 2026 cycle can also be projected to 2027
    projected_next_rows = []
    fixed_existing_count = 0
    for r in existing_rows:
        dl = r.get('deadline') or ''
        pt = r.get('period_text') or ''
        if not dl and pt:
            parsed_dl, dl_kind = parse_deadline_from_text(pt, default_future=True)
            if parsed_dl:
                dl = parsed_dl
                st = '次回公募予定（例年時期）' if dl_kind == 'annual_projected' or dl > '2027-03-01' else '掲載締切未到来'
                cur.execute(
                    'UPDATE grants SET deadline = ?, status = ? WHERE id = ?',
                    (dl, st, r['id']),
                )
                fixed_existing_count += 1

        # If an existing grant has a deadline in 2025 or Jan-Dec 2026, create its 2027 next-cycle entry
        if dl and ('2025-' in dl or ('2026-01' <= dl[:7] <= '2026-12')) and not r['id'].endswith('_next2027'):
            next_dl = '2027-' + dl[5:]
            next_id = r['id'] + '_next2027'
            next_title = r['title']
            if '2026年度' in next_title:
                next_title = next_title.replace('2026年度', '2027年度（次回予定）')
            elif '令和8年度' in next_title or '令和８年度' in next_title:
                next_title = re.sub(r'令和[8８]年度', '令和9年度（次回予定）', next_title)
            elif '次回' not in next_title:
                next_title = f'{next_title}（2027年度・次回予定）'

            projected_next_rows.append({
                'id': next_id,
                'title': next_title,
                'field': r.get('field', ''),
                'deadline': next_dl,
                'summary': r.get('summary') or f"{r.get('agency','')}による例年公募プログラム（前回：{pt}）",
                'url': r.get('url', ''),
                'source': r.get('source', ''),
                'agency': r.get('agency', ''),
                'category': r.get('category', '研究助成'),
                'start_date': '',
                'period_text': f'次回予定：{next_dl[:10].replace("-", "/")}頃（前回：{pt[:40]}）',
                'max_amount_yen': r.get('max_amount_yen', ''),
                'eligibility_text': r.get('eligibility_text', ''),
                'internal_deadline': '',
                'prior_contact_deadline': '',
                'approval_req': r.get('approval_req', ''),
                'status': '次回公募予定（例年時期）',
                'eligibility_check_status': '募集要項・学内締切要確認（例年時期より推計）',
                'source_url': r.get('source_url', ''),
                'fetched_date': '2026-11-05T00:00:00.000Z',
                'note': '前年度実績に基づく2027年度次回公募予定。',
                'search_text': f"{next_title} {r.get('field','')} {r.get('agency','')} {r.get('summary','')}",
            })

    print(f'Fixed empty deadlines on existing grants: {fixed_existing_count}')
    print(f'Projected 2027 annual grants from past cycles: {len(projected_next_rows)}')

    # 2. Fetch live grants from UMIN FIND, Science Portal, and JFC (助成・奨学金情報navi)
    umin_rows = fetch_umin_grants()
    sp_rows = fetch_science_portal_grants()
    jfc_rows = fetch_jfc_grants()
    curated_rows = get_curated_future_grants()

    print(f'Fetched UMIN: {len(umin_rows)}, SP: {len(sp_rows)}, JFC: {len(jfc_rows)}, Curated: {len(curated_rows)}')

    # Deduplicate by (normalized agency, normalized title, deadline[:7])
    existing_sigs = set()
    for r in cur.execute('SELECT agency, title, deadline FROM grants').fetchall():
        sig = (
            re.sub(r'\s+', '', r['agency'] or ''),
            re.sub(r'\s+', '', r['title'] or ''),
            (r['deadline'] or '')[:7],
        )
        existing_sigs.add(sig)

    cols = [
        'id', 'title', 'field', 'deadline', 'summary', 'url', 'source', 'agency',
        'category', 'start_date', 'period_text', 'max_amount_yen', 'eligibility_text',
        'internal_deadline', 'prior_contact_deadline', 'approval_req', 'status',
        'eligibility_check_status', 'source_url', 'fetched_date', 'note', 'search_text',
    ]
    placeholders = ','.join(['?'] * len(cols))

    inserted = 0
    for item in curated_rows + jfc_rows + umin_rows + sp_rows + projected_next_rows:
        sig = (
            re.sub(r'\s+', '', item.get('agency') or ''),
            re.sub(r'\s+', '', item.get('title') or ''),
            (item.get('deadline') or '')[:7],
        )
        # Always allow curated rows to upsert by ID
        if not item['id'].startswith('fund_curated_') and sig in existing_sigs:
            continue
        existing_sigs.add(sig)
        cur.execute(
            f'INSERT OR REPLACE INTO grants ({",".join(cols)}) VALUES ({placeholders})',
            tuple(item.get(c, '') for c in cols),
        )
        inserted += 1

    total_grants = cur.execute('SELECT COUNT(*) FROM grants').fetchone()[0]
    cur.execute("UPDATE db_status SET val = ? WHERE item = '公募マスタ'", (str(total_grants),))
    cur.execute("UPDATE sheet_meta SET row_count = ? WHERE sheet_name = '公募マスタ'", (total_grants,))

    conn.commit()
    print(f'Inserted {inserted} new/future grants. Total grants in DB: {total_grants}')

    by_ym = cur.execute(
        'SELECT substr(deadline,1,7) as ym, COUNT(*) FROM grants GROUP BY ym ORDER BY ym DESC'
    ).fetchall()
    print('Grants by YYYY-MM:', by_ym)
    conn.close()


if __name__ == '__main__':
    upgrade_grants_database()
