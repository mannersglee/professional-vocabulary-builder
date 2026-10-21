"""Local professional vocabulary demo. Run `python vocabulary.py --help`."""
import argparse
import json
import re
import sqlite3
import unicodedata
import uuid
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from threading import Timer, Thread
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, redirect, render_template_string, request, url_for, send_file, Response

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB = DATA / 'vocabulary.db'
FIELDS = ('english_term chinese_translation phonetic_symbols part_of_speech english_definition chinese_definition domain domains subdomain category difficulty priority example_sentence source_sentence collocations related_terms abbreviation sources source_locations original_contexts frequency').split()
LISTS = {'domains', 'collocations', 'related_terms', 'sources', 'source_locations', 'original_contexts'}
STATUSES = {'陌生', '模糊', '认识', '熟悉'}
APP_TZ = ZoneInfo('Asia/Shanghai')
app = Flask(__name__)
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024


def connect():
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL, added_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS books (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, source_id TEXT, imported_at TEXT NOT NULL,
      raw_json TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES documents(id));
    CREATE TABLE IF NOT EXISTS terms (
      term_id TEXT PRIMARY KEY, term_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
      learning_status TEXT NOT NULL DEFAULT '陌生', last_reviewed TEXT,
      next_review TEXT, review_count INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS book_terms (
      book_id TEXT NOT NULL, term_id TEXT NOT NULL, frequency INTEGER NOT NULL,
      PRIMARY KEY(book_id, term_id), FOREIGN KEY(book_id) REFERENCES books(id),
      FOREIGN KEY(term_id) REFERENCES terms(term_id));
    CREATE TABLE IF NOT EXISTS conversion_jobs (
      id TEXT PRIMARY KEY, state TEXT NOT NULL, progress INTEGER NOT NULL,
      message TEXT NOT NULL, document_id TEXT, created_at TEXT NOT NULL);''')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(documents)')}
    if 'domain' not in columns:
        db.execute("ALTER TABLE documents ADD COLUMN domain TEXT NOT NULL DEFAULT ''")
    if 'threshold' not in columns:
        db.execute('ALTER TABLE documents ADD COLUMN threshold INTEGER NOT NULL DEFAULT 50')
    if 'domain_paths' not in columns:
        db.execute("ALTER TABLE documents ADD COLUMN domain_paths TEXT NOT NULL DEFAULT '[]'")
    book_term_columns = {row['name'] for row in db.execute('PRAGMA table_info(book_terms)')}
    if 'domain' not in book_term_columns:
        db.execute("ALTER TABLE book_terms ADD COLUMN domain TEXT NOT NULL DEFAULT ''")
    if 'domains' not in book_term_columns:
        db.execute("ALTER TABLE book_terms ADD COLUMN domains TEXT NOT NULL DEFAULT '[]'")
    term_columns = {row['name'] for row in db.execute('PRAGMA table_info(terms)')}
    if 'review_streak' not in term_columns:
        db.execute('ALTER TABLE terms ADD COLUMN review_streak INTEGER NOT NULL DEFAULT 0')
    if db.execute("SELECT 1 FROM terms WHERE learning_status IN ('New','Learning','Mastered') LIMIT 1").fetchone():
        with db:
            db.execute("UPDATE terms SET learning_status=CASE learning_status "
                       "WHEN 'New' THEN '陌生' WHEN 'Learning' THEN '模糊' "
                       "WHEN 'Mastered' THEN '熟悉' ELSE learning_status END "
                       "WHERE learning_status IN ('New','Learning','Mastered')")
    return db


def now():
    return datetime.now(APP_TZ).isoformat(timespec='seconds')


def key(s):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', s).casefold()).strip()


def unique(items):
    seen, out = set(), []
    for item in items:
        value = str(item).strip()
        if value and key(value) not in seen:
            seen.add(key(value)); out.append(value)
    return out


def parse_domain_paths(raw):
    """Validate up to eight three-level paths from the upload form."""
    try:
        paths = json.loads(raw or '[]')
    except (ValueError, TypeError):
        raise ValueError('专业方向数据格式无效。')
    if not isinstance(paths, list) or len(paths) > 8:
        raise ValueError('最多添加 8 个专业方向。')
    normalized = []
    for path in paths:
        if not isinstance(path, list) or len(path) != 3:
            raise ValueError('专业方向需要一级、二级、三级字段。')
        levels = [str(v).strip() for v in path]
        if any(len(v) > 40 for v in levels):
            raise ValueError('每级专业方向不能超过 40 字。')
        if levels[1] and not levels[0] or levels[2] and not levels[1]:
            raise ValueError('请从一级开始依次填写专业方向。')
        if levels[0] and levels not in normalized:
            normalized.append(levels)
    return normalized


def domain_labels(paths):
    return [' / '.join(level for level in path if level) for path in paths]


def normalize_term(raw, source):
    if not isinstance(raw, dict) or not isinstance(raw.get('english_term'), str) or not raw['english_term'].strip():
        raise ValueError('Each term needs a nonempty english_term')
    item = {}
    for field in FIELDS:
        value = raw.get(field)
        if field in LISTS:
            if value is None: value = []
            if isinstance(value, str): value = [v.strip() for v in value.split(';')]
            if not isinstance(value, list): raise ValueError(f'{field} must be an array')
            item[field] = unique(value)
        elif field == 'frequency':
            try: item[field] = max(0, int(value or 0))
            except (ValueError, TypeError): raise ValueError('frequency must be an integer')
        else:
            item[field] = str(value or '').strip()
    item['english_term'] = re.sub(r'\s+', ' ', item['english_term'])
    if item['domain'] and not item['domains']:
        item['domains'] = [item['domain']]
    item['sources'] = unique(item['sources'] + [source])
    if item['difficulty'] and item['difficulty'] not in {'L1','L2','L3','L4'}: raise ValueError('difficulty must be L1-L4')
    if item['priority'] and item['priority'] not in {'P1','P2','P3'}: raise ValueError('priority must be P1-P3')
    return item


def merge(old, new):
    out = old.copy()
    for field in FIELDS:
        if field in LISTS: out[field] = unique(old.get(field, []) + new[field])
        elif field == 'frequency': out[field] = old.get(field, 0) + new[field]
        elif new[field]: out[field] = new[field]
    return out


def import_book(path):
    path = Path(path)
    obj = json.loads(path.read_text(encoding='utf-8-sig'))
    if isinstance(obj, list): obj = {'name': path.stem, 'terms': obj}
    if not isinstance(obj, dict) or not isinstance(obj.get('terms'), list): raise ValueError('JSON needs a terms array')
    name = str(obj.get('name') or path.stem).strip()
    source = str(obj.get('source') or name).strip()
    requested_source_id = obj.get('source_id')
    source_id = requested_source_id.strip() if isinstance(requested_source_id, str) else None
    items = [normalize_term(v, source) for v in obj['terms']]
    if not items: raise ValueError('terms array is empty')
    keys = [key(t['english_term']) for t in items]
    if len(keys) != len(set(keys)): raise ValueError('Duplicate english_term in one book')
    db = connect(); stamp = now(); book_id = str(uuid.uuid4())
    try:
        with db:
            if source_id:
                document = db.execute('SELECT domain,domain_paths FROM documents WHERE id=?', (source_id,)).fetchone()
                if document:
                    labels = domain_labels(json.loads(document['domain_paths']))
                    if not labels and document['domain']:
                        labels = [document['domain']]
                    if labels:
                        for item in items:
                            item['domain'] = labels[0]
                            item['domains'] = labels.copy()
                else:
                    # An externally generated book can carry a source ID from another database.
                    # Keep its citation data, but do not create a broken foreign-key reference.
                    source_id = None
            db.execute('INSERT INTO books VALUES (?,?,?,?,?)', (book_id, name, source_id, stamp, json.dumps(obj, ensure_ascii=False)))
            for item in items:
                row = db.execute('SELECT * FROM terms WHERE term_key=?', (key(item['english_term']),)).fetchone()
                if row:
                    term_id = row['term_id']; merged = merge(json.loads(row['payload']), item)
                    db.execute('UPDATE terms SET payload=?, updated_at=? WHERE term_id=?', (json.dumps(merged, ensure_ascii=False), stamp, term_id))
                else:
                    term_id = str(uuid.uuid4())
                    db.execute('''INSERT INTO terms
                        (term_id,term_key,payload,learning_status,last_reviewed,next_review,
                         review_count,created_at,updated_at,review_streak)
                        VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (term_id, key(item['english_term']), json.dumps(item, ensure_ascii=False),
                         '陌生', None, None, 0, stamp, stamp, 0))
                db.execute('INSERT INTO book_terms (book_id,term_id,frequency,domain,domains) VALUES (?,?,?,?,?)',
                           (book_id, term_id, item['frequency'], item['domain'], json.dumps(item['domains'], ensure_ascii=False)))
        return book_id, len(items)
    finally: db.close()


