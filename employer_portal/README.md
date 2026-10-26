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
profile, save a job as Open, and complete operator review before it is published. The public job
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
owner checks and HTTP-only session cookies. Before public launch finish managed
email verification/password recovery, host-level abuse controls, business review
operations and a published retention/privacy process. Business
profiles are explicitly employer supplied, not verified. The portal does not
send email/SMS, sync external calendars, run background checks or automatically
submit applications. Candidate notes are never published on the public board.

## Validate

```sh
.venv/bin/python -m unittest employer_portal.test_app -v
```

## Review businesses and operate the hiring queue

New and existing businesses start **pending review**. They can sign in, prepare
listings and review their existing candidates, but Open listings are not public
until an operator completes its business checks and approves the account. This
is a manual publication review, not proof of credentials or an automated
verification service. The existing database migrates without dropping records.
Use a trusted server shell with access to the portal's database:

```sh
.venv/bin/flask --app employer_portal.app:create_app review-employer --email business@example.com --decision approve
.venv/bin/flask --app employer_portal.app:create_app review-employer --email business@example.com --decision pause
```

Pausing hides the business's jobs and stops public applications while preserving
its private candidate history. Decisions are recorded locally. Do not approve a
business merely because it registered; verify the business and posting rights
through your own review process first. No review decisions were made by this PR.

The dashboard now surfaces the oldest five New/Reviewing candidates and next five
scheduled interviews. Candidate lists can be searched by name/email and filtered
by pipeline status. Interview times on the dashboard are explicitly UTC.

Login is limited to 10 POST attempts per 15-minute window per network address
and per normalized account email; registration to 5 per hour per network address;
application submission to 10 per hour per network address. Counters are atomic,
stored in SQLite across worker restarts, and contain keyed hashes rather than
plaintext addresses. Limits are fixed-window and count invalid submissions too.
The app ignores arbitrary forwarded-IP headers; behind a shared reverse proxy
its network-address bucket may be shared. Configure perimeter limits on the host
as well and review thresholds for your traffic. Email/session recovery remains
unconfigured; do not promise automated verification or password reset messages.

## Deployment configuration checks

Set these through your hosting environment, not tracked source files:

- `PORTAL_ENV=production`
- `PORTAL_SECRET_KEY`: a random secret of at least 32 characters
- `PORTAL_DATA_DIR`: an absolute persistent writable directory for SQLite
- `PORTAL_PRIVACY_CONTACT`: the real operator contact for applicant data requests

```sh
.venv/bin/flask --app employer_portal.app:create_app launch-check
```

`/healthz` returns a minimal database connectivity check without applicant data.
Passing `launch-check` checks settings only; independently verify HTTPS, backup
restore, the host's rate limits, account recovery and applicant privacy handling.
No hosting service, domain, privacy contact or credentials are created here.
All data should stay in one persistent deployment until a shared-database design
is implemented. If relocating an existing instance, copy its database and keep
the same session secret through a controlled backup/restore before changing
`PORTAL_DATA_DIR`; setting a new empty directory starts a fresh portal.

## Applicant data requests

The `/privacy` page explains what the portal stores, who can access it and how
to reach the configured operator. Set a real contact and establish a retention
schedule before accepting live applicant data. Following a verified deletion
request or your retention process, the operator can preview and then delete:

```sh
.venv/bin/flask --app employer_portal.app:create_app delete-application --application-id 123
.venv/bin/flask --app employer_portal.app:create_app delete-application --application-id 123 --apply
```

This removes that application and its interviews, not the business or job.
Existing backup copies require your separate backup-retention process. No
records are deleted by installing this feature and no retention job is scheduled.
