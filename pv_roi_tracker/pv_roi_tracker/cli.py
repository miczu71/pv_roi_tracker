"""Command-line entry point: python -m pv_roi_tracker.cli <subcommand>"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

HISTORIC_PATH = Path(os.environ.get('HISTORIC_PATH', '/data/historic.json'))
CSV_URL = os.environ.get(
    'CSV_URL',
    'https://docs.google.com/spreadsheets/d/e/'
    '2PACX-1vT_3gmj8s_2JPJzPijb9T27oh0FS3JhlQ3cB1HkKPbBa2yi8GyudqJb5ZeyM20QQ9IKbfB3SnbKZukC'
    '/pub?output=csv',
)


def cmd_import_csv(args: argparse.Namespace) -> int:
    from . import historic_store, importer, parser

    if HISTORIC_PATH.exists() and not args.force:
        print(f"Already imported ({HISTORIC_PATH}). Use --force to re-import.")
        return 0

    print(f"Fetching CSV from:\n  {CSV_URL}")
    csv_text = importer.fetch_csv(CSV_URL)

    print("Parsing pivot table …")
    records = parser.parse_csv(csv_text)

    if not records:
        print("ERROR: parser returned zero records.", file=sys.stderr)
        return 1

    years = sorted({r.year for r in records})
    print(f"Parsed {len(records)} monthly records across years: {years}")

    if HISTORIC_PATH.exists():
        bak = HISTORIC_PATH.with_suffix('.json.bak')
        print(f"Backing up existing file to {bak}")

    historic_store.save(records, HISTORIC_PATH)
    print(f"Written to {HISTORIC_PATH}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from . import historic_store

    records = historic_store.load(HISTORIC_PATH)
    if not records:
        print("No historic data. Run: python -m pv_roi_tracker.cli import-csv")
        return 1
    print(json.dumps([r.to_dict() for r in records], indent=2, ensure_ascii=False))
    return 0


def cmd_roi(args: argparse.Namespace) -> int:
    from . import historic_store, concat, roi, live_reader
    import dataclasses

    records = historic_store.load(HISTORIC_PATH)
    current = live_reader.read_current_month()
    all_records = concat.concat(records, current)
    result = roi.calculate(all_records)
    print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
    return 0


def cmd_reread_month(args: argparse.Namespace) -> int:
    """
    Ponownie odczytaj sumy miesięczne z długoterminowych statystyk HA i nadpisz
    rekord w historic.json. Używane do naprawy zerowego rekordu zapisanego przez
    month_close gdy strzelił po resecie utility_meter (błąd strefy czasowej).

    Przykład: python -m pv_roi_tracker.cli reread-month 2026-06
    """
    from . import historic_store, live_reader, rcem_scraper

    try:
        year_str, month_str = args.month.split('-')
        year, month = int(year_str), int(month_str)
    except ValueError:
        print(f'ERROR: nieprawidłowy format miesiąca: {args.month!r} (oczekiwany YYYY-MM)',
              file=sys.stderr)
        return 1

    rcem_history_path = Path(os.environ.get('RCEM_HISTORY_PATH', '/data/rcem_history.json'))
    rcem_price = None
    if rcem_history_path.exists():
        rcem_price = rcem_scraper._load_history(rcem_history_path).get(f'{year}-{month:02d}')

    print(f'Odczytuję statystyki HA dla {year}-{month:02d} (RCEm: {rcem_price})...')
    record = live_reader.read_month_from_statistics(year, month, rcem_price=rcem_price)
    if record is None:
        print('ERROR: brak danych w statystykach HA dla tego miesiąca.', file=sys.stderr)
        print('Sprawdź czy recorder ma włączone statystyki dla tych encji.', file=sys.stderr)
        return 1

    replaced = historic_store.replace_month(record, HISTORIC_PATH)
    action = 'Nadpisano' if replaced else 'Dopisano'
    print(f'{action} {year}-{month:02d}: produced={record.produced_kwh} kWh, '
          f'exported={record.exported_kwh} kWh, '
          f'self_consumed_savings={record.self_consumed_savings_pln} PLN, '
          f'feedin_revenue={record.feedin_revenue_pln} PLN (rcem_status={record.rcem_status})')
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog='pv_roi_tracker')
    sub = p.add_subparsers(dest='command')

    p_import = sub.add_parser('import-csv', help='One-shot CSV import to /data/historic.json')
    p_import.add_argument('--force', action='store_true', help='Re-import even if historic.json exists')
    p_import.set_defaults(func=cmd_import_csv)

    p_show = sub.add_parser('show', help='Print historic.json as JSON')
    p_show.set_defaults(func=cmd_show)

    p_roi = sub.add_parser('roi', help='Print current ROI calculation as JSON')
    p_roi.set_defaults(func=cmd_roi)

    p_reread = sub.add_parser(
        'reread-month',
        help='Odczytaj sumy miesięczne ze statystyk HA i nadpisz rekord w historic.json',
    )
    p_reread.add_argument('month', help='Miesiąc w formacie YYYY-MM, np. 2026-06')
    p_reread.set_defaults(func=cmd_reread_month)

    args = p.parse_args()
    if not hasattr(args, 'func'):
        p.print_help()
        sys.exit(1)

    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
