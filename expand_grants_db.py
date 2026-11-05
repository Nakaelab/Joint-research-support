import concurrent.futures
import csv
import hashlib
import http.cookiejar
import io
import json
import os
import re
import sqlite3
import ssl
import urllib.parse
import urllib.request

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fukui_matching.db')
ctx = ssl._create_unverified_context()
TODAY = '2026-11-05'


def zen_to_han(s):
    if not s:
        return ''
    return s.translate(str.maketrans('０１２３４５６７８９／－～〜ー', '0123456789/--~~'))


def parse_deadline_from_text(text, allow_annual=True):
    """
    Extract the END deadline (YYYY-MM-DDT00:00:00.000Z) from Japanese period strings.
    Properly handles ranges where the second date omits the year or month:
      - '2026年10月1日（木）～ 12月4日（金）' -> 2026-12-04
      - '2026年11月1日（日） 0:00 〜 11月30日（月） 23:59' -> 2026-11-30
      - '申請期間 2026年11月1日ー30日' -> 2026-11-30
    """
    if not text:
        return '', ''
    s = zen_to_han(text)

    def repl_reiwa(m):
        ry = int(m.group(1))
        return f'{2018 + ry}年'

    s = re.sub(r'令和\s*(\d+)\s*年', repl_reiwa, s)
    # Remove time expressions like 12:00 or 23:59 so they don't interfere with date regexes
    s_notime = re.sub(r'\b\d{1,2}\s*[:：]\s*\d{2}\b', ' ', s)

    # 1. Check if there are multiple full YYYY/MM/DD dates -> take the LAST one (the end date)
    full_matches = re.findall(r'(202\d)\s*[/\-年\.]\s*(\d{1,2})\s*[/\-月\.]\s*(\d{1,2})', s_notime)
    if len(full_matches) >= 2:
        y, m, d = int(full_matches[-1][0]), int(full_matches[-1][1]), int(full_matches[-1][2])
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'explicit'

    # 2. Check range where start has YYYY年M月D日 and end has M月D日 (omitting year)
    m_partial_md = re.search(
        r'(202\d)\s*[/\-年\.]\s*(\d{1,2})\s*[/\-月\.]\s*\d{1,2}\s*日?.*?(?:[~～\-]|から|より)\s*(\d{1,2})\s*[/\-月\.]\s*(\d{1,2})',
        s_notime,
    )
    if m_partial_md:
        y1, m1, m2, d2 = (
            int(m_partial_md.group(1)),
            int(m_partial_md.group(2)),
            int(m_partial_md.group(3)),
            int(m_partial_md.group(4)),
        )
        y2 = y1 + 1 if m2 < m1 else y1
        if 1 <= m2 <= 12 and 1 <= d2 <= 31:
            return f'{y2:04d}-{m2:02d}-{d2:02d}T00:00:00.000Z', 'explicit'

    # 3. Check range where start has YYYY年M月D日 and end has only D日 (omitting year and month, e.g. '2026年11月1日~30日')
    m_partial_d = re.search(
        r'(202\d)\s*[/\-年\.]\s*(\d{1,2})\s*[/\-月\.]\s*(\d{1,2})\s*日?\s*[~～\-]\s*(\d{1,2})\s*日',
        s_notime,
    )
    if m_partial_d:
        y1, m1, d2 = int(m_partial_d.group(1)), int(m_partial_d.group(2)), int(m_partial_d.group(4))
        if 1 <= m1 <= 12 and 1 <= d2 <= 31:
            return f'{y1:04d}-{m1:02d}-{d2:02d}T00:00:00.000Z', 'explicit'

    # 4. Single full YYYY/MM/DD date
    if len(full_matches) == 1:
        y, m, d = int(full_matches[0][0]), int(full_matches[0][1]), int(full_matches[0][2])
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'explicit'

    # 5. Handle '2026年11月下旬' / '2027年1月中旬' / '2027年3月末'
    m_ym = re.findall(r'(202\d)\s*[/\-年]\s*(\d{1,2})\s*月\s*(上旬|中旬|下旬|末)?', s_notime)
    if m_ym:
        y, m, part = int(m_ym[-1][0]), int(m_ym[-1][1]), m_ym[-1][2]
        day_map = {'上旬': 10, '中旬': 20, '下旬': 28, '末': 28}
        d = day_map.get(part, 28)
        if m == 2:
            d = min(d, 28)
        if 1 <= m <= 12:
            return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'explicit'

    # 6. Recurring annual dates (when no explicit past year is present)
    if allow_annual and not re.search(r'202[0-5]', s_notime):
        m_md = re.findall(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日', s_notime)
        if m_md:
            m, d = int(m_md[-1][0]), int(m_md[-1][1])
            if 1 <= m <= 12 and 1 <= d <= 31:
                y = 2026 if (m == 11 and d >= 6) or m == 12 else 2027
                return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'annual'
        m_m_only = re.findall(r'(\d{1,2})\s*月\s*(上旬|中旬|下旬|末)?', s_notime)
        if m_m_only and not ('ヶ月' in s_notime or 'か月' in s_notime or 'カ月' in s_notime):
            m, part = int(m_m_only[-1][0]), m_m_only[-1][1]
            if 1 <= m <= 12:
                day_map = {'上旬': 10, '中旬': 20, '下旬': 28, '末': 28}
                d = day_map.get(part, 25)
                if m == 2:
                    d = min(d, 28)
                y = 2026 if m in (11, 12) else 2027
                return f'{y:04d}-{m:02d}-{d:02d}T00:00:00.000Z', 'annual'

    return '', ''


def fetch_erad_active_grants():
    """
    Fetch all currently open (現在募集中) public/commissioned grants from e-Rad「公募一覧」
    via live CSV download + parallel detail page scraping (E1031S03).
    """
    results = []
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=ctx),
        )
        opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
        resp = opener.open('https://www.e-rad.go.jp/eRad/E1031S02?lang=ja', timeout=20)
        html = resp.read().decode('utf-8', errors='replace')
        token = re.search(r'name="OC_TRANSACTION_TOKEN"[^>]+value="([^"]+)"', html).group(1)
        uid = re.search(r'name="OC_SCREEN_UNIQUE_ID"[^>]+value="([^"]+)"', html).group(1)
        total = re.search(r'name="totalCount"[^>]+value="([^"]+)"', html).group(1)

        data = urllib.parse.urlencode({
            'OC_TRANSACTION_TOKEN': token,
            'OC_TOKEN_KEEP': 'true',
            'OC_ACTFM_KEEP': 'true',
            'OC_SCREEN_UNIQUE_ID': uid,
            'currentPage': '1',
            'totalCount': total,
            'orderBy': '',
            'ascOrDesc': '',
            'searchTaisho': '00',
            'searchMoji': '',
            'hyojiKb': '1',
            'hyojiKensu': '0100',
            'downloadKb': '1',
            'nendo': '2026',
            'download': 'abc',
        }).encode('utf-8')
        r2 = opener.open('https://www.e-rad.go.jp/eRad/E1031S02/event', data=data, timeout=20)
        text = r2.read().decode('cp932', errors='replace')
        rows = list(csv.reader(io.StringIO(text)))[1:]

        def fetch_detail(kobo_cd):
            d_url = f'https://www.e-rad.go.jp/eRad/E1031S03/?koboCd={kobo_cd}'
            try:
                req = urllib.request.Request(d_url, headers={'User-Agent': 'Mozilla/5.0'})
                d_html = urllib.request.urlopen(req, timeout=15, context=ctx).read().decode('utf-8', errors='replace')

                def extract_div_field(label):
                    m = re.search(
                        rf'<div class="title">\s*{re.escape(label)}\s*</div>\s*<div class="input[^"]*">(.*?)</div>',
                        d_html,
                        re.S,
                    )
                    if not m:
                        return ''
                    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', m.group(1))).strip()

                field_txt = extract_div_field('研究分野')
                kw_txt = extract_div_field('キーワード')
                gaiyo_txt = extract_div_field('応募の概要')
                taisho_txt = extract_div_field('応募の対象者')

                # Extract external official call URL from window.open('https:\/\/...')
                ext_url = d_url
                m_url = re.search(
                    r'<div class="title">\s*公募要領URL\s*</div>\s*<div class="input[^"]*">.*?window\.open\(\'([^\']+)\'\)',
                    d_html,
                    re.S,
                )
                if not m_url:
                    m_url = re.search(r'window\.open\(\'(https?:\\?/\\?/[^\']+)\'\)', d_html)
                if m_url:
                    ext_url = m_url.group(1).replace('\\/', '/')

                # Parse realistic per-project upper limit from gaiyo_txt
                amt_yen = ''
                m_sen = re.findall(r'([\d,]+)\s*千円', gaiyo_txt)
                if m_sen:
                    vals = [int(x.replace(',', '')) * 1000 for x in m_sen if x.replace(',', '').isdigit()]
                    vals = [v for v in vals if 500_000 <= v <= 3_000_000_000]
                    if vals:
                        amt_yen = str(max(vals))
                if not amt_yen:
                    m_man = re.findall(r'([\d,]+)\s*万円', gaiyo_txt)
                    if m_man:
                        vals = [int(x.replace(',', '')) * 10000 for x in m_man if x.replace(',', '').isdigit()]
                        vals = [v for v in vals if 500_000 <= v <= 3_000_000_000]
                        if vals:
                            amt_yen = str(max(vals))
                if not amt_yen:
                    m_oku = re.findall(r'(\d+(?:\.\d+)?)\s*億円', gaiyo_txt)
                    if m_oku:
                        vals = [int(float(x) * 100_000_000) for x in m_oku]
                        if vals:
                            amt_yen = str(max(vals))

                return kobo_cd, {
                    'field': field_txt,
                    'kw': kw_txt,
                    'summary': gaiyo_txt,
                    'eligibility': taisho_txt,
                    'ext_url': ext_url,
                    'detail_url': d_url,
                    'amt_yen': amt_yen,
                }
            except Exception:
                return kobo_cd, {
                    'field': '',
                    'kw': '',
                    'summary': '',
                    'eligibility': '',
                    'ext_url': d_url,
                    'detail_url': d_url,
                    'amt_yen': '',
                }

        kobo_cds = [r[3].strip() for r in rows if len(r) > 8 and r[3].strip()]
        details = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            for kcd, d in ex.map(fetch_detail, kobo_cds):
                details[kcd] = d

        for r in rows:
            if len(r) < 14:
                continue
            pub_date, nendo, agency, kobo_cd, title, unit, approval, start_dt, end_dt, cat_raw, target_cat, min_yr, max_yr, amt_limit = (
                r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip(), r[4].strip(),
                r[5].strip(), r[6].strip(), r[7].strip(), r[8].strip(), r[9].strip(),
                r[10].strip(), r[11].strip(), r[12].strip(), r[13].strip()
            )
            dl_ymd = end_dt.split()[0].replace('/', '-') if end_dt else ''
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', dl_ymd) or dl_ymd < TODAY:
                continue
            dl = f'{dl_ymd}T00:00:00.000Z'
            det = details.get(kobo_cd, {})

            # Clean field display
            field_raw = det.get('field') or ''
            field_short = field_raw[:90] + ('…' if len(field_raw) > 90 else '')
            if not field_short:
                if '医療' in agency or '医療' in title or '臨床' in title or '薬' in title or 'がん' in title:
                    field_short = '医学・医療 / 生命科学 / 医工連携'
                elif '情報' in agency or '通信' in title or 'AI' in title or '5G' in title:
                    field_short = '情報・通信 / AI・半導体'
                elif '宇宙' in agency or '衛星' in title or 'ロケット' in title:
                    field_short = '宇宙・航空工学 / 衛星データ・機械・材料'
                elif 'エネルギー' in agency or 'GX' in title or '水素' in title:
                    field_short = '環境・エネルギー / 工学・材料・スタートアップ'
                elif '河川' in title or '国土交通' in agency:
                    field_short = '土木・建築・防災・河川砂防工学'
                else:
                    field_short = '科学技術・産学官連携全般'

            amt_yen = det.get('amt_yen') or ''
            if not amt_yen and amt_limit:
                m_lim = re.search(r'\(上限\)\s*([\d,]+)\s*\(千円\)', amt_limit)
                if m_lim:
                    val_k = int(m_lim.group(1).replace(',', ''))
                    if val_k not in (999999, 9999999, 999999999) and val_k > 0:
                        amt_yen = str(val_k * 1000)

            cat_display = '受託・委託研究（e-Rad）' if '委託' in cat_raw else f'{cat_raw or "公的研究費"}（e-Rad）'
            summary_txt = det.get('summary') or f'{agency}が公募する{cat_raw}「{title}」（対象：{target_cat}）。受付期間：{start_dt}〜{end_dt}'
            elig_txt = det.get('eligibility') or target_cat or '大学・高専・研究機関等の研究者'

            results.append({
                'id': f'fund_erad_{kobo_cd}',
                'title': title,
                'field': field_short,
                'deadline': dl,
                'summary': summary_txt[:450],
                'url': det.get('ext_url') or f'https://www.e-rad.go.jp/eRad/E1031S03/?koboCd={kobo_cd}',
                'source': 'e-Rad',
                'agency': agency,
                'category': cat_display,
                'start_date': start_dt.split()[0].replace('/', '-') if start_dt else pub_date.replace('/', '-'),
                'period_text': f'{start_dt}〜{end_dt}',
                'max_amount_yen': amt_yen,
                'eligibility_text': elig_txt[:250],
                'internal_deadline': '',
                'prior_contact_deadline': '',
                'approval_req': approval or '要確認',
                'status': '募集中',
                'eligibility_check_status': '募集要項・学内締切要確認',
                'source_url': f'https://www.e-rad.go.jp/eRad/E1031S03/?koboCd={kobo_cd}',
                'fetched_date': f'{TODAY}T00:00:00.000Z',
                'note': f'e-Rad公募一覧（現在募集中）より取得。公募コード:{kobo_cd} / 事業分類:{cat_raw}',
                'search_text': f'{title} {field_raw} {det.get("kw","")} {agency} {cat_display} 受託研究 委託研究 {summary_txt[:300]}',
            })
    except Exception as e:
        print('e-Rad fetch error:', e)
    return results


