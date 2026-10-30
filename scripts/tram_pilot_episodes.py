#!/usr/bin/env python3
"""Stage 4: reviewable candidate episodes from stage-3 diagnostic segments.

No additional Python packages. Reads the original stage-3 CSVs (folder or ZIP),
links only adjacent bad segments that were split by telemetry gaps, and never
claims a bridged gap is confirmed continuous downtime or a proven failure.

Usage:
    python3 tram_pilot_episodes.py --input tram_pilot_diagnostics \
       --output tram_pilot_episodes --bridge-seconds 90
"""
from __future__ import annotations

import argparse
import collections
import csv
from datetime import datetime
import json
from pathlib import Path
import zipfile

CATEGORIES = {
    'subsystem_error': 'subsystem_error_segments.csv',
    'sensor_stale': 'sensor_stale_segments.csv',
}
KEY = ('category', 'signal', 'loco_number', 'device_id', 'cabin_type')
OUTPUT_COLUMNS = [
    'episode_id', *KEY,
    'first_bad_at', 'last_bad_at', 'first_non_bad_at',
    'observed_window_seconds', 'sum_segment_bad_spans_seconds',
    'bad_snapshots', 'adas_true_snapshots', 'adas_false_snapshots',
    'segment_count', 'bridged_gap_count', 'bridged_gap_seconds',
    'start_left_censored', 'end_right_censored', 'final_close_reason',
    'final_clear_state', 'first_source_file', 'last_source_file',
    'first_prior_good_at', 'evidence_class', 'note',
]
SEGMENT_LINK_COLUMNS = ['episode_id', 'source_segment_number', 'category',
                        'signal', 'loco_number', 'device_id', 'cabin_type',
                        'first_bad_at', 'last_bad_at', 'bad_snapshots',
                        'close_reason', 'left_censored', 'right_censored']


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def to_seconds(x, default=0.0):
    try:
        return float(x)
    except (ValueError, TypeError):
        return default


def to_int(x):
    try:
        return int(x)
    except (ValueError, TypeError):
        return 0


def truthy(x):
    return str(x).strip().lower() == 'true'


def get_csv(source: Path, filename: str) -> list[dict]:
    if source.is_dir():
        f = source / filename
        with f.open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            names = [name for name in archive.namelist() if name.rsplit('/', 1)[-1] == filename]
            if len(names) != 1:
                raise ValueError(f'Expected one {filename} inside ZIP, found {len(names)}')
            import io
            with archive.open(names[0]) as raw:
                with io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as stream:
                    return list(csv.DictReader(stream))
    raise FileNotFoundError(f'Input should be diagnostics directory or ZIP: {source}')


def validate(rows: list[dict], category: str):
    required = set(KEY) | {'first_bad_at', 'last_bad_at', 'close_reason',
                           'bad_snapshots', 'observed_span_seconds',
                           'left_censored', 'right_censored'}
    for index, row in enumerate(rows, 1):
        absent = required - set(row)
        if absent:
            raise ValueError(f'{category} row {index}: missing columns {absent}')
        if not row['first_bad_at'] or not row['last_bad_at']:
            raise ValueError(f'{category} row {index}: missing timestamps')
        if ts(row['first_bad_at']) > ts(row['last_bad_at']):
            raise ValueError(f'{category} row {index}: invalid timestamp order')
        if row['category'] != category:
            raise ValueError(f'{category} row {index}: unexpected category {row["category"]!r}')


def linkable(prev: dict, nxt: dict, limit: float):
    # Explicit observed clearance ALWAYS terminates the preceding candidate.
    if prev['close_reason'] != 'telemetry_gap':
        return False, None
    if tuple(prev[k] for k in KEY) != tuple(nxt[k] for k in KEY):
        return False, None
    if not truthy(nxt['left_censored']):
        return False, None
    # A clearance is a barrier even when unknown snapshots or long gaps follow it.
    cleared_at = nxt.get('prior_good_at')
    if cleared_at and ts(prev['last_bad_at']) <= ts(cleared_at) <= ts(nxt['first_bad_at']):
        return False, None
    distance = (ts(nxt['first_bad_at']) - ts(prev['last_bad_at'])).total_seconds()
    # Only link tightly spaced consecutive unknown intervals, not arbitrary outages.
    return 0 < distance <= limit, distance


