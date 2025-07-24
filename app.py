import os
import sqlite3
import openpyxl
import click
import re
import math
from markdown import markdown
from docxtpl import DocxTemplate
from flask import Flask, render_template, request, redirect, url_for, g, send_file, send_from_directory, session, flash
from datetime import date, datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from collections import defaultdict
from openpyxl.styles import Alignment

# --- 기본 설정 ---
DATA_DIR = 'data'
DATABASE = os.path.join(DATA_DIR, 'database.db')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
MAINTENANCE_FOLDER = os.path.join(DATA_DIR, 'maintenance_uploads')
LAST_RESET_FILE = os.path.join(DATA_DIR, 'last_reset.txt')
ALLOWED_EXTENSIONS = {'xlsx', 'pdf', 'docx'}

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a-very-secret-key-that-you-should-change'

# --- 데이터베이스 관련 함수 ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        position TEXT,
        contact TEXT,
        location TEXT,
        last_updated DATE
    )
    ''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, doc_type TEXT NOT NULL, filename TEXT, title TEXT NOT NULL, created_by_user TEXT NOT NULL, created_at DATE NOT NULL, status TEXT NOT NULL DEFAULT 'open', task_name TEXT, position TEXT, contact TEXT, work_time TEXT, work_location TEXT, work_details TEXT)''')
    cursor.execute('CREATE TABLE IF NOT EXISTS location_history (id INTEGER PRIMARY KEY, username TEXT NOT NULL, location TEXT, timestamp DATETIME NOT NULL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS wiki_pages (id INTEGER PRIMARY KEY, title TEXT UNIQUE NOT NULL, content TEXT, group_id INTEGER, last_edited_by TEXT, last_edited_at DATETIME, FOREIGN KEY (group_id) REFERENCES wiki_groups (id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS wiki_groups (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, parent_id INTEGER, FOREIGN KEY (parent_id) REFERENCES wiki_groups (id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS maintenance_files (id INTEGER PRIMARY KEY, filename TEXT NOT NULL, description TEXT, uploaded_by TEXT NOT NULL, uploaded_at DATETIME NOT NULL)')
    db.commit()

@app.cli.command('init-db')
@click.command('init-db')
def init_db_command():
    with app.app_context():
        init_db()
    click.echo('Initialized the database.')

# --- 헬퍼 함수 ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_id') != 1:
            flash("관리자만 접근할 수 있는 페이지입니다."); return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def daily_location_reset():
    try:
        # 수정된 경로 변수 사용
        with open(LAST_RESET_FILE, 'r') as f: last_reset_date = date.fromisoformat(f.read().strip())
    except (FileNotFoundError, ValueError):
        last_reset_date = date.min
    today = date.today()
    now = datetime.now()
    if now.hour >= 9 and last_reset_date < today:
        with app.app_context():
            db = get_db()
            today_str = today.strftime('%Y-%m-%d')
            db.execute("UPDATE users SET location = '사무실' WHERE last_updated < ?", (today_str,)); db.commit()
            # 수정된 경로 변수 사용
            with open(LAST_RESET_FILE, 'w') as f: f.write(today_str)
            print(f"[{today_str}] Daily location reset has been performed.")

def process_wiki_links(content):
    def replace_link(match):
        title = match.group(1)
        url = url_for('wiki_view', title=title)
        return f'<a href="{url}">{title}</a>'
    return re.sub(r'\[\[(.*?)\]\]', replace_link, content)

def get_wiki_data_structured():
    db = get_db()
    groups = db.execute("SELECT * FROM wiki_groups ORDER BY name").fetchall()
    pages = db.execute("SELECT title, group_id FROM wiki_pages ORDER BY title").fetchall()
    groups_by_id = {g['id']: dict(g, children=[], pages=[]) for g in groups}
    for page in pages:
        if page['group_id'] is not None and page['group_id'] in groups_by_id:
            groups_by_id[page['group_id']]['pages'].append(dict(page))
    structured_groups = []
    for g_data in groups_by_id.values():
        parent_id = g_data.get('parent_id')
        if parent_id in groups_by_id:
            groups_by_id[parent_id]['children'].append(g_data)
        elif parent_id is None:
            structured_groups.append(g_data)
    pages_no_group = [p for p in pages if p['group_id'] is None]
    return groups, structured_groups, pages_no_group

# --- 라우트 (페이지) 정의 ---

# --- 인증 라우트 ---
@app.route('/register', methods=('GET', 'POST'))
def register():
    if request.method == 'POST':
        name = request.form['name']
        username = request.form['username']
        password = request.form['password']
        password2 = request.form['password2']
        position = request.form.get('position', '') # 직급 정보 가져오기
        contact = request.form.get('contact', '')   # 연락처 정보 가져오기
        
        db = get_db()
        error = None

        if not name or not username or not password:
            error = '이름, 아이디, 비밀번호는 필수 항목입니다.'
        elif password != password2:
            error = '비밀번호가 일치하지 않습니다.'
        
        if error is None:
            try:
                # DB에 직급, 연락처 정보 추가
                db.execute(
                    "INSERT INTO users (name, username, password, position, contact, location, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, username, generate_password_hash(password), position, contact, '사무실', '2000-01-01'),
                )
                db.commit()
                flash("회원가입이 완료되었습니다. 로그인해주세요.")
                return redirect(url_for("login"))
            except db.IntegrityError:
                error = f"아이디 '{username}'는 이미 사용 중입니다."
        flash(error)
    return render_template('register.html')


@app.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        if user is None or not check_password_hash(user['password'], password):
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
        else:
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['name'] = user['name']
            session['position'] = user['position'] # 세션에 직급 저장
            session['contact'] = user['contact']   # 세션에 연락처 저장
            return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

# --- 메인 및 사용자 위치 라우트 ---
@app.route('/')
@login_required
def index():
    daily_location_reset()
    db = get_db()
    users_data = db.execute("SELECT id, name, username, location, last_updated FROM users ORDER BY username").fetchall()
    today_str = date.today().strftime('%Y-%m-%d')
    user_status_list = [{'name': u['name'], 'username': u['username'], 'location': u['location'] if u['last_updated'] == today_str else '사무실'} for u in users_data]
    
    plan_docs = db.execute("SELECT * FROM documents WHERE doc_type = '작업계획서' ORDER BY created_at DESC, id DESC LIMIT 5").fetchall()
    report_docs = db.execute("SELECT * FROM documents WHERE doc_type = '작업완료보고서' ORDER BY created_at DESC, id DESC LIMIT 5").fetchall()
            
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M')
    return render_template('index.html', users=user_status_list, plan_docs=plan_docs, report_docs=report_docs, current_time=current_time)

@app.route('/update_location', methods=['POST'])
@login_required
def update_location():
    db = get_db()
    db.execute("UPDATE users SET location = ?, last_updated = ? WHERE username = ?", (request.form['new_location'], date.today().strftime('%Y-%m-%d'), session['username']))
    db.execute("INSERT INTO location_history (username, location, timestamp) VALUES (?, ?, ?)", (session['username'], request.form['new_location'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    db.commit()
    return redirect(url_for('index'))

@app.route('/history/<username>')
@login_required
def history(username):
    db = get_db(); filter_period = request.args.get('filter', 'all')
    query = "SELECT location, timestamp FROM location_history WHERE username = ? "; params = [username]
    if filter_period == 'weekly': query += "AND timestamp >= ? "; params.append(datetime.now() - timedelta(days=7))
    elif filter_period == 'monthly': query += "AND timestamp >= ? "; params.append(datetime.now() - timedelta(days=30))
    query += "ORDER BY timestamp DESC"
    records = db.execute(query, tuple(params)).fetchall()
    user = db.execute("SELECT name, username FROM users WHERE username = ?", (username,)).fetchone()
    if user is None: flash(f"'{username}' 사용자를 찾을 수 없습니다."); return redirect(url_for('index'))
    return render_template('history.html', user=user, records=records, current_filter=filter_period)

# --- 문서 관련 라우트 ---
@app.route('/documents/<doc_type>')
@login_required
def documents(doc_type):
    if doc_type not in ['plan', 'report']: return "잘못된 접근입니다.", 404
    db = get_db(); page = request.args.get('page', 1, type=int); PER_PAGE = 10; offset = (page - 1) * PER_PAGE
    selected_year = request.args.get('year', ''); selected_month = request.args.get('month', ''); selected_user = request.args.get('user', 'all')
    doc_type_str = "작업계획서" if doc_type == 'plan' else "작업완료보고서"
    
    title = f"{doc_type_str} 전체 목록"
    filter_desc = []
    if selected_year: filter_desc.append(f"{selected_year}년")
    if selected_month: filter_desc.append(f"{selected_month}월")
    if selected_user != 'all': filter_desc.append(f"작성자: {selected_user}")
    if filter_desc: title = f"{' / '.join(filter_desc)} - {doc_type_str} 목록"

    conditions = ["doc_type = ?"]; params = [doc_type_str]
    if selected_year: conditions.append("STRFTIME('%Y', created_at) = ?"); params.append(selected_year)
    if selected_month: conditions.append("STRFTIME('%m', created_at) = ?"); params.append(f"{int(selected_month):02d}")
    if selected_user != 'all': conditions.append("created_by_user = ?"); params.append(selected_user)
    where_clause = " WHERE " + " AND ".join(conditions)
    
    count_query = "SELECT COUNT(id) FROM documents" + where_clause
    total_count = db.execute(count_query, tuple(params)).fetchone()[0]
    total_pages = math.ceil(total_count / PER_PAGE)
    
    data_query = "SELECT * FROM documents" + where_clause + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    data_params = tuple(params + [PER_PAGE, offset])
    docs = db.execute(data_query, data_params).fetchall()
    
    years = db.execute("SELECT DISTINCT STRFTIME('%Y', created_at) as year FROM documents ORDER BY year DESC").fetchall()
    all_users = db.execute("SELECT name FROM users ORDER BY name").fetchall()
    args_for_pagination = request.args.copy(); args_for_pagination.pop('page', None)
    
    return render_template('document_list.html', docs=docs, doc_type=doc_type, title=title, years=years, all_users=all_users,
                           selected_year=selected_year, selected_month=selected_month, selected_user=selected_user, page=page, total_pages=total_pages, args_for_pagination=args_for_pagination)

@app.route('/select_plan_for_report')
@login_required
def select_plan_for_report():
    db = get_db()
    plan_docs = db.execute("SELECT id, title, created_at FROM documents WHERE doc_type = '작업계획서' AND created_by_user = ? AND status = 'open' ORDER BY created_at DESC", (session['name'],)).fetchall()
    return render_template('select_plan.html', plan_docs=plan_docs)

@app.route('/create/<doc_type>')
@login_required
def create(doc_type):
    if doc_type not in ['plan', 'report']: return "잘못된 접근입니다.", 404
    
    doc_data = None
    from_plan_id = request.args.get('from_plan_id')
    if doc_type == 'report' and from_plan_id:
        db = get_db()
        doc_to_import = db.execute('SELECT * FROM documents WHERE id = ?', (from_plan_id,)).fetchone()
        if doc_to_import:
            doc_data = dict(doc_to_import)

    today = date.today().strftime('%Y-%m-%d')
    return render_template('create.html', today=today, doc_type=doc_type, doc=doc_data)

@app.route('/submit', methods=['POST'])
@login_required
def submit():
    db = get_db()
    try:
        doc_id = request.form.get('doc_id')
        from_plan_id = request.form.get('from_plan_id')
        doc_type = request.form.get('doc_type')
        
        form_data = {
            'title': request.form.get('title', '').strip(),
            'task_name': request.form.get('task_name', '').strip(),
            'position': request.form.get('position', '').strip(),
            'contact': request.form.get('contact', '').strip(),
            'work_date': request.form.get('work_date', '').strip(),
            'work_time': request.form.get('work_time', '').strip(),
            'work_location': request.form.get('work_location', '').strip(),
            'work_details': request.form.get('work_details', '').strip(),
            'worker_name': session.get('name')
        }
        
        doc_type_str = "작업계획서" if doc_type == 'plan' else "작업완료보고서"
        
        final_doc_id = None
        if doc_id:
            final_doc_id = doc_id
            db.execute('''UPDATE documents SET 
                          title=?, created_at=?, task_name=?, position=?, contact=?, work_time=?, work_location=?, work_details=?
                          WHERE id=?''',
                       (form_data['title'], form_data['work_date'], form_data['task_name'], form_data['position'], form_data['contact'],
                        form_data['work_time'], form_data['work_location'], form_data['work_details'], doc_id))
        else:
             cursor = db.execute('''INSERT INTO documents 
                           (doc_type, title, created_by_user, created_at, task_name, position, contact, work_time, work_location, work_details, filename) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                       (doc_type_str, form_data['title'], session['name'], form_data['work_date'], form_data['task_name'], form_data['position'],
                        form_data['contact'], form_data['work_time'], form_data['work_location'], form_data['work_details'], "placeholder"))
             final_doc_id = cursor.lastrowid
             
             if doc_type == 'report' and from_plan_id:
                 # ▼▼ from_plan_id를 정수(int)로 변환하여 오류 해결 ▼▼
                 db.execute("UPDATE documents SET status = 'closed' WHERE id = ?", (int(from_plan_id),))

        template_filename = f"{doc_type_str}_양식.docx"
        safe_title = re.sub(r'[^\w\s-]', '', form_data['title']).strip() or "제목없음"
        safe_location = re.sub(r'[^\w\s-]', '', form_data['work_location']).strip() or "위치없음"
        filename = f"{form_data['work_date']}_{doc_type_str}_{final_doc_id}_{safe_title}_{session['name']}.docx"
        
        db.execute("UPDATE documents SET filename = ? WHERE id = ?", (filename, final_doc_id))

        doc = DocxTemplate(template_filename)
        doc.render(form_data)
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        doc.save(save_path)
        
        db.commit()
        
        flash("문서가 성공적으로 저장되었습니다.")
        return redirect(url_for('documents', doc_type=doc_type))
        
    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        flash(f"저장 중 심각한 오류가 발생했습니다: {e}")
        return redirect(url_for('index'))
    
    