def extract(input_path, source_name=None, domain='', threshold=50, domain_paths=None, progress=None):
    path = Path(input_path).resolve()
    if not path.is_file(): raise ValueError('File not found')
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        from PyPDF2 import PdfReader
        reader = PdfReader(str(path))
        pages = []
        total = len(reader.pages)
        for i, pdf_page in enumerate(reader.pages, 1):
            pages.append((f'Page {i}', pdf_page.extract_text() or ''))
            if progress: progress(round(i / max(total, 1) * 90), f'正在提取第 {i}/{total} 页')
    elif suffix == '.docx':
        from docx import Document
        document = Document(str(path))
        pages = [('Document', '\n'.join(p.text for p in document.paragraphs if p.text.strip()))]
        if progress: progress(90, '文档文字已提取')
    elif suffix == '.txt':
        pages = [('Text', path.read_text(encoding='utf-8-sig'))]
        if progress: progress(90, '文本已读取')
    else: raise ValueError('Supported files: PDF, DOCX, TXT')
    if not any(t.strip() for _, t in pages): raise ValueError('No extractable text; scanned PDFs need OCR')
    DATA.mkdir(exist_ok=True)
    doc_id = str(uuid.uuid4())
    output = DATA / f'{doc_id}.txt'
    labels = domain_labels(domain_paths or [])
    body = [f'SOURCE: {source_name or path.name}', f'SOURCE_ID: {doc_id}',
            f'TARGET_DOMAIN: {domain or (labels[0] if labels else "")}',
            f'TARGET_DOMAINS: {json.dumps(labels, ensure_ascii=False)}',
            f'SELECTION_THRESHOLD: {threshold}',
            'PAGE MARKERS: preserve for source_locations', '']
    for label, content in pages:
        cleaned = unicodedata.normalize('NFKC', content).replace('\x00', '')
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        body += [f'=== {label} ===', cleaned.strip(), '']
    output.write_text('\n'.join(body), encoding='utf-8')
    db = connect()
    with db: db.execute('INSERT INTO documents (id,title,path,added_at,domain,threshold,domain_paths) VALUES (?,?,?,?,?,?,?)',
                        (doc_id, source_name or path.name, str(output), now(), domain or (labels[0] if labels else ''),
                         threshold, json.dumps(domain_paths or [], ensure_ascii=False)))
    db.close()
    if progress: progress(100, 'TXT 已生成')
    return output, doc_id


def all_terms(db, book='', domain='', status='', query=''):
    rows = db.execute('SELECT * FROM terms').fetchall()
    allowed = None
    if book: allowed = {r['term_id']: r for r in db.execute('SELECT term_id,domain,domains FROM book_terms WHERE book_id=?', (book,))}
    result = []
    for row in rows:
        p = json.loads(row['payload'])
        if allowed is not None:
            if row['term_id'] not in allowed: continue
            source_term = allowed[row['term_id']]
            p['domain'] = source_term['domain'] or p['domain']
            p['domains'] = json.loads(source_term['domains']) or p.get('domains', [p['domain']])
        if domain and domain not in p.get('domains', [p['domain']]) and domain != p['domain']: continue
        if status and row['learning_status'] != status: continue
        if query and key(query) not in key(p['english_term'] + ' ' + p['chinese_translation'] + ' ' + p['english_definition']): continue
        result.append({**dict(row), **p})
    return result

