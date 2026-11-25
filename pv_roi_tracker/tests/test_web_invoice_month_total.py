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


def test_g13_zone_fields_exposed_in_row_and_parts():
    g13 = dict(_rec('0022/26', '03.11.2026 - 18.11.2026', 'G13', 287.0, 39.0, 10.0),
               morning_gross=0.9048, evening_gross=1.496, other_gross=0.6257,
               imported_kwh_morning=26.0, imported_kwh_evening=12.0, imported_kwh_other=249.0,
               energy_morning_net=0.4718, energy_evening_net=0.783, energy_other_net=0.426,
               dist_var_morning_net=0.2203, dist_var_evening_net=0.3898, dist_var_other_net=0.0392)
    part = dict(g13, invoice_number='0023/26', billing_period_raw='19.11.2026 - 30.11.2026')
    (row,) = web._build_invoices_data([], {'2026-11': g13, '2026-11~cz~0023_26': part})
    for src in (row, row['parts'][0]):
        assert (src['morning_gross'], src['evening_gross'], src['other_gross']) == (0.9048, 1.496, 0.6257)
    assert row['imported_kwh_other'] == 249.0
    assert row['energy_evening_net'] == 0.783 and row['dist_var_morning_net'] == 0.2203


def test_two_zone_invoice_keeps_peak_rates_and_no_g13_values():
    inv = dict(_rec('0020/26', '01.10.2026 - 31.10.2026', 'G12W', 100.0, 5.0, 1.0),
               peak_gross=1.2304, offpeak_gross=0.6306)
    (row,) = web._build_invoices_data([], {'2026-10': inv})
    assert (row['peak_gross'], row['offpeak_gross']) == (1.2304, 0.6306)
    assert row['morning_gross'] is None and row['other_gross'] is None


def test_part_carries_detail_panel_fields():
    """Część miesiąca (FAKTURA 1/2) ma te same pola panelu szczegółów co faktura główna."""
    main = _rec('0022/26', '03.11.2026 - 18.11.2026', 'G13', 287.0, 39.0, 10.0)
    part = dict(_rec('0021/26', '01.11.2026 - 02.11.2026', 'G12W', 20.5, 1.0, 2.5),
                energy_peak_net=0.5, dist_oze_net=0.0, fixed_mocowa_net=1.25, fixed_total_net=9.5,
                deposit_current_pln=3.0, deposit_previous_pln=4.0, imported_kwh_peak=8.0,
                imported_kwh_offpeak=12.5, blended_gross=0.9, avg_price_pln_kwh=0.8)
    (row,) = web._build_invoices_data([], {'2026-11': main, '2026-11~cz~0021_26': part})
    p = row['parts'][0]
    assert p['energy_peak_net'] == 0.5 and p['fixed_mocowa_net'] == 1.25 and p['fixed_total_net'] == 9.5
    assert (p['deposit_current'], p['deposit_previous']) == (3.0, 4.0)
    assert (p['imported_kwh_peak'], p['imported_kwh_offpeak']) == (8.0, 12.5)
    assert p['blended_gross'] == 0.9 and p['avg_price'] == 0.8
    assert set(row) >= set(p) - {'key', 'amount_due', 'has_pdf'}
