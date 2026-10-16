# Employers & Providers — Post a Job

A separate Flask/SQLite web portal for employer-owned hiring records. It does
not modify the personal CSV job-search tracker or connect Indeed credentials.

## Run locally

From the repository root, using Python 3.10+:

```sh
python3 -m venv .venv
.venv/bin/pip install -r employer_portal/requirements.txt
.venv/bin/flask --app employer_portal.app:create_app run
```

Open http://127.0.0.1:5000. Create an employer account, complete a business
profile, and save a job as Open to publish it on this portal. The public job
detail page accepts applications; the employer dashboard links to each job's
candidate list and individual review/interview pages. Draft and Closed jobs
are not public and cannot receive new applications.

Fields include duties, qualifications, USD pay range, hourly/annual pay,
category, employment type, eligible location/states and Remote/Hybrid/On-site.
Applicant records store contact details, optional HTTPS resume/portfolio link,
introduction, consent timestamp, pipeline status and private notes. Resume
uploads are not accepted. Each business can access only its own jobs, candidates
and interview records. Interview times are stored in UTC with the selected time
zone; overlapping interviews for the same business are rejected. Cancellation
is recorded locally. Calendar downloads contain no emailed invitation.

## Deployment status and requirements

This repository addition is runnable code, not a hosted deployment. It needs a
Python WSGI host and persistent writable storage; static-only hosting cannot run
it. For production set `PORTAL_ENV=production` and a long random
`PORTAL_SECRET_KEY` in the host's secret settings, serve HTTPS, and run the app
factory with a production WSGI server. Secure cookies are enabled in production.
Do not use Flask's development server for public hosting.

SQLite and the development session secret live in gitignored `instance/`.
Back up the database, restrict file access and keep it on persistent storage.
Use one database-backed deployment; multiple ephemeral replicas need a shared
database design before use. Authentication uses password hashing, CSRF tokens,
owner checks and HTTP-only session cookies. Before public launch add managed
email verification/password recovery, abuse controls/rate limits, business
verification/moderation and a published retention/privacy process. Business
profiles are explicitly employer supplied, not verified. The portal does not
send email/SMS, sync external calendars, run background checks or automatically
submit applications. Candidate notes are never published on the public board.

## Validate

```sh
.venv/bin/python -m unittest employer_portal.test_app -v
```
