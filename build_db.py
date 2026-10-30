import zipfile
import xml.etree.ElementTree as ET
import sqlite3
import json
import os
import re

XLSX_PATH = os.path.join(os.path.dirname(__file__), 'FukuiMatchingDB.xlsx')
DB_PATH = os.path.join(os.path.dirname(__file__), 'fukui_matching.db')

NS_MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
NS_REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def col_to_idx(col_str):
    idx = 0
    for ch in col_str:
        idx = idx * 26 + (ord(ch) - ord('A') + 1)
    return idx - 1


def parse_xlsx(xlsx_path):
    with zipfile.ZipFile(xlsx_path, 'r') as z:
        shared_strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            ss_root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in ss_root:
                shared_strings.append(
                    ''.join(t.text or '' for t in si.iter(f'{NS_MAIN}t'))
                )

        wb_root = ET.fromstring(z.read('xl/workbook.xml'))
        rels_root = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        rels = {r.attrib['Id']: r.attrib['Target'] for r in rels_root}

        sheets = {}
        for sheet_el in wb_root.iter(f'{NS_MAIN}sheet'):
            name = sheet_el.attrib['name']
            rid = sheet_el.attrib[f'{NS_REL}id']
            target = 'xl/' + rels[rid].lstrip('/')
            if target.startswith('xl/xl/'):
                target = target[3:]
            sheets[name] = target

        def read_sheet_rows(target):
            rows = []
            headers = []
            for event, el in ET.iterparse(z.open(target), events=('end',)):
                if el.tag == f'{NS_MAIN}row':
                    row_dict = {}
                    max_col = -1
                    for c in el:
                        r_attr = c.attrib.get('r', '')
                        col_letters = ''.join(ch for ch in r_attr if ch.isalpha())
                        c_idx = col_to_idx(col_letters) if col_letters else (max_col + 1)
                        if c_idx > max_col:
                            max_col = c_idx
                        t = c.attrib.get('t')
                        v = c.find(f'{NS_MAIN}v')
                        is_el = c.find(f'{NS_MAIN}is')
                        if t == 's' and v is not None and v.text:
                            val = shared_strings[int(v.text)]
                        elif is_el is not None:
                            val = ''.join(x.text or '' for x in is_el.iter(f'{NS_MAIN}t'))
                        elif v is not None and v.text:
                            val = v.text
                        else:
                            val = ''
                        row_dict[c_idx] = val.strip() if isinstance(val, str) else str(val)
                    if not headers:
                        headers = [row_dict.get(i, f'col_{i}') for i in range(max_col + 1)]
                    else:
                        if any(v for v in row_dict.values()):
                            item = {}
                            for i, h in enumerate(headers):
                                if h:
                                    item[h] = row_dict.get(i, '')
                            rows.append(item)
                    el.clear()
            return headers, rows

        data = {}
        for name, target in sheets.items():
            headers, rows = read_sheet_rows(target)
            data[name] = (headers, rows)
            print(f'Loaded sheet "{name}": {len(rows)} rows')
        return data


