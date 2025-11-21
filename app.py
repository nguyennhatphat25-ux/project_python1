#
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
import pymysql
import hashlib
import hmac
import json
from datetime import datetime
import io
import xlsxwriter
from functools import wraps

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'

# MySQL Configuration
MYSQL_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '',  # Điền mật khẩu DB của bạn vào đây
    'db': 'expense_splitter',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

# MoMo Configuration (Giữ nguyên)
MOMO_CONFIG = {
    'partnerCode': 'MOMOBKUN20180529',
    'accessKey': 'klm05TvNBzhg7h7j',
    'secretKey': 'at67qH6mk8w5Y1nAyMoYKMWACiEi2bsa',
    'endpoint': 'https://test-payment.momo.vn/v2/gateway/api/create',
    'redirectUrl': 'http://localhost:5000/momo-callback',
    'ipnUrl': 'http://localhost:5000/momo-ipn'
}

# ==============================
# Database helper
# ==============================
def get_db():
    return pymysql.connect(**MYSQL_CONFIG)

# ==============================
# Login Required Decorator
# ==============================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Vui lòng đăng nhập!', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ==============================
# Authentication Routes
# ==============================
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()

        if user:
            flash('Email đã được đăng ký!', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('register'))

        hashed = hashlib.sha256(password.encode()).hexdigest()
        cur.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
                    (name, email, hashed))
        conn.commit()
        cur.close()
        conn.close()

        flash('Đăng ký thành công! Vui lòng đăng nhập.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        hashed = hashlib.sha256(password.encode()).hexdigest()

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s AND password = %s", (email, hashed))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user:
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            flash('Đăng nhập thành công!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Email hoặc mật khẩu không đúng!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Đã đăng xuất!', 'success')
    return redirect(url_for('login'))

# ==============================
# Dashboard & Group Logic
# ==============================
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    cur = conn.cursor()
    # Lấy danh sách nhóm và tổng chi tiêu
    cur.execute("""
        SELECT g.*, 
               COUNT(DISTINCT gm.id) AS member_count,
               COUNT(DISTINCT e.id) AS expense_count,
               COALESCE(SUM(e.amount), 0) AS total_amount
        FROM `groups` g
        LEFT JOIN group_members gm ON g.id = gm.group_id
        LEFT JOIN expenses e ON g.id = e.group_id
        WHERE g.created_by = %s
        GROUP BY g.id
        ORDER BY g.created_at DESC
    """, (session['user_id'],))
    groups = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('dashboard.jinja2', groups=groups)

@app.route('/group/create', methods=['POST'])
@login_required
def create_group():
    name = request.form['name']
    currency = request.form['currency']
    members_raw = request.form.get('members', '')
    # Tách tên thành viên từ chuỗi nhập vào (ngăn cách bởi dấu phẩy)
    members = [m.strip() for m in members_raw.split(',') if m.strip()]

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO `groups` (name, currency, created_by) VALUES (%s, %s, %s)",
                    (name, currency, session['user_id']))
        group_id = cur.lastrowid

        # Thêm người tạo là thành viên đầu tiên
        cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", 
                    (group_id, session['user_name']))

        for m in members:
            cur.execute("INSERT INTO group_members (group_id, name) VALUES (%s, %s)", (group_id, m))
        
        conn.commit()
        flash('Tạo nhóm thành công!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Lỗi: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('group_detail', group_id=group_id))

# --- FIX QUAN TRỌNG: Viết lại group_detail dùng SQL thuần ---
@app.route('/group/<int:group_id>')
@login_required
def group_detail(group_id):
    conn = get_db()
    cur = conn.cursor()

    # 1. Lấy thông tin nhóm
    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    group = cur.fetchone()
    if not group:
        flash("Nhóm không tồn tại", "error")
        return redirect(url_for('dashboard'))

    # 2. Lấy danh sách thành viên
    cur.execute("SELECT * FROM group_members WHERE group_id = %s", (group_id,))
    members = cur.fetchall() # List dict [{'id': 1, 'name': 'A'}]

    # 3. Lấy danh sách chi tiêu
    cur.execute("SELECT * FROM expenses WHERE group_id = %s ORDER BY date DESC", (group_id,))
    expenses = cur.fetchall()

    # 4. TÍNH TOÁN SỐ DƯ (BALANCES)
    # Logic: Balance = (Tiền mình đã trả) - (Tiền mình phải chịu)
    balances = {m['name']: 0.0 for m in members}

    for exp in expenses:
        amount = float(exp['amount'])
        payer = exp['paid_by']

        # Lấy danh sách người chịu phí cho khoản này
        cur.execute("SELECT member_name FROM expense_splits WHERE expense_id = %s", (exp['id'],))
        splits = cur.fetchall()
        split_names = [s['member_name'] for s in splits]

        if not split_names:
            continue
            
        per_person = amount / len(split_names)

        # Cộng tiền cho người trả (Vì họ đã trả giúp)
        if payer in balances:
            balances[payer] += amount
        
        # Trừ tiền những người thụ hưởng (Vì họ nợ khoản này)
        for name in split_names:
            if name in balances:
                balances[name] -= per_person

    cur.close()
    conn.close()

    # Làm tròn số dư
    for name in balances:
        balances[name] = round(balances[name], 2)

    return render_template("group_detail.jinja2",
                           group=group,
                           members=members,
                           balances=balances,
                           expenses=expenses)

@app.route('/expense/create', methods=['POST'])
@login_required
def create_expense():
    group_id = request.form['group_id']
    description = request.form['description']
    amount = float(request.form['amount'])
    category = request.form['category']
    paid_by = request.form['paid_by']
    # Lấy danh sách người được chia tiền (checkboxes)
    split_with = request.form.getlist('split_with') 

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO expenses (group_id, description, amount, category, paid_by, date)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (group_id, description, amount, category, paid_by, datetime.now()))
        expense_id = cur.lastrowid

        for member_name in split_with:
            cur.execute("INSERT INTO expense_splits (expense_id, member_name) VALUES (%s, %s)",
                        (expense_id, member_name))
        conn.commit()
        flash("Thêm chi tiêu thành công!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Lỗi: {str(e)}", "error")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('group_detail', group_id=group_id))

