"""Run with: flask --app employer_portal.app run (see README for production)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, Response
from werkzeug.security import check_password_hash, generate_password_hash

CATEGORIES = ('Customer service', 'Human resources', 'Retail support', 'Store management',
              'Listing services', 'Marketing and advertising', 'Customer care', 'Data entry',
              'Accounting', 'Product marketing', 'Sales', 'Other')
STATUSES = ('New', 'Reviewing', 'Shortlisted', 'Interview', 'Offer', 'Hired', 'Not selected', 'Withdrawn')
MODES = ('Remote', 'Hybrid', 'On-site')
TYPES = ('Full-time', 'Part-time', 'Contract', 'Temporary')
ZONES = ('America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
         'America/Anchorage', 'Pacific/Honolulu', 'UTC')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS businesses (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password TEXT NOT NULL,
 title TEXT NOT NULL, industry TEXT NOT NULL, website TEXT NOT NULL DEFAULT '',
 location TEXT NOT NULL, description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL REFERENCES businesses(id),
 title TEXT NOT NULL, category TEXT NOT NULL, duties TEXT NOT NULL,
 requirements TEXT NOT NULL, pay_min TEXT NOT NULL, pay_max TEXT NOT NULL,
 pay_unit TEXT NOT NULL, location TEXT NOT NULL, work_mode TEXT NOT NULL,
 employment_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Draft',
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
 name TEXT NOT NULL, email TEXT NOT NULL, resume_url TEXT NOT NULL DEFAULT '',
 message TEXT NOT NULL DEFAULT '', consent_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'New', notes TEXT NOT NULL DEFAULT '',
 UNIQUE(job_id, email));
CREATE TABLE IF NOT EXISTS interviews (
 id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 starts_at TEXT NOT NULL, timezone TEXT NOT NULL, duration INTEGER NOT NULL,
 location TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Scheduled');
'''


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    production = os.environ.get('PORTAL_ENV') == 'production'
    secret = os.environ.get('PORTAL_SECRET_KEY')
    if production and not secret:
        raise RuntimeError('Set PORTAL_SECRET_KEY for production')
    if not secret:
        secret_file = Path(app.instance_path) / 'portal-secret'
        if not secret_file.exists():
            try:
                with open(secret_file, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                    handle.write(secrets.token_hex(32))
            except FileExistsError:
                pass
        secret = secret_file.read_text()
    app.config.update(SECRET_KEY=secret, DATABASE=str(Path(app.instance_path) / 'portal.sqlite3'),
                      MAX_CONTENT_LENGTH=100_000, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=production,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    if test_config:
        app.config.update(test_config)

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'])
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop('db', None)
        if connection:
            connection.close()

    with app.app_context():
        db().executescript(SCHEMA)

    def field(name, maximum=500, required=True):
        value = request.form.get(name, '').strip()
        if (required and not value) or len(value) > maximum:
            raise ValueError(f'Please enter a valid {name.replace("_", " ")} (up to {maximum} characters).')
        return value

    def email():
        value = field('email', 254).lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Please enter a valid email address.')
        return value

    def website(name):
        value = field(name, 2000, False)
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f'{name.replace("_", " ").title()} must be an HTTPS link.')
        return value

    def choice(name, options):
        value = field(name)
        if value not in options:
            raise ValueError(f'Please select a valid {name.replace("_", " ")}.')
        return value

    def employer(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.business:
                return redirect(url_for('login'))
            return view(*args, **kwargs)
        return wrapped

    def owned_job(job_id):
        job = db().execute('SELECT * FROM jobs WHERE id=? AND business_id=?',
                           (job_id, g.business['id'])).fetchone()
        if not job:
            abort(404)
        return job

    def owned_application(application_id):
        item = db().execute('''SELECT a.*, j.title AS job_title FROM applications a
            JOIN jobs j ON j.id=a.job_id WHERE a.id=? AND j.business_id=?''',
                            (application_id, g.business['id'])).fetchone()
        if not item:
            abort(404)
        return item

    @app.before_request
    def security():
        g.business = db().execute('SELECT * FROM businesses WHERE id=?',
                                  (session.get('business_id'),)).fetchone()
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method == 'POST' and not secrets.compare_digest(
                request.form.get('csrf', ''), session['csrf']):
            abort(400, 'Your form expired. Reload the page and try again.')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.context_processor
    def context():
        return dict(categories=CATEGORIES, modes=MODES, employment_types=TYPES,
                    candidate_statuses=STATUSES, zones=ZONES)

    @app.errorhandler(ValueError)
    def invalid(error):
        return render_template('error.html', message=str(error)), 400

    @app.route('/')
    def index():
        query = request.args.get('q', '').strip()[:200]
        mode = request.args.get('mode', '')
        category = request.args.get('category', '')
        sql = '''SELECT j.*, b.title AS business_title FROM jobs j JOIN businesses b
                 ON b.id=j.business_id WHERE j.status='Open' '''
        values = []
        if query:
            sql += ' AND (j.title LIKE ? OR b.title LIKE ? OR j.duties LIKE ?)'
            values += ['%' + query + '%'] * 3
        if mode in MODES:
            sql += ' AND j.work_mode=?'
            values.append(mode)
        if category in CATEGORIES:
            sql += ' AND j.category=?'
            values.append(category)
        jobs = db().execute(sql + ' ORDER BY j.id DESC', values).fetchall()
        return render_template('index.html', jobs=jobs, q=query, mode=mode, category=category)

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            address = email()
            password = field('password', 200)
            if len(password) < 12:
                raise ValueError('Use a password with at least 12 characters.')
            values = (address, generate_password_hash(password), field('title', 160),
                      field('industry', 100), website('website'), field('location', 200),
                      field('description', 5000, False))
            try:
                cursor = db().execute('''INSERT INTO businesses
                    (email,password,title,industry,website,location,description) VALUES (?,?,?,?,?,?,?)''', values)
                db().commit()
            except sqlite3.IntegrityError:
                raise ValueError('Unable to register this email. Try signing in.')
            session.clear()
            session['business_id'] = cursor.lastrowid
            session.permanent = True
            return redirect(url_for('dashboard'))
        return render_template('profile.html', registering=True, business={})

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            business = db().execute('SELECT * FROM businesses WHERE email=?', (email(),)).fetchone()
            if not business or not check_password_hash(business['password'], field('password', 200)):
                raise ValueError('Email or password is incorrect.')
            session.clear()
            session['business_id'] = business['id']
            session.permanent = True
            return redirect(url_for('dashboard'))
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('index'))

    @app.route('/employer/profile', methods=['GET', 'POST'])
    @employer
    def profile():
        if request.method == 'POST':
            db().execute('UPDATE businesses SET title=?,industry=?,website=?,location=?,description=? WHERE id=?',
                         (field('title', 160), field('industry', 100), website('website'),
                          field('location', 200), field('description', 5000, False), g.business['id']))
            db().commit()
            flash('Business profile updated.')
            return redirect(url_for('dashboard'))
        return render_template('profile.html', registering=False, business=g.business)

    @app.get('/employer')
    @employer
    def dashboard():
        jobs = db().execute('''SELECT j.*, COUNT(a.id) AS applicants FROM jobs j
            LEFT JOIN applications a ON a.job_id=j.id WHERE j.business_id=?
            GROUP BY j.id ORDER BY j.id DESC''', (g.business['id'],)).fetchall()
        return render_template('dashboard.html', jobs=jobs)

    @app.route('/employer/jobs/new', methods=['GET', 'POST'])
    @app.route('/employer/jobs/<int:job_id>/edit', methods=['GET', 'POST'])
    @employer
    def edit_job(job_id=None):
        job = owned_job(job_id) if job_id else {}
        if request.method == 'POST':
            try:
                low, high = Decimal(field('pay_min', 20)), Decimal(field('pay_max', 20))
                if not low.is_finite() or not high.is_finite() or low < 0 or high < low or high > 100000000:
                    raise InvalidOperation
            except InvalidOperation:
                raise ValueError('Enter a valid pay range, with maximum at least the minimum.')
            values = (field('title', 180), choice('category', CATEGORIES), field('duties', 10000),
                      field('requirements', 10000), str(low), str(high), choice('pay_unit', ('hour', 'year')),
                      field('location', 300), choice('work_mode', MODES), choice('employment_type', TYPES),
                      choice('status', ('Draft', 'Open', 'Closed')))
            if job_id:
                db().execute('''UPDATE jobs SET title=?,category=?,duties=?,requirements=?,pay_min=?,pay_max=?,
                    pay_unit=?,location=?,work_mode=?,employment_type=?,status=? WHERE id=?''', values + (job_id,))
            else:
                db().execute('''INSERT INTO jobs (title,category,duties,requirements,pay_min,pay_max,pay_unit,
                    location,work_mode,employment_type,status,business_id,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', values + (g.business['id'], datetime.now(timezone.utc).isoformat()))
            db().commit()
            flash('Job saved.')
            return redirect(url_for('dashboard'))
        return render_template('job_form.html', job=job)

    @app.route('/jobs/<int:job_id>', methods=['GET', 'POST'])
    def job_detail(job_id):
        job = db().execute('''SELECT j.*, b.title AS business_title, b.description AS business_description
            FROM jobs j JOIN businesses b ON b.id=j.business_id WHERE j.id=? AND j.status='Open' ''',
                           (job_id,)).fetchone()
        if not job:
            abort(404)
        if request.method == 'POST':
            if request.form.get('consent') != 'yes':
                raise ValueError('Please consent to sharing this application with the hiring business.')
            values = (job_id, field('name', 160), email(), website('resume_url'), field('message', 5000, False),
                      datetime.now(timezone.utc).isoformat())
            try:
                db().execute('INSERT INTO applications (job_id,name,email,resume_url,message,consent_at) VALUES (?,?,?,?,?,?)', values)
                db().commit()
            except sqlite3.IntegrityError:
                raise ValueError('An application for this email and job is already recorded.')
            return render_template('success.html')
        return render_template('job_detail.html', job=job)

    @app.get('/employer/jobs/<int:job_id>/candidates')
    @employer
    def candidates(job_id):
        job = owned_job(job_id)
        items = db().execute('SELECT * FROM applications WHERE job_id=? ORDER BY id DESC', (job_id,)).fetchall()
        return render_template('candidates.html', job=job, candidates=items)

    @app.route('/employer/applications/<int:application_id>', methods=['GET', 'POST'])
    @employer
    def application(application_id):
        item = owned_application(application_id)
        if request.method == 'POST':
            db().execute('UPDATE applications SET status=?,notes=? WHERE id=?',
                         (choice('status', STATUSES), field('notes', 10000, False), application_id))
            db().commit()
            flash('Candidate record updated.')
            return redirect(url_for('application', application_id=application_id))
        interviews = db().execute('SELECT * FROM interviews WHERE application_id=? ORDER BY starts_at', (application_id,)).fetchall()
        return render_template('application.html', candidate=item, interviews=interviews)

    @app.post('/employer/applications/<int:application_id>/interviews')
    @employer
    def schedule(application_id):
        item = owned_application(application_id)
        if item['status'] in ('Hired', 'Not selected', 'Withdrawn'):
            raise ValueError('Update the candidate status before scheduling another interview.')
        try:
            zone = choice('timezone', ZONES)
            local = datetime.fromisoformat(field('starts_at', 40))
            if local.tzinfo is not None:
                raise ValueError
            start = local.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
            if start.astimezone(ZoneInfo(zone)).replace(tzinfo=None) != local:
                raise ValueError
            duration = int(field('duration', 3))
            if start <= datetime.now(timezone.utc) or not 15 <= duration <= 240:
                raise ValueError
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Choose a valid future interview time and a duration of 15–240 minutes.')
        finish = start + timedelta(minutes=duration)
        for other in db().execute('''SELECT i.* FROM interviews i JOIN applications a ON a.id=i.application_id
            JOIN jobs j ON j.id=a.job_id WHERE j.business_id=? AND i.status='Scheduled' ''', (g.business['id'],)):
            other_start = datetime.fromisoformat(other['starts_at'])
            if start < other_start + timedelta(minutes=other['duration']) and finish > other_start:
                raise ValueError('This time overlaps an interview already scheduled for your business.')
        db().execute('INSERT INTO interviews (application_id,starts_at,timezone,duration,location,notes) VALUES (?,?,?,?,?,?)',
                     (application_id, start.isoformat(), zone, duration, field('location', 2000), field('notes', 3000, False)))
        db().execute("UPDATE applications SET status='Interview' WHERE id=?", (application_id,))
        db().commit()
        flash('Interview saved. Download the calendar file and share the details with the candidate; no invitation was sent.')
        return redirect(url_for('application', application_id=application_id))

    @app.post('/employer/interviews/<int:interview_id>/cancel')
    @employer
    def cancel_interview(interview_id):
        interview = db().execute('SELECT * FROM interviews WHERE id=?', (interview_id,)).fetchone()
        if not interview:
            abort(404)
        owned_application(interview['application_id'])
        db().execute("UPDATE interviews SET status='Cancelled' WHERE id=?", (interview_id,))
        db().commit()
        flash('Interview cancelled in the portal. Notify the candidate separately.')
        return redirect(url_for('application', application_id=interview['application_id']))

    @app.get('/employer/interviews/<int:interview_id>/calendar.ics')
    @employer
    def calendar(interview_id):
        item = db().execute('SELECT * FROM interviews WHERE id=?', (interview_id,)).fetchone()
        if not item:
            abort(404)
        candidate = owned_application(item['application_id'])
        def escape(value):
            return value.replace('\\', '\\\\').replace('\r', '').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')
        def stamp(value):
            return value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        start = datetime.fromisoformat(item['starts_at'])
        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Hiring Portal//Interview//EN',
                 'BEGIN:VEVENT', f'UID:interview-{item["id"]}@hiring-portal.local',
                 'DTSTAMP:' + stamp(datetime.now(timezone.utc)), 'DTSTART:' + stamp(start),
                 'DTEND:' + stamp(start + timedelta(minutes=item['duration'])),
                 'SUMMARY:' + escape('Interview: ' + candidate['job_title']),
                 'LOCATION:' + escape(item['location']),
                 'DESCRIPTION:' + escape(item['notes']),
                 'STATUS:' + ('CANCELLED' if item['status'] == 'Cancelled' else 'CONFIRMED'),
                 'END:VEVENT', 'END:VCALENDAR']
        # Fold at 75 UTF-8 octets as required by iCalendar.
        folded = []
        for line in lines:
            current = ''
            for char in line:
                if len((current + char).encode()) > 75:
                    folded.append(current)
                    current = ' '
                current += char
            folded.append(current)
        return Response('\r\n'.join(folded) + '\r\n', mimetype='text/calendar',
                        headers={'Content-Disposition': 'attachment; filename=interview.ics'})

    return app