def build_database():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    data = parse_xlsx(XLSX_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Store all raw tables and metadata
    cur.execute('''
        CREATE TABLE sheet_meta (
            sheet_name TEXT PRIMARY KEY,
            row_count INTEGER,
            headers_json TEXT
        )
    ''')

    for sheet_name, (headers, rows) in data.items():
        cur.execute(
            'INSERT INTO sheet_meta VALUES (?, ?, ?)',
            (sheet_name, len(rows), json.dumps(headers, ensure_ascii=False))
        )

    # 1. Researchers (研究者マスタ + enriched fields from researchmapプロフィール, 研究キーワード, 在籍名簿, 連携プロフィール)
    rm_prof_map = {}
    for r in data.get('researchmapプロフィール', ([], []))[1]:
        rid = r.get('研究者ID', '')
        if rid:
            rm_prof_map[rid] = r

    roster_map = {}
    for r in data.get('在籍名簿', ([], []))[1]:
        rid = r.get('採用研究者ID') or r.get('既存研究者ID候補')
        if rid:
            roster_map[rid] = r

    kw_map = {}
    for r in data.get('研究キーワード', ([], []))[1]:
        rid = r.get('研究者ID', '')
        kw = r.get('キーワード', '')
        if rid and kw:
            kw_map.setdefault(rid, [])
            if kw not in kw_map[rid]:
                kw_map[rid].append(kw)

    info_kw_map = {}
    info_dept_map = {}
    for r in data.get('研究情報明細', ([], []))[1]:
        rid = r.get('研究者ID', '')
        if not rid:
            continue
        kw = r.get('キーワード', '')
        title = r.get('タイトル', '')
        kind = r.get('種別', '')
        if kw:
            for part in re.split(r'[/／、,，\n]+', kw):
                part = part.strip()
                if 1 < len(part) <= 25:
                    info_kw_map.setdefault(rid, [])
                    if len(info_kw_map[rid]) < 12 and part not in info_kw_map[rid]:
                        info_kw_map[rid].append(part)
        if kind == '教員公開情報' and '：' in title:
            dept_part = title.split('：', 1)[1].replace('の公開分野', '').strip()
            if dept_part:
                info_dept_map[rid] = dept_part

    cur.execute('''
        CREATE TABLE researchers (
            id TEXT PRIMARY KEY,
            name TEXT,
            affiliation TEXT,
            department TEXT,
            specialty TEXT,
            summary TEXT,
            matching_summary TEXT,
            evidence TEXT,
            enrollment_status TEXT,
            identity_status TEXT,
            matching_target TEXT,
            profile_url TEXT,
            researchmap_url TEXT,
            orcid TEXT,
            kaken_id TEXT,
            keywords_json TEXT,
            research_fields TEXT,
            rm_profile TEXT,
            data_notice TEXT,
            updated_at TEXT,
            priority_score INTEGER
        )
    ''')

    researcher_rows = data.get('研究者マスタ', ([], []))[1]
    for r in researcher_rows:
        rid = r.get('研究者ID', '')
        if not rid:
            continue
        rm = rm_prof_map.get(rid, {})
        roster = roster_map.get(rid, {})
        kws = list(kw_map.get(rid, []))
        for k in info_kw_map.get(rid, []):
            if k not in kws:
                kws.append(k)
        # Also extract keywords from specialty if kws is empty
        spec = r.get('専門', '')
        if not kws and spec:
            for part in re.split(r'[/／、,，]+', spec):
                p = part.strip()
                if p and p not in ('研究課題', '論文実績', '福井大学') and len(p) <= 24:
                    kws.append(p)

        dept = (
            info_dept_map.get(rid)
            or roster.get('所属', '')
            or rm.get('現所属原文', '')
            or rm.get('研究分野', '')
            or r.get('所属系', '福井大学')
        )
        # Clean department label for UI badges (e.g. 情報系 / 工学系 / 医学系 / 教育系)
        enrollment = r.get('在籍確認', '未確認')
        target = r.get('マッチング対象', '要確認')
        identity = r.get('本人照合', '未確認')

        score = 0
        if target == '対象':
            score += 100
        if '福井大学' in enrollment:
            score += 50
        elif '公式教員一覧' in enrollment:
            score += 40
        if rid in rm_prof_map:
            score += 25
        if kws:
            score += 10

        cur.execute('''
            INSERT OR REPLACE INTO researchers VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', (
            rid,
            r.get('氏名', ''),
            r.get('所属系', '福井大学'),
            dept,
            spec,
            r.get('研究概要', ''),
            r.get('マッチング用研究概要', ''),
            r.get('根拠', ''),
            enrollment,
            identity,
            target,
            r.get('公式プロフィールURL', '') or roster.get('プロフィールURL', ''),
            r.get('researchmapURL候補', '') or rm.get('出典URL', ''),
            r.get('ORCID候補', '') or rm.get('ORCID', ''),
            r.get('研究者番号候補', '') or rm.get('研究者番号', ''),
            json.dumps(kws[:15], ensure_ascii=False),
            rm.get('研究分野', ''),
            rm.get('研究プロフィール', ''),
            r.get('データ注意', ''),
            r.get('情報更新日', ''),
            score
        ))

    cur.execute('CREATE INDEX idx_researchers_priority ON researchers(priority_score DESC)')
    cur.execute('CREATE INDEX idx_researchers_target ON researchers(matching_target)')

    # 2. Research details: papers, projects, info items
    cur.execute('''
        CREATE TABLE papers (
            id TEXT PRIMARY KEY,
            researcher_id TEXT,
            title TEXT,
            authors TEXT,
            journal TEXT,
            pub_date TEXT,
            doi TEXT,
            url TEXT,
            summary TEXT
        )
    ''')
    for p in data.get('論文明細', ([], []))[1]:
        if p.get('論文ID'):
            cur.execute('''
                INSERT OR REPLACE INTO papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                p.get('論文ID', ''),
                p.get('研究者ID', ''),
                p.get('論文名', ''),
                p.get('著者', ''),
                p.get('掲載誌', ''),
                p.get('発行年月日原文', ''),
                p.get('DOI', ''),
                p.get('出典URL', ''),
                p.get('概要抜粋', '')
            ))
    cur.execute('CREATE INDEX idx_papers_rid ON papers(researcher_id)')

    cur.execute('''
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            researcher_id TEXT,
            title TEXT,
            agency TEXT,
            program TEXT,
            start_date TEXT,
            end_date TEXT,
            role TEXT,
            summary TEXT,
            budget_yen TEXT,
            url TEXT
        )
    ''')
    for pr in data.get('研究課題明細', ([], []))[1]:
        if pr.get('研究課題ID'):
            cur.execute('''
                INSERT OR REPLACE INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pr.get('研究課題ID', ''),
                pr.get('研究者ID', ''),
                pr.get('研究課題名', ''),
                pr.get('資金配分機関', ''),
                pr.get('制度', ''),
                pr.get('開始日原文', ''),
                pr.get('終了日原文', ''),
                pr.get('本人役割原文', ''),
                pr.get('概要抜粋', ''),
                pr.get('総額円', ''),
                pr.get('出典URL', '')
            ))
    cur.execute('CREATE INDEX idx_projects_rid ON projects(researcher_id)')

    cur.execute('''
        CREATE TABLE research_info (
            id TEXT PRIMARY KEY,
            researcher_id TEXT,
            kind TEXT,
            title TEXT,
            keywords TEXT,
            summary TEXT,
            source_url TEXT,
            status TEXT,
            note TEXT
        )
    ''')
    for info in data.get('研究情報明細', ([], []))[1]:
        if info.get('情報ID'):
            cur.execute('''
                INSERT OR REPLACE INTO research_info VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                info.get('情報ID', ''),
                info.get('研究者ID', ''),
                info.get('種別', ''),
                info.get('タイトル', ''),
                info.get('キーワード', ''),
                info.get('概要', ''),
                info.get('出典URL', ''),
                info.get('確認状態', ''),
                info.get('備考', '')
            ))
    cur.execute('CREATE INDEX idx_info_rid ON research_info(researcher_id)')

    # 3. Grants (公募マスタ)
    cur.execute('''
        CREATE TABLE grants (
            id TEXT PRIMARY KEY,
            title TEXT,
            field TEXT,
            deadline TEXT,
            summary TEXT,
            url TEXT,
            source TEXT,
            agency TEXT,
            category TEXT,
            start_date TEXT,
            period_text TEXT,
            max_amount_yen TEXT,
            eligibility_text TEXT,
            internal_deadline TEXT,
            prior_contact_deadline TEXT,
            approval_req TEXT,
            status TEXT,
            eligibility_check_status TEXT,
            source_url TEXT,
            fetched_date TEXT,
            note TEXT,
            search_text TEXT
        )
    ''')
    for g in data.get('公募マスタ', ([], []))[1]:
        gid = g.get('公募ID', '')
        if not gid:
            continue
        cur.execute('''
            INSERT OR REPLACE INTO grants VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', (
            gid,
            g.get('公募名', ''),
            g.get('対象分野', ''),
            g.get('締切日', ''),
            g.get('概要', ''),
            g.get('URL', ''),
            g.get('取得元', ''),
            g.get('配分機関', ''),
            g.get('公募種別', '研究助成'),
            g.get('受付開始日', ''),
            g.get('募集期間原文', ''),
            g.get('助成上限円', ''),
            g.get('応募資格原文', ''),
            g.get('学内締切日', ''),
            '',
            g.get('機関承認', ''),
            g.get('募集状態', ''),
            g.get('資格確認状態', '募集要項・学内締切要確認'),
            g.get('出典URL', ''),
            g.get('取得日', ''),
            g.get('備考', ''),
            g.get('検索用テキスト', '')
        ))

    # 4. Equipment (機器マスタ + 機器料金 + 機器利用条件)
    cur.execute('''
        CREATE TABLE equipment (
            id TEXT PRIMARY KEY,
            eq_number TEXT,
            name TEXT,
            resource_type TEXT,
            institution TEXT,
            department TEXT,
            keywords TEXT,
            usage_status TEXT,
            spec_samples TEXT,
            provision_type TEXT,
            operation_status TEXT,
            contact TEXT,
            source_doc TEXT,
            source_page TEXT,
            revised_date TEXT,
            fetched_date TEXT
        )
    ''')
    for eq in data.get('機器マスタ', ([], []))[1]:
        eq_id = eq.get('機器ID', '')
        if not eq_id:
            continue
        cur.execute('''
            INSERT OR REPLACE INTO equipment VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', (
            eq_id,
            eq.get('機器番号', ''),
            eq.get('機器名', ''),
            eq.get('資源種別', '機器'),
            eq.get('所属機関', '福井大学'),
            eq.get('管理部門', '研究推進機構 総合実験研究支援センター'),
            eq.get('検索キーワード', ''),
            eq.get('用途確認状態', ''),
            eq.get('仕様・適合試料', ''),
            eq.get('提供形態', ''),
            eq.get('稼働・予約状況', ''),
            eq.get('問い合わせ先', ''),
            eq.get('出典資料', ''),
            eq.get('出典ページ', ''),
            eq.get('料金改定日', ''),
            eq.get('取得日', '')
        ))

    cur.execute('''
        CREATE TABLE equipment_rates (
            id TEXT PRIMARY KEY,
            equipment_id TEXT,
            user_category TEXT,
            method TEXT,
            base_fee_yen TEXT,
            base_unit TEXT,
            consumable_fee_yen TEXT,
            consumable_unit TEXT,
            guidance_fee_yen TEXT,
            guidance_unit TEXT,
            maintenance_fee_yen TEXT,
            maintenance_unit TEXT,
            base_raw TEXT,
            consumable_raw TEXT,
            guidance_raw TEXT,
            maintenance_raw TEXT,
            note TEXT,
            source_doc TEXT,
            source_page TEXT
        )
    ''')
    for rt in data.get('機器料金', ([], []))[1]:
        rid = rt.get('料金ID', '')
        if not rid:
            continue
        cur.execute('''
            INSERT OR REPLACE INTO equipment_rates VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', (
            rid,
            rt.get('機器ID', ''),
            rt.get('利用者区分', ''),
            rt.get('測定方法', ''),
            rt.get('基本料円', ''),
            rt.get('基本料単位', ''),
            rt.get('消耗品費円', ''),
            rt.get('消耗品費単位', ''),
            rt.get('指導料円', ''),
            rt.get('指導料単位', ''),
            rt.get('維持費円', ''),
            rt.get('維持費単位', ''),
            rt.get('基本料原文', ''),
            rt.get('消耗品費原文', ''),
            rt.get('指導料原文', ''),
            rt.get('維持費原文', ''),
            rt.get('適用条件・備考', ''),
            rt.get('出典資料', ''),
            rt.get('出典ページ', '')
        ))
    cur.execute('CREATE INDEX idx_eq_rates_eqid ON equipment_rates(equipment_id)')

    cur.execute('''
        CREATE TABLE equipment_rules (
            id TEXT PRIMARY KEY,
            scope TEXT,
            rule_text TEXT,
            source_doc TEXT,
            source_page TEXT,
            revised_date TEXT
        )
    ''')
    for rl in data.get('機器利用条件', ([], []))[1]:
        if rl.get('条件ID'):
            cur.execute('''
                INSERT OR REPLACE INTO equipment_rules VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                rl.get('条件ID', ''),
                rl.get('適用範囲', ''),
                rl.get('条件原文', ''),
                rl.get('出典資料', ''),
                rl.get('出典ページ', ''),
                rl.get('改定日', '')
            ))

    # 5. DB Status & AppSheet config
    cur.execute('''
        CREATE TABLE db_status (
            item TEXT,
            val TEXT,
            description TEXT
        )
    ''')
    for st in data.get('DB整備状況', ([], []))[1]:
        cur.execute('''
            INSERT INTO db_status VALUES (?, ?, ?)
        ''', (
            st.get('福井大学 共同研究マッチングDB', ''),
            st.get('値', ''),
            st.get('状態・説明', '')
        ))

    # 6. Consultations, Saved Proposals, and Execution History
    cur.execute('''
        CREATE TABLE consultations (
            id TEXT PRIMARY KEY,
            email TEXT,
            user_name TEXT,
            title TEXT,
            research_content TEXT,
            desired_collaboration TEXT,
            note TEXT,
            status TEXT,
            target_material TEXT,
            required_method TEXT,
            desired_timing TEXT,
            user_category TEXT,
            budget_limit_yen TEXT,
            run_id TEXT,
            updated_at TEXT
        )
    ''')
    for c in data.get('相談一覧', ([], []))[1]:
        cid = c.get('相談ID', '')
        if cid:
            cur.execute('''
                INSERT OR REPLACE INTO consultations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            ''', (
                cid,
                c.get('登録者メール', ''),
                c.get('相談者', ''),
                c.get('件名', ''),
                c.get('研究したい内容', ''),
                c.get('求める協力', ''),
                c.get('補足', ''),
                c.get('処理状況', ''),
                c.get('研究対象・材料', ''),
                c.get('必要な手法', ''),
                c.get('実施希望時期', ''),
                c.get('利用者区分', '学内'),
                c.get('予算上限円', ''),
                c.get('提案実行ID', ''),
                c.get('更新日時', '')
            ))

    cur.execute('''
        CREATE TABLE saved_proposals (
            id TEXT PRIMARY KEY,
            mode TEXT,
            title TEXT,
            research_content TEXT,
            researchers_json TEXT,
            grants_json TEXT,
            equipment_json TEXT,
            plan_json TEXT,
            created_at TEXT
        )
    ''')

    cur.execute('''
        CREATE TABLE run_history (
            run_id TEXT PRIMARY KEY,
            consultation_id TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            model_used TEXT,
            researcher_count INTEGER,
            grant_count INTEGER,
            equipment_count INTEGER,
            error_msg TEXT
        )
    ''')

    cur.execute('''
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cur.execute("INSERT OR REPLACE INTO app_settings VALUES ('deepseek_model', 'deepseek-chat')")
    cur.execute("INSERT OR REPLACE INTO app_settings VALUES ('deepseek_base_url', 'https://api.deepseek.com')")

    conn.commit()
    conn.close()
    print(f'Successfully built SQLite database at {DB_PATH}')


if __name__ == '__main__':
    build_database()
