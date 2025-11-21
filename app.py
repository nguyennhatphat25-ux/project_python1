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
import requests  # Cần cài đặt: pip install requests
import uuid

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'

# ==============================
# Cấu hình (Configuration)
# ==============================
MYSQL_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': '',  # Điền mật khẩu MySQL của bạn nếu có
    'db': 'expense_splitter',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

MOMO_CONFIG = {
    'partnerCode': 'MOMOBKUN20180529',
    'accessKey': 'klm05TvNBzhg7h7j',
    'secretKey': 'at67qH6mk8w5Y1nAyMoYKMWACiEi2bsa',
    'endpoint': 'https://test-payment.momo.vn/v2/gateway/api/create',
    'redirectUrl': 'http://localhost:5000/momo-callback',
    'ipnUrl': 'http://localhost:5000/momo-ipn'
}

# ==============================
# Hàm hỗ trợ (Helpers)
# ==============================
def get_db():
    return pymysql.connect(**MYSQL_CONFIG)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Vui lòng đăng nhập!', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def calculate_settlements(balances):
    debtors = []
    creditors = []
    
    for person, amount in balances.items():
        if amount < -1: 
            debtors.append({'person': person, 'amount': amount})
        elif amount > 1: 
            creditors.append({'person': person, 'amount': amount})
            
    debtors.sort(key=lambda x: x['amount'])
    creditors.sort(key=lambda x: x['amount'], reverse=True)
    
    settlements = []
    i = 0
    j = 0
    
    while i < len(debtors) and j < len(creditors):
        debtor = debtors[i]
        creditor = creditors[j]
        
        amount = min(abs(debtor['amount']), creditor['amount'])
        
        settlements.append({
            'from_user': debtor['person'],
            'to_user': creditor['person'],
            'amount': amount
        })
        
        debtor['amount'] += amount
        creditor['amount'] -= amount
        
        if abs(debtor['amount']) < 1: i += 1
        if creditor['amount'] < 1: j += 1
        
    return settlements

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
        if cur.fetchone():
            flash('Email đã được đăng ký!', 'error')
            return redirect(url_for('register'))
            
        hashed = hashlib.sha256(password.encode()).hexdigest()
        cur.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)", (name, email, hashed))
        conn.commit()
        cur.close()
        conn.close()
        flash('Đăng ký thành công!', 'success')
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
            flash('Sai email hoặc mật khẩu!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Đã đăng xuất!', 'success')
    return redirect(url_for('login'))

# ==============================
# Main Application Routes
# ==============================
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    cur = conn.cursor()
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
    members = [m.strip() for m in members_raw.split(',') if m.strip()]

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO `groups` (name, currency, created_by) VALUES (%s, %s, %s)",
                    (name, currency, session['user_id']))
        group_id = cur.lastrowid

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

@app.route('/group/<int:group_id>')
@login_required
def group_detail(group_id):
    conn = get_db()
    cur = conn.cursor()

    # Lấy thông tin nhóm
    cur.execute("SELECT * FROM `groups` WHERE id = %s", (group_id,))
    group = cur.fetchone()
    if not group:
        flash("Nhóm không tồn tại!", "error")
        return redirect(url_for('dashboard'))

    # Lấy thành viên
    cur.execute("SELECT * FROM group_members WHERE group_id = %s", (group_id,))
    members = cur.fetchall()

    # Lấy chi tiêu kèm tên người được chia
    cur.execute("""
        SELECT e.*, GROUP_CONCAT(es.member_name SEPARATOR ', ') as split_members
        FROM expenses e
        LEFT JOIN expense_splits es ON e.id = es.expense_id
        WHERE e.group_id = %s
        GROUP BY e.id
        ORDER BY e.date DESC
    """, (group_id,))
    expenses = cur.fetchall()

    # Tính toán số dư
    balances = {m['name']: 0.0 for m in members}
    
    for exp in expenses:
        amount = float(exp['amount'])
        paid_by = exp['paid_by']
        
        cur.execute("SELECT member_name FROM expense_splits WHERE expense_id = %s", (exp['id'],))
        splits = cur.fetchall()
        
        if splits:
            per_person = amount / len(splits)
            if paid_by in balances:
                balances[paid_by] += amount
            for s in splits:
                m_name = s['member_name']
                if m_name in balances:
                    balances[m_name] -= per_person

    # Làm tròn và tính toán thanh toán
    for k in balances:
        balances[k] = round(balances[k], 0)

    settlements = calculate_settlements(balances)

    cur.close()
    conn.close()

    return render_template("group_detail.jinja2",
                           group=group,
                           members=members,
                           balances=balances,
                           expenses=expenses,
                           settlements=settlements)