@app.route('/documents/edit/<int:doc_id>')
@login_required
def edit_document(doc_id):
    db = get_db()
    doc = db.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()

    if doc is None:
        flash("존재하지 않는 문서입니다."); return redirect(url_for('index'))
    if doc['created_by_user'] != session['name'] and session['user_id'] != 1:
        flash("문서를 수정할 권한이 없습니다."); return redirect(url_for('index'))
    
    doc_type = 'plan' if doc['doc_type'] == '작업계획서' else 'report'
    return render_template('create.html', doc_type=doc_type, doc=dict(doc), today=doc['created_at'])


@app.route('/documents/delete/<int:doc_id>', methods=['POST'])
@login_required
def delete_document(doc_id):
    db = get_db()
    doc = db.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()
    
    if doc is None:
        flash("존재하지 않는 문서입니다."); return redirect(url_for('index'))
    if doc['created_by_user'] != session['name'] and session['user_id'] != 1:
        flash("문서를 삭제할 권한이 없습니다."); return redirect(url_for('index'))

    try:
        # 수정된 경로 변수 UPLOAD_FOLDER 사용
        os.remove(os.path.join(UPLOAD_FOLDER, doc['filename']))
        db.execute('DELETE FROM documents WHERE id = ?', (doc_id,))
        db.commit()
        flash("문서가 성공적으로 삭제되었습니다.")
    except Exception as e:
        flash(f"삭제 중 오류가 발생했습니다: {e}")
        
    return redirect(url_for('documents', doc_type='plan'))