# ==============================
# Export Excel (Giữ nguyên logic SQL)
# ==============================
@app.route('/export/<int:group_id>')
@login_required
def export_excel(group_id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    group = cur.fetchone()

    # Lấy chi tiêu
    cur.execute("""
        SELECT e.*, GROUP_CONCAT(es.member_name SEPARATOR ', ') AS split_members
        FROM expenses e
        LEFT JOIN expense_splits es ON e.id = es.expense_id
        WHERE e.group_id = %s
        GROUP BY e.id
    """, (group_id,))
    expenses = cur.fetchall()

    # Tính lại balance để xuất Excel
    cur.execute("SELECT * FROM group_members WHERE group_id = %s", (group_id,))
    members = cur.fetchall()
    balances = {m["name"]: 0.0 for m in members}

    # (Copy logic tính toán từ group_detail vào đây hoặc tách hàm riêng nếu muốn chuẩn hơn)
    for exp in expenses:
        cur.execute("SELECT member_name FROM expense_splits WHERE expense_id = %s", (exp['id'],))
        splits = cur.fetchall()
        split_names = [s['member_name'] for s in splits]
        
        if split_names:
            amount = float(exp['amount'])
            per_person = amount / len(split_names)
            if exp['paid_by'] in balances:
                balances[exp['paid_by']] += amount
            for name in split_names:
                if name in balances:
                    balances[name] -= per_person

    cur.close()
    conn.close()

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output)
    
    # Sheet 1: Chi tiết
    sheet = workbook.add_worksheet("Chi tiêu")
    headers = ["Ngày", "Mô tả", "Danh mục", "Số tiền", "Người trả", "Chia cho"]
    for i, h in enumerate(headers):
        sheet.write(0, i, h)
    
    for idx, e in enumerate(expenses, start=1):
        sheet.write(idx, 0, str(e["date"]))
        sheet.write(idx, 1, e["description"])
        sheet.write(idx, 2, e["category"])
        sheet.write(idx, 3, float(e["amount"]))
        sheet.write(idx, 4, e["paid_by"])
        sheet.write(idx, 5, e.get("split_members", ""))

    # Sheet 2: Tổng kết nợ
    sheet2 = workbook.add_worksheet("Tổng kết nợ")
    sheet2.write(0, 0, "Thành viên")
    sheet2.write(0, 1, "Số dư (VND)")
    sheet2.write(0, 2, "Trạng thái")
    
    row = 1
    for name, bal in balances.items():
        sheet2.write(row, 0, name)
        sheet2.write(row, 1, bal)
        status = "Nhận lại" if bal > 0 else "Phải trả" if bal < 0 else "-"
        sheet2.write(row, 2, status)
        row += 1

    workbook.close()
    output.seek(0)

    return send_file(output,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"Bao_cao_{group['name']}.xlsx")

# ==============================
# MoMo Payment & Main
# ==============================
# ... (Giữ nguyên code MoMo của bạn) ...

if __name__ == '__main__':
    app.run(debug=True)