@app.route('/expense/create', methods=['POST'])
@login_required
def create_expense():
    group_id = request.form['group_id']
    description = request.form['description']
    amount = float(request.form['amount'])
    category = request.form['category']
    paid_by = request.form['paid_by']
    split_with = request.form.getlist('split_with[]') 

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO expenses (group_id, description, amount, category, paid_by, date)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (group_id, description, amount, category, paid_by, datetime.now()))
        expense_id = cur.lastrowid

        for member in split_with:
            cur.execute("INSERT INTO expense_splits (expense_id, member_name) VALUES (%s, %s)",
                        (expense_id, member))
        
        conn.commit()
        flash("Thêm chi tiêu thành công!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Lỗi: {str(e)}", "error")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/export/<int:group_id>')
@login_required
def export_excel(group_id):
    # Bạn có thể dùng lại logic export từ code cũ nếu cần
    # Hiện tại redirect về trang chi tiết để tránh lỗi nếu chưa cần tính năng này ngay
    flash("Tính năng đang cập nhật", "info")
    return redirect(url_for('group_detail', group_id=group_id))

# ==============================
# MoMo Payment Logic (Updated)
# ==============================
@app.route('/momo/create-payment', methods=['POST'])
@login_required
def create_momo_payment():
    try:
        data = request.json
        amount = str(int(data['amount'])) 
        
        # Tạo mã đơn hàng ngẫu nhiên
        order_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        order_info = f"Thanh toan cho {data['to']}"
        
        # Tạo chữ ký (Signature)
        raw_signature = (
            f"accessKey={MOMO_CONFIG['accessKey']}"
            f"&amount={amount}"
            f"&extraData="
            f"&ipnUrl={MOMO_CONFIG['ipnUrl']}"
            f"&orderId={order_id}"
            f"&orderInfo={order_info}"
            f"&partnerCode={MOMO_CONFIG['partnerCode']}"
            f"&redirectUrl={MOMO_CONFIG['redirectUrl']}"
            f"&requestId={request_id}"
            f"&requestType=captureWallet"
        )
        
        signature = hmac.new(
            MOMO_CONFIG['secretKey'].encode(),
            raw_signature.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # Payload gửi sang MoMo
        payload = {
            'partnerCode': MOMO_CONFIG['partnerCode'],
            'partnerName': "Expense Splitter",
            'storeId': "MomoTestStore",
            'requestId': request_id,
            'amount': amount,
            'orderId': order_id,
            'orderInfo': order_info,
            'redirectUrl': MOMO_CONFIG['redirectUrl'],
            'ipnUrl': MOMO_CONFIG['ipnUrl'],
            'lang': 'vi',
            'extraData': "",
            'requestType': "captureWallet",
            'signature': signature
        }
        
        # Gửi request sang server MoMo
        response = requests.post(MOMO_CONFIG['endpoint'], json=payload)
        return jsonify(response.json())
        
    except Exception as e:
        print(e)
        return jsonify({'errorCode': -1, 'message': str(e)})

@app.route('/momo-callback')
def momo_callback():
    resultCode = request.args.get("resultCode")
    if resultCode == "0":
        flash("Thanh toán thành công!", "success")
    else:
        flash("Thanh toán thất bại hoặc bị hủy!", "error")
    return redirect(url_for("dashboard"))

@app.route('/momo-ipn', methods=['POST'])
def momo_ipn():
    return jsonify({"message": "success"})

if __name__ == '__main__':
    app.run(debug=True)