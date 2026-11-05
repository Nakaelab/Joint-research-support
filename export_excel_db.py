import os
import re
import shutil
import sqlite3
import zipfile
from xml.sax.saxutils import escape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'fukui_matching.db')
XLSX_PATH = os.path.join(BASE_DIR, 'FukuiMatchingDB.xlsx')
GRANTS_XLSX_PATH = os.path.join(BASE_DIR, '公募データベース一覧_2026-2028.xlsx')


def clean_xml_text(val):
    if val is None:
        return ''
    s = str(val)
    # Remove control characters invalid in XML 1.0
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    return escape(s)


def build_sheet6_xml(grants_rows, header_prefix, footer_suffix):
    headers = [
        '公募ID', '公募名', '対象分野', '締切日', '概要', 'URL', '取得元', '取得元公募ID',
        '配分機関', '公募種別', '受付開始日', '募集期間原文', '助成上限円', '応募資格原文',
        '学内締切日', '機関承認', '募集状態', '資格確認状態', '確認担当', '出典URL',
        '取得日', '備考', '取得範囲', '検索用テキスト'
    ]
    col_letters = [chr(ord('A') + i) for i in range(24)]

    parts = [header_prefix, '<x:sheetData>']
    # Row 1: Header
    r1_cells = []
    for c_let, h_text in zip(col_letters, headers):
        r1_cells.append(f'<x:c r="{c_let}1" s="27" t="str"><x:v>{clean_xml_text(h_text)}</x:v></x:c>')
    parts.append('<x:row r="1" ht="40" customHeight="1">' + ''.join(r1_cells) + '</x:row>')

    # Data rows
    for idx, g in enumerate(grants_rows, start=2):
        gid = g.get('id', '')
        src_id = gid.replace('fund_', '')
        dl = g.get('deadline', '')
        amt = g.get('max_amount_yen', '')
        fetched = g.get('fetched_date', '') or '2026-11-05T00:00:00.000Z'
        scope = '詳細・スケジュール取得済' if ('curated' in gid or 'jfc' in gid or 'umin' in gid) else '一覧情報のみ'

        vals = [
            (gid, '24', 'str'),
            (g.get('title', ''), '24', 'str'),
            (g.get('field', ''), '24', 'str'),
            (dl, '28', 'd' if dl else 'str'),
            (g.get('summary', ''), '24', 'str'),
            (g.get('url', ''), '24', 'str'),
            (g.get('source', ''), '24', 'str'),
            (src_id, '24', 'str'),
            (g.get('agency', ''), '24', 'str'),
            (g.get('category', ''), '24', 'str'),
            (g.get('start_date', ''), '28', 'str'),
            (g.get('period_text', ''), '24', 'str'),
            (amt, '29', 'n' if (amt and str(amt).isdigit()) else 'str'),
            (g.get('eligibility_text', ''), '24', 'str'),
            (g.get('internal_deadline', ''), '28', 'str'),
            (g.get('approval_req', ''), '24', 'str'),
            (g.get('status', ''), '24', 'str'),
            (g.get('eligibility_check_status', ''), '24', 'str'),
            ('', '24', 'str'),
            (g.get('source_url', ''), '24', 'str'),
            (fetched, '28', 'd' if 'T' in fetched else 'str'),
            (g.get('note', ''), '24', 'str'),
            (scope, '24', 'str'),
            (g.get('search_text', ''), '24', 'str'),
        ]

        row_cells = []
        for c_let, (val, style, ctype) in zip(col_letters, vals):
            if val == '' or val is None:
                if ctype == 'n':
                    row_cells.append(f'<x:c r="{c_let}{idx}" s="{style}" />')
                else:
                    row_cells.append(f'<x:c r="{c_let}{idx}" s="{style}" t="str" />')
            else:
                if ctype == 'n':
                    row_cells.append(f'<x:c r="{c_let}{idx}" s="{style}" t="n"><x:v>{clean_xml_text(val)}</x:v></x:c>')
                else:
                    row_cells.append(f'<x:c r="{c_let}{idx}" s="{style}" t="{ctype}"><x:v>{clean_xml_text(val)}</x:v></x:c>')

        parts.append(f'<x:row r="{idx}" ht="54" customHeight="1">' + ''.join(row_cells) + '</x:row>')

    parts.append('</x:sheetData>')
    parts.append(footer_suffix)
    return ''.join(parts)


