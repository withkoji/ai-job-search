#!/usr/bin/env python3
"""Emit provider-neutral search arguments; the agent host executes connected tools."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_plan(config, category=None):
    if config.get('country_code') != 'US' or config.get('remote_only') is not True:
        raise ValueError('this preset requires US and remote_only=true')
    groups = config['categories']
    if category:
        groups = [group for group in groups if group['id'] == category]
        if not groups:
            raise ValueError(f'unknown category: {category}')
    return [{'category': group['id'], 'search': query, 'country_code': 'US',
             'location': 'United States', 'remote_only': True}
            for group in groups for query in group['queries']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/us_remote_search.json')
    parser.add_argument('--category')
    args = parser.parse_args()
    try:
        print(json.dumps(build_plan(json.loads(args.config.read_text()), args.category), indent=2))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f'Cannot build search plan: {error}\n')


if __name__ == '__main__':
    main()