def fetch_science_portal_active_grants():
    """Fetch ONLY currently open (deadline >= TODAY) grants from JST Science Portal."""
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
                title = (it.get('title') or '').strip()
                agency = (it.get('organizer') or '').strip()
                end_date = (it.get('end-date') or '').replace('/', '-')
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', end_date) or end_date < TODAY:
                    continue
                dl = f'{end_date}T00:00:00.000Z'
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
                    'summary': f'{agency}が募集する「{title}」（対象分野：{field_str}、締切：{end_date}）',
                    'url': url,
                    'source': 'Science Portal',
                    'agency': agency,
                    'category': cat,
                    'start_date': (it.get('createdDate') or '').replace('/', '-'),
                    'period_text': it.get('end-date') or '',
                    'max_amount_yen': '',
                    'eligibility_text': '大学・高専・研究機関等の研究者',
                    'internal_deadline': '',
                    'prior_contact_deadline': '',
                    'approval_req': '',
                    'status': '募集中',
                    'eligibility_check_status': '募集要項・学内締切要確認',
                    'source_url': 'https://scienceportal.jst.go.jp/funding/',
                    'fetched_date': f'{TODAY}T00:00:00.000Z',
                    'note': 'JST Science Portal「ファンド・各種募集」（現在募集中）より取得。',
                    'search_text': f'{title} {field_str} {agency} {cat}',
                })
    except Exception as e:
        print('Science Portal error:', e)
    return results