STYLE = '''<style>
:root{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#243329;background:#f6f8f3}*{box-sizing:border-box}body{margin:0}header{background:#183b32;color:white;padding:22px max(24px,calc((100vw - 1120px)/2))}header h1{margin:0;font-size:23px}nav{display:flex;gap:22px;margin-top:14px}a{color:#18735b;text-decoration:none}nav a{color:#d9f4e7}main{max-width:1120px;margin:28px auto;padding:0 20px}.card{background:white;border:1px solid #e3e9e1;border-radius:16px;padding:22px;margin:14px 0;box-shadow:0 5px 20px #17382909}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.muted{color:#67766b}.pill{display:inline-block;background:#e6f3ea;color:#245e47;padding:4px 9px;border-radius:30px;margin:3px;font-size:12px}button,.button{background:#21845e;color:white;border:0;border-radius:8px;padding:10px 15px;cursor:pointer;font:inherit}button.secondary{background:#e8eee9;color:#244a36}input,select{border:1px solid #cdd8ce;border-radius:8px;padding:9px;font:inherit;max-width:100%}form.inline{display:flex;gap:9px;flex-wrap:wrap;align-items:center}.term{font-size:23px;font-weight:650;margin:0 0 8px}.count{font-size:30px;font-weight:700}.small{font-size:13px}blockquote{border-left:3px solid #91c8a8;margin:12px 0;padding:5px 12px;color:#425a49}details{margin-top:12px}svg{width:100%;height:500px;background:#fbfdfb;border:1px solid #e3e9e1;border-radius:12px}line{stroke:#abc5b2;stroke-width:2}circle{fill:#2b8a61}circle.center{fill:#e39b3e}text{font-size:13px;fill:#244234}.network-node{cursor:pointer;transition:opacity .3s}.network-node:hover circle{fill:#e39b3e}.network-link{transition:opacity .3s}#graph-tip{position:absolute;display:none;background:#183b32;color:white;padding:9px 12px;border-radius:8px;pointer-events:none;z-index:2;max-width:240px;font-size:13px}.network-wrap{position:relative}</style>'''
STYLE += '<style>' + (ROOT / 'static' / 'style.css').read_text(encoding='utf-8') + '</style>'
BASE = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>专业词汇学习</title>''' + STYLE + '''<body class="{{'study-mode' if request.endpoint=='study' else 'term-mode' if request.endpoint=='term_detail' else ''}}"><header><h1>专业词汇学习 · Demo</h1><nav><a href="/">总览</a><a href="/terms">总词库</a><a href="/books">来源词本</a><a href="/study">学习</a><a href="/network">联想网络</a><a href="/new">添加语料</a></nav></header><main>{{ body|safe }}</main></body></html>'''

def page(template, **context):
    from flask import render_template_string as render
    if request.endpoint == 'network' and request.args.get('embed') == '1':
        embed_style = '<style>html,body{width:100%;height:100%;margin:0!important;padding:0!important;overflow:hidden!important}main{width:100%;height:100%;margin:0!important;padding:0!important;max-width:none!important}.card{width:100%;height:100%;margin:0;padding:0;border:0;border-radius:0;box-shadow:none;background:transparent;display:flex;flex-direction:column}.card>h2,.card>p,.card>select{display:none!important}.network-wrap{flex:1;min-height:0;width:100%}#graph{display:block;width:100%;height:100%;border:0;background:#f7faf7}#graph text{font-size:18px}</style>'
        return '<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' + STYLE + embed_style + '</head><body><main>' + render(template, **context) + '</main></body></html>'
    return render(BASE, body=render(template, **context))

@app.route('/')
def home():
    db = connect()
    total = db.execute('SELECT count(*) FROM terms').fetchone()[0]
    due = db.execute("SELECT count(*) FROM terms WHERE next_review IS NULL OR next_review<=?",
                     (now(),)).fetchone()[0]
    book_count = db.execute('SELECT count(*) FROM books').fetchone()[0]
    db.close()
    return page('''<section class="hero"><div><span class="eyebrow">YOUR VOCABULARY SPACE</span><h1>让专业阅读，成为可积累的知识。</h1><p>从语料中提炼术语，建立词本，并用复习和联想网络巩固理解。</p><a class="button" href="/new">添加新语料 <span aria-hidden="true">↗</span></a></div><div class="hero-visual"><div class="orbit orbit-one"></div><div class="orbit orbit-two"></div><div class="hero-word">Vocabulary<span>connections · context · recall</span></div></div></section><section class="stats-grid"><a class="stat-card" href="/terms"><span class="stat-icon">◫</span><span class="muted">总单词本</span><strong>{{total}}</strong><span>查看全部术语 →</span></a><a class="stat-card" href="/study"><span class="stat-icon">◉</span><span class="muted">待学习与复习</span><strong>{{due}}</strong><span>开始学习 →</span></a><a class="stat-card" href="/books"><span class="stat-icon">▤</span><span class="muted">来源单词本</span><strong>{{book_count}}</strong><span>浏览词本 →</span></a></section><section class="section-heading"><div><span class="eyebrow">YOUR WORKFLOW</span><h2>从一篇文章，走向一张知识网络</h2></div></section><div class="step-grid"><div class="step-card"><span>01</span><h3>导入语料</h3><p>上传 PDF、DOCX 或 TXT，检查规范化的文本。</p></div><div class="step-card"><span>02</span><h3>生成词本</h3><p>将 TXT 与专属 SKILL 手动发给 AI，再导入 JSON。</p></div><div class="step-card"><span>03</span><h3>学习与联想</h3><p>按来源和方向学习，在词卡中探索相关术语。</p></div></div>''',total=total,due=due,book_count=book_count)


@app.route('/books')
def books():
    db = connect()
    rows = db.execute("""SELECT b.id,b.name,b.imported_at,b.source_id,d.title AS source_title,
        d.domain,d.domain_paths,COUNT(bt.term_id) AS term_count
        FROM books b LEFT JOIN documents d ON d.id=b.source_id
        LEFT JOIN book_terms bt ON bt.book_id=b.id
        GROUP BY b.id ORDER BY b.imported_at DESC""").fetchall()
    book_domains = {row['id']: unique(r['domain'] for r in db.execute(
        'SELECT DISTINCT domain FROM book_terms WHERE book_id=?', (row['id'],))) for row in rows}
    db.close()
    books = []
    for row in rows:
        item = dict(row)
        paths = json.loads(item['domain_paths'] or '[]')
        item['domains'] = domain_labels(paths) or ([item['domain']] if item['domain'] else book_domains[row['id']])
        books.append(item)
    domains = sorted({d for b in books for d in b['domains']})
    selected_domain = request.args.get('domain', '')
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    if selected_domain: books = [b for b in books if selected_domain in b['domains']]
    if date_from: books = [b for b in books if b['imported_at'][:10] >= date_from]
    if date_to: books = [b for b in books if b['imported_at'][:10] <= date_to]
    sort = request.args.get('sort', 'newest')
    if sort == 'oldest': books.sort(key=lambda b:b['imported_at'])
    elif sort == 'name': books.sort(key=lambda b:b['name'].casefold())
    elif sort == 'largest': books.sort(key=lambda b:-b['term_count'])
    else: books.sort(key=lambda b:b['imported_at'], reverse=True)
    view = request.args.get('view', 'cards')
    if view not in {'cards','list'}: view = 'cards'
    return page('''<section class="page-heading"><span class="eyebrow">SOURCE COLLECTIONS</span><h1>来源单词本</h1><p>按导入时间与专业方向找到你想学习的词本。</p></section><div class="card filter-card"><form class="inline"><label>专业方向<select name="domain"><option value="">全部方向</option>{% for d in domains %}<option value="{{d}}" {{'selected' if request.args.get('domain')==d else ''}}>{{d}}</option>{% endfor %}</select></label><label>起始日期<input type="date" name="from" value="{{request.args.get('from','')}}"></label><label>截止日期<input type="date" name="to" value="{{request.args.get('to','')}}"></label><label>排序<select name="sort"><option value="newest" {{'selected' if sort=='newest' else ''}}>最新导入</option><option value="oldest" {{'selected' if sort=='oldest' else ''}}>最早导入</option><option value="name" {{'selected' if sort=='name' else ''}}>名称</option><option value="largest" {{'selected' if sort=='largest' else ''}}>词条数量</option></select></label><label>视图<select name="view"><option value="cards" {{'selected' if view=='cards' else ''}}>卡片</option><option value="list" {{'selected' if view=='list' else ''}}>列表</option></select></label><button>应用</button></form></div><p class="muted">找到 {{books|length}} 个词本</p>{% if view=='cards' %}<div class="book-grid">{% for b in books %}<a class="book-card" href="{{url_for('terms',book=b.id)}}"><span class="book-mark">BOOK</span><h2>{{b.name}}</h2><p class="muted book-source" title="{{b.source_title or '手动导入的词本'}}">{{b.source_title or '手动导入的词本'}}</p><div>{% for d in b.domains %}<span class="pill">{{d}}</span>{% endfor %}</div><footer><span>{{b.term_count}} 个词</span><span>{{b.imported_at[:10]}}</span></footer></a>{% else %}<div class="card">没有匹配的词本。</div>{% endfor %}</div>{% else %}<div class="card table-wrap"><table><thead><tr><th>词本</th><th>专业方向</th><th>词条</th><th>导入日期</th></tr></thead><tbody>{% for b in books %}<tr><td><a href="{{url_for('terms',book=b.id)}}">{{b.name}}</a></td><td>{{b.domains|join(' · ') or '未设置'}}</td><td>{{b.term_count}}</td><td>{{b.imported_at[:10]}}</td></tr>{% else %}<tr><td colspan="4">没有匹配的词本。</td></tr>{% endfor %}</tbody></table></div>{% endif %}''',books=books,domains=domains,sort=sort,view=view,request=request)


def job_update(job_id, state, percent, message, document_id=None):
    db = connect()
    with db:
        db.execute('UPDATE conversion_jobs SET state=?,progress=?,message=?,document_id=? WHERE id=?',
                   (state, percent, message, document_id, job_id))
    db.close()


def run_conversion(job_id, temporary, filename, paths, threshold):
    try:
        _, doc_id = extract(temporary, source_name=filename, threshold=threshold,
                            domain_paths=paths,
                            progress=lambda percent, message: job_update(job_id, 'working', percent, message))
        job_update(job_id, 'done', 100, 'TXT 已生成', doc_id)
    except Exception as exc:
        job_update(job_id, 'failed', 0, f'转换失败：{exc}')
    finally:
        temporary.unlink(missing_ok=True)


@app.route('/new', methods=['GET', 'POST'])
def new_source():
    if request.method == 'POST':
        upload = request.files.get('file')
        try:
            if not upload or not upload.filename: raise ValueError('请选择 PDF、DOCX 或 TXT 文件。')
            suffix = Path(upload.filename).suffix.lower()
            if suffix not in {'.pdf', '.docx', '.txt'}: raise ValueError('仅支持 PDF、DOCX、TXT 文件。')
            threshold = int(request.form.get('threshold', '50'))
            if not 0 <= threshold <= 100: raise ValueError('词汇数量阈值必须在 0 到 100 之间。')
            paths = parse_domain_paths(request.form.get('domain_paths', '[]'))
            DATA.mkdir(exist_ok=True)
            temporary = DATA / f'upload-{uuid.uuid4()}{suffix}'
            upload.save(temporary)
            job_id = str(uuid.uuid4())
            db = connect()
            with db:
                db.execute('INSERT INTO conversion_jobs VALUES (?,?,?,?,?,?)',
                           (job_id, 'working', 0, '文件已上传，准备转换', None, now()))
            db.close()
            Thread(target=run_conversion,
                   args=(job_id, temporary, Path(upload.filename).name, paths, threshold), daemon=True).start()
            return jsonify({'job_id': job_id, 'status_url': url_for('conversion_status', job_id=job_id)})
        except (ValueError, OSError) as exc:
            return jsonify({'error': str(exc)}), 400
    db = connect()
    existing_paths = []
    for doc in db.execute('SELECT domain,domain_paths FROM documents ORDER BY added_at DESC'):
        paths = json.loads(doc['domain_paths']) or ([[doc['domain'], '', '']] if doc['domain'] else [])
        for path in paths:
            if path not in existing_paths: existing_paths.append(path)
    db.close()
    return page('''<section class="page-heading"><span class="eyebrow">BUILD YOUR LIBRARY</span><h1>添加语料</h1><p>上传文档，检查 TXT，再将专属 SKILL 与 TXT 手动发送给 AI。</p></section><div class="two-col"><div class="card"><h2>01 · 生成 TXT</h2><form id="source-form" method="post" enctype="multipart/form-data"><label class="field">选择文件<input type="file" name="file" accept=".pdf,.docx,.txt" required></label><div class="field"><strong>专业方向</strong><p class="muted small">可选择已有方向，或添加多个一级 / 二级 / 三级方向。</p>{% if existing_paths %}<div class="inline"><select id="existing-domain"><option value="">选择已有方向</option>{% for path in existing_paths %}<option value="{{path|tojson|forceescape}}">{{path|join(' / ')}}</option>{% endfor %}</select><button type="button" class="secondary" id="use-domain">添加所选方向</button></div>{% endif %}<div id="domain-rows"></div><button type="button" class="secondary" id="add-domain">＋ 新建专业方向</button></div><label class="field">词汇数量阈值 <strong id="threshold-value">50</strong><input id="threshold" type="range" name="threshold" min="0" max="100" value="50"><span class="muted small">越低，AI 应选入越多专业词汇。</span></label><input type="hidden" name="domain_paths" id="domain-paths"><button id="convert-button" type="submit">生成 TXT</button><div id="progress-panel" class="progress-panel" hidden><div id="progress-label">准备中</div><progress id="convert-progress" max="100" value="0"></progress><p id="progress-message" class="muted small"></p></div><p id="upload-error" class="error" role="alert"></p></form></div><div class="card"><h2>02 · 导入 AI 单词本</h2><p>检查 TXT 后，下载专属 SKILL；手动交给 AI，最后上传它返回的 JSON。</p><form method="post" action="{{url_for('upload_book')}}" enctype="multipart/form-data"><label class="field">JSON 文件<input type="file" name="file" accept=".json,application/json" required></label><button>导入 JSON</button></form><p class="muted small">最大文件 20 MB。扫描版 PDF 需先 OCR。</p></div></div><script>
