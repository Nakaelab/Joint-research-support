import os
import sqlite3

SRC_DB = os.path.join(os.path.dirname(__file__), 'fukui_matching.db')
DST_DB = os.path.join(os.path.dirname(__file__), 'api', 'fukui_data.db')


def export_compact_db():
    os.makedirs(os.path.dirname(DST_DB), exist_ok=True)
    if os.path.exists(DST_DB):
        os.remove(DST_DB)

    src = sqlite3.connect(SRC_DB)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(DST_DB)

    tables_to_copy = [
        'researchers',
        'grants',
        'equipment',
        'equipment_rates',
        'papers',
        'projects',
    ]

    for tbl in tables_to_copy:
        ddl_row = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        if not ddl_row:
            continue
        dst.execute(ddl_row['sql'])

        rows = src.execute(f'SELECT * FROM {tbl}').fetchall()
        if not rows:
            continue

        col_names = rows[0].keys()
        placeholders = ','.join(['?'] * len(col_names))

        out_rows = []
        for r in rows:
            d = dict(r)
            if tbl == 'researchers':
                if d.get('summary'):
                    d['summary'] = d['summary'][:500]
                if d.get('matching_summary'):
                    d['matching_summary'] = d['matching_summary'][:500]
                if d.get('rm_profile'):
                    d['rm_profile'] = d['rm_profile'][:400]
            out_rows.append(tuple(d[c] for c in col_names))

        dst.executemany(f'INSERT INTO {tbl} VALUES ({placeholders})', out_rows)
        print(f'Copied {tbl}: {len(out_rows)} rows')

    # Empty runtime tables (no secrets committed to Git!)
    dst.execute('''
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    dst.execute("INSERT INTO app_settings VALUES ('deepseek_model', 'deepseek-chat')")
    dst.execute("INSERT INTO app_settings VALUES ('deepseek_base_url', 'https://api.deepseek.com')")

    dst.execute('''
        CREATE TABLE users (
            email TEXT PRIMARY KEY,
            name TEXT,
            department TEXT,
            role TEXT,
            password_hash TEXT,
            created_at TEXT,
            researcher_id TEXT DEFAULT ''
        )
    ''')
    dst.execute('''
        CREATE TABLE likes (
            key TEXT PRIMARY KEY,
            item_type TEXT,
            item_id TEXT,
            item_json TEXT,
            research_context TEXT,
            created_at TEXT,
            user_email TEXT DEFAULT ''
        )
    ''')
    dst.execute('''
        CREATE TABLE consultation_history (
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

    dst.commit()
    dst.execute('VACUUM')
    dst.close()
    src.close()
    size_mb = os.path.getsize(DST_DB) / (1024 * 1024)
    print(f'Exported {DST_DB} ({size_mb:.2f} MB)')


if __name__ == '__main__':
    export_compact_db()
