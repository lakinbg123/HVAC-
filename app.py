from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, send_file
from twilio.rest import Client

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'leads.db'
CSV_PATH = BASE_DIR / 'leads_export.csv'

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-in-production')
app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD', 'change-me')
app.config['BUSINESS_NAME'] = os.environ.get('BUSINESS_NAME', 'Gulf Coast Same-Day HVAC')
app.config['PHONE_DISPLAY'] = '228-365-7474'
app.config['PHONE_TEL'] = '2283657474'
app.config['SERVICE_AREA'] = 'Biloxi, Gulfport, D’Iberville & nearby Gulf Coast areas'
app.config['CITY_LIST'] = ['Biloxi', 'Gulfport', 'D\'Iberville', 'Ocean Springs', 'St. Martin']

app.config['TWILIO_ACCOUNT_SID'] = os.environ.get('TWILIO_ACCOUNT_SID', '')
app.config['TWILIO_AUTH_TOKEN'] = os.environ.get('TWILIO_AUTH_TOKEN', '')
app.config['TWILIO_PHONE_NUMBER'] = os.environ.get('TWILIO_PHONE_NUMBER', '')
app.config['ALERT_PHONE_NUMBER'] = os.environ.get('ALERT_PHONE_NUMBER', '')


def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error: Exception | None) -> None:
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.execute(
        '''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            city TEXT,
            service_type TEXT,
            urgency TEXT,
            details TEXT,
            source TEXT,
            page_url TEXT,
            status TEXT NOT NULL DEFAULT 'new'
        )
        '''
    )
    db.commit()
    db.close()


init_db()


def send_sms_alert(message: str) -> None:
    account_sid = app.config['TWILIO_ACCOUNT_SID']
    auth_token = app.config['TWILIO_AUTH_TOKEN']
    from_number = app.config['TWILIO_PHONE_NUMBER']
    to_number = app.config['ALERT_PHONE_NUMBER']

    if not all([account_sid, auth_token, from_number, to_number]):
        raise RuntimeError('Missing Twilio environment variables.')

    client = Client(account_sid, auth_token)
    client.messages.create(
        body=message,
        from_=from_number,
        to=to_number,
    )


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return view_func(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {
        'business_name': app.config['BUSINESS_NAME'],
        'phone_display': app.config['PHONE_DISPLAY'],
        'phone_tel': app.config['PHONE_TEL'],
        'service_area': app.config['SERVICE_AREA'],
        'city_list': app.config['CITY_LIST'],
        'current_year': datetime.now().year,
    }


@app.route('/')
def index():
    return render_template('index.html')


@app.post('/lead')
def create_lead():
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    city = request.form.get('city', '').strip()
    service_type = request.form.get('service_type', '').strip()
    urgency = request.form.get('urgency', '').strip()
    details = request.form.get('details', '').strip()
    honeypot = request.form.get('company', '').strip()

    if honeypot:
        abort(400)

    if not name or not phone:
        flash('Name and phone are required.', 'error')
        return redirect(url_for('index'))

    created_at = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    source = request.form.get('source', 'website')
    page_url = request.form.get('page_url', '/')

    db = get_db()
    db.execute(
        '''
        INSERT INTO leads (created_at, name, phone, city, service_type, urgency, details, source, page_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            created_at,
            name,
            phone,
            city,
            service_type,
            urgency,
            details,
            source,
            page_url,
        ),
    )
    db.commit()

    sms_body = (
        f"NEW HVAC LEAD\n"
        f"Name: {name}\n"
        f"Phone: {phone}\n"
        f"City: {city or 'N/A'}\n"
        f"Service: {service_type or 'N/A'}\n"
        f"Urgency: {urgency or 'N/A'}\n"
        f"Details: {details or 'N/A'}"
    )

    try:
        send_sms_alert(sms_body)
    except Exception as e:
        # Keep saving leads even if texting fails
        print(f"SMS send failed: {e}")

    return redirect(url_for('thank_you', name=name))


@app.route('/thank-you')
def thank_you():
    name = request.args.get('name', 'there')
    return render_template('thank_you.html', name=name)


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == app.config['ADMIN_PASSWORD']:
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Incorrect password.', 'error')
    return render_template('admin_login.html')


@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    db = get_db()
    leads = db.execute(
        'SELECT * FROM leads ORDER BY datetime(created_at) DESC, id DESC LIMIT 200'
    ).fetchall()
    return render_template('admin_dashboard.html', leads=leads)


@app.post('/admin/lead/<int:lead_id>/status')
@login_required
def update_status(lead_id: int):
    new_status = request.form.get('status', 'new')
    if new_status not in {'new', 'contacted', 'booked', 'closed'}:
        abort(400)
    db = get_db()
    db.execute('UPDATE leads SET status = ? WHERE id = ?', (new_status, lead_id))
    db.commit()
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/export.csv')
@login_required
def export_csv():
    db = get_db()
    rows = db.execute('SELECT * FROM leads ORDER BY datetime(created_at) DESC, id DESC').fetchall()
    with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'created_at', 'name', 'phone', 'city', 'service_type', 'urgency', 'details', 'source', 'page_url', 'status'])
        for row in rows:
            writer.writerow([row['id'], row['created_at'], row['name'], row['phone'], row['city'], row['service_type'], row['urgency'], row['details'], row['source'], row['page_url'], row['status']])
    return send_file(CSV_PATH, as_attachment=True, download_name='leads.csv')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


if __name__ == '__main__':
    app.run(debug=True)