@app.route('/view/<filename>')
@login_required
def view(filename):
    db = get_db()
    doc_from_db = db.execute('SELECT * FROM documents WHERE filename = ?', (filename,)).fetchone()

    if doc_from_db is None:
        flash("데이터베이스에서 해당 문서를 찾을 수 없습니다.")
        return redirect(url_for('index'))
        
    return render_template('view.html', data=doc_from_db)

@app.route('/download/<filename>')
@login_required
def download(filename):
    # 수정된 경로 변수 UPLOAD_FOLDER 사용
    return send_file(os.path.join(UPLOAD_FOLDER, filename), as_attachment=True)

# --- 유지보수 내역 관련 라우트 ---
@app.route('/maintenance')
@login_required
def maintenance():
    """DB에서 유지보수 파일 목록을 페이지네이션으로 가져와 보여줍니다."""
    db = get_db()
    
    page = request.args.get('page', 1, type=int)
    PER_PAGE = 10 # 한 페이지에 10개씩 표시
    offset = (page - 1) * PER_PAGE
    
    # 전체 파일 수 계산
    total_count = db.execute("SELECT COUNT(id) FROM maintenance_files").fetchone()[0]
    total_pages = math.ceil(total_count / PER_PAGE)
    
    # 현재 페이지에 해당하는 파일만 가져오기
    files = db.execute("SELECT * FROM maintenance_files ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
                       (PER_PAGE, offset)).fetchall()
    
    return render_template('maintenance.html', 
                           files=files,
                           page=page,
                           total_pages=total_pages)