const rows=document.getElementById('domain-rows'), add=document.getElementById('add-domain');
function addRow(values=['','','']){if(rows.children.length>=8)return;const row=document.createElement('div');row.className='domain-row';['一级','二级','三级'].forEach((label,i)=>{const input=document.createElement('input');input.placeholder=label+'专业方向';input.maxLength=40;input.value=values[i];input.setAttribute('aria-label',label+'专业方向');row.appendChild(input)});const remove=document.createElement('button');remove.type='button';remove.className='secondary';remove.textContent='移除';remove.onclick=()=>row.remove();row.appendChild(remove);rows.appendChild(row)}
add.onclick=()=>addRow();addRow();const use=document.getElementById('use-domain');if(use)use.onclick=()=>{const selected=document.getElementById('existing-domain').value;if(selected)addRow(JSON.parse(selected))};const slider=document.getElementById('threshold');slider.oninput=()=>document.getElementById('threshold-value').textContent=slider.value;
const form=document.getElementById('source-form'),panel=document.getElementById('progress-panel'),bar=document.getElementById('convert-progress'),label=document.getElementById('progress-label'),message=document.getElementById('progress-message'),error=document.getElementById('upload-error'),button=document.getElementById('convert-button');
form.onsubmit=async e=>{e.preventDefault();error.textContent='';const paths=[...rows.children].map(row=>[...row.querySelectorAll('input')].map(input=>input.value.trim())).filter(path=>path.some(Boolean));document.getElementById('domain-paths').value=JSON.stringify(paths);button.disabled=true;panel.hidden=false;bar.value=0;label.textContent='上传文件';const data=new FormData(form);const xhr=new XMLHttpRequest();xhr.open('POST','/new');xhr.upload.onprogress=e=>{if(e.lengthComputable){bar.value=Math.min(15,Math.round(e.loaded/e.total*15));message.textContent='上传中 '+Math.round(e.loaded/e.total*100)+'%'}};xhr.onerror=()=>{error.textContent='网络错误，请重试。';button.disabled=false};xhr.onload=async()=>{let result;try{result=JSON.parse(xhr.responseText)}catch{result={error:'服务器响应无效。'}}if(xhr.status!==200){error.textContent=result.error||'上传失败。';button.disabled=false;return}label.textContent='转换中';async function poll(){try{const response=await fetch(result.status_url);const state=await response.json();bar.value=Math.max(bar.value,state.progress);message.textContent=state.message;if(state.state==='done'){location.href=state.document_url;return}if(state.state==='failed'){error.textContent=state.message;button.disabled=false;return}setTimeout(poll,500)}catch{error.textContent='无法获取转换进度。';button.disabled=false}}poll()};xhr.send(data)};
</script>''',existing_paths=existing_paths)


@app.route('/jobs/<job_id>')
def conversion_status(job_id):
    db = connect(); row = db.execute('SELECT * FROM conversion_jobs WHERE id=?', (job_id,)).fetchone(); db.close()
    if not row: abort(404)
    return jsonify({'state':row['state'],'progress':row['progress'],'message':row['message'],
                    'document_url':url_for('document_preview', doc_id=row['document_id']) if row['document_id'] else None})


@app.route('/document/<doc_id>')
def document_preview(doc_id):
    db=connect(); doc=db.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone(); db.close()
    if not doc: abort(404)
    path=Path(doc['path'])
    if not path.is_file(): abort(404)
    content=path.read_text(encoding='utf-8')
    labels = domain_labels(json.loads(doc['domain_paths'])) or ([doc['domain']] if doc['domain'] else [])
    return page('''<section class="page-heading"><span class="eyebrow">SOURCE READY</span><h1>{{doc['title']}}</h1><p>检查 TXT，按需调整阈值，再下载 TXT 与专属 SKILL.md。</p></section><div class="two-col"><div class="card"><h2>语料设置</h2><p><strong>专业方向</strong></p><div>{% for label in labels %}<span class="pill">{{label}}</span>{% else %}<span class="muted">由 AI 判断</span>{% endfor %}</div><form method="post" action="{{url_for('update_threshold',doc_id=doc['id'])}}"><label class="field">词汇数量阈值 <strong id="threshold-value">{{doc['threshold']}}</strong><input id="threshold" type="range" name="threshold" min="0" max="100" value="{{doc['threshold']}}" oninput="document.getElementById('threshold-value').textContent=this.value"></label><button class="secondary">保存阈值</button></form><p class="muted small">保存后 TXT 页首及下载的 SKILL 会同步更新。阈值越低，建议提取的词越多。</p><p><a class="button" href="{{url_for('document_download',doc_id=doc['id'])}}">下载 TXT</a> <a class="button secondary" href="{{url_for('skill_download',doc_id=doc['id'])}}">下载 SKILL.md</a></p><p class="muted small">SOURCE_ID: {{doc['id']}} · {{content|length}} 字符</p></div><div class="card"><h2>导入 AI 生成的单词本</h2><p>将 TXT 与 SKILL 手动发送给 AI 后，上传它返回的 JSON。</p><form method="post" action="{{url_for('upload_book')}}" enctype="multipart/form-data"><label class="field">JSON 文件<input type="file" name="file" accept=".json,application/json" required></label><button>导入 JSON</button></form><p class="muted small">JSON 中的 source_id 应与上方一致。</p></div></div><div class="card"><details open><summary>预览转换后的 TXT</summary><pre class="source-preview">{{content}}</pre></details></div>''',doc=doc,content=content,labels=labels)


@app.post('/document/<doc_id>/threshold')
def update_threshold(doc_id):
    try:
        threshold = int(request.form.get('threshold', ''))
    except ValueError:
        abort(400)
    if not 0 <= threshold <= 100: abort(400)
    db = connect(); doc = db.execute('SELECT path FROM documents WHERE id=?', (doc_id,)).fetchone()
    if not doc: db.close(); abort(404)
    path = Path(doc['path'])
    if not path.is_file(): db.close(); abort(404)
    content = path.read_text(encoding='utf-8')
    replacement = f'SELECTION_THRESHOLD: {threshold}'
    if re.search(r'^SELECTION_THRESHOLD:.*$', content, flags=re.MULTILINE):
        content = re.sub(r'^SELECTION_THRESHOLD:.*$', replacement, content, count=1, flags=re.MULTILINE)
    else:
        content = replacement + '\n' + content
    path.write_text(content, encoding='utf-8')
    with db: db.execute('UPDATE documents SET threshold=? WHERE id=?', (threshold, doc_id))
    db.close()
    return redirect(url_for('document_preview', doc_id=doc_id))

@app.route('/document/<doc_id>/download')
def document_download(doc_id):
    db=connect(); doc=db.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone(); db.close()
    if not doc or not Path(doc['path']).is_file(): abort(404)
    return send_file(doc['path'],as_attachment=True,download_name=Path(doc['title']).stem+'.txt',mimetype='text/plain')

@app.route('/skill/download')
def skill_download():
    content = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
    doc_id = request.args.get('doc_id')
    if doc_id:
        db = connect(); doc = db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone(); db.close()
        if not doc: abort(404)
        labels = domain_labels(json.loads(doc['domain_paths'])) or ([doc['domain']] if doc['domain'] else [])
        content += (f'\n## Settings for this source\n- SOURCE_ID: `{doc_id}`\n'
                    f'- TARGET_DOMAINS: `{json.dumps(labels, ensure_ascii=False)}`\n'
                    f'- SELECTION_THRESHOLD: `{doc["threshold"]}` out of 100.\n'
                    'Apply these settings to every term in this source.\n')
    return Response(content, mimetype='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="SKILL.md"'})

@app.post('/books/upload')
def upload_book():
    upload=request.files.get('file')
    if not upload or Path(upload.filename or '').suffix.lower()!='.json':
        return page('<div class="card"><h2>导入失败</h2><p>请选择 JSON 文件。</p><a href="/new">返回</a></div>'),400
    DATA.mkdir(exist_ok=True)
    temporary=DATA / f'book-{uuid.uuid4()}.json'
    try:
        upload.save(temporary)
        submitted = json.loads(temporary.read_text(encoding='utf-8-sig'))
        requested_source_id = submitted.get('source_id') if isinstance(submitted, dict) else None
        book_id,count=import_book(temporary)
        db=connect(); linked=db.execute('SELECT source_id FROM books WHERE id=?',(book_id,)).fetchone()['source_id'];db.close()
        unlinked=bool(requested_source_id and not linked)
        return page('''<div class="card"><h1>导入完成</h1><p>已生成来源单词本，包含 {{count}} 个词条；总单词本已更新。</p>{% if unlinked %}<p class="muted">JSON 中的 source_id 在本地语料库中未找到，已作为独立词本导入；JSON 中的文献来源和位置仍保留。</p>{% endif %}<p><a class="button" href="{{url_for('terms',book=book_id)}}">查看新词本</a> <a href="/terms">查看总单词本</a></p></div>''',book_id=book_id,count=count,unlinked=unlinked)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return page('<div class="card"><h2>导入失败</h2><p>{{error}}</p><a href="/new">返回</a></div>',error=str(exc)),400
    finally:
        temporary.unlink(missing_ok=True)

TERM_CONTENT = '''<div class="term-detail-layout"><div class="term-main"><div class="detail-section"><span class="eyebrow">MEANING</span><h2>{{t.chinese_translation or '暂无中文译名'}}</h2><p title="{{t.english_definition}}">{{t.english_definition}}</p><p title="{{t.chinese_definition}}">{{t.chinese_definition}}</p></div><div class="detail-section"><span class="eyebrow">EXAMPLES</span>{% if t.example_sentence %}<p title="{{t.example_sentence}}">{{t.example_sentence}}</p>{% endif %}{% if t.source_sentence %}<blockquote title="{{t.source_sentence}}">{{t.source_sentence}}</blockquote>{% endif %}</div><div class="detail-section"><span class="eyebrow">CONNECTIONS</span><p><strong>常见搭配</strong> {{t.collocations|join(' · ') or '暂无'}}</p><p><strong>关联术语</strong> {% for name,id in related %}{% if id %}<a href="{{url_for('term_detail',term_id=id)}}">{{name}}</a>{% else %}{{name}}{% endif %}{% if not loop.last %} · {% endif %}{% else %}暂无{% endfor %}</p></div><div class="detail-section"><span class="eyebrow">SOURCE & PROGRESS</span><p>来源：{{t.sources|join(' · ') or '暂无'}}</p><p>位置：{{t.source_locations|join(' · ') or '暂无'}}</p><p>频次：{{t.frequency}} · 下次复习：{{t.next_review or '待学习'}}</p>{% for context in t.original_contexts %}<blockquote title="{{context}}">{{context}}</blockquote>{% endfor %}</div></div><aside class="term-network"><div class="network-sticky"><span class="eyebrow">ASSOCIATION MAP</span><h3>联想网络</h3><iframe scrolling="no" title="{{t.english_term}} 的联想网络" src="{{url_for('network',term=t.term_id,embed=1)}}"></iframe></div></aside></div>'''


def related_for(db, term):
    result = []
    for name in term.get('related_terms', []):
        row = db.execute('SELECT term_id FROM terms WHERE term_key=?', (key(name),)).fetchone()
        result.append((name, row['term_id'] if row else None))
    return result


def term_detail_html(term, related):
    return render_template_string(TERM_CONTENT, t=term, related=related)


def term_domain_paths(term):
    return term.get('domains') or ([term.get('domain', '')] if term.get('domain') else [])


def domain_tree_for(terms):
    tree = {}
    for term in terms:
        for label in term_domain_paths(term):
            levels = [part.strip() for part in label.split(' / ') if part.strip()]
            if levels:
                root = tree.setdefault(levels[0], {})
                if len(levels) > 1:
                    branch = root.setdefault(levels[1], set())
                    if len(levels) > 2: branch.add(levels[2])
    return {first:{second:sorted(thirds) for second,thirds in sorted(children.items())}
            for first,children in sorted(tree.items())}


def in_domain_branch(term, level1, level2, level3):
    if not level1: return True
    for label in term_domain_paths(term):
        levels = [part.strip() for part in label.split(' / ') if part.strip()]
        if levels and levels[0] == level1 and (not level2 or len(levels)>1 and levels[1]==level2) \
                and (not level3 or len(levels)>2 and levels[2]==level3):
            return True
    return False


@app.route('/terms')
def terms():
    db = connect()
    books = db.execute('SELECT id,name FROM books ORDER BY imported_at DESC').fetchall()
    all_items = all_terms(db)
    tree = domain_tree_for(all_items)
    level1 = request.args.get('level1', '')
    level2 = request.args.get('level2', '') if level1 in tree else ''
    level3 = request.args.get('level3', '') if level2 in tree.get(level1, {}) else ''
    if level2 not in tree.get(level1, {}): level2 = level3 = ''
    if level3 not in tree.get(level1, {}).get(level2, []): level3 = ''
    items = all_terms(db, book=request.args.get('book',''), status=request.args.get('status',''),
                      query=request.args.get('q',''))
    db.close()
    items = [item for item in items if in_domain_branch(item, level1, level2, level3)]
    view = request.args.get('view', 'cards')
    if view not in {'cards', 'list'}: view = 'cards'
    return page('''<section class="page-heading"><span class="eyebrow">WORD LIBRARY</span><h1>{{'来源词库' if request.args.get('book') else '总词库'}}</h1><p>{{items|length}} 个术语 · 点击词卡查看完整内容</p></section><div class="card filter-card"><form id="term-filters" class="inline"><label>搜索<input name="q" placeholder="英文、中文或释义" value="{{request.args.get('q','')}}"></label><label>词本<select name="book"><option value="">总词库</option>{% for b in books %}<option value="{{b['id']}}" {{'selected' if request.args.get('book')==b['id'] else ''}}>{{b['name']}}</option>{% endfor %}</select></label><label>一级领域<select id="level1" name="level1"><option value="">全部领域</option>{% for name in tree %}<option value="{{name}}" {{'selected' if level1==name else ''}}>{{name}}</option>{% endfor %}</select></label><label id="level2-wrap" {% if not level1 %}hidden{% endif %}>二级领域<select id="level2" name="level2"></select></label><label id="level3-wrap" {% if not level2 %}hidden{% endif %}>三级领域<select id="level3" name="level3"></select></label><label>状态<select name="status"><option value="">全部状态</option>{% for status in ['陌生','模糊','认识','熟悉'] %}<option value="{{status}}" {{'selected' if request.args.get('status')==status else ''}}>{{status}}</option>{% endfor %}</select></label><label>视图<select name="view"><option value="cards" {{'selected' if view=='cards' else ''}}>卡片</option><option value="list" {{'selected' if view=='list' else ''}}>列表</option></select></label><button>应用筛选</button></form></div>{% if view=='cards' %}<div class="term-grid">{% for t in items %}<a class="vocab-card term-tile" href="{{url_for('term_detail',term_id=t.term_id)}}"><span class="eyebrow">{{t.category or 'TERM'}}</span><h2>{{t.english_term}}</h2><p class="phonetic">{{t.phonetic_symbols}}</p><p class="translation">{{t.chinese_translation}}</p><div>{% for d in t.domains or [t.domain] %}<span class="pill">{{d}}</span>{% endfor %}</div><p class="muted small">{{t.english_definition[:160]}}</p><footer><span>{{t.learning_status}} · 频次 {{t.frequency}}</span><span>查看详情 →</span></footer></a>{% else %}<div class="card">没有匹配词汇。</div>{% endfor %}</div>{% else %}<div class="card table-wrap"><table><thead><tr><th>英文术语</th><th>音标</th><th>中文译名</th><th>专业方向</th><th>状态</th></tr></thead><tbody>{% for t in items %}<tr><td><a href="{{url_for('term_detail',term_id=t.term_id)}}">{{t.english_term}}</a></td><td>{{t.phonetic_symbols}}</td><td>{{t.chinese_translation}}</td><td>{{(t.domains or [t.domain])|join(' · ')}}</td><td>{{t.learning_status}}</td></tr>{% else %}<tr><td colspan="5">没有匹配词汇。</td></tr>{% endfor %}</tbody></table></div>{% endif %}<script>
