#!/usr/bin/env python3
"""Summarize Radar Scout source coverage from GitHub Actions text or ZIP logs.

This is a *diagnostic*, not proof a tender portal contains no tenders or all
available bids were captured. No API calls, no credentials or files changed.
Usage: python tools/source_coverage_report.py logs.zip --json
"""
import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

CORP = re.compile(r'\[CORP-COVERAGE\]\s+track=(\S+)\s+scope=(\S+)\s+indexed=(\d+)\s+qualified=(\d+)')
CORP_ERRORS = re.compile(r'\[CORP-COVERAGE\]\s+track=(\S+)\s+search_error=')
CORP_SUMMARY = re.compile(r'\[CORP-COVERAGE\]\s+total=(\d+)\s+direct=(\d+)\s+signals=(\d+)\s+queries=(\d+)\s+errors=(\d+)\s+raw=(\d+)')
GOV = re.compile(r'\[gov-index\]\s+(\S+)\s+(\d+) indexed item\(s\),\s+(\d+) provisional detail\(s\)',re.I)
CPPP = re.compile(r'\[cppp-index\]\s+(\S+)\s+(\d+) provisional detail[s]? from (\d+) indexed hits',re.I)
ADAPTER = re.compile(r'\[adapter\]\s+(\w+):\s+(\d+) lead\(s\)')
GATES = re.compile(r'After gates:\s+(\d+) leads survive \(from (\d+)\)')
DRY = re.compile(r'\[DRY RUN\] Would evaluate (\d+) leads')
BSE = re.compile(r'\[CORP-FILINGS\]\s+bse_rss_relevant=(\d+)')


def read_logs(path):
    path=Path(path)
    if zipfile.is_zipfile(path):
        pieces=[]; size=0
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir() or info.file_size > 8_000_000:
                    continue
                if not info.filename.lower().endswith(('.txt','.log','.out')):
                    continue
                if size + info.file_size > 35_000_000:
                    break
                pieces.append(z.read(info).decode('utf-8','replace'))
                size+=info.file_size
        return '\n'.join(pieces)
    return path.read_text(encoding='utf-8',errors='replace')[:35_000_000]


def summarize(log_text):
    report={"corporate_tracks":{},"corporate_errors":[],"corporate_summary":None,
            "government_portals":{},"cppp_hosts":{},"adapters":{},
            "bse_rss_relevant":None,"after_gates":None,"dry_run_would_evaluate":None,
            "notes":["Indexed candidates are not verified purchases; zero results do not prove source completeness."]}
    for line in log_text.splitlines():
        m=CORP.search(line)
        if m:
            key=f'{m[1]} / {m[2]}'
            old=report['corporate_tracks'].get(key,{"indexed":0,"qualified":0,"queries":0})
            old['indexed']+=int(m[3]);old['qualified']+=int(m[4]);old['queries']+=1
            report['corporate_tracks'][key]=old
        m=CORP_ERRORS.search(line)
        if m:report['corporate_errors'].append(m[1])
        m=CORP_SUMMARY.search(line)
        if m:report['corporate_summary']={k:int(v) for k,v in zip(
            ('total','direct','indirect','queries','errors','raw'),m.groups())}
        m=GOV.search(line)
        if m:
            report['government_portals'][m[1]]={"indexed":int(m[2]),"provisional":int(m[3])}
        m=CPPP.search(line)
        if m:
            report['cppp_hosts'][m[1]]={"provisional":int(m[2]),"indexed":int(m[3])}
        m=ADAPTER.search(line)
        if m:report['adapters'][m[1]]=int(m[2])
        m=BSE.search(line)
        if m:report['bse_rss_relevant']=int(m[1])
        m=GATES.search(line)
        if m:report['after_gates']={"selected":int(m[1]),"discovered":int(m[2])}
        m=DRY.search(line)
        if m:report['dry_run_would_evaluate']=int(m[1])
    report['corporate_zero_tracks']=[k for k,v in report['corporate_tracks'].items() if v['qualified']==0]
    report['government_zero_portals']=[k for k,v in report['government_portals'].items() if v['provisional']==0]
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logfile',help='GitHub Actions log ZIP, .log or .txt')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    try:
        data=summarize(read_logs(args.logfile))
    except (OSError,ValueError,zipfile.BadZipFile) as exc:
        print('Could not read log:',exc,file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data,indent=2,ensure_ascii=False))
    else:
        print('Radar Scout source coverage (observed in supplied run only)')
        print('Corporate:',data['corporate_summary'])
        print('BSE RSS:',data['bse_rss_relevant'])
        print('Zero corporate tracks:',', '.join(data['corporate_zero_tracks']) or 'none reported')
        print('CPPP:',data['cppp_hosts'])
        print('Government portal counts:',data['government_portals'])
        print('Adapter totals:',data['adapters'])
        print('Filtering:',data['after_gates'])
        print('Dry-run AI candidates:',data['dry_run_would_evaluate'])
        print(*data['notes'],sep='\n')
    return 0

if __name__ == '__main__':sys.exit(main())
