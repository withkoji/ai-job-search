from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from employer_portal.app import create_app


class HiringPortal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / 'test.sqlite3')
        self.app = create_app({'TESTING': True, 'DATABASE': self.database, 'SECRET_KEY': 'testing-secret'})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, data, client=None):
        client = client or self.client
        client.get('/')
        with client.session_transaction() as state:
            token = state['csrf']
        return client.post(path, data={**data, 'csrf': token})

    def register(self, address='employer@example.com', client=None):
        return self.post('/register', {'email': address, 'password': 'a-secure-test-password',
            'title': 'Example Services', 'industry': 'Business services', 'location': 'United States',
            'description': 'A growing provider.'}, client)

    def job(self, **overrides):
        data = dict(title='Customer care specialist', category='Customer care', duties='Respond to customers.',
            requirements='Customer service experience.', pay_min='20.50', pay_max='25.75', pay_unit='hour',
            location='United States; eligible states specified at interview', work_mode='Remote',
            employment_type='Full-time', status='Open')
        return self.post('/employer/jobs/new', {**data, **overrides})

    def apply(self, **overrides):
        return self.post('/jobs/1', {'name': 'Candidate Example', 'email': 'candidate@example.com',
            'resume_url': 'https://example.com/resume', 'message': 'Experienced in customer support.',
            'consent': 'yes', **overrides})

    def schedule(self, **overrides):
        start = (datetime.now() + timedelta(days=10)).replace(hour=15, minute=0).isoformat(timespec='minutes')
        return self.post('/employer/applications/1/interviews', {'starts_at': start, 'timezone': 'UTC',
            'duration': '30', 'location': 'Video call details to follow', 'notes': 'Introductory interview', **overrides})

    def rows(self, table):
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute('SELECT * FROM ' + table)]

    def test_complete_hiring_flow_and_private_notes(self):
        self.assertEqual(self.register().status_code, 302)
        self.assertEqual(self.job().status_code, 302)
        self.assertEqual(self.apply().status_code, 200)
        self.assertIn(b'Candidate Example', self.client.get('/employer/jobs/1/candidates').data)
        self.assertEqual(self.post('/employer/applications/1', {'status': 'Shortlisted', 'notes': 'Private review note'}).status_code, 302)
        self.assertNotIn(b'Private review note', self.client.get('/jobs/1').data)
        self.assertEqual(self.schedule().status_code, 302)
        self.assertEqual(self.rows('applications')[0]['status'], 'Interview')
        calendar = self.client.get('/employer/interviews/1/calendar.ics')
        self.assertEqual(calendar.status_code, 200)
        self.assertIn(b'BEGIN:VEVENT', calendar.data)
        self.assertIn(b'DTSTART:', calendar.data)
        self.assertNotIn(b'ATTENDEE:', calendar.data)
        self.assertEqual(self.post('/employer/interviews/1/cancel', {}).status_code, 302)
        self.assertIn(b'STATUS:CANCELLED', self.client.get('/employer/interviews/1/calendar.ics').data)

    def test_owner_isolation_for_all_hiring_records(self):
        self.register()
        self.job()
        self.apply()
        self.schedule()
        other = self.app.test_client()
        self.register('other@example.com', other)
        for path in ('/employer/jobs/1/edit', '/employer/jobs/1/candidates',
                     '/employer/applications/1', '/employer/interviews/1/calendar.ics'):
            self.assertEqual(other.get(path).status_code, 404)
        for path in ('/employer/applications/1', '/employer/applications/1/interviews', '/employer/interviews/1/cancel'):
            self.assertEqual(self.post(path, {}, other).status_code, 404)
        self.assertEqual(other.get('/jobs/1').status_code, 200)

    def test_draft_closed_and_remote_filters(self):
        self.register()
        self.job(status='Draft')
        self.assertEqual(self.client.get('/jobs/1').status_code, 404)
        self.assertEqual(self.apply().status_code, 404)
        self.job(title='Store cashier', work_mode='On-site')
        self.assertIn(b'Store cashier', self.client.get('/').data)
        self.assertNotIn(b'Store cashier', self.client.get('/?mode=Remote').data)
        self.assertNotIn(b'Customer care specialist', self.client.get('/').data)

    def test_duplicates_consent_and_unsafe_links(self):
        self.register()
        self.job()
        self.assertEqual(self.apply(consent='').status_code, 400)
        self.assertEqual(self.apply(resume_url='javascript:alert(1)').status_code, 400)
        self.assertEqual(len(self.rows('applications')), 0)
        self.assertEqual(self.apply().status_code, 200)
        self.assertEqual(self.apply().status_code, 400)
        self.assertEqual(len(self.rows('applications')), 1)

    def test_pay_validation_and_escaping(self):
        self.register()
        for low, high in [('NaN','20'), ('30','10'), ('-1','20'), ('invalid','20')]:
            self.assertEqual(self.job(pay_min=low, pay_max=high).status_code, 400)
        self.assertEqual(self.job(title='<script>alert(1)</script>').status_code, 302)
        page = self.client.get('/jobs/1').data
        self.assertNotIn(b'<script>', page)
        self.assertIn(b'&lt;script&gt;', page)
        self.assertEqual(self.client.get('/').headers['X-Content-Type-Options'], 'nosniff')

    def test_csrf_login_logout_and_password_hashing(self):
        self.assertEqual(self.client.post('/register', data={}).status_code, 400)
        self.register()
        self.assertNotEqual(self.rows('businesses')[0]['password'], 'a-secure-test-password')
        self.post('/logout', {})
        self.assertEqual(self.client.get('/employer').status_code, 302)
        self.assertEqual(self.post('/login', {'email':'employer@example.com', 'password':'incorrect'}).status_code, 400)
        self.assertEqual(self.post('/login', {'email':'employer@example.com', 'password':'a-secure-test-password'}).status_code, 302)

    def test_scheduling_conflicts_dates_and_duration(self):
        self.register()
        self.job()
        self.apply()
        for change in ({'starts_at':'2020-01-01T12:00'}, {'duration':'0'}, {'timezone':'invalid'}):
            self.assertEqual(self.schedule(**change).status_code, 400)
        self.assertEqual(self.schedule().status_code, 302)
        self.assertEqual(self.schedule().status_code, 400)
        self.assertEqual(len(self.rows('interviews')), 1)
        self.post('/employer/interviews/1/cancel', {})
        self.assertEqual(self.schedule().status_code, 302)

    def test_profile_and_candidate_review_pages(self):
        self.assertEqual(self.client.get('/register').status_code, 200)
        self.assertEqual(self.client.get('/login').status_code, 200)
        self.register()
        self.assertEqual(self.client.get('/employer/profile').status_code, 200)
        self.post('/employer/profile', {'title':'Updated Services','industry':'Retail','location':'Ohio'})
        self.assertIn(b'Updated Services', self.client.get('/employer').data)
        self.assertEqual(self.client.get('/employer/jobs/new').status_code, 200)
        self.job()
        self.apply()
        self.assertEqual(self.client.get('/employer/applications/1').status_code, 200)
        self.assertEqual(self.post('/employer/applications/1', {'status':'Made up'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