@app.route('/upload_maintenance', methods=['POST'])
@login_required
def upload_maintenance():
    if 'file' not in request.files or request.files['file'].filename == '':
        flash("업로드할 파일을 선택해주세요."); return redirect(url_for('maintenance'))
    file = request.files['file']; description = request.form.get('description', '')
    if file and allowed_file(file.filename):
        filename = file.filename
        file.save(os.path.join(MAINTENANCE_FOLDER, filename))
        db = get_db()
        db.execute("INSERT INTO maintenance_files (filename, description, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?)",
                   (filename, description, session['name'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        db.commit()
        flash(f"'{filename}' 파일이 성공적으로 업로드되었습니다.")
    else: flash("허용되지 않는 파일 형식입니다. (xlsx, pdf만 가능)")
    return redirect(url_for('maintenance'))

@app.route('/view_maintenance/<filename>')
@login_required
def view_maintenance(filename):
    return send_from_directory(MAINTENANCE_FOLDER, filename)

@app.route('/delete_maintenance/<int:file_id>', methods=['POST'])
@login_required
def delete_maintenance(file_id):
    db = get_db(); file_to_delete = db.execute("SELECT filename FROM maintenance_files WHERE id = ?", (file_id,)).fetchone()
    if file_to_delete:
        try:
            os.remove(os.path.join(MAINTENANCE_FOLDER, file_to_delete['filename']))
            db.execute("DELETE FROM maintenance_files WHERE id = ?", (file_id,)); db.commit()
            flash("파일이 성공적으로 삭제되었습니다.")
        except OSError as e: flash(f"파일 삭제 중 오류 발생: {e}")
    else: flash("삭제할 파일을 찾을 수 없습니다.")
    return redirect(url_for('maintenance'))

# --- 관리자 기능 라우트 ---
@app.route('/admin')
@login_required
@admin_required
def admin():
    db = get_db(); users = db.execute("SELECT id, name, username FROM users ORDER BY id").fetchall()
    return render_template('admin.html', users=users)

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    if user_id == 1: flash("관리자 계정은 삭제할 수 없습니다.")
    else:
        db = get_db(); db.execute("DELETE FROM users WHERE id = ?", (user_id,)); db.commit()
        flash("사용자가 성공적으로 삭제되었습니다.")
    return redirect(url_for('admin'))

@app.route('/admin/change_password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def change_password(user_id):
    new_password = request.form['new_password']
    if not new_password: flash("새로운 비밀번호를 입력해주세요.")
    else:
        db = get_db(); db.execute("UPDATE users SET password = ? WHERE id = ?", (generate_password_hash(new_password), user_id)); db.commit()
        flash("비밀번호가 성공적으로 변경되었습니다.")
    return redirect(url_for('admin'))

# --- 위키 관련 라우트 ---
@app.route('/wiki')
@login_required
def wiki_index():
    groups, structured_groups, pages_no_group = get_wiki_data_structured()
    return render_template('wiki_view.html', all_groups_flat=groups, structured_groups=structured_groups, pages_no_group=pages_no_group)

@app.route('/wiki/create', methods=['POST'])
@login_required
def wiki_create():
    title = request.form['title']
    if not title:
        flash("문서 제목을 입력해주세요."); return redirect(url_for('wiki_index'))
    return redirect(url_for('wiki_edit', title=title))

@app.route('/wiki/view/<title>')
@login_required
def wiki_view(title):
    db = get_db(); page = db.execute("SELECT * FROM wiki_pages WHERE title = ?", (title,)).fetchone()
    groups, structured_pages, pages_no_group = get_wiki_data_structured()
    if page:
        html_content = markdown(page['content'], extensions=['fenced_code', 'tables'])
        processed_content = process_wiki_links(html_content)
        return render_template('wiki_view.html', page=page, content=processed_content, all_groups_flat=groups, structured_groups=structured_pages, pages_no_group=pages_no_group)
    else: return redirect(url_for('wiki_edit', title=title))

@app.route('/wiki/edit/<title>', methods=['GET', 'POST'])
@login_required
def wiki_edit(title):
    db = get_db()
    if request.method == 'POST':
        content = request.form['content']; group_id = request.form.get('group_id')
        page = db.execute("SELECT id FROM wiki_pages WHERE title = ?", (title,)).fetchone()
        current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        final_group_id = group_id if group_id and group_id != 'none' else None
        if page:
            db.execute("UPDATE wiki_pages SET content=?, group_id=?, last_edited_by=?, last_edited_at=? WHERE title=?", (content, final_group_id, session['name'], current_time_str, title))
        else:
            db.execute("INSERT INTO wiki_pages (title, content, group_id, last_edited_by, last_edited_at) VALUES (?, ?, ?, ?, ?)", (title, content, final_group_id, session['name'], current_time_str))
        db.commit(); return redirect(url_for('wiki_view', title=title))
    
    all_groups_flat, structured_groups, pages_no_group = get_wiki_data_structured()
    page = db.execute("SELECT * FROM wiki_pages WHERE title = ?", (title,)).fetchone()
    content = page['content'] if page else f"# {title}\n\n이 페이지는 아직 작성되지 않았습니다."
    return render_template('wiki_edit.html', title=title, content=content, page=page, all_groups_flat=all_groups_flat, structured_groups=structured_groups, pages_no_group=pages_no_group)

@app.route('/wiki/groups/create', methods=['POST'])
@login_required
def create_group():
    group_name = request.form['group_name']; parent_id = request.form.get('parent_id')
    if group_name:
        db = get_db()
        try:
            db.execute("INSERT INTO wiki_groups (name, parent_id) VALUES (?, ?)", (group_name, parent_id if parent_id != 'none' else None)); db.commit()
            flash(f"'{group_name}' 그룹이 생성되었습니다.")
        except db.IntegrityError: flash(f"'{group_name}' 그룹은 이미 존재합니다.")
    return redirect(url_for('wiki_index'))

@app.route('/wiki/groups/delete/<int:group_id>', methods=['POST'])
@login_required
def delete_group(group_id):
    db = get_db()
    db.execute("UPDATE wiki_pages SET group_id = NULL WHERE group_id = ?", (group_id,))
    db.execute("UPDATE wiki_groups SET parent_id = NULL WHERE parent_id = ?", (group_id,))
    db.execute("DELETE FROM wiki_groups WHERE id = ?", (group_id,)); db.commit()
    flash("그룹이 삭제되었습니다."); return redirect(url_for('wiki_index'))

@app.route('/wiki/groups/edit/<int:group_id>')
@login_required
@admin_required
def edit_group(group_id):
    db = get_db()
    group = db.execute("SELECT * FROM wiki_groups WHERE id = ?", (group_id,)).fetchone()
    if group is None: flash("존재하지 않는 그룹입니다."); return redirect(url_for('wiki_index'))
    return render_template('wiki_group_edit.html', group=group)

@app.route('/wiki/search')
@login_required
def wiki_search():
    query = request.args.get('query', ''); page = request.args.get('page', 1, type=int)
    PER_PAGE = 10; offset = (page - 1) * PER_PAGE; db = get_db()
    search_term = f"%{query}%"
    count_query = "SELECT COUNT(id) FROM wiki_pages WHERE title LIKE ? OR content LIKE ?"
    total_count = db.execute(count_query, (search_term, search_term)).fetchone()[0]
    total_pages = math.ceil(total_count / PER_PAGE)
    results_query = "SELECT title, content, last_edited_at FROM wiki_pages WHERE title LIKE ? OR content LIKE ? ORDER BY last_edited_at DESC LIMIT ? OFFSET ?"
    results = db.execute(results_query, (search_term, search_term, PER_PAGE, offset)).fetchall()
    return render_template('wiki_search_results.html', results=results, query=query, page=page, total_pages=total_pages)

@app.route('/wiki/delete/<title>', methods=['POST'])
@login_required
def wiki_delete(title):
    db = get_db(); db.execute("DELETE FROM wiki_pages WHERE title = ?", (title,)); db.commit()
    flash(f"'{title}' 문서가 삭제되었습니다."); return redirect(url_for('wiki_index'))