const tree={{tree|tojson}},one=document.getElementById('level1'),two=document.getElementById('level2'),three=document.getElementById('level3'),twoWrap=document.getElementById('level2-wrap'),threeWrap=document.getElementById('level3-wrap');
function fill(select,values,selected,allLabel){select.replaceChildren();const blank=new Option(allLabel,'');select.add(blank);values.forEach(value=>select.add(new Option(value,value)));select.value=values.includes(selected)?selected:''}
function updateTwo(selectedTwo='',selectedThree=''){const first=one.value,children=tree[first]||{};twoWrap.hidden=!first;fill(two,Object.keys(children),selectedTwo,'全部二级领域');updateThree(selectedThree)}
function updateThree(selected=''){const children=tree[one.value]||{},thirds=children[two.value]||[];threeWrap.hidden=!one.value||!two.value;fill(three,thirds,selected,'全部三级领域')}
one.onchange=()=>updateTwo();two.onchange=()=>updateThree();updateTwo({{level2|tojson}},{{level3|tojson}});
</script>''',items=items,books=books,tree=tree,level1=level1,level2=level2,level3=level3,view=view,request=request)


@app.route('/term/<term_id>')
def term_detail(term_id):
    db = connect()
    row = db.execute('SELECT * FROM terms WHERE term_id=?', (term_id,)).fetchone()
    if not row: db.close(); abort(404)
    t = {**dict(row), **json.loads(row['payload'])}
    related = related_for(db, t)
    db.close()
    detail = term_detail_html(t, related)
    return page('''<section class="page-heading"><a href="{{url_for('terms')}}">← 返回词库</a><span class="eyebrow">TERM PROFILE</span><h1>{{t.english_term}}</h1><p class="phonetic">{{t.phonetic_symbols}}</p><div>{% for d in t.domains or [t.domain] %}{% if d %}<span class="pill">{{d}}</span>{% endif %}{% endfor %}<span class="pill">{{t.difficulty}}</span><span class="pill">{{t.learning_status}}</span></div><a class="term-network-link" href="{{url_for('network',term=t.term_id)}}">打开联想网络 ↗</a></section>{{detail|safe}}''',t=t,detail=detail)


@app.route('/study')
def study():
    db = connect()
    books = db.execute('SELECT id,name FROM books ORDER BY imported_at DESC').fetchall()
    all_items = all_terms(db)
    domains = sorted({d for x in all_items for d in term_domain_paths(x) if d})
    items = all_terms(db, book=request.args.get('book',''), domain=request.args.get('domain',''))
    current_time = now()
    items = [x for x in items if not x['next_review'] or x['next_review'] <= current_time]
    items.sort(key=lambda x: (x['learning_status']=='熟悉', x['next_review'] or '',
                {'P1':0,'P2':1,'P3':2}.get(x['priority'],3),
                -datetime.fromisoformat(x['updated_at']).timestamp(), -x['frequency']))
    t = items[0] if items else None
    detail = term_detail_html(t, related_for(db,t)) if t else ''
    db.close()
    return page('''<div class="study-shell"><div class="study-top"><div class="study-topline"><div><span class="eyebrow">DAILY PRACTICE</span><h1>学习与复习</h1></div><strong class="study-count">{{items|length}} 个待学</strong></div><details class="study-filter"><summary>选择单词本与专业方向 <span aria-hidden="true">⌄</span></summary><form class="inline"><label>词本<select name="book"><option value="">总词库</option>{% for b in books %}<option value="{{b['id']}}" {{'selected' if request.args.get('book')==b['id'] else ''}}>{{b['name']}}</option>{% endfor %}</select></label><label>专业方向<select name="domain"><option value="">全部方向</option>{% for d in domains %}<option value="{{d}}" {{'selected' if request.args.get('domain')==d else ''}}>{{d}}</option>{% endfor %}</select></label><button>应用筛选</button></form></details>{% if t %}<div class="study-word"><span class="eyebrow">RECALL CARD</span><h2>{{t.english_term}}</h2><p class="study-phonetic">{{t.phonetic_symbols or '暂无音标'}}</p><div class="study-tags">{% for d in t.domains or [t.domain] %}{% if d %}<span class="pill">{{d}}</span>{% endif %}{% endfor %}{% if t.difficulty %}<span class="pill">{{t.difficulty}}</span>{% endif %}{% if t.priority %}<span class="pill">{{t.priority}}</span>{% endif %}</div></div></div><div class="study-stage" id="study-stage"><div class="study-front"><span class="eyebrow">THINK BEFORE YOU REVEAL</span><p>先回想这个术语的含义、用法与相关概念。</p>{% if t.source_sentence %}<blockquote title="{{t.source_sentence}}">{{t.source_sentence}}</blockquote>{% endif %}<button type="button" id="reveal-answer">显示答案</button></div><div class="study-reveal" id="study-reveal" hidden>{{detail|safe}}<a class="study-full-link" href="{{url_for('term_detail',term_id=t.term_id)}}">查看完整词卡 ↗</a></div></div>{% else %}</div><div class="study-stage study-empty"><h2>当前没有到期词汇</h2><p>调整筛选条件，或稍后再来复习。</p><a class="button" href="/terms">浏览总词库</a></div>{% endif %}</div>{% if t %}<div class="study-bottom"><div class="study-bottom-inner"><div class="study-bottom-title"><span class="eyebrow">HOW WELL DID YOU RECALL?</span><strong>选择记忆状态</strong></div><form method="post" action="{{url_for('review',term_id=t.term_id)}}" id="review-form"><input type="hidden" name="book" value="{{request.args.get('book','')}}"><input type="hidden" name="domain" value="{{request.args.get('domain','')}}"><button name="grade" value="unfamiliar" class="grade-unfamiliar" disabled><strong>陌生</strong><small>10 分钟后</small></button><button name="grade" value="fuzzy" class="grade-fuzzy" disabled><strong>模糊</strong><small>明天</small></button><button name="grade" value="known" class="grade-known" disabled><strong>认识</strong><small>3 天起</small></button><button name="grade" value="familiar" class="grade-familiar" disabled><strong>熟悉</strong><small>7 天起</small></button></form></div></div><script>const revealButton=document.getElementById('reveal-answer'),reveal=document.getElementById('study-reveal'),stage=document.getElementById('study-stage');if(revealButton){revealButton.onclick=()=>{stage.classList.add('is-revealed');reveal.hidden=false;for(const button of document.querySelectorAll('#review-form button'))button.disabled=false}}</script>{% endif %}''',items=items,t=t,books=books,domains=domains,detail=detail,request=request)


def review_schedule(grade, streak, current=None):
    """A simple spaced-review schedule: weaker recall returns sooner."""
    current = current or datetime.now(APP_TZ)
    if grade == 'unfamiliar':
        return '陌生', current + timedelta(minutes=10), 0
    if grade == 'fuzzy':
        return '模糊', current + timedelta(days=1), 0
    if grade == 'known':
        return '认识', current + timedelta(days=min(60, 3 * 2 ** min(streak, 4))), streak + 1
    if grade == 'familiar':
        return '熟悉', current + timedelta(days=min(120, 7 * 2 ** min(streak, 4))), streak + 1
    raise ValueError('Invalid review grade')


@app.post('/review/<term_id>')
def review(term_id):
    grade = request.form.get('grade')
    if grade not in {'unfamiliar','fuzzy','known','familiar'}: abort(400)
    db = connect()
    row = db.execute('SELECT review_count,review_streak FROM terms WHERE term_id=?', (term_id,)).fetchone()
    if not row: db.close(); abort(404)
    status, next_time, streak = review_schedule(grade, row['review_streak'])
    with db:
        db.execute('''UPDATE terms SET learning_status=?,last_reviewed=?,next_review=?,
                      review_count=?,review_streak=? WHERE term_id=?''',
                   (status, now(), next_time.isoformat(timespec='seconds'),
                    row['review_count'] + 1, streak, term_id))
    db.close()
    return redirect(url_for('study',book=request.form.get('book',''),
                            domain=request.form.get('domain','')))

@app.route('/network')
def network():
    db=connect(); items=all_terms(db); db.close()
    nodes=[{'id':t['term_id'],'label':t['english_term'],'zh':t['chinese_translation'],
            'related':t['related_terms'],'collocations':t['collocations']} for t in items]
    return page('''<div class="card"><h2>联想网络</h2><p>点击节点切换中心；悬停查看中文释义。关联词和常见搭配来自词本。</p><select id="term-select">{% for t in items %}<option value="{{t.term_id}}" {{'selected' if request.args.get('term')==t.term_id else ''}}>{{t.english_term}}</option>{% endfor %}</select>{% if items %}<div class="network-wrap"><svg id="graph" viewBox="{{'0 0 600 520' if request.args.get('embed')=='1' else '0 0 900 520'}}" role="img" aria-label="可交互的词汇联想网络"></svg><div id="graph-tip"></div></div><p id="graph-detail" class="muted"></p>{% else %}<p>导入词本后显示网络。</p>{% endif %}</div><script>
