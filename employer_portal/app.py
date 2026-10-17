"""Run with: flask --app employer_portal.app run (see README for production)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import hashlib
import hmac
import time

import click
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
 location TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 approved INTEGER NOT NULL DEFAULT 0);
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
CREATE TABLE IF NOT EXISTS request_limits (
 bucket TEXT NOT NULL, window INTEGER NOT NULL, count INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 PRIMARY KEY(bucket,window));
CREATE TABLE IF NOT EXISTS moderation_events (
 id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL REFERENCES businesses(id),
 decision TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS applications_job_id ON applications(job_id);
CREATE INDEX IF NOT EXISTS interviews_application_id ON interviews(application_id);
CREATE INDEX IF NOT EXISTS jobs_business_id ON jobs(business_id);
'''


def create_app(test_config=None):
    instance = os.environ.get('PORTAL_DATA_DIR')
    app = Flask(__name__, instance_relative_config=True,
                instance_path=str(Path(instance).resolve()) if instance else None)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True, mode=0o700)
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
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8), REQUIRE_EMPLOYER_APPROVAL=True,
                      PRIVACY_CONTACT=os.environ.get('PORTAL_PRIVACY_CONTACT', ''),
                      RATE_LIMITS={'login': (10, 900), 'register': (5, 3600), 'job_detail': (10, 3600)})
    if test_config:
        app.config.update(test_config)

    def db():
        if 'db' not in g:
            descriptor = os.open(app.config['DATABASE'], os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
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
        # Add the review flag to existing installations without dropping hiring data.
        with db():
            db().execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db().execute('PRAGMA table_info(businesses)')}
            if 'approved' not in columns:
                db().execute('ALTER TABLE businesses ADD COLUMN approved INTEGER NOT NULL DEFAULT 0')

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

    def limit_request():
        limit = app.config['RATE_LIMITS'].get(request.endpoint)
        if not limit or request.method != 'POST':
            return
        maximum, seconds = limit
        now = int(time.time())
        window = now // seconds
        # Do not trust forwarding headers from arbitrary clients. If a deployment
        # uses a reverse proxy, enforce its own perimeter rate limits as well.
        identities = ['ip:' + (request.remote_addr or 'unknown')]
        if request.endpoint == 'login':
            identities.append('account:' + request.form.get('email', '').strip().casefold()[:254])
        rejected = False
        with db():
            for identity in identities:
                raw = f'{request.endpoint}:{identity}'.encode()
                bucket = hmac.new(app.config['SECRET_KEY'].encode(), raw, hashlib.sha256).hexdigest()
                db().execute('''INSERT INTO request_limits(bucket,window,count,expires_at) VALUES (?,?,1,?)
                    ON CONFLICT(bucket,window) DO UPDATE SET count=count+1''', (bucket, window, (window + 1) * seconds))
                count = db().execute('SELECT count FROM request_limits WHERE bucket=? AND window=?',
                                     (bucket, window)).fetchone()['count']
                rejected = rejected or count > maximum
            # A bounded history; no email addresses or IPs are stored in plaintext.
            db().execute('DELETE FROM request_limits WHERE expires_at <= ?', (now,))
        if rejected:
            response = Response('Too many requests. Please try again later.', status=429)
            response.headers['Retry-After'] = str(seconds - now % seconds)
            abort(response)

    @app.before_request
    def security():
        if request.endpoint == 'health':
            return
        g.business = db().execute('SELECT * FROM businesses WHERE id=?',
                                  (session.get('business_id'),)).fetchone()
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method == 'POST' and not secrets.compare_digest(
                request.form.get('csrf', '').encode(), session['csrf'].encode()):
            abort(400, 'Your form expired. Reload the page and try again.')
        limit_request()

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def health():
        try:
            db().execute('SELECT 1 FROM businesses LIMIT 1').fetchone()
            return {'status': 'ok'}
        except sqlite3.Error:
            return {'status': 'unavailable'}, 503

    @app.get('/privacy')
    def privacy():
        return render_template('privacy.html', contact=app.config['PRIVACY_CONTACT'])

    @app.cli.command('review-employer')
    @click.option('--email', 'address', required=True)
    @click.option('--decision', type=click.Choice(['approve', 'pause']), required=True)
    def review_employer(address, decision):
        """Record an operator's completed business review; sends no messages."""
        with app.app_context():
            business = db().execute('SELECT id FROM businesses WHERE email=?', (address.strip().lower(),)).fetchone()
            if not business:
                raise click.ClickException('Business not found.')
            with db():
                db().execute('UPDATE businesses SET approved=? WHERE id=?', (int(decision == 'approve'), business['id']))
                db().execute('INSERT INTO moderation_events(business_id,decision,created_at) VALUES (?,?,?)',
                             (business['id'], decision, datetime.now(timezone.utc).isoformat()))
            click.echo(f'Business {business["id"]}: {decision}.')

    @app.cli.command('delete-application')
    @click.option('--application-id', required=True, type=int)
    @click.option('--apply', 'apply_change', is_flag=True)
    def delete_application(application_id, apply_change):
        """Preview or execute an operator-authorized applicant data deletion."""
        with app.app_context():
            item = db().execute('SELECT id FROM applications WHERE id=?', (application_id,)).fetchone()
            if not item:
                raise click.ClickException('Application not found.')
            if not apply_change:
                click.echo(f'Preview: delete application {application_id} and its interviews. Repeat with --apply to delete.')
                return
            with db():
                db().execute('DELETE FROM interviews WHERE application_id=?', (application_id,))
                db().execute('DELETE FROM applications WHERE id=?', (application_id,))
            click.echo(f'Deleted application {application_id} and its interviews.')

    @app.cli.command('launch-check')
    def launch_check():
        """Check deployment settings without exposing credentials or applicant data."""
        failures = []
        if not production:
            failures.append('Set PORTAL_ENV=production.')
        if len(app.config['SECRET_KEY']) < 32:
            failures.append('Use a random secret of at least 32 characters.')
        if not app.config['PRIVACY_CONTACT']:
            failures.append('Set PORTAL_PRIVACY_CONTACT to your applicant privacy contact.')
        if not os.environ.get('PORTAL_DATA_DIR'):
            failures.append('Set PORTAL_DATA_DIR to a persistent writable directory.')
        if not app.config['REQUIRE_EMPLOYER_APPROVAL']:
            failures.append('Enable employer approval before publishing.')
        if not app.config['SESSION_COOKIE_SECURE']:
            failures.append('Enable secure cookies and serve through HTTPS.')
        if failures:
            raise click.ClickException('\n'.join(failures))
        click.echo('Configuration checks passed. Confirm HTTPS, backups, recovery, and privacy operations before inviting users.')

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
        if app.config['REQUIRE_EMPLOYER_APPROVAL']:
            sql += ' AND b.approved=1'
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
        pending = db().execute('''SELECT a.id, a.name, a.status, j.title AS job_title
            FROM applications a JOIN jobs j ON j.id=a.job_id
            WHERE j.business_id=? AND a.status IN ('New','Reviewing') ORDER BY a.id LIMIT 5''',
            (g.business['id'],)).fetchall()
        upcoming = db().execute('''SELECT i.*, a.name, j.title AS job_title
            FROM interviews i JOIN applications a ON a.id=i.application_id JOIN jobs j ON j.id=a.job_id
            WHERE j.business_id=? AND i.status='Scheduled' AND i.starts_at>=? ORDER BY i.starts_at LIMIT 5''',
            (g.business['id'], datetime.now(timezone.utc).isoformat())).fetchall()
        return render_template('dashboard.html', jobs=jobs, pending=pending, upcoming=upcoming)

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
        job = db().execute('''SELECT j.*, b.title AS business_title, b.approved AS business_approved, b.description AS business_description
            FROM jobs j JOIN businesses b ON b.id=j.business_id WHERE j.id=? AND j.status='Open' ''',
                           (job_id,)).fetchone()
        if not job or (app.config['REQUIRE_EMPLOYER_APPROVAL'] and not job['business_approved']):
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
        status = request.args.get('status', '')
        query = request.args.get('q', '').strip()[:200]
        sql, values = 'SELECT * FROM applications WHERE job_id=?', [job_id]
        if status in STATUSES:
            sql += ' AND status=?'
            values.append(status)
        if query:
            sql += ' AND (name LIKE ? OR email LIKE ?)'
            values += ['%' + query + '%'] * 2
        items = db().execute(sql + ' ORDER BY id DESC', values).fetchall()
        return render_template('candidates.html', job=job, candidates=items, selected_status=status, q=query)

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