def update_fukui_matching_xlsx():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    grants_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM grants ORDER BY CASE WHEN deadline = '' THEN '9998' ELSE deadline END ASC, id ASC"
        ).fetchall()
    ]
    conn.close()

    total_rows = len(grants_rows) + 1  # +1 for header row

    with zipfile.ZipFile(XLSX_PATH, 'r') as zin:
        s6_orig = zin.read('xl/worksheets/sheet6.xml').decode('utf-8')
        t1_orig = zin.read('xl/tables/table1.xml').decode('utf-8')
        s13_orig = zin.read('xl/worksheets/sheet13.xml').decode('utf-8')

        header_prefix = s6_orig.split('<x:sheetData>', 1)[0]
        footer_suffix = s6_orig.split('</x:sheetData>', 1)[1]

        new_s6 = build_sheet6_xml(grants_rows, header_prefix, footer_suffix)
        new_t1 = re.sub(r'ref="A1:X\d+"', f'ref="A1:X{total_rows}"', t1_orig)
        new_s13 = re.sub(
            r'(<x:c r="A30"[^>]*><x:v>公募マスタ</x:v></x:c><x:c r="B30"[^>]*><x:v>)\d+(</x:v></x:c>)',
            rf'\g<1>{len(grants_rows)}\g<2>',
            s13_orig,
        )

        tmp_xlsx = XLSX_PATH + '.tmp'
        with zipfile.ZipFile(tmp_xlsx, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == 'xl/worksheets/sheet6.xml':
                    zout.writestr(item, new_s6.encode('utf-8'))
                elif item.filename == 'xl/tables/table1.xml':
                    zout.writestr(item, new_t1.encode('utf-8'))
                elif item.filename == 'xl/worksheets/sheet13.xml':
                    zout.writestr(item, new_s13.encode('utf-8'))
                else:
                    zout.writestr(item, zin.read(item.filename))

    os.replace(tmp_xlsx, XLSX_PATH)
    print(f'Updated {XLSX_PATH} with {len(grants_rows)} grants in 公募マスタ sheet.')

    # Also build a standalone lightweight Excel file containing just 公募マスタ (and DB整備状況)
    create_standalone_grants_xlsx(grants_rows, new_s6, new_t1)


def create_standalone_grants_xlsx(grants_rows, s6_xml, t1_xml):
    with zipfile.ZipFile(XLSX_PATH, 'r') as zin:
        styles_xml = zin.read('xl/styles.xml')
        theme_xml = zin.read('xl/theme/theme1.xml')
        s6_rels = zin.read('xl/worksheets/_rels/sheet6.xml.rels')

    wb_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<x:workbook xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<x:sheets>'
        '<x:sheet name="公募マスタ" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" />'
        '</x:sheets>'
        '</x:workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet6.xml" />'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml" />'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml" />'
        '</Relationships>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml" />'
        '</Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
        '<Default Extension="xml" ContentType="application/xml" />'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
        '<Override PartName="/xl/worksheets/sheet6.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml" />'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml" />'
        '<Override PartName="/xl/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml" />'
        '<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml" />'
        '</Types>'
    )

    with zipfile.ZipFile(GRANTS_XLSX_PATH, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
        zout.writestr('[Content_Types].xml', content_types.encode('utf-8'))
        zout.writestr('_rels/.rels', root_rels.encode('utf-8'))
        zout.writestr('xl/workbook.xml', wb_xml.encode('utf-8'))
        zout.writestr('xl/_rels/workbook.xml.rels', wb_rels.encode('utf-8'))
        zout.writestr('xl/styles.xml', styles_xml)
        zout.writestr('xl/theme/theme1.xml', theme_xml)
        zout.writestr('xl/worksheets/sheet6.xml', s6_xml.encode('utf-8'))
        zout.writestr('xl/worksheets/_rels/sheet6.xml.rels', s6_rels)
        zout.writestr('xl/tables/table1.xml', t1_xml.encode('utf-8'))

    print(f'Exported standalone Excel file: {GRANTS_XLSX_PATH} ({len(grants_rows)} rows)')


if __name__ == '__main__':
    update_fukui_matching_xlsx()
