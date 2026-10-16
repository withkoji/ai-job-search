#!/usr/bin/env python3
"""Import verified plugin results into the existing discovery queue; preview by default."""
import argparse
import csv
from datetime import date
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from job_key import make_key

ROOT = Path(__file__).resolve().parent.parent


def identity(company, title):
    return (company.strip().casefold(), title.strip().casefold())


def merge_jobs(state, jobs, provider, tracked=()):
    if not isinstance(state, dict) or not isinstance(state.get('seen'), dict):
        raise ValueError('state must contain a seen object')
    if not isinstance(jobs, list):
        raise ValueError('input must be a JSON array')
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError('provider is required')
    seen = dict(state['seen'])
    pairs = set(tracked)
    urls = set()
    for entry in seen.values():
        pairs.add(identity(entry.get('company', ''), entry.get('title', '')))
        urls.add(entry.get('url'))
    added = []
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f'job {index + 1}: expected an object')
        for field in ('title', 'company', 'url'):
            if not isinstance(job.get(field), str) or not job[field].strip():
                raise ValueError(f'job {index + 1}: missing {field}')
        url = urlsplit(job['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError(f'job {index + 1}: expected an HTTPS posting URL')
        for field in ('posted_date', 'deadline'):
            value = job.get(field)
            if value is not None:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError(f'job {index + 1}: invalid {field}')
        pair = identity(job['company'], job['title'])
        key = make_key(job['company'], job['title'], job['url'])
        if pair in pairs or job['url'] in urls or key in seen:
            continue
        entry = {field: job[field] for field in ('title', 'company', 'url')}
        entry.update(first_seen=date.today().isoformat(), posted_date=job.get('posted_date'),
                     deadline=job.get('deadline'), status='new', source='plugin',
                     portal=provider.strip())
        if isinstance(job.get('location'), str):
            entry['location'] = job['location']
        seen[key] = entry
        added.append(key)
        pairs.add(pair)
        urls.add(job['url'])
    return {**state, 'seen': seen}, added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    state_path = args.root / 'job_scraper/seen_jobs.json'
    tracker = args.root / 'job_search_tracker.csv'
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {'seen': {}}
        tracked = set()
        if tracker.exists():
            with tracker.open(newline='', encoding='utf-8-sig') as handle:
                for row in csv.DictReader(handle):
                    tracked.add(identity(row.get('company', ''), row.get('role', row.get('title', ''))))
        merged, added = merge_jobs(state, json.loads(args.input.read_text()), args.provider, tracked)
        if args.apply and added:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_name = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=state_path.parent, delete=False) as handle:
                    temp_name = handle.name
                    json.dump(merged, handle, ensure_ascii=False, indent=2)
                    handle.write('\n')
                os.replace(temp_name, state_path)
            finally:
                if temp_name and os.path.exists(temp_name):
                    os.unlink(temp_name)
        print(json.dumps({'mode': 'applied' if args.apply else 'preview', 'added': added}))
    except (ValueError, OSError, TypeError) as error:
        parser.exit(1, f'Import failed: {error}\n')


if __name__ == '__main__':
    main()