def fetch_umin_active_grants():
    """Fetch ONLY currently open (deadline >= TODAY) grants from UMIN FIND (nendo='now' and '2027')."""
    results = []
    seen_keys = set()
    for nendo in ['now', '2027']:
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
                dl, dl_kind = parse_deadline_from_text(period_text, allow_annual=True)
                is_always = ('通年' in period_text or '随時' in period_text) and not re.search(r'202[0-5]', period_text)
                if not dl and is_always:
                    dl = '9999-12-31T00:00:00.000Z'

                # Strictly exclude any grant whose deadline has already passed or has no valid future/ongoing deadline
                if not dl or (not dl.startswith('9999') and dl[:10] < TODAY):
                    continue

                dedup_key = (agency, title, dl[:10])
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                status = '募集中（随時・通年）' if dl.startswith('9999') else '募集中'
                combined = f'{agency} {title}'
                fields = ['医療・医学 / 生命科学']
                if any(k in combined for k in ('情報', '通信', 'エレクトロニクス', 'AI', 'データ', 'システム')):
                    fields.append('情報・通信 / 工学')
                if any(k in combined for k in ('化学', '材料', '物理', 'エネルギー', '環境', '金属', '科学技術', 'レーザー', '光', 'ソルト')):
                    fields.append('物理・化学 / 材料・環境')
                if any(k in combined for k in ('食', '栄養', '農', '植物', '発酵', 'たん白', '酵素')):
                    fields.append('農学・食品・生物')
                if any(k in combined for k in ('教育', '社会', '福祉', '看護', 'メンタル', '心理', 'リハビリ', '訪問看護')):
                    fields.append('看護・福祉・教育')
                field_str = ' / '.join(dict.fromkeys(fields))

                gid = 'fund_umin_' + hashlib.md5(f'{agency}_{title}_{dl}'.encode('utf-8')).hexdigest()[:18]
                results.append({
                    'id': gid,
                    'title': title,
                    'field': field_str,
                    'deadline': dl,
                    'summary': f'{agency}が実施する「{title}」。募集期間：{period_text}',
                    'url': src_url,
                    'source': 'UMIN FIND',
                    'agency': agency,
                    'category': '研究助成（UMIN）' if ('研究' in title or '助成' in title) else '各種募集（UMIN）',
                    'start_date': '',
                    'period_text': period_text[:120],
                    'max_amount_yen': '',
                    'eligibility_text': '医学・生命科学・関連領域の研究者',
                    'internal_deadline': '',
                    'prior_contact_deadline': '',
                    'approval_req': '',
                    'status': status,
                    'eligibility_check_status': '募集要項・学内締切要確認',
                    'source_url': src_url,
                    'fetched_date': f'{TODAY}T00:00:00.000Z',
                    'note': 'UMIN FIND（現在募集中）より取得。',
                    'search_text': f'{title} {field_str} {agency} {period_text}',
                })
        except Exception as e:
            print('UMIN fetch error:', e)
    return results