def assemble(rows: list[dict], limit: float, prefix: str):
    grouped = collections.defaultdict(list)
    for i, row in enumerate(rows, 1):
        row = dict(row)
        row['_ordinal'] = i
        grouped[tuple(row[k] for k in KEY)].append(row)
    candidates = []
    for identity in sorted(grouped):
        ordered = sorted(grouped[identity], key=lambda r: (ts(r['first_bad_at']), ts(r['last_bad_at']), r['_ordinal']))
        current = []
        bridges = []
        for row in ordered:
            can, seconds = linkable(current[-1], row, limit) if current else (False, None)
            if can:
                current.append(row)
                bridges.append(seconds)
            else:
                if current:
                    candidates.append((current, bridges))
                current, bridges = [row], []
        if current:
            candidates.append((current, bridges))
    candidates.sort(key=lambda x: (ts(x[0][0]['first_bad_at']),
                                   x[0][0]['loco_number'], x[0][0]['device_id']))
    output = []
    segment_links = []
    for n, (parts, bridges) in enumerate(candidates, 1):
        first, last = parts[0], parts[-1]
        episode_id = f'{prefix}-{n:05d}'
        first_time = ts(first['first_bad_at'])
        last_time = ts(last['last_bad_at'])
        gap_count = len(bridges)
        if gap_count:
            evidence = 'POSSIBLE_CONTINUATION_ACROSS_MISSING_DATA'
            note = 'Consecutive bad observations linked across unobserved gaps; no proof of continuity during gaps.'
        elif last['close_reason'] == 'observed_non_bad':
            evidence = 'OBSERVED_BAD_THEN_CLEARED'
            note = 'A non-bad state was observed after the last bad snapshot; neither full recovery nor physical repair is implied.'
        else:
            evidence = 'INCOMPLETE_OBSERVATION'
            note = 'Bad state observed, but episode boundary is not fully known.'
        entry = {
            'episode_id': episode_id,
            **{k: first[k] for k in KEY},
            'first_bad_at': first['first_bad_at'],
            'last_bad_at': last['last_bad_at'],
            'first_non_bad_at': last.get('first_non_bad_at', ''),
            'observed_window_seconds': round((last_time-first_time).total_seconds(), 3),
            'sum_segment_bad_spans_seconds': round(sum(to_seconds(r['observed_span_seconds']) for r in parts), 3),
            'bad_snapshots': sum(to_int(r['bad_snapshots']) for r in parts),
            'adas_true_snapshots': sum(to_int(r.get('adas_true_snapshots')) for r in parts),
            'adas_false_snapshots': sum(to_int(r.get('adas_false_snapshots')) for r in parts),
            'segment_count': len(parts),
            'bridged_gap_count': gap_count,
            'bridged_gap_seconds': round(sum(bridges), 3),
            'start_left_censored': first['left_censored'],
            'end_right_censored': last['right_censored'],
            'final_close_reason': last['close_reason'],
            'final_clear_state': last.get('clear_state', ''),
            'first_source_file': first.get('first_source_file',''),
            'last_source_file': last.get('last_source_file',''),
            'first_prior_good_at': first.get('prior_good_at', ''),
            'evidence_class': evidence,
            'note': note,
        }
        output.append(entry)
        for part in parts:
            segment_links.append({
                'episode_id': episode_id,
                'source_segment_number': part['_ordinal'],
                **{k: part[k] for k in KEY},
                **{k: part[k] for k in ('first_bad_at','last_bad_at','bad_snapshots',
                                        'close_reason','left_censored','right_censored')},
            })
    assert len(segment_links) == len(rows), 'Some original segments were lost'
    assert sum(x['bad_snapshots'] for x in output) == sum(to_int(x['bad_snapshots']) for x in rows)
    return output, segment_links