const embedded={{(request.args.get('embed')=='1')|tojson}};const all={{nodes|tojson}};const svg=document.getElementById('graph');const select=document.getElementById('term-select');const tip=document.getElementById('graph-tip');const detail=document.getElementById('graph-detail');
if(svg&&all.length){const ns='http://www.w3.org/2000/svg';const byId=new Map(all.map(n=>[n.id,n]));const byName=new Map(all.map(n=>[n.label.toLocaleLowerCase(),n]));let current=byId.get(select.value)||all[0];
function element(tag,attrs){let e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e}
function focus(id){const found=byId.get(id);if(!found)return;current=found;select.value=id;const next=new URL(location.href);next.searchParams.set('term',id);history.replaceState(null,'',next.pathname+next.search);render()}
function render(){svg.replaceChildren();tip.style.display='none';const width=embedded?Math.max(220,svg.clientWidth):900,height=embedded?Math.max(220,svg.clientHeight):520;if(embedded)svg.setAttribute('viewBox',`0 0 ${width} ${height}`);const names=[...new Set([...current.related,...current.collocations,...all.filter(n=>n.related.some(r=>r.toLocaleLowerCase()===current.label.toLocaleLowerCase())).map(n=>n.label)])].filter(n=>n.toLocaleLowerCase()!==current.label.toLocaleLowerCase()).slice(0,embedded?10:18);const count=names.length;const center={x:width/2,y:height/2};let group=element('g',{});svg.appendChild(group);
const points=names.map((label,i)=>{let angle=2*Math.PI*i/Math.max(1,count)-Math.PI/2;const radiusX=embedded?Math.max(70,width*.30):195,radiusY=embedded?Math.max(75,height*.30):190;return{x:center.x+radiusX*Math.cos(angle),y:center.y+radiusY*Math.sin(angle),label,term:byName.get(label.toLocaleLowerCase())}});
for(const p of points){let line=element('line',{x1:center.x,y1:center.y,x2:p.x,y2:p.y,class:'network-link'});group.appendChild(line)}
function node(label,term,x,y,isCenter){let g=element('g',{class:'network-node'}),circle=element('circle',{cx:x,cy:y,r:isCenter?19:10,class:isCenter?'center':''}),text=element('text',{x:x,y:y+(isCenter?-28:25),'text-anchor':'middle'});text.textContent=label;g.append(circle,text);g.addEventListener('mouseenter',e=>{tip.textContent=term?.zh||'暂无中文释义';tip.style.display='block'});g.addEventListener('mousemove',e=>{let box=svg.getBoundingClientRect(),wrap=svg.parentElement.getBoundingClientRect();tip.style.left=(e.clientX-wrap.left+12)+'px';tip.style.top=(e.clientY-wrap.top+12)+'px'});g.addEventListener('mouseleave',()=>tip.style.display='none');if(term)g.addEventListener('click',()=>focus(term.id));group.appendChild(g)}
for(const p of points)node(p.label,p.term,p.x,p.y,false);node(current.label,current,center.x,center.y,true);detail.textContent=current.label+' · '+(current.zh||'暂无中文释义')+' · '+count+' 个关联节点';group.animate([{opacity:0,transform:'scale(.92)',transformOrigin:center.x+'px '+center.y+'px'},{opacity:1,transform:'scale(1)',transformOrigin:center.x+'px '+center.y+'px'}],{duration:330,easing:'ease-out'})}
select.addEventListener('change',()=>focus(select.value));if(embedded)window.addEventListener('resize',render);render()}
</script>''',items=items,nodes=nodes,request=request)

@app.route('/api/terms')
def api_terms():
    db=connect(); result=all_terms(db,book=request.args.get('book',''),domain=request.args.get('domain',''));db.close();return jsonify(result)

@app.route('/api/pronunciation/<term_id>')
def pronunciation(term_id):
    return jsonify({'term_id':term_id,'available':False,'audio_url':None,'message':'Pronunciation interface reserved for future implementation'}),501


def main():
    parser=argparse.ArgumentParser(description='Professional vocabulary demo')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('convert',help='Convert PDF/DOCX/TXT to normalized TXT');p.add_argument('file')
    p=sub.add_parser('import',help='Import an AI-generated JSON vocabulary book');p.add_argument('file')
    p=sub.add_parser('run',help='Start local learning website');p.add_argument('--port',type=int,default=5000);p.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='convert':
            path,doc_id=extract(args.file);print(f'TXT: {path}\nSOURCE_ID: {doc_id}')
        elif args.command=='import':
            book_id,count=import_book(args.file);print(f'Imported {count} terms; book ID: {book_id}')
        else:
            connect().close()
            if not args.no_browser: Timer(1.0,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}/')).start()
            app.run(host='127.0.0.1',port=args.port,debug=False)
    except (ValueError,FileNotFoundError,ImportError,json.JSONDecodeError) as exc:
        parser.exit(1,f'Error: {exc}\n')

if __name__=='__main__': main()
