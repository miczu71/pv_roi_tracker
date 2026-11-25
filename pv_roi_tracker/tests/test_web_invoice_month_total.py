"""`_build_invoices_data`: miesiąc rozliczony dwiema fakturami niesie sumy nagłówka i taryfy."""
from pv_roi_tracker import web


def _rec(num, period, tariff, imp, exp, due):
    return {'invoice_number': num, 'billing_period_raw': period, 'tariff': tariff, 'doc_type': 'rozliczeniowa',
            'imported_kwh': imp, 'exported_kwh': exp, 'amount_due_pln': due}


def test_month_total_sums_main_and_parts():
    stored = {
        '2026-11': _rec('0022/26', '03.11.2026 - 18.11.2026', 'G13', 287.0, 39.0, 10.0),
        '2026-11~cz~0021_26': _rec('0021/26', '01.11.2026 - 02.11.2026', 'G12W', 20.5, 1.0, 2.5),
    }
    (row,) = web._build_invoices_data([], stored)
    assert row['month_total'] == {'invoices': 2, 'imported_kwh': 307.5, 'exported_kwh': 40.0, 'amount_due': 12.5,
                                  'deposit_used': None}
    assert row['tariff'] == 'G13' and row['parts'][0]['tariff'] == 'G12W'
    assert {'deposit_used', 'peak_gross', 'offpeak_gross', 'warnings_count'} <= set(row['parts'][0])


def test_month_total_absent_without_parts():
    (row,) = web._build_invoices_data([], {'2026-10': _rec('0020/26', '01.10.2026 - 31.10.2026', 'G12W', 100.0, 5.0, 1.0)})
    assert row['month_total'] is None