def write_csv(path: Path, records: list[dict], columns: list[str]):
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)


def count_by(rows, key):
    return dict(collections.Counter(r[key] for r in rows).most_common())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', default='data/diagnostics', help='Stage-3 diagnostics directory or ZIP')
    ap.add_argument('--output', default='data/episodes', help='Destination folder (must be new)')
    ap.add_argument('--bridge-seconds', type=float, default=90,
                    help='Maximum time between bad observations when no recovery was observed (default 90 sec)')
    args = ap.parse_args()
    if args.bridge_seconds <= 0:
        ap.error('--bridge-seconds must be > 0')
    source = Path(args.input)
    out = Path(args.output)
    if out.exists() and any(out.iterdir()):
        ap.error('Output directory already has contents. Choose a fresh --output directory.')
    inputs = {}
    results = {}
    total_links = []
    for category, name in CATEGORIES.items():
        rows = get_csv(source, name)
        validate(rows, category)
        inputs[category] = rows
        result, links = assemble(rows, args.bridge_seconds,
                                 'ERR' if category == 'subsystem_error' else 'STA')
        results[category] = result
        total_links += links
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / 'candidate_error_episodes.csv', results['subsystem_error'], OUTPUT_COLUMNS)
    write_csv(out / 'candidate_sensor_episodes.csv', results['sensor_stale'], OUTPUT_COLUMNS)
    write_csv(out / 'episode_segment_links.csv', total_links, SEGMENT_LINK_COLUMNS)
    by_tram = count_by(results['subsystem_error'], 'loco_number')
    sensitivity = {}
    for threshold in (30, 45, 60, 90, 120, 300):
        series, _ = assemble(inputs['subsystem_error'], threshold, 'ERR')
        sensitivity[str(threshold)] = len(series)
    summary = {
        'scope': 'Candidate episodes for REVIEW only; NOT verified physical failures or continuous downtime.',
        'input': str(source),
        'bridge_seconds': args.bridge_seconds,
        'observed_error_segments': len(inputs['subsystem_error']),
        'candidate_error_episodes': len(results['subsystem_error']),
        'observed_sensor_stale_segments': len(inputs['sensor_stale']),
        'candidate_sensor_episodes': len(results['sensor_stale']),
        'error_snapshots_preserved': sum(r['bad_snapshots'] for r in results['subsystem_error']),
        'sensor_stale_snapshots_preserved': sum(r['bad_snapshots'] for r in results['sensor_stale']),
        'error_episode_counts_by_tram': by_tram,
        'error_episode_evidence_classes': count_by(results['subsystem_error'], 'evidence_class'),
        'error_episode_final_close_reasons': count_by(results['subsystem_error'], 'final_close_reason'),
        'error_episode_final_clear_states': count_by(results['subsystem_error'], 'final_clear_state'),
        'error_episode_gap_links_total': sum(r['bridged_gap_count'] for r in results['subsystem_error']),
        'error_sensitivity_to_bridge_threshold_seconds': sensitivity,
        'caveats': [
            'A gap-bridged candidate could represent multiple separate real failures.',
            'A gap-bridged interval is NOT proven downtime; no uptime/MTTR/MTBF is calculated.',
            'Observed non-error closes a segment; it does NOT necessarily mean ProperlyWork.',
            'Episode IDs are provisional and can change with data scope and sort order.',
            'Input scope depends on the preceding scan and row limits; it may be incomplete.',
        ],
    }
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print('\n=== TRAM PILOT CANDIDATE EPISODES ===')
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print('\nOutput:', out.resolve())


if __name__ == '__main__':
    main()
