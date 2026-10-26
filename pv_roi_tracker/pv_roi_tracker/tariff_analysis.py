"""
Tariff comparison: G12w (actual) vs Dynamic (HA-simulated via hourly RCE).

G12w costs: HA Statistics for sensor.koszt_zmienny_g12w_miesieczny
            (utility_meter accumulating monthly G12w variable cost).
Dynamic costs: HA Statistics for sensor.symulacja_miesieczna_dynamicznej_faktura
               (accumulated from calkowity_koszt_1_kwh_dynamiczna × energy every 15 min).

Both series are fetched via WebSocket (recorder/statistics_during_period).
Production/self-consumption context still comes from the add-on's own records.

NOTE: RCEm is NOT used here. RCEm is the prosumer SELL price for excess PV — not the
dynamic tariff BUY price.
"""

# Fallback G12w reference rates (PLN/kWh) — used by battery_sim when no parsed
# invoice rates are available.
_DEFAULT_PEAK_GROSS = 1.23
_DEFAULT_OFFPEAK_GROSS = 0.63

_MONTHS_PL = ['', 'Sty', 'Lut', 'Mar', 'Kwi', 'Maj', 'Cze',
               'Lip', 'Sie', 'Wrz', 'Paź', 'Lis', 'Gru']

def _month_label(year: int, month: int) -> str:
    return f'{year}-{_MONTHS_PL[month]}'