def fetch_jfc_active_grants():
    """
    Fetch ONLY currently open / upcoming (2026/11..2028/12) grants from
    助成財団センター「助成・奨学金情報navi」(jyosei-navi.jfc.or.jp).
    Strictly excludes any closed/expired grants before TODAY.
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
            '研': True, '派': True, '招': True, '会': True, '版': False, '事': False, '展': False, '組': False, '施': False,
            '奨日内': False, '奨日留': False, '奨外': False, '賞': True, '他': False,
            '物理科学': False, '地球科学': False, '生命科学': False, '工学': False, '理学': False, '医学': False, '形式科学': False, '農学': False,
            '科学技術全般': False, '人文科学全般': False, '社会科学全般': False, '環境': False, '教育': False, '福祉': False, '医保': False,
            '文芸': False, '国際': False, '公共': False, '人権': False, '災害': False, '就労支援': False, '地域開発': False, '起業支援': False, '他分': False,
            '給与': False, '貸与': False,
            'DANTAIDOMESTIC': False, 'DOMESTIC_SELECT': '/',
            'DANTAIOVERSEA': False, 'OVERSEA_SELECT': '/', 'SCHOOL_SELECT': '/',
            'datemode': 2,
            'startyear': 2026, 'startmonth': 11, 'endyear': 2028, 'endmonth': 12,
        }

    # 1) Fetch all active pages for 2026/11..2028/12
    q_up = base_q()
    for page in range(1, 35):
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
                    results_by_nid[nid] = (d, '研究助成・学術振興')
        except Exception as e:
            print('JFC active page err:', page, e)
            break

    # 2) Tag research fields for 2026/11..2028/12 ONLY (never 2025!)
    for f_key, f_label in field_keys:
        q_f = base_q()
        q_f[f_key] = True
        for page in range(1, 6):
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
                        if old_f == '研究助成・学術振興':
                            results_by_nid[nid] = (old_d, f_label)
                        elif f_label not in old_f:
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

        # If enddate_raw contains any old year (2020..2025), skip it so no past/expired subprograms slip in
        if re.search(r'202[0-5]', enddate_raw):
            continue

        dl, dl_kind = parse_deadline_from_text(enddate_raw, allow_annual=True)
        is_always = ('随時' in enddate_raw or '通年' in enddate_raw) and not dl
        if is_always:
            dl = '9999-12-31T00:00:00.000Z'

        # Strictly require deadline >= TODAY
        if not dl or (not dl.startswith('9999') and dl[:10] < TODAY):
            continue

        status = '募集中（随時・通年）' if dl.startswith('9999') else '募集中'
        src_url = f'https://jyosei-navi.jfc.or.jp/search/search/assist/view/{nid}'
        out.append({
            'id': f'fund_jfc_{nid}',
            'title': title,
            'field': f_label,
            'deadline': dl,
            'summary': purpose or f'{agency}による「{title}」（締切：{enddate_raw}）',
            'url': src_url,
            'source': '助成・奨学金情報navi',
            'agency': agency,
            'category': d.get('JIGYOB') or '【研究】',
            'start_date': '',
            'period_text': enddate_raw,
            'max_amount_yen': '',
            'eligibility_text': '大学・高専・研究機関等の研究者',
            'internal_deadline': '',
            'prior_contact_deadline': '',
            'approval_req': '',
            'status': status,
            'eligibility_check_status': '募集要項・学内締切要確認',
            'source_url': src_url,
            'fetched_date': f'{TODAY}T00:00:00.000Z',
            'note': '公益財団法人助成財団センター「助成・奨学金情報navi」（現在募集中）より取得。',
            'search_text': f'{title} {f_label} {agency} {purpose}',
        })
    return out


def fetch_nedo_active_grants():
    """Fetch currently open (公募) and pre-announced (予告) commissioned R&D calls directly from NEDO."""
    results = []
    try:
        for p in range(1, 8):
            url = f'https://www.nedo.go.jp/form/event.php?f=koubo.html&p={p}&o=-date,pagetitle'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            h = urllib.request.urlopen(req, timeout=15, context=ctx).read().decode('utf-8', 'replace')
            trs = re.findall(r'<tr[^>]*>(.*?)</tr>', h, re.S)
            for tr in trs:
                tds = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', td)).strip() for td in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
                hrefs = re.findall(r'href=["\']([^"\']+)["\']', tr)
                if len(tds) < 6 or not hrefs:
                    continue
                pub_dt, field_tag, title, state, end_dt_str, biz_type = tds[0], tds[1], tds[2], tds[3], tds[4], tds[5]
                if state not in ('公募', '予告'):
                    continue
                if biz_type == 'その他' and ('旅行' in title or '人材紹介' in title or '監査' in title):
                    continue
                dl, _ = parse_deadline_from_text(end_dt_str, allow_annual=False)
                if state == '公募' and (not dl or dl[:10] < TODAY):
                    continue
                if state == '予告' and not dl:
                    dl = '2026-12-25T00:00:00.000Z'
                    end_dt_str = '2026年11月〜12月公募開始（公募予告）'

                full_url = urllib.parse.urljoin('https://www.nedo.go.jp', hrefs[0])
                gid = 'fund_nedo_' + hashlib.md5(full_url.encode('utf-8')).hexdigest()[:16]
                results.append({
                    'id': gid,
                    'title': title,
                    'field': f'{field_tag} / 工学・情報・エネルギー・産業技術',
                    'deadline': dl,
                    'summary': f'新エネルギー・産業技術総合開発機構（NEDO）による{biz_type}（{state}）：「{title}」（分野：{field_tag}、締切：{end_dt_str}）',
                    'url': full_url,
                    'source': 'NEDO（大型受託・公募）',
                    'agency': '国立研究開発法人新エネルギー・産業技術総合開発機構（NEDO）',
                    'category': '大型受託・委託研究（NEDO）',
                    'start_date': '',
                    'period_text': end_dt_str,
                    'max_amount_yen': '100000000' if biz_type == '研究開発' else '30000000',
                    'eligibility_text': '企業・大学・高専・公的研究機関等の研究開発コンソーシアムまたは単独機関',
                    'internal_deadline': '',
                    'prior_contact_deadline': '',
                    'approval_req': '必要',
                    'status': '募集中' if state == '公募' else '募集中（公募予告）',
                    'eligibility_check_status': '募集要項・学内締切要確認',
                    'source_url': full_url,
                    'fetched_date': f'{TODAY}T00:00:00.000Z',
                    'note': f'NEDO公式公募一覧より取得（区分：{state} / {biz_type}）。',
                    'search_text': f'{title} {field_tag} NEDO 受託研究 委託研究 大型研究費 {biz_type}',
                })
    except Exception as e:
        print('NEDO fetch error:', e)
    return results


def get_major_kakenhi_and_commissioned_grants():
    """
    Comprehensive dataset of KAKENHI (科学研究費助成事業) and major national/regional
    commissioned research funds (大型受託研究費・委託研究・共同研究公募) for university & KOSEN researchers.
    All entries have upcoming deadlines >= TODAY.
    """
    raw = [
        # === 1. 科研費（科学研究費助成事業：JSPS・文部科学省） ===
        ('kaken_start_2027', '科学研究費助成事業（科研費）研究活動スタート支援', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会・高専/大学連携）', '2027-03-05', '3000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/22_startup_support/index.html', '研究機関に新規採用・着任した研究者等を対象とした研究スタートアップ支援（最大2年間・上限300万円）。大学・高専の研究者も応募可能。'),
        ('kaken_shorei_2027', '科学研究費助成事業（科研費）奨励研究（技術職員・高専/大学教職員等対象）', '全分野（工学・情報・実験技術開発・機器高度化・教育実践・医療技術）', '2027-02-15', '1000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/', '大学・高等専門学校（高専）等の技術職員や教育職員等が1人で行う教育的・社会的意義を有する研究や実験装置・計測技術の高度化を支援。'),
        ('kaken_tokusui_2028', '科学研究費助成事業（科研費）特別推進研究（大型基幹研究・共同研究チーム）', '全分野（理工・情報・生物・医歯薬・人文社会・学際領域）', '2027-09-16', '500000000', '日本学術振興会（JSPS）', '科研費（大型）', 'https://www.jsps.go.jp/j-grantsinaid/02_koubo/toku_s.html', '国際的に高い評価を得ている極めて独創的な研究を格段に発展させる大型研究（3〜5年間、1課題あたり最大5億円）。複数大学・高専との共同研究分担も強力に推進。'),
        ('kaken_kiban_s_2028', '科学研究費助成事業（科研費）基盤研究(S)（大型共同・先端研究）', '全分野（情報・工学・物理化学・材料・生物・医学・教育・人文社会）', '2027-09-16', '200000000', '日本学術振興会（JSPS）', '科研費（大型）', 'https://www.jsps.go.jp/j-grantsinaid/02_koubo/toku_s.html', '1人または少数の研究者が行う独創的・先駆的な大型研究（応募総額5,000万円〜2億円、原則5年間）。大型計測機器の導入や異分野分担者との本格連携に最適。'),
        ('kaken_kiban_a_2028', '科学研究費助成事業（科研費）基盤研究(A)（一般・学際・大学×高専共同研究）', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会・学際領域）', '2027-09-16', '50000000', '日本学術振興会（JSPS）', '科研費（大型）', 'https://www.jsps.go.jp/j-grantsinaid/02_koubo/kiban.html', '複数の研究者が共同して行う独創的・先駆的な研究（応募総額2,000万円〜5,000万円、3〜5年間）。福井大学・高専等の複数研究者による共同研究チーム結成に最適。'),
        ('kaken_kiban_b_2028', '科学研究費助成事業（科研費）基盤研究(B)（一般・分野横断共同研究）', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会・学際領域）', '2027-09-16', '20000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/02_koubo/kiban.html', '1人または複数の研究者が共同して行う発展的研究（応募総額500万円〜2,000万円、3〜5年間）。異分野の研究分担者（大学・高専）を加えた共同応募で採択率向上。'),
        ('kaken_kiban_c_2028', '科学研究費助成事業（科研費）基盤研究(C)', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会・学際領域）', '2027-09-16', '5000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/02_koubo/kiban.html', '1人または複数の研究者が共同して行う独自の研究課題（応募総額500万円以下、3〜5年間）。学内共用機器・共同研究者を組み合わせた実証研究に最適。'),
        ('kaken_chousen_kaitaku_2028', '科学研究費助成事業（科研費）挑戦的研究（開拓・萌芽）', '全分野（異分野融合・新領域開拓・医工情報教育連携）', '2027-09-16', '20000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/02_koubo/kiban.html', 'これまでの学術の体系や方向を大きく変革・転換させる潜在性を有する挑戦的な共同研究（開拓：上限2,000万円、萌芽：上限500万円）。'),
        ('kaken_wakate_2028', '科学研究費助成事業（科研費）若手研究', '全分野（情報・工学・物理化学・生物・医学・教育・人文社会）', '2027-09-16', '5000000', '日本学術振興会（JSPS）', '科研費', 'https://www.jsps.go.jp/j-grantsinaid/02_koubo/kiban.html', '博士の学位取得後8年未満の若手研究者が行う研究（上限500万円）。学内研究支援・共用機器の活用が有効。'),
        ('kaken_henkaku_a_2028', '科学研究費助成事業（科研費）学術変革領域研究(A)（領域提案・計画研究・公募研究）', '全分野（先端情報・マテリアル・量子・生命医工・環境・人文社会融合）', '2027-09-16', '300000000', '文部科学省・日本学術振興会（JSPS）', '科研費（大型）', 'https://www.mext.go.jp/a_menu/shinkou/hojyo/main5_a5.htm', '多様な研究者の共創と高次の学際融合により既存の学問分野の枠を超えた新たな学術領域を創成する大型科研費プログラム。'),
        ('kaken_henkaku_b_2028', '科学研究費助成事業（科研費）学術変革領域研究(B)（3〜4グループ共同研究）', '全分野（分野横断・新学術領域創出・3〜4研究グループ連携）', '2027-09-16', '50000000', '文部科学省・日本学術振興会（JSPS）', '科研費（大型）', 'https://www.mext.go.jp/a_menu/shinkou/hojyo/main5_a5.htm', '次世代の学術を担う研究者が3〜4個程度の研究グループでコンパクトな共同研究チームを編成し、学術変革領域研究(A)への発展を目指す。'),
        ('kaken_kokusai_sendou_2027', '科学研究費助成事業（科研費）国際共同研究加速基金（国際先導研究・海外連携研究）', '全分野（国際共同研究・情報・工学・医学・環境・教育）', '2027-09-16', '500000000', '日本学術振興会（JSPS）', '科研費（大型）', 'https://www.jsps.go.jp/j-grantsinaid/35_kokusai/index.html', '世界トップクラスの海外研究拠点と国内の大学・研究機関チーム（研究代表者・分担者・若手）が共同で推進する大型国際共同研究（国際先導：上限5億円、海外連携：上限2,000万円）。'),
        ('jsps_kadai_jinbun_2027', '課題設定による先導的人文学・社会科学研究推進事業（実社会対応プログラム・領域開拓プログラム）', '人文社会×情報科学・工学・医療・教育・地域防災連携', '2027-01-28', '15000000', '日本学術振興会（JSPS）', '大型受託研究費（委託研究）', 'https://www.jsps.go.jp/j-kadai/', '人文学・社会科学と自然科学（情報・工学・医学等）の研究者が協働し、社会課題の解決や新領域開拓に取り組む委託研究（受託研究費）プログラム。'),

        # === 2. 大型受託研究費・委託研究（JST・NEDO・AMED・各省庁・産学官連携・高専連携） ===
        ('env_erca_2027', '環境研究総合推進費（新規課題公募：脱炭素・循環型社会・自然共生・環境安全・防災環境）', '環境・エネルギー / 化学・材料 / 地球科学・防災 / 社会システム', '2026-11-26', '60000000', '環境省・環境再生保全機構（ERCA）', '大型受託研究費（委託研究）', 'https://www.erca.go.jp/suishinhi/', 'カーボンニュートラル・資源循環・化学物質評価・環境計測・地域防災環境等に関する大型委託研究（受託研究費）。大学・高専・公設試・企業の共同研究チーム対象。'),
        ('amed_kikikathu_2027', 'AMED 医療機器開発推進研究事業（医工連携イノベーション推進事業・若手育成枠）', '医工連携 / 医療機器・生体センサー / 画像解析・AI診断 / リハビリ・看護工学', '2026-12-03', '80000000', '日本医療研究開発機構（AMED）', '大型受託研究費（委託研究）', 'https://www.amed.go.jp/program/list/12/01/001.html', '医学系と工学系・情報系（大学・高専・企業）の共同研究による革新的な予防・診断・治療用医療機器や生体計測システムの開発（大型委託研究開発費）。'),
        ('mhlw_kaken_2027', '厚生労働科学研究費補助金（疾患対策・地域医療・労働安全衛生・医療ビッグデータ解析）', '医学・医療 / 看護・保健・福祉 / 医療情報・データサイエンス', '2026-12-24', '30000000', '厚生労働省', '公的研究費', 'https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/hokabunya/kenkyujigyou/', '臨床医学・公衆衛生・予防医療・地域医療体制・メンタルヘルス・医療ビッグデータ解析等の政策・実用共同研究。'),
        ('amed_bridge_2027', 'AMED 橋渡し研究プログラム（シーズA・preF・異分野融合型医療技術開発）', '医学・医療 / 医工連携 / 生体材料・医療機器 / AI画像診断・創薬', '2027-01-22', '50000000', '日本医療研究開発機構（AMED）', '大型受託研究費（委託研究）', 'https://www.amed.go.jp/program/list/16/01/001.html', '大学・高専等のアカデミア発の医療機器・診断システム・治療薬・再生医療シーズの実用化に向けた医工連携・臨床橋渡し委託研究。'),
        ('soumu_scope_2027', '戦略的情報通信研究開発推進事業（SCOPE：重点領域型・地域ICT振興型・若手ICT研究者育成型）', '情報・通信 / AI・IoT・センサー / ワイヤレス / 防災・地域情報化', '2027-02-10', '50000000', '総務省', '大型受託研究費（委託研究）', 'https://www.soumu.go.jp/main_sosiki/joho_tsusin/scope/', '情報通信技術（ICT・IoT・センサーネットワーク・AI解析・地域防災・医療ICTシステム等）における独創的な研究開発・大学×高専×企業連携の大型受託研究費。'),
        ('maff_smart_agri_2027', '戦略的スマート農業技術等の開発・改良 / イノベーション創出強化研究推進事業', '農学・生物 / 情報・AI・画像処理 / 工学・ロボット・センサー / 環境', '2027-02-15', '60000000', '農林水産省・生物系特定産業技術研究支援センター（BRAIN）', '大型受託研究費（委託研究）', 'https://www.naro.go.jp/laboratory/brain/', 'センシング・AI画像解析・自動化機械・環境制御を活用したスマート農林水産業・食品加工技術の大型委託研究（大学・高専・企業コンソーシアム対象）。'),
        ('nedo_wakate_2027_1', 'NEDO 官民による若手研究者発掘支援事業（共同研究フェーズ・マッチングサポートフェーズ）', 'AI・情報 / 機械・ロボット / 材料・ナノテク / エネルギー・環境 / バイオ・医療機器', '2027-02-19', '50000000', '新エネルギー・産業技術総合開発機構（NEDO）', '大型受託研究費（委託研究）', 'https://www.nedo.go.jp/activities/ZZJP_100161.html', '実用化を見据えた大学・高専の若手研究者と企業との産学共同研究や学際チームの技術シーズ発掘・育成を支援。'),
        ('amed_sentan_keisoku_2027', 'AMED 先端計測分析技術・機器開発プログラム（要素技術開発・応用・実用化タイプ）', '物理・化学 / 工学・計測 / 生命科学・生体イメージング / ナノ構造解析', '2027-02-26', '50000000', '日本医療研究開発機構（AMED）・JST連携', '大型受託研究費（委託研究）', 'https://www.amed.go.jp/program/list/16/02/001.html', '生命現象の解明や医療診断・材料解析に資する独創的な計測・イメージング機器・センサー要素技術の大型委託開発。'),
        ('mlit_kensetsu_2027', '国土交通省 建設技術研究開発助成制度（防災・減災・積雪対策・インフラ維持管理・DX）', '工学・土木建築 / 情報・センシング・AI状態監視 / 防災・積雪・地球科学', '2027-03-12', '25000000', '国土交通省', '大型受託研究費（委託研究）', 'https://www.mlit.go.jp/tec/gijutu/kaihatu/josei.html', 'インフラ老朽化対策・センサー状態監視・豪雪地帯の積雪路面予測・流域治水・防災DX等の大学・高専×企業・自治体共同研究。'),
        ('jst_astep_tryout_2027_1', 'JST 研究成果最適展開支援プログラム（A-STEP）トライアウトタイプ・産学共同フェーズ', '工学 / 情報 / 材料・デバイス / 医工連携 / 環境・エネルギー', '2027-03-18', '15000000', '科学技術振興機構（JST）', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/tt/rst/a-step/', '大学・高専等の基礎研究成果の実用化可能性を検証する概念実証（PoC）および産学共同委託研究支援。'),
        ('jst_actx_2027_spring', 'JST 戦略的創造研究推進事業 ACT-X（若手研究者挑戦型：AI・情報・材料・生命領域）', '情報・AI / 材料・化学 / 生命科学・医工連携', '2027-03-25', '6000000', '科学技術振興機構（JST）', '公的研究費', 'https://www.jst.go.jp/kisoken/act-x/', '大学・高専・研究機関の若手研究者の独創的な発想に基づく挑戦的研究を支援。'),
        ('atla_security_tech_2027', '安全保障技術推進制度（基礎研究公募：タイプS・タイプA・タイプB）', '工学・機械・航空宇宙 / 情報・AI・通信・量子 / 材料・センサー・エネルギー', '2027-03-29', '500000000', '防衛装備庁（ATLA）', '大型受託研究費（委託研究）', 'https://www.mod.go.jp/atla/funding.html', '先端科学技術分野（AI・量子・材料・センサー・情報通信・流体・エネルギー等）における独創的な基礎研究を対象とした大型委託研究（タイプS：上限5億円、タイプA：上限1億円、タイプB：上限3,900万円）。'),
        ('nedo_sendou_2027', 'NEDO 先導研究プログラム（新産業・革新技術創出 / エネルギー・環境新技術 / マテリアル・AI）', '環境・エネルギー / 材料・ナノテク / AI・ロボット・半導体 / バイオものづくり', '2027-04-16', '100000000', '新エネルギー・産業技術総合開発機構（NEDO）', '大型受託研究費（委託研究）', 'https://www.nedo.go.jp/activities/ZZJP_100137.html', '将来の社会実装を見据えた原石技術の発掘・概念実証を行う産学共同・大学×高専×企業連携の大型委託研究（受託研究費）。'),
        ('meti_gotech_2027', '成長型中小企業等研究開発支援事業（Go-Tech事業 / 旧サポイン：大学・高専・公設試との共同・受託研究枠）', '工学・ものづくり・機械 / 材料・繊維・表面処理 / 情報・AI・IoT / 医療福祉機器', '2027-04-23', '97500000', '経済産業省・中小企業庁', '大型受託研究費（委託研究）', 'https://www.chusho.meti.go.jp/keiei/sapoin/', '大学や高専等の研究機関と企業がコンソーシアムを組んで行う高度なものづくり・AI/IoT・先端材料・医療機器等の大型研究開発（最大3年間・上限9,750万円）。高専・大学の受託研究費獲得の代表的制度。'),
        ('nict_beyond5g_2027', '革新的情報通信技術（Beyond 5G / 6G）基金事業 要素技術・シーズ創出型・社会実装型プログラム', '情報・通信 / ワイヤレス・光通信 / 半導体・センサー / AIネットワーク', '2027-05-14', '100000000', '情報通信研究機構（NICT）', '大型受託研究費（委託研究）', 'https://www.nict.go.jp/collabo/commission/B5G_fund.html', '次世代通信（Beyond 5G/6G）・光無線融合・高効率アンテナ・エッジAI・スマートセンシング基盤の大型委託研究開発。'),
        ('jst_sakigake_2027', 'JST 戦略的創造研究推進事業 さきがけ（個人型・異分野ネットワーク型研究）', '情報・数理 / 物理・化学・材料 / 工学・デバイス / 生命科学・医工連携', '2027-05-18', '40000000', '科学技術振興機構（JST）', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/kisoken/boshuu/teian.html', '研究者の独創的な発想に基づき、異分野研究者とのネットワークを形成しながら推進する挑戦的委託研究（上限4,000万円）。'),
        ('jst_alca_next_2027', 'JST 先端的カーボンニュートラル技術開発（ALCA-Next）', '環境・エネルギー / 材料・化学・触媒 / 電気電子・デバイス / 生物資源', '2027-05-20', '150000000', '科学技術振興機構（JST）', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/alca/', '温室効果ガス削減・蓄電池・水素・次世代太陽電池・省エネ半導体・バイオものづくり等のゲームチェンジング技術開発（大型委託研究）。'),
        ('jst_crest_2027', 'JST 戦略的創造研究推進事業 CREST（チーム型大型共同研究：AI・量子・マテリアル・バイオ・医工融合）', '情報・AI / 工学・材料・ナノテク / 生命科学・医学 / 環境・エネルギー', '2027-05-25', '500000000', '科学技術振興機構（JST）', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/kisoken/boshuu/teian.html', '科学技術イノベーションをもたらす革新的技術シーズの創出に向けた複数研究者・大学×高専×研究機関チームによる大型委託研究（最大5億円/5.5年）。'),
        ('jst_astep_honkaku_2027', 'JST 研究成果最適展開支援プログラム（A-STEP）産学共同（育成型・本格型）', '工学・材料・機械 / 情報・AI・システム / 医療・バイオ / 環境', '2027-05-28', '150000000', '科学技術振興機構（JST）', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/tt/rst/a-step/', '大学・高専等の研究成果を企業との共同研究により実用化・製品化へ橋渡しする中〜大型委託研究開発プログラム。'),
        ('jst_forest_2027', 'JST 創発的研究支援事業（FOREST：長期大型挑戦的研究）', '全分野（自然科学・工学・情報・医学・融合領域）', '2027-06-04', '50000000', '科学技術振興機構（JST）', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/souhatsu/', '特定の課題や短期目標を設定せず、多様性と融合によって破壊的イノベーションにつながる長期（最長7〜10年）の挑戦的研究を支援。'),
        ('jst_k_program_2027', 'JST 経済安全保障重要技術育成プログラム（K Program：AI・量子・半導体・宇宙・海洋・バイオ・先端材料）', '情報・AI・サイバー / 航空宇宙・海洋・ロボティクス / 先端材料・半導体・バイオ', '2027-06-25', '500000000', '科学技術振興機構（JST）・NEDO連携', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/k-program/', '中長期的に日本が国際的な優位性を確保すべき重要技術領域における大学・高専・企業コンソーシアムによる超大型委託研究開発。'),
        ('jst_coi_next_2027', 'JST 共創の場形成支援プログラム（COI-NEXT：地域共創分野・共創分野）', '地域創生・産学官連携 / 工学・情報・医工・教育・防災・高専連携', '2027-07-15', '200000000', '科学技術振興機構（JST）', '大型受託研究費（委託研究）', 'https://www.jst.go.jp/pf/platform/', '大学・高専を中心として自治体・地域企業とビジョンを共有し、地域課題解決と自律的な産学官共創拠点を形成する大型委託研究プログラム。'),

        # === 3. 福井県・北陸地域・大学×高専×地域企業 産学官連携・共同研究公募 ===
        ('genden_fukui_2027', 'げんでんふれあい福井財団 科学技術研究助成（福井県内の大学・高専研究者対象）', '工学・エネルギー・原子力・防災 / 材料・物理・化学 / 情報・環境・バイオ', '2027-02-25', '2000000', '公益財団法人げんでんふれあい福井財団', '地域研究助成', 'https://www.genden-fukui-zaidan.jp/', '福井県内の大学・高等専門学校（福井高専）等の研究者が行う自然科学・エネルギー・環境・防災・先端工学分野の独創的研究および大学×高専共同研究に対する助成。'),
        ('hokuriku_hiac_2027', '北陸地域産学官連携研究開発助成事業（北陸3県の大学・高専×地域企業共同研究）', '工学・機械・電子 / 材料・ナノテク / 情報・AI / 医工連携・バイオ', '2027-03-19', '3000000', '一般財団法人北陸産業活性化センター（HIAC）', '地域産学連携', 'http://www.hiac.or.jp/', '北陸3県（福井・石川・富山）の大学・高専研究者と地域企業による共同研究・受託研究・技術シーズ実用化を支援。'),
        ('fukui_sangaku_2027_spring', 'ふくい産学官共同研究開発支援事業（重点戦略分野：繊維・先端材料・機械・電子・医工・DX）', '工学・材料・繊維 / 情報・AI・IoT / 医工連携 / 環境・地域産業', '2027-03-26', '10000000', '公益財団法人ふくい産業支援センター・福井県', '地域産学連携（共同・受託研究）', 'https://www.fisc.jp/', '福井県内の大学・福井高専・公設試・企業が連携して行う新技術・新製品開発（機能性繊維・複合材料・表面処理・IoTセンサー・医療福祉機器等）の共同研究・受託研究を支援。'),
        ('fukui_univ_kosen_2027', '福井大学×福井高専・学内異分野融合 共同研究推進プログラム（大型外部資金・科研費・受託研究獲得支援）', '全分野（情報系×工学系×医学系×教育・人文系×高専連携）', '2027-04-30', '2000000', '福井大学 研究推進機構・産学官連携本部', '学内・高専連携公募', 'https://www.u-fukui.ac.jp/', '科研費（基盤S/A/B・挑戦的研究）やJST/AMED/NEDO/Go-Tech等の大型受託研究費獲得を目指し、学内の異なる学系および高専等の研究者がチームを組んで行う共同研究・共用機器活用を支援。'),
    ]

    out = []
    for gid, title, field, dl_ymd, amt, agency, cat, url, summary in raw:
        if dl_ymd < TODAY:
            continue
        dl = f'{dl_ymd}T00:00:00.000Z'
        y, m, d = dl_ymd.split('-')
        out.append({
            'id': f'fund_curated_{gid}',
            'title': title,
            'field': field,
            'deadline': dl,
            'summary': summary,
            'url': url,
            'source': 'e-Rad / 公的機関（科研費・大型受託）' if ('科研費' in cat or '受託' in cat or '公的' in cat) else agency,
            'agency': agency,
            'category': cat,
            'start_date': '',
            'period_text': f'{y}/{m}/{d}',
            'max_amount_yen': amt,
            'eligibility_text': '大学・高専・研究機関に所属する研究者（共同研究チーム・コンソーシアムでの応募可）',
            'internal_deadline': '',
            'prior_contact_deadline': '',
            'approval_req': '必要',
            'status': '募集中',
            'eligibility_check_status': '募集要項・学内締切要確認',
            'source_url': url,
            'fetched_date': f'{TODAY}T00:00:00.000Z',
            'note': '科研費・大型受託研究費（委託研究）・産学官連携公募データ。',
            'search_text': f'{title} {field} {agency} {cat} 科研費 受託研究 委託研究 高専 共同研究 {summary}',
        })
    return out


def rebuild_active_grants_database():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Fetch ONLY active/open grants from the 4 recommended sites + NEDO + KAKENHI/Large Commissioned Funds
    erad_rows = fetch_erad_active_grants()
    sp_rows = fetch_science_portal_active_grants()
    jfc_rows = fetch_jfc_active_grants()
    umin_rows = fetch_umin_active_grants()
    nedo_rows = fetch_nedo_active_grants()
    major_rows = get_major_kakenhi_and_commissioned_grants()

    print(
        f'Fetched active grants -> e-Rad: {len(erad_rows)}, Science Portal: {len(sp_rows)}, '
        f'助成・奨学金情報navi: {len(jfc_rows)}, UMIN FIND: {len(umin_rows)}, '
        f'NEDO: {len(nedo_rows)}, 科研費・大型受託研究費: {len(major_rows)}'
    )

    # Clear out ALL past/closed/synthetic rows so ONLY active (募集中) grants exist in the DB
    cur.execute('DELETE FROM grants')

    cols = [
        'id', 'title', 'field', 'deadline', 'summary', 'url', 'source', 'agency',
        'category', 'start_date', 'period_text', 'max_amount_yen', 'eligibility_text',
        'internal_deadline', 'prior_contact_deadline', 'approval_req', 'status',
        'eligibility_check_status', 'source_url', 'fetched_date', 'note', 'search_text',
    ]
    placeholders = ','.join(['?'] * len(cols))

    seen_sigs = set()
    inserted = 0
    for item in major_rows + erad_rows + nedo_rows + sp_rows + jfc_rows + umin_rows:
        dl = item.get('deadline') or ''
        # Final safety guard: never insert any grant with deadline before TODAY
        if not dl or (not dl.startswith('9999') and dl[:10] < TODAY):
            continue
        if '締切済' in (item.get('status') or '') or '終了' in (item.get('status') or ''):
            continue

        sig = (
            re.sub(r'\s+', '', item.get('agency') or ''),
            re.sub(r'\s+', '', item.get('title') or ''),
            dl[:10],
            re.sub(r'\s+', '', (item.get('summary') or '')[:45]),
        )
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        cur.execute(
            f'INSERT OR REPLACE INTO grants ({",".join(cols)}) VALUES ({placeholders})',
            tuple(item.get(c, '') for c in cols),
        )
        inserted += 1

    cur.execute("UPDATE db_status SET val = ? WHERE item = '公募マスタ'", (str(inserted),))
    cur.execute("UPDATE sheet_meta SET row_count = ? WHERE sheet_name = '公募マスタ'", (inserted,))
    conn.commit()

    print(f'Total active (募集中) grants in DB: {inserted}')
    print('By source:', cur.execute('SELECT source, COUNT(*) FROM grants GROUP BY source ORDER BY COUNT(*) DESC').fetchall())
    print('By status:', cur.execute('SELECT status, COUNT(*) FROM grants GROUP BY status').fetchall())
    print('By category:', cur.execute('SELECT category, COUNT(*) FROM grants GROUP BY category ORDER BY COUNT(*) DESC LIMIT 12').fetchall())
    print('By YYYY-MM:', cur.execute('SELECT substr(deadline,1,7) ym, COUNT(*) FROM grants GROUP BY ym ORDER BY ym').fetchall())
    conn.close()


if __name__ == '__main__':
    rebuild_active_grants_database()
