"""
Statement upload & analysis engine (Phase 2) — ported from the NestEgg / KKR
Tracker Flask app into this FastAPI dashboard.

Two public entry points, both taking raw file bytes + a filename:

    analyse_pension(file_bytes, filename)      -> pension statement analysis
    analyse_investments(file_bytes, filename)  -> investment / KKR portfolio analysis

Supported formats: PDF (pdfplumber), Excel .xlsx/.xls/.xlsm (pandas + openpyxl),
CSV (pandas). The KKR "Investment Portfolio Summary" PDF is detected and parsed
by a dedicated text parser; other statements fall back to table then regex
extraction. Every holding is run through a fund-classification / benchmarking
engine, and Irish-tax-aware commentary (CGT 33%, Exit Tax 41%, SFT/CET, FX) is
generated.

No network calls: unlike the original NestEgg code this module does not enrich
holdings with live yfinance prices (the dashboard already has live quotes via the
OpenBB API). `_inv_ticker_guess` still records a best-guess ticker so the
frontend can wire up a quote if it wants to.
"""

import io
import re

# pandas / pdfplumber are only needed when a matching file type is uploaded, so
# import them lazily inside the parsers and degrade gracefully if missing.


# ══════════════════════════════════════════════════════════════════════════════
# ── FUND CLASSIFICATION / BENCHMARKING ────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def classify_holding(name):
    """Deep-classify a fund by name into type, active/passive, risk, fees, recommendation."""
    n = name.lower()

    # ── Fund type ──
    # Private equity & structured products are checked FIRST: their names often
    # contain words ("growth", "asian", "north american", "buffered") that would
    # otherwise land them in an equity bucket.
    if any(k in n for k in ['kkr', 'blackstone', 'carlyle', 'apollo', 'partners group',
                            'harbourvest', 'private equity', 'buyout', 'unlev', 'esc (']):
        ftype, benchmark = 'Private Equity / Alternatives', 'Private markets (illiquid)'
    elif (any(k in n for k in ['buffered', 'autocall', 'auto-call', 'structured note', 'barrier',
                               'kick-in', 'phoenix', 'ace di', 'wosx5', ' sx5e', 'callable', ' acs7'])
          or any(k in n for k in ['bnp', 'barclays', 'citigroup', 'goldman sachs',
                                  'morgan stanley', 'societe generale', 'deutsche bank'])):
        ftype, benchmark = 'Structured Note / Alternatives', 'Capital-protected note'
    elif any(k in n for k in ['global', 'world', 'msci world', 'all cap', 'all-world', 'international', 'ftse all']):
        ftype, benchmark = 'Global Equity', 'MSCI World (~12% 1Y)'
    elif any(k in n for k in ['us equity', 's&p', 'america', 'nasdaq', 'north america', 'united states']):
        ftype, benchmark = 'US Equity', 'S&P 500 (~23% 1Y)'
    elif any(k in n for k in ['uk equity', 'ftse 100', 'ftse100', 'ftse 250', 'britain', 'british', 'united kingdom']):
        ftype, benchmark = 'UK Equity', 'FTSE All-Share (~8% 1Y)'
    elif any(k in n for k in ['europe', 'european', 'eurozone', 'euro stoxx']):
        ftype, benchmark = 'European Equity', 'Euro Stoxx 50 (~9% 1Y)'
    elif any(k in n for k in ['emerging', 'asia', 'pacific', 'china', 'japan', 'far east', 'developing']):
        ftype, benchmark = 'Emerging/Asia Equity', 'MSCI EM (~8% 1Y)'
    elif any(k in n for k in ['bond', 'gilt', 'fixed income', 'fixed interest', 'corporate bond', 'government bond', 'treasury']):
        if 'corporate' in n:
            ftype, benchmark = 'Corporate Bonds', 'GBP Corp Bonds (~5% 1Y)'
        elif any(k in n for k in ['gilt', 'government', 'treasury']):
            ftype, benchmark = 'Government Bonds / Gilts', 'UK Gilts (~4% 1Y)'
        else:
            ftype, benchmark = 'Mixed Bonds', 'Bond index (~4-5% 1Y)'
    elif any(k in n for k in ['cash', 'money market', 'deposit', 'liquidity']):
        ftype, benchmark = 'Cash / Money Market', 'Base rate (~5% 1Y)'
    elif any(k in n for k in ['property', 'real estate', 'reit']):
        ftype, benchmark = 'Property / Real Estate', 'UK Property (~2% 1Y)'
    elif any(k in n for k in ['balanced', 'mixed', 'multi asset', 'multi-asset', 'diversified']):
        ftype, benchmark = 'Multi-Asset / Balanced', 'Balanced 60/40 (~7% 1Y)'
    elif any(k in n for k in ['lifestyle', 'target', 'drawdown', 'retirement', 'annuity']):
        ftype, benchmark = 'Lifestyle / Target Date', 'Varies by glidepath'
    elif any(k in n for k in ['small cap', 'smaller companies', 'small company']):
        ftype, benchmark = 'Small Cap Equity', 'MSCI Small Cap (~10% 1Y)'
    elif any(k in n for k in ['infrastructure', 'commodity', 'commodities', 'gold', 'energy']):
        ftype, benchmark = 'Alternatives', 'Varies'
    elif any(k in n for k in ['ethical', 'esg', 'sustainable', 'responsible', 'green', 'climate']):
        ftype, benchmark = 'ESG / Ethical Equity', 'MSCI ESG (~11% 1Y)'
    elif any(k in n for k in ['growth', 'equity', 'shares']):
        ftype, benchmark = 'General Equity', 'MSCI World (~12% 1Y)'
    else:
        ftype, benchmark = 'Unknown', '—'

    # ── Active vs Passive ──
    passive_keywords = ['index', 'tracker', 'passive', 'etf', 'vanguard', 'blackrock', 'hsbc index',
                        'legal & general index', 'l&g index', 'fidelity index', 'aviva index',
                        'ishares', 'xtrackers', 'amundi index', 'dimensional']
    active_keywords = ['active', 'managed', 'selection', 'stock pick', 'alpha', 'conviction',
                       'jupiter', 'aberdeen', 'schroders', 'fidelity active', 'm&g', 'baillie gifford',
                       'artemis', 'threadneedle', 'invesco', 'newton', 'liontrust']

    if any(k in n for k in passive_keywords):
        is_passive = True
        fee_est = '0.1–0.3%'
        fee_note = 'Low-cost index fund ✓'
    elif any(k in n for k in active_keywords):
        is_passive = False
        fee_est = '0.6–1.5%'
        fee_note = 'Active management — verify fees justify performance'
    else:
        is_passive = None
        fee_est = '0.3–0.8%'
        fee_note = 'Check fund factsheet for exact OCF'

    # ── Risk level (1=low, 5=high) ──
    risk_map = {
        'Cash / Money Market': 1,
        'Government Bonds / Gilts': 2,
        'Mixed Bonds': 2, 'Corporate Bonds': 3,
        'Multi-Asset / Balanced': 3, 'Lifestyle / Target Date': 3,
        'Property / Real Estate': 3, 'Alternatives': 3,
        'Structured Note / Alternatives': 4, 'Private Equity / Alternatives': 5,
        'UK Equity': 4, 'European Equity': 4,
        'Global Equity': 4, 'ESG / Ethical Equity': 4, 'General Equity': 4,
        'US Equity': 4, 'Emerging/Asia Equity': 5, 'Small Cap Equity': 5,
    }
    risk = risk_map.get(ftype, 3)
    risk_label = {1: 'Very Low', 2: 'Low', 3: 'Medium', 4: 'High', 5: 'Very High'}.get(risk, 'Medium')

    # ── Specific recommendation ──
    if ftype == 'Private Equity / Alternatives':
        rec = 'review'
        rec_detail = 'Private equity is illiquid (often 7–10 year lock-ups) and valued periodically, not daily. Confirm the allocation matches your horizon and that you hold enough liquid assets elsewhere. Values shown are the manager\'s estimates, not a sale price.'
    elif ftype == 'Structured Note / Alternatives':
        rec = 'review'
        rec_detail = 'Structured note — understand the capital-protection/barrier level, the underlying index, the maturity date, and the issuer\'s credit risk before holding to maturity.'
    elif ftype == 'Cash / Money Market':
        rec = 'reduce'
        rec_detail = 'Cash earns ~5% but loses to inflation long-term. Redeploy into a global equity index unless needed as an emergency buffer or you are near retirement.'
    elif ftype == 'Government Bonds / Gilts':
        rec = 'review'
        rec_detail = 'Gilts provide stability but low real returns. Appropriate if within 5 years of retirement; otherwise consider trimming in favour of equities.'
    elif ftype == 'Lifestyle / Target Date':
        rec = 'review'
        rec_detail = 'Lifestyle funds auto de-risk as you approach retirement. Ensure your target date is set correctly — an incorrect date could leave you too conservatively positioned.'
    elif ftype in ('Global Equity', 'ESG / Ethical Equity') and is_passive:
        rec = 'hold'
        rec_detail = 'Low-cost global equity index — excellent core holding. Ensure this forms the bulk of your portfolio if you are more than 10 years from retirement.'
    elif ftype in ('Global Equity', 'General Equity') and is_passive is False:
        rec = 'review'
        rec_detail = 'Active global equity fund. Compare 5-year performance net of fees against a MSCI World index fund. If it does not consistently beat the index, consider switching to a passive alternative (e.g., Vanguard Global All Cap, HSBC FTSE All World).'
    elif ftype == 'US Equity':
        rec = 'hold'
        rec_detail = 'Strong long-term performer, but concentrated in one market. If you already hold a global fund, check for heavy US overlap (global funds are ~65% US).'
    elif ftype == 'UK Equity':
        rec = 'review'
        rec_detail = 'UK equities have underperformed global markets over the past decade. Consider whether your UK allocation is justified vs a broader global fund.'
    elif ftype == 'Emerging/Asia Equity':
        rec = 'hold'
        rec_detail = 'Good diversifier with long-term growth potential. Higher volatility — keep to 10–20% of equity allocation.'
    elif ftype == 'Multi-Asset / Balanced':
        rec = 'review'
        rec_detail = 'Balanced funds are convenient but typically charge more than building your own allocation with low-cost index funds. Review the underlying holdings and total fees.'
    elif ftype == 'Property / Real Estate':
        rec = 'hold'
        rec_detail = 'Property provides inflation protection and income. Keep to 5–15% of portfolio for diversification without overconcentration.'
    elif ftype == 'Small Cap Equity':
        rec = 'hold'
        rec_detail = 'Small caps offer a long-term premium over large caps. Keep to 5–15% of equity allocation for a diversification tilt.'
    else:
        rec = 'review'
        rec_detail = 'Review this holding against your overall allocation targets and compare net-of-fee performance to an appropriate index benchmark.'

    # Passive bonus
    if is_passive and rec == 'review':
        rec = 'hold'

    # ── Suggested alternatives ──
    alternatives = {
        'Global Equity': 'Vanguard Global All Cap (VWRL), HSBC FTSE All World, iShares MSCI World',
        'US Equity': 'iShares Core S&P 500 (CSP1), Vanguard S&P 500',
        'UK Equity': 'Vanguard FTSE UK All Share, iShares Core FTSE 100',
        'Emerging/Asia Equity': 'iShares Core MSCI EM (EMIM), Vanguard FTSE Emerging Markets',
        'Government Bonds / Gilts': 'iShares Core UK Gilts (IGLT), Vanguard UK Gilt',
        'Corporate Bonds': 'iShares Core £ Corp Bond (SLXX), Vanguard GBP Corp Bond',
        'Multi-Asset / Balanced': 'Vanguard LifeStrategy 80% Equity (for growth), or build your own with index funds',
        'General Equity': 'Vanguard Global All Cap (VWRL), HSBC FTSE All World Index',
    }

    return {
        'type': ftype,
        'benchmark': benchmark,
        'isPassive': is_passive,
        'feeEst': fee_est,
        'feeNote': fee_note,
        'risk': risk,
        'riskLabel': risk_label,
        'rec': rec,
        'recDetail': rec_detail,
        'alternatives': alternatives.get(ftype),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── PENSION ANALYSIS ──────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def analyse_pension(file_bytes, filename):
    """Parse an Excel/CSV/PDF pension statement and return structured analysis."""
    import pandas as pd

    ext = filename.lower().split('.')[-1]
    sheets = {}
    try:
        if ext in ('xlsx', 'xls', 'xlsm'):
            xl = pd.ExcelFile(io.BytesIO(file_bytes))
            for name in xl.sheet_names:
                df = xl.parse(name, header=None)
                sheets[name] = df
        elif ext == 'csv':
            # header=None keeps the header line as a data row so the
            # header-row detection below can find it (read_csv would
            # otherwise consume it as column names and no holdings parse)
            df = pd.read_csv(io.BytesIO(file_bytes), header=None)
            sheets = {'Sheet1': df}
        elif ext == 'pdf':
            return _analyse_pension_pdf(file_bytes, filename)
        else:
            return {'error': f'Unsupported file type: .{ext}. Please upload a PDF, Excel (.xlsx) or CSV file.'}
    except Exception as e:
        return {'error': f'Could not read file: {e}'}

    def _pn(v):
        """Parse a number from a cell, stripping currency symbols."""
        try:
            return float(str(v).replace(',', '').replace('£', '').replace('$', '').replace('€', '').replace('%', '').strip())
        except Exception:
            return None

    # ── Detect currency from column headers and cell values ──
    # Default EUR — this is an Irish pension app; only switch on explicit markers.
    currency = 'EUR'
    for df in sheets.values():
        for r in range(min(5, len(df))):
            for c in range(len(df.columns)):
                val = df.iloc[r, c]
                if pd.isna(val):
                    continue
                s = str(val)
                if '£' in s or 'GBP' in s.upper():
                    currency = 'GBP'
                elif ('$' in s or 'USD' in s.upper()) and currency == 'EUR':
                    currency = 'USD'

    # ── Structured column-based parsing ──
    COL_PATTERNS = {
        'name': ['description', 'name', 'fund name', 'holding', 'security', 'asset name'],
        'value': ['market value', 'market val', 'mv', 'current value', 'value('],
        'cost': ['book cost', 'cost', 'purchase cost', 'original cost'],
        'gainpct': ['% gain', 'gain or loss', 'gain/loss', '% return', 'gain%', 'performance'],
        'qty': ['quantity', 'qty', 'units', 'shares', 'number'],
        'isin': ['isin'],
        'type': ['type', 'asset class', 'asset type', 'category'],
        'price': ['market price', 'price', 'nav', 'unit price'],
        'exch': ['exchange currency', 'currency'],
    }

    def find_col(headers, patterns):
        for i, h in enumerate(headers):
            h_low = str(h).lower().strip()
            if any(p in h_low for p in patterns):
                return i
        return None

    holdings = []
    seen_isins = set()
    skip_words = {'warning', 'past performance', 'total', 'grand total', 'cash total',
                  'portfolio total', 'subtotal', 'nan', 'none', ''}

    for sheet_name, df in sheets.items():
        # Find header row (first row with 2+ recognisable column names)
        header_row = None
        for r in range(min(6, len(df))):
            row_low = [str(df.iloc[r, c]).lower().strip() for c in range(len(df.columns))]
            hits = sum(1 for v in row_low if any(p in v for p in
                       ['description', 'name', 'value', 'quantity', 'qty', 'isin', 'cost', 'type', 'price', 'return', 'gain', 'currency']))
            if hits >= 2:
                header_row = r
                break
        if header_row is None:
            continue

        headers = [str(df.iloc[header_row, c]) for c in range(len(df.columns))]
        ci = {k: find_col(headers, v) for k, v in COL_PATTERNS.items()}

        if ci['name'] is None and ci['value'] is None:
            continue

        for r in range(header_row + 1, len(df)):
            row = [df.iloc[r, c] for c in range(len(df.columns))]
            row_s = [str(v).strip() for v in row]
            row_text = ' '.join(row_s).lower()

            # Skip blank / disclaimer rows
            if len(row_text.strip()) < 3 or 'warning' in row_text or 'past performance' in row_text:
                continue

            name = row_s[ci['name']].strip() if ci['name'] is not None else ''
            if not name or name.lower() in skip_words or len(name) < 3:
                continue

            isin = row_s[ci['isin']].strip() if ci['isin'] is not None else ''
            isin = isin if isin not in ('nan', 'None', '') else None

            # Deduplicate across sheets by ISIN
            if isin:
                if isin in seen_isins:
                    continue
                seen_isins.add(isin)

            value = _pn(row[ci['value']]) if ci['value'] is not None else None
            cost = _pn(row[ci['cost']]) if ci['cost'] is not None else None
            gain_pct = _pn(row[ci['gainpct']]) if ci['gainpct'] is not None else None
            qty = _pn(row[ci['qty']]) if ci['qty'] is not None else None
            price = _pn(row[ci['price']]) if ci['price'] is not None else None
            asset_type = row_s[ci['type']].strip() if ci['type'] is not None else ''
            exch_curr = row_s[ci['exch']].strip() if ci['exch'] is not None else ''

            if value is None or value == 0:
                continue

            holdings.append({
                'name': name,
                'value': value,
                'bookCost': cost,
                'gainPct': gain_pct,   # % gain/loss since purchase (from file)
                'pct': None,           # allocation %, filled in below
                'return1y': None,      # period returns not in this file
                'quantity': qty,
                'price': price,
                'isin': isin,
                'assetType': asset_type,
                'exchCurrency': exch_curr,
            })

    # ── Total = sum of market values ──
    total_value = round(sum(h['value'] for h in holdings), 2) if holdings else None

    # ── Allocation % per holding ──
    if total_value and total_value > 0:
        for h in holdings:
            h['pct'] = round((h['value'] / total_value) * 100, 2)

    # ── Asset class breakdown from the Type column ──
    type_map = {
        'private equity': 'alternatives',
        'other alternatives': 'alternatives',
        'alternatives': 'alternatives',
        'cash fund': 'cash',
        'cash': 'cash',
        'money market': 'cash',
        'multi-asset funds of funds': 'balanced',
        'multi-asset': 'balanced',
        'balanced': 'balanced',
        'equity': 'equities',
        'bond': 'bonds',
        'fixed': 'bonds',
        'property': 'property',
    }
    detected_classes = {}
    for h in holdings:
        at = h.get('assetType', '').lower().strip()
        cls = next((v for k, v in type_map.items() if k in at), None)
        if cls is None:
            nm = h['name'].lower()
            if any(k in nm for k in ['equity', 'stock', 'shares', 'growth', 'global', 'world']):
                cls = 'equities'
            elif any(k in nm for k in ['bond', 'gilt', 'fixed']):
                cls = 'bonds'
            elif any(k in nm for k in ['cash', 'liquidity', 'money market']):
                cls = 'cash'
            elif any(k in nm for k in ['balanced', 'multi', 'mixed']):
                cls = 'balanced'
            else:
                cls = 'alternatives'
        pct = h.get('pct') or 0
        detected_classes[cls] = round(detected_classes.get(cls, 0) + pct, 2)

    # ── Classify each holding for recommendations ──
    BENCHMARK_TICKERS = {
        'Global Equity': 'VWRL.L',
        'US Equity': 'CSPX.L',
        'UK Equity': 'ISF.L',
        'European Equity': 'IMEU.L',
        'Emerging/Asia Equity': 'EMIM.L',
        'Government Bonds / Gilts': 'IGLT.L',
        'Corporate Bonds': 'SLXX.L',
        'Multi-Asset / Balanced': 'VAGP.L',
        'Property / Real Estate': 'IPRP.L',
        'ESG / Ethical Equity': 'VWRL.L',
        'Small Cap Equity': 'WSML.L',
        'General Equity': 'VWRL.L',
    }
    for h in holdings:
        cl = classify_holding(f"{h.get('assetType', '')} {h['name']}")
        h['fundType'] = cl['type']
        h['benchmark'] = cl['benchmark']
        h['risk'] = cl['risk']
        h['riskLabel'] = cl['riskLabel']
        h['rec'] = cl['rec']
        h['recDetail'] = cl['recDetail']
        h['feeEst'] = cl['feeEst']
        h['isPassive'] = cl['isPassive']
        h['benchmarkTicker'] = BENCHMARK_TICKERS.get(cl['type'])

    # ── Recommendations & warnings ──
    warnings_list = []
    positives = []

    alt_pct = detected_classes.get('alternatives', 0)
    cash_pct = detected_classes.get('cash', 0)
    bal_pct = detected_classes.get('balanced', 0)
    eq_pct = detected_classes.get('equities', 0)

    if alt_pct > 40:
        warnings_list.append({'level': 'warn', 'text': f'High alternatives/private equity exposure ({alt_pct:.0f}%). These are illiquid and harder to value — ensure you are comfortable with lock-up periods and valuation uncertainty.'})
    if cash_pct > 10:
        warnings_list.append({'level': 'info', 'text': f'Cash allocation of {cash_pct:.0f}%. Money market funds preserve capital but may lag inflation over the long term.'})
    if len(holdings) > 0:
        positives.append(f'Portfolio of {len(holdings)} holdings across {len(detected_classes)} asset classes detected.')

    # Gain/loss insights
    best = max(holdings, key=lambda h: h.get('gainPct') or -999, default=None)
    worst = min(holdings, key=lambda h: h.get('gainPct') or 999, default=None)
    if best and best.get('gainPct') is not None and best['gainPct'] > 0:
        positives.append(f'Best performer: {best["name"][:40]} (+{best["gainPct"]:.1f}% since purchase).')
    if worst and worst.get('gainPct') is not None and worst['gainPct'] < 0:
        warnings_list.append({'level': 'warn', 'text': f'{worst["name"][:40]} is down {worst["gainPct"]:.1f}% since purchase — review whether the thesis still holds.'})

    recommendations = [
        {'priority': 'high', 'title': 'Review private equity liquidity', 'detail': 'Private equity funds (like KKR) typically have 7–10 year lock-up periods and are valued quarterly. Ensure this illiquidity matches your investment horizon and that you have sufficient liquid assets elsewhere.'},
        {'priority': 'high', 'title': 'Understand your structured products', 'detail': 'Structured notes (BNP, Citigroup) have complex payoff profiles tied to indices like EuroStoxx 50. Understand the maturity date, capital protection level, and counterparty risk before holding to maturity.'},
        {'priority': 'medium', 'title': 'Benchmark the Davy funds', 'detail': 'Compare the Davy Long Term Growth funds against a low-cost MSCI World ETF (e.g. VWRL) net of fees. If underperforming consistently, switching to a passive alternative could significantly improve long-term returns.'},
        {'priority': 'medium', 'title': 'Assess currency exposure', 'detail': 'KKR funds are USD-denominated but valued in EUR. This creates FX risk — a strengthening EUR reduces your returns in euro terms. Consider whether this currency exposure is intentional.'},
        {'priority': 'low', 'title': 'Maximise tax-advantaged contributions', 'detail': 'Pension contributions receive income tax relief at your marginal rate (up to 40% in Ireland). Ensure you are contributing the maximum allowed given your age and income.'},
        {'priority': 'low', 'title': 'Diversify into public markets', 'detail': 'A significant portion of this portfolio is in private/alternative assets. Adding low-cost global equity index funds could improve liquidity and long-term diversification.'},
    ]

    # Gain-based returns for chart (since no period returns in file)
    gain_returns = {}
    for h in holdings:
        if h.get('gainPct') is not None:
            short_name = h['name'][:25] + ('…' if len(h['name']) > 25 else '')
            gain_returns[short_name] = h['gainPct']

    if not holdings:
        return {'error': 'No fund holdings found. Ensure the file has recognisable column headers such as "Description", "Market Value", "ISIN".'}

    return {
        'sheets': list(sheets.keys()),
        'holdings': holdings,
        'totalValue': total_value,
        'currency': currency,
        'assetClasses': detected_classes,
        'returns': {},            # no period returns in this file format
        'gainReturns': gain_returns,  # % gain per holding since purchase
        'annualContrib': None,
        'warnings': warnings_list,
        'positives': positives,
        'recommendations': recommendations,
    }


def _analyse_pension_pdf(file_bytes, filename='statement.pdf'):
    """Extract pension holdings from a PDF.

    Delegates to analyse_investments (which handles KKR/Davy/generic PDF formats)
    then converts the result into pension analysis format.
    """
    inv = analyse_investments(file_bytes, filename)
    if inv.get('error'):
        return inv

    inv_holdings = inv.get('holdings') or []
    if not inv_holdings:
        return {'error': 'No holdings found in this document. Please check it is a complete account statement.'}

    FX = {'GBP': 1.17, 'USD': 0.92, 'EUR': 1.0}
    doc_currency = (inv.get('currency') or 'EUR').upper()
    doc_fx = FX.get(doc_currency[:3], 1.0)

    CAT_MAP = {
        'Private Equity / Alternatives': 'Alternatives',
        'Pooled / Unallocated': 'Alternatives',
        'Structured Note': 'Alternatives',
        'Individual Equity': 'Equities',
        'Global Equity ETF': 'Equities',
        'US Equity ETF': 'Equities',
        'ETF': 'Equities',
        'Bond': 'Bonds',
        'Bond ETF': 'Bonds',
        'Cash': 'Cash',
        'Managed Fund': 'Balanced',
    }

    mapped = []
    for h in inv_holdings:
        raw_val = h.get('value') or 0
        h_currency = (h.get('exchCurrency') or doc_currency).upper()
        val_eur = raw_val * FX.get(h_currency[:3], doc_fx)
        if val_eur <= 0:
            continue
        name = (h.get('name') or 'Unknown Fund').strip()
        cat = CAT_MAP.get(h.get('category') or '', h.get('category') or 'Fund')
        mapped.append({
            'name': name,
            'value_eur': round(val_eur, 2),
            'allocation': h.get('pct') or 0,
            'ter': None,
            'category': cat,
            'isin': h.get('isin'),
            'gainPct': h.get('gainPct'),
        })

    if not mapped:
        return {'error': 'Could not convert holdings to EUR values. Please try an Excel or CSV export of your statement.'}

    total_eur = sum(h['value_eur'] for h in mapped)
    for h in mapped:
        h['allocation'] = round(h['value_eur'] / total_eur * 100, 1) if total_eur else 0

    # Detect provider from text
    all_text = ' '.join(inv.get('sectionsText') or []).upper()
    provider = None
    for p in ['DAVY', 'IRISH LIFE', 'ZURICH', 'AVIVA', 'STANDARD LIFE', 'NEW IRELAND', 'MERCER']:
        if p in all_text:
            provider = p.title()
            break
    if not provider and 'KKR' in ' '.join(str(h.get('name', '')) for h in mapped).upper():
        provider = 'Davy / KKR'

    return _build_pension_result(mapped, total_eur, provider, inv.get('reportDate'))


def _build_pension_result(holdings, total_value, provider=None, statement_date=None):
    """Build a rich pension analysis — same depth as the KKR tracker."""

    def fmt(n):
        return f"€{n:,.0f}"

    # ── 1. Classify every holding with the full classify_holding engine ─────
    for h in holdings:
        cl = classify_holding(f"{h.get('category', '')} {h.get('name', '')}")
        h['fundType'] = cl['type']
        h['benchmark'] = cl['benchmark']
        h['risk'] = cl['risk']
        h['riskLabel'] = cl['riskLabel']
        h['rec'] = cl['rec']
        h['recDetail'] = cl['recDetail']
        h['feeEst'] = cl['feeEst']
        h['isPassive'] = cl['isPassive']

    # ── 2. Asset class breakdown ───────────────────────────────────────────
    TYPE_TO_CLASS = {
        'Cash / Money Market': 'cash',
        'Government Bonds / Gilts': 'bonds',
        'Mixed Bonds': 'bonds',
        'Corporate Bonds': 'bonds',
        'Multi-Asset / Balanced': 'balanced',
        'Lifestyle / Target Date': 'balanced',
        'Property / Real Estate': 'property',
        'Alternatives': 'alternatives',
        'Private Equity / Alternatives': 'alternatives',
        'Structured Note / Alternatives': 'alternatives',
        'Global Equity': 'equities',
        'US Equity': 'equities',
        'UK Equity': 'equities',
        'European Equity': 'equities',
        'Emerging/Asia Equity': 'equities',
        'ESG / Ethical Equity': 'equities',
        'Small Cap Equity': 'equities',
        'General Equity': 'equities',
    }
    class_totals = {}
    for h in holdings:
        cls = TYPE_TO_CLASS.get(h.get('fundType', ''), 'alternatives')
        class_totals[cls] = round(class_totals.get(cls, 0) + (h.get('value_eur') or 0), 2)

    def pct_of(cls):
        return class_totals.get(cls, 0) / total_value * 100 if total_value else 0

    # ── 3. Fee analysis ───────────────────────────────────────────────────
    avg_ter = None
    ters = [h['ter'] for h in holdings if h.get('ter') is not None]
    if ters:
        avg_ter = round(sum(ters) / len(ters), 3)

    # ── 4. Gain / performance insight ────────────────────────────────────
    with_gains = [h for h in holdings if h.get('gainPct') is not None]
    best = max(with_gains, key=lambda h: h['gainPct'], default=None)
    worst = min(with_gains, key=lambda h: h['gainPct'], default=None)

    # ── 5. Irish peer benchmarks (CSO / Pension Authority 2024) ──────────
    MEDIAN_PENSION = 55_000
    TOP10_PENSION = 180_000
    TOP5_PENSION = 280_000
    TOP1_PENSION = 500_000

    def peer_pct(val):
        if val >= TOP1_PENSION:
            return 'top 1% of Irish pension savers'
        if val >= TOP5_PENSION:
            return 'top 5% of Irish pension savers'
        if val >= TOP10_PENSION:
            return 'top 10% of Irish pension savers'
        if val >= MEDIAN_PENSION * 2:
            return 'top 20% of Irish pension savers'
        if val >= MEDIAN_PENSION:
            return 'above the Irish median'
        return 'below the Irish median'

    peer_label = peer_pct(total_value) if total_value else None

    # ── 6. SFT projections ────────────────────────────────────────────────
    SFT_SCHEDULE = {2024: 2_000_000, 2025: 2_150_000, 2026: 2_200_000,
                    2027: 2_400_000, 2028: 2_600_000}
    SFT_MAX = 2_800_000
    CURRENT_YEAR = 2026

    def sft_in_year(yr):
        return SFT_SCHEDULE.get(yr, SFT_MAX)

    GROWTH = 0.07  # 7% nominal

    def fv_grow(pv, n, r=GROWTH):
        return pv * (1 + r) ** n if n > 0 else pv

    sft_warnings = []
    proj_10yr = fv_grow(total_value, 10)
    proj_20yr = fv_grow(total_value, 20)
    sft_10yr = sft_in_year(CURRENT_YEAR + 10)
    sft_20yr = SFT_MAX
    if proj_10yr > sft_10yr:
        excess = proj_10yr - sft_10yr
        sft_warnings.append({
            'type': 'warning',
            'title': 'SFT breach possible within 10 years',
            'body': f'At 7% growth, your pension could reach {fmt(proj_10yr)} in 10 years — above the then-SFT of {fmt(sft_10yr)}. A 40% Chargeable Excess Tax on {fmt(excess)} would cost {fmt(excess*0.40)}. Consider splitting into a PRSA and an occupational scheme, or slowing contributions.'
        })
    elif proj_20yr > sft_20yr:
        sft_warnings.append({
            'type': 'info',
            'title': 'SFT on the horizon (20-year horizon)',
            'body': f'Projected pension of {fmt(proj_20yr)} in 20 years approaches the long-term SFT cap of {fmt(SFT_MAX)}. Monitor contributions annually.'
        })

    # ── 7. Build recommendations ──────────────────────────────────────────
    recs = []

    if avg_ter is not None:
        if avg_ter > 1.5:
            recs.append({'type': 'warning', 'title': 'High fees — action required',
                         'body': f'Average annual charge: {avg_ter:.2f}%. Over 20 years this erodes tens of thousands. Ask your provider for passive/index alternatives charging 0.2–0.5%.'})
        elif avg_ter > 0.75:
            recs.append({'type': 'info', 'title': 'Room to reduce fees',
                         'body': f'Average charge: {avg_ter:.2f}%. Passive index funds typically cost 0.2–0.5%. Switching could add significantly to your pot at retirement.'})
        else:
            recs.append({'type': 'success', 'title': 'Competitive fees ✓',
                         'body': f'Average annual charge of {avg_ter:.2f}% is low. Keep reviewing — providers occasionally increase charges.'})

    cash_pct = pct_of('cash')
    alt_pct = pct_of('alternatives')
    eq_pct = pct_of('equities')
    bond_pct = pct_of('bonds')

    if cash_pct > 10:
        recs.append({'type': 'warning', 'title': f'High cash allocation ({cash_pct:.0f}%)',
                     'body': 'Money market / cash funds preserve capital but lose value to inflation over time. Unless you are within 3 years of retirement, consider redeploying into a diversified equity fund.'})
    if alt_pct > 40:
        recs.append({'type': 'warning', 'title': f'High alternatives / private equity ({alt_pct:.0f}%)',
                     'body': 'Private equity and structured products are illiquid and valued infrequently. Ensure this matches your liquidity needs and that you understand any lock-up periods.'})
    if bond_pct > 60:
        recs.append({'type': 'info', 'title': f'Conservative bond allocation ({bond_pct:.0f}%)',
                     'body': 'Heavy bond weighting limits long-term growth. If retirement is 10+ years away, consider increasing equity exposure — historically equities outperform bonds over long horizons.'})
    if eq_pct > 85:
        recs.append({'type': 'info', 'title': f'High equity concentration ({eq_pct:.0f}%)',
                     'body': 'Fully equity-invested is appropriate for long time horizons, but consider whether some allocation to bonds or property would reduce volatility as you near retirement.'})

    review_funds = [h for h in holdings if h.get('rec') == 'review' and (h.get('value_eur') or 0) > 0]
    if review_funds:
        names = ', '.join(h['name'][:30] for h in review_funds[:3])
        recs.append({'type': 'info', 'title': 'Funds to review',
                     'body': f'These holdings warrant closer inspection: {names}. Compare their performance net of fees against a simple index fund over 3–5 years.'})

    if best and best.get('gainPct', 0) > 0:
        recs.append({'type': 'success', 'title': f'Best performer: {best["name"][:35]}',
                     'body': f'Up {best["gainPct"]:.1f}% since purchase. Ensure this gain reflects skill, not just market beta, before increasing the position.'})
    if worst and worst.get('gainPct', 0) < -10:
        recs.append({'type': 'warning', 'title': f'Underperformer: {worst["name"][:35]}',
                     'body': f'Down {worst["gainPct"]:.1f}% since purchase. Review whether the investment thesis still holds or whether switching to a lower-cost alternative makes sense.'})

    recs.extend(sft_warnings)

    if peer_label:
        recs.append({'type': 'success' if 'top' in peer_label else 'info',
                     'title': f'Peer comparison: {peer_label}',
                     'body': f'Your pension of {fmt(total_value)} puts you in the {peer_label} based on CSO / Pensions Authority 2024 data. Irish median pension pot is around {fmt(MEDIAN_PENSION)}.'})

    if any('davy' in (h.get('name', '') or '').lower() for h in holdings):
        recs.append({'type': 'info', 'title': 'Davy GPS funds: benchmark them',
                     'body': 'Compare the Davy Long Term Growth funds against a low-cost MSCI World ETF (e.g. Vanguard VWRL) net of all fees over 3–5 years. If consistently underperforming, a switch to passive could meaningfully improve your outcome.'})

    if not recs:
        recs.append({'type': 'success', 'title': 'Looking good',
                     'body': 'Your pension appears well-structured. Keep reviewing fees and allocation annually — small adjustments compound significantly over a career.'})

    # ── 8. Build positives summary ───────────────────────────────────────
    positives = []
    if total_value >= TOP10_PENSION:
        positives.append(f'Strong pension pot of {fmt(total_value)} — {peer_label}.')
    if len(holdings) >= 3:
        positives.append(f'{len(holdings)} funds across {len(class_totals)} asset class{"es" if len(class_totals)>1 else ""} — reasonable diversification.')
    if best and best.get('gainPct', 0) > 5:
        positives.append(f'Best performer up {best["gainPct"]:.1f}% since purchase.')

    return {
        'holdings': holdings,
        'totalValue': round(total_value, 2),
        'avgTer': avg_ter,
        'assetClasses': class_totals,
        'recommendations': recs,
        'positives': positives,
        'peerLabel': peer_label,
        'provider': provider,
        'statementDate': statement_date,
        'proj10yr': round(proj_10yr),
        'proj20yr': round(proj_20yr),
        'sft10yr': sft_10yr,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── PERSONAL INVESTMENTS ANALYSIS ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _inv_pn(s):
    """Parse number from string, handles €/$/£ symbols, commas, parentheses for negatives."""
    if s is None:
        return None
    s = str(s).strip()
    neg = s.startswith('(') and s.endswith(')')
    s = re.sub(r'[£$€,\s]', '', s).replace('(', '').replace(')', '').replace('%', '').strip()
    try:
        v = float(s)
        return -v if neg else v
    except Exception:
        return None


def _inv_currency(texts):
    """Detect primary currency from a list of strings."""
    combined = ' '.join(str(t) for t in texts).upper()
    counts = {
        'EUR': combined.count('€') + combined.count('EUR'),
        'USD': combined.count('$') + combined.count('USD'),
        'GBP': combined.count('£') + combined.count('GBP'),
    }
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else 'EUR'


def _inv_find_date(texts):
    """Try to find a report date from text."""
    combined = ' '.join(str(t) for t in texts)
    patterns = [
        r'(?:as at|as of|valued at|valuation date)[:\s]+(\d{1,2}[\/\-\. ]\w+[\/\-\. ]\d{4})',
        r'(?:as at|as of|valued at|valuation date)[:\s]+(\w+ \d{1,2},? \d{4})',
        r'(?:quarter|period) end(?:ed|ing)?[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})',
        r'\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})\b',
        r'\b(\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d{4})\b',
        r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d{4})\b',
    ]
    for pat in patterns:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _inv_extract_pdf(file_bytes):
    """Extract structured data from a PDF investment statement using pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        return None, None, 'PDF parsing needs pdfplumber (pip install pdfplumber).'

    all_text = []
    all_tables = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ''
                all_text.append(txt)
                for tbl in (page.extract_tables() or []):
                    if tbl and len(tbl) > 1:
                        all_tables.append(tbl)
    except Exception as e:
        return None, None, f'PDF parse error: {e}'

    # ── Detect KKR portfolio summary format ──
    combined = '\n'.join(all_text).upper()
    if 'INVESTMENT PORTFOLIO SUMMARY' in combined and 'KKR' in combined:
        kkr_holdings = _inv_parse_kkr_text(all_text)
        if kkr_holdings:
            # 4-tuple signals a pre-parsed format
            return all_text, None, None, {'kind': 'kkr', 'holdings': kkr_holdings}

    # ── Detect Davy / Irish pension "Portfolio Valuation" format ──
    if 'PORTFOLIO VALUATION' in combined and 'BOOK COST' in combined and 'MARKET VALUE' in combined:
        davy_holdings = _inv_parse_davy_valuation(all_text)
        if davy_holdings:
            return all_text, None, None, {'kind': 'davy', 'holdings': davy_holdings}

    return all_text, all_tables, None


_KKR_AMT = r'\$\s*([\d,]+(?:\.\d+)?|-|\([\d,]+(?:\.\d+)?\))'
_KKR_ROW = re.compile(
    r'^(.+?)\s+' + (_KKR_AMT + r'\s+') * 5 + _KKR_AMT + r'\s*$'
)
_KKR_SKIP = {
    'fund expenses', 'export date', 'page ',
}
_KKR_POOLED = {'pooled - interest income', 'pooled contributions'}


def _kkr_parse_amt(s):
    s = s.strip()
    if not s or s == '-':
        return 0.0
    neg = s.startswith('(')
    s = s.replace('(', '').replace(')', '').replace(',', '')
    try:
        return -float(s) if neg else float(s)
    except Exception:
        return 0.0


def _inv_parse_kkr_text(all_text):
    """Parse KKR Investment Portfolio Summary PDF text into holdings."""
    holdings = []
    current_fund = None
    doc_total = None   # document-stated grand total
    fund_short = {
        'european': 'KKR European Fund VI',
        'infrastructure': 'KKR Global Infrastructure IV',
        'north america': 'KKR North America Fund XIII',
    }

    for page_text in all_text:
        for raw_line in page_text.split('\n'):
            line = raw_line.strip()
            if not line:
                continue

            low = line.lower()

            # ── Capture grand total row ──
            if low.startswith('total') and ('discretionary' in low or 'committed' in low):
                m = _KKR_ROW.match(line)
                if m and 'discretionary' in low:
                    doc_total = _kkr_parse_amt(m.group(6))
                continue

            # ── Detect fund header ──
            if low.startswith('kkr') and not low.startswith('total'):
                for key, short in fund_short.items():
                    if key in low:
                        current_fund = short
                        break
                continue

            # ── Skip header / total / noise rows ──
            if (low.startswith('total') or
                    any(sk in low for sk in _KKR_SKIP) or
                    low.startswith('investment portfolio') or
                    low.startswith('tomas') or
                    low.startswith('investor') or
                    low.startswith('as of') or
                    low.startswith('discretionary') or
                    low.startswith('committed funds') or
                    low.startswith('amount') or
                    low.startswith('daw')):
                continue

            # ── Try to match a 6-dollar-column row ──
            m = _KKR_ROW.match(line)
            if not m:
                continue

            name = m.group(1).strip()
            if len(name) < 2:
                continue

            amt_invested = _kkr_parse_amt(m.group(2))  # Amount Invested
            cost_basis = _kkr_parse_amt(m.group(3))    # Remaining Cost Basis
            amt_realized = _kkr_parse_amt(m.group(4))  # Amount Realized
            rem_value = _kkr_parse_amt(m.group(5))     # Remaining Value
            total_value = _kkr_parse_amt(m.group(6))   # Total Value
            net_gain = _kkr_parse_amt(m.group(7))      # Net Gain/(Loss)

            if total_value == 0 and rem_value == 0 and amt_invested == 0:
                continue

            # Pooled items: real value but not portfolio companies
            is_pooled = any(p in low for p in _KKR_POOLED)
            effective_cost = cost_basis if cost_basis else amt_invested
            gain_pct = round((net_gain / effective_cost) * 100, 1) \
                if effective_cost and effective_cost != 0 else None

            holdings.append({
                'name': name,
                'isin': None,
                'fund': current_fund or 'KKR',
                'value': total_value,
                'bookCost': effective_cost,
                'gainPct': gain_pct,
                'gainAbs': net_gain,
                'amtInvested': amt_invested,
                'amtRealized': amt_realized,
                'qty': None,
                'price': None,
                'weight': None,
                'exchCurrency': 'USD',
                'category': 'Pooled / Unallocated' if is_pooled else 'Private Equity / Alternatives',
                'risk': 2 if is_pooled else 5,
                'riskLabel': 'Low' if is_pooled else 'Very High',
                'comment': ('Undeployed capital / accrued interest within the fund'
                            if is_pooled else
                            f'KKR private equity holding — {current_fund}'),
            })

    # Store document total for use in analysis
    if doc_total and holdings:
        holdings[0]['_doc_total'] = doc_total

    return holdings


# ── Davy / Irish "Portfolio Valuation" statements ──────────────────────────────
# These group holdings under category headers (Private Equity, Other Alternatives,
# Multi-Asset Funds of Funds, Cash …), carry no ISINs, report every market value
# already converted to EUR, and wrap long fund names onto a second line. Row shape:
#   <quantity> <name> <book cost> <market price>[ CCY^] <accrued|-> <market value> <%gain> <%total>
_DAVY_ROW = re.compile(
    r'^\s*([\d,]+(?:\.\d+)?)\s+'      # quantity
    r'(.+?)\s+'                        # name (part 1 — may continue on next line)
    r'([\d,]+)\s+'                     # book cost (EUR)
    r'([\d,]+\.\d+)\s+'                # market price
    r'(?:(USD|EUR|GBP)\s*)?\^?\s*'     # optional price currency + estimated-value caret
    r'(-|[\d,]+(?:\.\d+)?)\s+'         # accrued income (often "-")
    r'([\d,]+)\s+'                     # market value (EUR)
    r'(-?\d+(?:\.\d+)?)\s+'            # % gain/loss since inception
    r'(-?\d+(?:\.\d+)?)\s*$'           # % of total value
)
# Category-header lines that must never be swallowed as a name continuation.
_DAVY_CATS = {
    'private equity', 'other alternatives', 'multi-asset funds of funds',
    'equities', 'bonds', 'fixed income', 'property', 'cash',
    'trading accounts', 'deposit accounts',
}
_DAVY_CONT_SKIP = ('total', 'deposit', 'trading', '**', '^', 'exchange', 'maturity')


def _davy_num(s):
    s = (s or '').replace(',', '').strip()
    if s in ('', '-'):
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _inv_parse_davy_valuation(all_text):
    """Parse a Davy / Irish pension 'Portfolio Valuation' PDF into holdings."""
    lines = '\n'.join(all_text).split('\n')
    holdings = []
    for i, raw in enumerate(lines):
        m = _DAVY_ROW.match(raw.strip())
        if not m:
            continue
        qty, name1, book, price, ccy, accrued, mv, gain, total = m.groups()
        market_value = _davy_num(mv)
        if market_value <= 0:
            continue

        # Long fund names wrap onto the following line — append it unless that
        # line is really the next category header, a footer, or a duplicate.
        cont = ''
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if (nxt and not _DAVY_ROW.match(nxt) and nxt.lower() not in _DAVY_CATS
                    and not nxt.lower().startswith(_DAVY_CONT_SKIP)):
                cont = nxt
        name = name1.strip() if (cont and cont in name1) else (name1.strip() + ' ' + cont).strip()

        cl = _inv_classify_stock(name)
        holdings.append({
            'name': name,
            'isin': None,
            'value': market_value,
            'bookCost': _davy_num(book) or None,
            'gainPct': float(gain),
            'qty': _davy_num(qty) or None,
            'price': _davy_num(price) or None,
            'weight': _davy_num(total) or None,
            'exchCurrency': 'EUR',   # market values are already reported in EUR
            'category': cl['category'],
            'risk': cl['risk'],
            'riskLabel': cl['riskLabel'],
            'comment': cl['comment'],
        })
    return holdings


def _inv_find_col(headers, patterns):
    """Find column index by matching header text against patterns."""
    for i, h in enumerate(headers):
        h_low = str(h).lower().strip()
        if any(p in h_low for p in patterns):
            return i
    return None


ISIN_RE = re.compile(r'\b([A-Z]{2}[A-Z0-9]{10})\b')


def _inv_extract_isins(text):
    """Find all ISIN codes in a block of text."""
    return ISIN_RE.findall(text.upper())


def _inv_classify_stock(name, isin=''):
    """Classify a named stock / ETF / structured note / fund for the investments tab."""
    n = (name or '').lower()
    n_up = (name or '').upper()

    # ── Structured note / bond ──
    structured_keywords = ['bnp', 'citigroup', 'citi ', 'goldman sachs', 'barclays', 'hsbc structured',
                           'morgan stanley', 'jpmorgan', 'deutsche bank', 'societe generale',
                           'autocall', 'kick-in', 'barrier', 'accelerated', 'callable', 'phoenix']
    index_codes = ['sx5e', 'spx', 'ukx', 'ndx', 'nikkei', 'stoxx', 'eurostoxx']
    if any(k in n for k in structured_keywords) or any(k in n for k in index_codes):
        return {'category': 'Structured Note', 'risk': 4, 'riskLabel': 'High',
                'comment': 'Structured product — understand capital protection level, barrier, and maturity date.'}

    # ── ETFs ──
    etf_issuers = ['ishares', 'vanguard', 'xtrackers', 'amundi', 'spdr', 'invesco etf', 'lyxor',
                   'wisdomtree', 'vaneck', 'global x', 'proshares']
    if any(k in n for k in etf_issuers) or n.endswith('etf') or 'ucits' in n:
        if any(k in n for k in ['world', 'global', 'all-world', 'all cap', 'msci world']):
            return {'category': 'Global Equity ETF', 'risk': 4, 'riskLabel': 'High',
                    'comment': 'Diversified global equity ETF — strong core holding.'}
        if any(k in n for k in ['s&p', 'sp500', 'us equity', 'america', 'nasdaq']):
            return {'category': 'US Equity ETF', 'risk': 4, 'riskLabel': 'High',
                    'comment': 'US equity ETF — strong historical performer but concentrated in one market.'}
        if any(k in n for k in ['bond', 'gilt', 'fixed income', 'treasury']):
            return {'category': 'Bond ETF', 'risk': 2, 'riskLabel': 'Low-Medium',
                    'comment': 'Fixed income ETF — provides stability and income.'}
        return {'category': 'ETF', 'risk': 3, 'riskLabel': 'Medium',
                'comment': 'Exchange-traded fund — review underlying exposure and expense ratio.'}

    # ── Private equity / alternatives ──
    pe_keywords = ['kkr', 'blackstone', 'carlyle', 'apollo', 'partners group', 'harbourvest',
                   'private equity', 'infrastructure', 'alternatives', 'venture']
    if any(k in n for k in pe_keywords):
        return {'category': 'Private Equity / Alternatives', 'risk': 5, 'riskLabel': 'Very High',
                'comment': 'Private/alternative asset — illiquid, valued periodically. Ensure you understand lock-up terms.'}

    # ── Davy / managed funds ──
    davy_keywords = ['davy', 'zurich', 'irish life', 'aviva', 'standard life', 'new ireland', 'friends first']
    if any(k in n for k in davy_keywords):
        return {'category': 'Managed Fund', 'risk': 3, 'riskLabel': 'Medium',
                'comment': 'Managed fund — compare net-of-fee performance to a passive benchmark.'}

    # ── Individual equity ──
    equity_indicators = ['plc', 'inc.', 'corp', 'ltd', 'limited', 'ag ', 'sa ', 'nv ', 'se ', 'holdings', 'group']
    if any(k in n for k in equity_indicators) or (isin and isin[:2] in ('US', 'IE', 'GB', 'DE', 'FR', 'NL', 'CH')):
        return {'category': 'Individual Equity', 'risk': 4, 'riskLabel': 'High',
                'comment': 'Single-stock holding — concentrated risk. Consider position sizing relative to total portfolio.'}

    # ── Cash / money market ──
    if any(k in n for k in ['cash', 'money market', 'deposit', 'liquidity', 'overnight']):
        return {'category': 'Cash', 'risk': 1, 'riskLabel': 'Very Low',
                'comment': 'Cash / money market — low risk but consider deploying into growth assets if long-term horizon.'}

    # ── Bonds ──
    if any(k in n for k in ['bond', 'gilt', 'treasury', 'government bond', 'corporate bond', 'note', 'fixed']):
        return {'category': 'Bond', 'risk': 2, 'riskLabel': 'Low-Medium',
                'comment': 'Fixed income holding — review duration and credit quality.'}

    return {'category': 'Other', 'risk': 3, 'riskLabel': 'Medium',
            'comment': 'Review this holding against your overall allocation strategy.'}


def _inv_ticker_guess(name):
    """Guess a yfinance/OpenBB ticker from a holding name (no network call)."""
    known = {
        'kkr': 'KKR', 'apple': 'AAPL', 'microsoft': 'MSFT', 'alphabet': 'GOOGL',
        'google': 'GOOGL', 'amazon': 'AMZN', 'nvidia': 'NVDA', 'tesla': 'TSLA',
        'meta': 'META', 'berkshire': 'BRK-B', 'johnson & johnson': 'JNJ',
        'vanguard global all cap': 'VWRL.L', 'vanguard ftse all-world': 'VWRA.L',
        'ishares msci world': 'IWDA.AS', 'euro stoxx 50': '^STOXX50E',
        's&p 500': '^GSPC', 'ftse 100': '^FTSE',
    }
    n = (name or '').lower().strip()
    for k, v in known.items():
        if k in n:
            return v
    return None


def _inv_try_enrich_tickers(holdings):
    """Attach a best-guess ticker to each holding (no live-price fetch here — the
    dashboard already sources live quotes via the OpenBB API, so this stays
    network-free)."""
    for h in holdings:
        if h.get('ticker'):
            continue
        guess = _inv_ticker_guess(h.get('name', ''))
        if guess:
            h['ticker'] = guess


def _inv_tables_to_holdings(tables, currency):
    """Extract holdings from pdfplumber table data."""
    COL_PAT = {
        'name': ['description', 'name', 'security', 'holding', 'fund', 'stock', 'investment', 'asset'],
        'value': ['market value', 'market val', 'value', 'mv', 'current value', 'valuation', 'amount'],
        'cost': ['book cost', 'cost', 'purchase', 'original', 'proceeds', 'book value'],
        'gainpct': ['gain', 'loss', 'return', 'performance', '%', 'profit'],
        'qty': ['quantity', 'qty', 'units', 'shares', 'number', 'holding'],
        'isin': ['isin', 'code'],
        'price': ['price', 'nav', 'unit price', 'market price', 'close'],
        'weight': ['weight', 'allocation', '%', 'pct', 'percent'],
        'currency': ['currency', 'ccy', 'fx'],
    }

    holdings = []
    seen_isins = set()
    SKIP = {'total', 'grand total', 'subtotal', 'cash total', 'portfolio total', 'net asset',
            'nan', 'none', '', 'n/a', '—', '-'}

    for tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        # Find header row
        header_row = None
        for r_idx, row in enumerate(tbl[:5]):
            row_low = [str(c).lower().strip() for c in (row or [])]
            hits = sum(1 for cell in row_low if any(p in cell for p in
                       ['description', 'name', 'value', 'quantity', 'isin', 'cost', 'price', 'return', 'gain', 'currency', 'weight']))
            if hits >= 2:
                header_row = r_idx
                break
        if header_row is None:
            continue

        headers = [str(c) for c in (tbl[header_row] or [])]
        ci = {k: _inv_find_col(headers, v) for k, v in COL_PAT.items()}
        if ci['name'] is None and ci['value'] is None:
            continue

        for row in tbl[header_row + 1:]:
            if not row:
                continue
            row_s = [str(c).strip() if c is not None else '' for c in row]
            row_text = ' '.join(row_s).strip().lower()
            if len(row_text) < 3 or any(sw in row_text for sw in ['warning', 'past performance', 'note:']):
                continue

            name = row_s[ci['name']].strip() if ci['name'] is not None else ''
            if not name or name.lower() in SKIP or len(name) < 3:
                continue
            # Skip obvious total rows
            if any(sw in name.lower() for sw in ['total', 'subtotal', 'grand', 'portfolio total']):
                continue

            isin = None
            if ci['isin'] is not None:
                raw_isin = row_s[ci['isin']].strip()
                if re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', raw_isin):
                    isin = raw_isin
            # Try to find ISIN in the row itself
            if not isin:
                for cell in row_s:
                    m = ISIN_RE.search(cell.upper())
                    if m:
                        isin = m.group(1)
                        break

            if isin and isin in seen_isins:
                continue
            if isin:
                seen_isins.add(isin)

            value = _inv_pn(row_s[ci['value']]) if ci['value'] is not None else None
            cost = _inv_pn(row_s[ci['cost']]) if ci['cost'] is not None else None
            gain_pct = _inv_pn(row_s[ci['gainpct']]) if ci['gainpct'] is not None else None
            qty = _inv_pn(row_s[ci['qty']]) if ci['qty'] is not None else None
            price = _inv_pn(row_s[ci['price']]) if ci['price'] is not None else None
            weight = _inv_pn(row_s[ci['weight']]) if ci['weight'] is not None else None
            exch_cur = row_s[ci['currency']].strip() if ci['currency'] is not None else ''

            if value is None or value == 0:
                continue

            cl = _inv_classify_stock(name, isin or '')
            holdings.append({
                'name': name,
                'isin': isin,
                'value': value,
                'bookCost': cost,
                'gainPct': gain_pct,
                'qty': qty,
                'price': price,
                'weight': weight,
                'exchCurrency': exch_cur or currency,
                'category': cl['category'],
                'risk': cl['risk'],
                'riskLabel': cl['riskLabel'],
                'comment': cl['comment'],
            })

    return holdings


def _inv_text_holdings(all_text, currency):
    """Fallback: extract holdings from raw PDF text using regex patterns."""
    holdings = []
    cur_sym = {'EUR': '€', 'USD': '$', 'GBP': '£'}.get(currency, '€')
    combined = '\n'.join(all_text)

    # Pattern: ISIN at start, then name, then value
    # "IE00B3RBWM25  Vanguard FTSE All-World   €12,345.67"
    pat_isin_line = re.compile(
        r'([A-Z]{2}[A-Z0-9]{10})\s+([\w &\-\./,()\']+?)\s+[' + re.escape(cur_sym) + r'£$€]?\s*([\d,]+(?:\.\d+)?)',
        re.MULTILINE
    )
    for m in pat_isin_line.finditer(combined):
        isin_m, name_m, val_s = m.group(1), m.group(2).strip(), m.group(3)
        val = _inv_pn(val_s)
        if val and val > 10:
            cl = _inv_classify_stock(name_m, isin_m)
            holdings.append({
                'name': name_m, 'isin': isin_m,
                'value': val, 'bookCost': None, 'gainPct': None,
                'qty': None, 'price': None, 'weight': None,
                'exchCurrency': currency,
                'category': cl['category'], 'risk': cl['risk'],
                'riskLabel': cl['riskLabel'], 'comment': cl['comment'],
            })
    return holdings


def _inv_extract_performance(texts):
    """Extract period return figures from text."""
    combined = ' '.join(texts)
    perf = {}
    patterns = [
        (r'(?:1|one)[- ]?(?:month|mth|mo)\.?\s*[:\-]?\s*([+\-]?\d+\.?\d*)\s*%', '1m'),
        (r'(?:3|three)[- ]?(?:month|mth|mo)\.?\s*[:\-]?\s*([+\-]?\d+\.?\d*)\s*%', '3m'),
        (r'(?:6|six)[- ]?(?:month|mth|mo)\.?\s*[:\-]?\s*([+\-]?\d+\.?\d*)\s*%', '6m'),
        (r'(?:1|one)[- ]?(?:year|yr|y)\.?\s*[:\-]?\s*([+\-]?\d+\.?\d*)\s*%', '1y'),
        (r'(?:3|three)[- ]?(?:year|yr|y)\.?\s*[:\-]?\s*([+\-]?\d+\.?\d*)\s*%', '3y'),
        (r'(?:since\s+inception|inception)[:\-]?\s*([+\-]?\d+\.?\d*)\s*%', 'inception'),
        (r'ytd\s*[:\-]?\s*([+\-]?\d+\.?\d*)\s*%', 'ytd'),
    ]
    for pat, key in patterns:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            try:
                perf[key] = float(m.group(1))
            except Exception:
                pass
    return perf


def _inv_generate_commentary(holdings, total_value, currency, asset_classes, perf, doc_type, report_date):
    """Generate rich, personalised Irish-tax-aware commentary sections."""
    cur_sym = {'EUR': '€', 'USD': '$', 'GBP': '£'}.get(currency, '€')

    def fmt(n):
        return f"{cur_sym}{n:,.0f}" if n is not None else '—'

    sections = []
    highlights = []
    warnings_list = []

    n_holdings = len(holdings)
    total_str = fmt(total_value) if total_value else '—'
    is_kkr = doc_type == 'kkr_report'

    # ── 1. Portfolio Overview ──────────────────────────
    holding_words = f"{n_holdings} holding{'s' if n_holdings != 1 else ''}"
    date_str = f"as at {report_date}" if report_date else ''

    if is_kkr:
        total_cost = sum(h.get('bookCost') or 0 for h in holdings)
        total_gain = sum(h.get('gainAbs') or 0 for h in holdings)
        total_realized = sum(h.get('amtRealized') or 0 for h in holdings)
        overall_return = round((total_gain / total_cost) * 100, 1) if total_cost else None
        ret_str = f" — an overall net gain of <strong style='color:#22c55e'>{fmt(total_gain)} ({'+' if overall_return >= 0 else ''}{overall_return:.1f}%)</strong>" if overall_return is not None else ''
        sections.append({
            'title': '📊 Portfolio Overview',
            'body': (
                f"This is your <strong>KKR Investor Portal</strong> statement {date_str}. "
                f"Total portfolio value: <strong>{fmt(total_value)}</strong> across {holding_words} spread across KKR funds{ret_str}. "
                f"You invested <strong>{fmt(total_cost)}</strong> and have realised <strong>{fmt(total_realized)}</strong> in cash distributions to date. "
                f"All values are in USD."
            ),
        })
    else:
        sections.append({
            'title': '📊 Portfolio Overview',
            'body': (
                f"Your statement shows a portfolio of <strong>{total_str}</strong> across {holding_words} {date_str}. "
                + (_build_alloc_sentence(asset_classes) if asset_classes else '')
            ),
        })

    # ── 2. Performance commentary ─────────────────────
    if perf:
        perf_lines = []
        for k, v in sorted(perf.items()):
            label = {'1m': '1 month', '3m': '3 months', '6m': '6 months', '1y': '1 year', '3y': '3 years',
                     'ytd': 'year to date', 'inception': 'since inception'}.get(k, k)
            col = '#22c55e' if v >= 0 else '#ef4444'
            perf_lines.append(f"<strong style='color:{col}'>{'+' if v>=0 else ''}{v:.1f}%</strong> ({label})")
        sections.append({
            'title': '📈 Performance',
            'body': f"Returns detected in your statement: {', '.join(perf_lines)}. "
                    f"For context: MSCI World returned ~12% over the last year, global bonds ~4–5%.",
        })
        best_perf = max(perf.values())
        if best_perf >= 15:
            highlights.append(f"Strong performance: {best_perf:.1f}% return detected")

    # ── 3. Holdings breakdown ─────────────────────────
    if holdings:
        top3 = sorted([h for h in holdings if h.get('value')], key=lambda h: h['value'], reverse=True)[:3]
        top_names = ', '.join(f"<strong>{h['name'][:30]}</strong> ({fmt(h['value'])})" for h in top3)
        sections.append({
            'title': '🏦 Key Holdings',
            'body': f"Top holdings by value: {top_names}." +
                    (" " + _build_gains_sentence(holdings) if any(h.get('gainPct') is not None for h in holdings) else ''),
        })

        # Concentration
        if total_value and total_value > 0:
            top1 = top3[0] if top3 else None
            if top1:
                pct1 = (top1['value'] / total_value) * 100
                if pct1 > 50:
                    warnings_list.append({'level': 'warn',
                                          'text': f"{top1['name'][:40]} represents {pct1:.0f}% of your portfolio — significant concentration risk."})
                elif pct1 > 30:
                    warnings_list.append({'level': 'info',
                                          'text': f"{top1['name'][:40]} is your largest holding at {pct1:.0f}% of the portfolio."})

    # ── 4. Asset allocation commentary ───────────────
    if asset_classes:
        alt_pct = asset_classes.get('alternatives', 0) + asset_classes.get('private_equity', 0)
        cash_pct = asset_classes.get('cash', 0)
        eq_pct = asset_classes.get('equities', 0)
        bond_pct = asset_classes.get('bonds', 0)

        alloc_notes = []
        if alt_pct > 30:
            alloc_notes.append(f"Private equity / alternatives at {alt_pct:.0f}% creates illiquidity — typical lock-up periods are 7–10 years")
            warnings_list.append({'level': 'warn',
                                  'text': f"High alternatives exposure ({alt_pct:.0f}%). These assets are difficult to sell quickly — ensure you have sufficient liquid assets elsewhere."})
        if cash_pct > 15:
            alloc_notes.append(f"cash at {cash_pct:.0f}% may be a drag on long-term returns")
            warnings_list.append({'level': 'info',
                                  'text': f"Cash allocation of {cash_pct:.0f}%. Consider whether this should be deployed into growth assets given your investment horizon."})
        if eq_pct > 0:
            alloc_notes.append(f"equity allocation of {eq_pct:.0f}% provides long-term growth exposure")

        if alloc_notes:
            sections.append({
                'title': '⚖️ Allocation Observations',
                'body': 'Your allocation shows: ' + '; '.join(alloc_notes) + '.',
            })

    # ── 5. FX Risk ────────────────────────────────────
    usd_holdings = [h for h in holdings if (h.get('exchCurrency') or '').upper() == 'USD']
    usd_value = sum(h['value'] for h in usd_holdings if h.get('value'))
    if currency == 'EUR' and usd_value > 0 and total_value:
        usd_pct = (usd_value / total_value) * 100
        sections.append({
            'title': '💱 FX Risk',
            'body': (f"Approximately {usd_pct:.0f}% of your portfolio ({fmt(usd_value)}) appears to be denominated in USD. "
                     f"As a EUR-based investor, a 10% EUR/USD appreciation would reduce this portion's EUR value by roughly "
                     f"{fmt(usd_value * 0.10)}. "
                     f"This is relevant if you hold KKR or other USD-priced assets through a broker reporting in EUR."),
        })
        if usd_pct > 50:
            warnings_list.append({'level': 'warn',
                                  'text': f"More than half your portfolio ({usd_pct:.0f}%) is in USD. Consider whether this FX exposure is intentional."})

    # ── KKR-specific: fund breakdown + winners/losers ──
    if is_kkr:
        funds = {}
        for h in holdings:
            f = h.get('fund', 'KKR')
            if f not in funds:
                funds[f] = {'value': 0, 'cost': 0, 'gain': 0, 'holdings': []}
            funds[f]['value'] += h.get('value', 0) or 0
            funds[f]['cost'] += h.get('bookCost', 0) or 0
            funds[f]['gain'] += h.get('gainAbs', 0) or 0
            funds[f]['holdings'].append(h)

        fund_rows = []
        for fname, fd in sorted(funds.items(), key=lambda x: -x[1]['value']):
            ret = round((fd['gain'] / fd['cost']) * 100, 1) if fd['cost'] else 0
            col = '#22c55e' if ret >= 0 else '#ef4444'
            fund_rows.append(
                f"<div style='display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #0a1525;font-size:12px'>"
                f"<span style='color:#e2e8f0;font-weight:600'>{fname}</span>"
                f"<span><strong>{fmt(fd['value'])}</strong> &nbsp;"
                f"<span style='color:{col}'>{'+' if ret >= 0 else ''}{ret:.1f}%</span> &nbsp;"
                f"<span style='color:#475569;font-size:10px'>{len(fd['holdings'])} holdings</span></span>"
                f"</div>"
            )
        sections.append({
            'title': '🏗️ Fund-by-Fund Breakdown',
            'body': ''.join(fund_rows) + (
                f"<div style='margin-top:12px;font-size:12px;color:#64748b'>"
                f"KKR European Fund VI focuses on European mid-market buyouts. "
                f"KKR Global Infrastructure IV holds infrastructure assets globally (energy, data, transport). "
                f"KKR North America Fund XIII targets North American buyouts across technology, healthcare, and services."
                f"</div>"
            ),
        })

        # Top winners and losers
        with_gains = [h for h in holdings if h.get('gainAbs') is not None]
        winners = sorted(with_gains, key=lambda h: h.get('gainAbs', 0), reverse=True)[:5]
        losers = [h for h in sorted(with_gains, key=lambda h: h.get('gainAbs', 0)) if (h.get('gainAbs') or 0) < 0][:5]
        winner_html = ''.join(
            f"<div style='display:flex;justify-content:space-between;padding:4px 0;font-size:12px'>"
            f"<span style='color:#94a3b8'>{h['name'][:35]}</span>"
            f"<span style='color:#22c55e'>+{fmt(h['gainAbs'])}"
            f"{'  (+' + str(h['gainPct']) + '%)' if h.get('gainPct') else ''}</span></div>"
            for h in winners
        )
        loser_html = ''.join(
            f"<div style='display:flex;justify-content:space-between;padding:4px 0;font-size:12px'>"
            f"<span style='color:#94a3b8'>{h['name'][:35]}</span>"
            f"<span style='color:#ef4444'>{fmt(h['gainAbs'])}"
            f"{'  (' + str(h['gainPct']) + '%)' if h.get('gainPct') else ''}</span></div>"
            for h in losers
        ) if losers else '<div style="font-size:12px;color:#334155">No positions currently in loss.</div>'
        sections.append({
            'title': '🏆 Top Performers & Laggards',
            'body': (
                f"<div style='margin-bottom:10px'><div style='font-size:11px;font-weight:700;color:#22c55e;margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em'>Top 5 Gainers</div>{winner_html}</div>"
                f"<div><div style='font-size:11px;font-weight:700;color:#ef4444;margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em'>Positions in Loss</div>{loser_html}</div>"
            ),
        })

        # KKR-specific insights
        total_cost = sum(h.get('bookCost', 0) or 0 for h in holdings)
        total_gain_all = sum(h.get('gainAbs', 0) or 0 for h in holdings)
        total_realized = sum(h.get('amtRealized', 0) or 0 for h in holdings)
        sections.append({
            'title': '🔍 Private Equity Context',
            'body': (
                f"Your KKR investments are <strong>private equity (PE)</strong> — stakes in private companies, not publicly traded stocks. "
                f"Key things to understand:<br><br>"
                f"<strong>Valuation:</strong> The values shown ({fmt(total_value)}) are KKR's internal quarterly estimates based on comparable company multiples and DCF models — not a price you can sell at today. Actual exit proceeds may differ significantly.<br><br>"
                f"<strong>Distributions:</strong> You have received {fmt(total_realized)} in realised cash distributions to date (proceeds from exits and dividends). This is real cash returned to you.<br><br>"
                f"<strong>Lock-up &amp; liquidity:</strong> KKR funds typically have 10-year lives (5-year investment period + 5-year harvest). You cannot redeem your investment on demand — liquidity comes only when KKR exits individual portfolio companies.<br><br>"
                f"<strong>J-curve effect:</strong> Early in a fund's life, fees and slow initial deployments cause negative returns. As portfolio companies mature and are exited, returns typically improve.<br><br>"
                f"<strong>TVPI context:</strong> Your total value (remaining + realised) vs cost invested = "
                f"<strong style='color:#60a5fa'>{round((total_value + total_realized) / total_cost, 2) if total_cost else '—'}× TVPI</strong> "
                f"(Total Value to Paid-In capital). A TVPI of 1.0× means breakeven; top-quartile PE funds typically target 2.0–3.0×."
            ),
        })

        # Irish tax — KKR specific
        total_gain_for_tax = sum(h.get('gainAbs', 0) or 0 for h in holdings)
        sections.append({
            'title': '🇮🇪 Irish Tax — KKR Private Equity',
            'body': (
                f"<strong>CGT (Capital Gains Tax) at 33%:</strong> KKR fund interests held directly by an Irish resident individual "
                f"are likely subject to CGT at 33% on gains (not Exit Tax at 41%, which applies to regulated investment funds). "
                f"Your gain to date of approx {fmt(total_gain_for_tax)} would generate a "
                f"CGT liability of approx <strong style='color:#ef4444'>{fmt(total_gain_for_tax * 0.33 * 0.926)}</strong> "
                f"(after €1,270 annual exemption) if fully crystallised today — though the actual liability only arises when KKR returns cash to you via distributions.<br><br>"
                f"<strong>Timing of liability:</strong> CGT arises when distributions or exit proceeds are received, not when the quarterly valuation increases. You have received {fmt(total_realized)} in distributions to date — ensure these have been reported on your Irish income tax returns.<br><br>"
                f"<strong>Tax treatment classification:</strong> The precise tax treatment of your KKR investment (CGT vs Exit Tax vs income) depends on the fund structure. Given the amounts involved, get a written opinion from a tax adviser — the cost is trivial relative to the 8 percentage-point difference (33% CGT vs 41% Exit Tax) on gains of {fmt(total_gain_for_tax)}.<br><br>"
                f"<strong>USD distributions — FX gain:</strong> When distributions are received in USD and converted to EUR, any FX gain since your original EUR investment may also be subject to Irish CGT."
            ),
        })

    # ── 6. Irish tax commentary (non-KKR) ────────────
    else:
        has_pe = any('private equity' in (h.get('category') or '').lower() or
                     'alternatives' in (h.get('category') or '').lower() for h in holdings)

        tax_body = (
            "<strong>Capital Gains Tax (CGT):</strong> Gains on disposal are taxed at 33% in Ireland, with an annual exemption of €1,270. "
            "CGT due on Jan–Nov disposals must be paid by 15 December; December disposals by 31 January. "
        )
        if has_pe:
            tax_body += ("<br><br><strong>Exit Tax on Funds:</strong> Irish-domiciled funds are subject to "
                         "Exit Tax at 41% on gains — applies on disposal, maturity, or every 8 years (deemed disposal). "
                         "Ensure you are tracking your 8-year deemed disposal dates.")
        tax_body += ("<br><br><strong>Dividend income:</strong> Dividends from foreign stocks are subject to income tax, USC, and PRSI at ~52% marginal rate.")
        sections.append({'title': '🇮🇪 Irish Tax Considerations', 'body': tax_body})

    # ── 7. Positives ──────────────────────────────────
    positives = []
    if is_kkr:
        total_gain_pos = sum(h.get('gainAbs', 0) or 0 for h in holdings)
        overall_ret = round((total_gain_pos / sum(h.get('bookCost', 0) or 0 for h in holdings)) * 100, 1) if holdings and sum(h.get('bookCost', 0) or 0 for h in holdings) else 0
        if overall_ret > 0:
            positives.append(f"Portfolio up {overall_ret:.1f}% overall since inception")
        winners_count = sum(1 for h in holdings if (h.get('gainAbs') or 0) > 0)
        if winners_count > len(holdings) * 0.6:
            positives.append(f"{winners_count}/{len(holdings)} holdings in profit")
        positives.append("Real cash distributions of " + fmt(sum(h.get('amtRealized', 0) or 0 for h in holdings)) + " received to date")
    else:
        if n_holdings >= 5:
            positives.append(f"Well-diversified across {n_holdings} holdings")
        if asset_classes and len(asset_classes) >= 3:
            positives.append(f"Exposure across {len(asset_classes)} asset classes")
        if perf:
            good = {k: v for k, v in perf.items() if v >= 10}
            if good:
                positives.append("Strong returns: " + ', '.join(f"{v:.1f}% ({k})" for k, v in good.items()))

    return sections, warnings_list, positives, highlights


def _build_alloc_sentence(ac):
    """Build a human-readable allocation summary sentence."""
    if not ac:
        return ''
    parts = []
    labels = {
        'equities': 'equities', 'bonds': 'bonds / fixed income', 'cash': 'cash',
        'alternatives': 'alternatives / private equity', 'balanced': 'multi-asset / balanced funds',
        'property': 'property', 'private_equity': 'private equity',
    }
    sorted_ac = sorted(ac.items(), key=lambda x: -x[1])
    for cls, pct in sorted_ac[:4]:
        if pct > 2:
            parts.append(f"{labels.get(cls, cls)} ({pct:.0f}%)")
    return "Allocation breakdown: " + ", ".join(parts) + "." if parts else ''


def _build_gains_sentence(holdings):
    """Summarise best and worst performers."""
    with_gains = [h for h in holdings if h.get('gainPct') is not None]
    if not with_gains:
        return ''
    best = max(with_gains, key=lambda h: h['gainPct'])
    worst = min(with_gains, key=lambda h: h['gainPct'])
    parts = []
    if best['gainPct'] > 0:
        parts.append(f"Best performer: {best['name'][:25]} (+{best['gainPct']:.1f}%)")
    if worst['gainPct'] < 0:
        parts.append(f"Weakest: {worst['name'][:25]} ({worst['gainPct']:.1f}%)")
    return ' · '.join(parts)


def _inv_asset_classes_from_holdings(holdings):
    """Infer asset class breakdown from classified holdings."""
    CAT_MAP = {
        'Private Equity / Alternatives': 'alternatives',
        'Structured Note': 'alternatives',
        'Individual Equity': 'equities',
        'Global Equity ETF': 'equities',
        'US Equity ETF': 'equities',
        'ETF': 'equities',
        'Bond': 'bonds',
        'Bond ETF': 'bonds',
        'Cash': 'cash',
        'Managed Fund': 'balanced',
    }
    result = {}
    total = sum(h.get('value', 0) or 0 for h in holdings)
    if not total:
        return result
    for h in holdings:
        cat = CAT_MAP.get(h.get('category', ''), 'other')
        pct = ((h.get('value') or 0) / total) * 100
        result[cat] = round(result.get(cat, 0) + pct, 1)
    return result


def analyse_investments(file_bytes, filename):
    """Parse a personal investment statement (PDF/Excel/CSV) and return rich analysis."""
    import pandas as pd

    ext = filename.lower().rsplit('.', 1)[-1]
    holdings = []
    all_texts = []
    report_date = None
    doc_type = 'general_statement'
    doc_total = None

    # ── Parse file ───────────────────────────────────
    if ext == 'pdf':
        pdf_result = _inv_extract_pdf(file_bytes)
        if len(pdf_result) == 4:
            # A recognised format (KKR or Davy) — pre-parsed holdings returned
            texts, _, err, pre = pdf_result
            if err:
                return {'error': err}
            all_texts = texts or []
            report_date = _inv_find_date(all_texts)
            perf = {}
            holdings = pre['holdings']
            if pre['kind'] == 'kkr':
                currency = 'USD'
                doc_type = 'kkr_report'
                doc_total = holdings[0].pop('_doc_total', None) if holdings else None
            else:  # davy portfolio valuation — values already in EUR
                currency = 'EUR'
                doc_type = 'davy_valuation'
        else:
            texts, tables, err = pdf_result
            if err:
                return {'error': err}
            all_texts = texts or []
            currency = _inv_currency(all_texts)
            report_date = _inv_find_date(all_texts)
            perf = _inv_extract_performance(all_texts)

            combined_text = ' '.join(all_texts).lower()
            if any(k in combined_text for k in ['quarterly', 'quarter', 'q1', 'q2', 'q3', 'q4']):
                doc_type = 'quarterly_report'
            elif any(k in combined_text for k in ['share', 'equity', 'holdings', 'portfolio valuation']):
                doc_type = 'holdings_statement'

            holdings = _inv_tables_to_holdings(tables or [], currency)
            if not holdings:
                holdings = _inv_text_holdings(all_texts, currency)

    elif ext in ('xlsx', 'xls', 'xlsm', 'csv'):
        try:
            if ext in ('xlsx', 'xls', 'xlsm'):
                xl = pd.ExcelFile(io.BytesIO(file_bytes))
                sheets = {n: xl.parse(n, header=None) for n in xl.sheet_names}
            else:
                sheets = {'Sheet1': pd.read_csv(io.BytesIO(file_bytes), header=None)}
        except Exception as e:
            return {'error': f'Could not read file: {e}'}

        all_texts = [str(df.values.tolist()) for df in sheets.values()]
        currency = _inv_currency(all_texts)
        perf = {}
        report_date = _inv_find_date(all_texts)

        COL_PAT = {
            'name': ['description', 'name', 'fund name', 'holding', 'security', 'stock', 'ticker', 'asset', 'investment'],
            'value': ['market value', 'market val', 'mv', 'current value', 'value', 'valuation', 'amount', 'worth'],
            'cost': ['book cost', 'cost', 'purchase cost', 'original cost', 'proceeds', 'acquisition'],
            'gainpct': ['% gain', '% return', 'gain%', 'gain or loss', 'gain/loss', 'performance', 'change%', 'return%'],
            'qty': ['quantity', 'qty', 'units', 'shares', 'number of shares'],
            'isin': ['isin'],
            'price': ['market price', 'price', 'nav', 'unit price', 'close price'],
            'weight': ['weight', 'allocation', '% of portfolio', '% weight'],
            'currency': ['currency', 'ccy', 'exchange currency'],
        }

        seen_isins = set()
        SKIP = {'total', 'grand total', 'subtotal', 'cash total', 'portfolio total', 'nan', 'none', '', 'n/a'}

        for sheet_name, df in sheets.items():
            header_row = None
            for r in range(min(8, len(df))):
                row_low = [str(df.iloc[r, c]).lower().strip() for c in range(len(df.columns))]
                hits = sum(1 for v in row_low if any(p in v for p in
                           ['description', 'name', 'value', 'quantity', 'isin', 'cost', 'price', 'return', 'gain', 'currency', 'shares']))
                if hits >= 2:
                    header_row = r
                    break
            if header_row is None:
                continue

            headers = [str(df.iloc[header_row, c]) for c in range(len(df.columns))]
            ci = {k: _inv_find_col(headers, v) for k, v in COL_PAT.items()}
            if ci['name'] is None and ci['value'] is None:
                continue

            for r in range(header_row + 1, len(df)):
                row = [df.iloc[r, c] for c in range(len(df.columns))]
                row_s = [str(v).strip() for v in row]
                row_text = ' '.join(row_s).lower()
                if len(row_text.strip()) < 3 or 'warning' in row_text or 'past performance' in row_text:
                    continue

                name = row_s[ci['name']].strip() if ci['name'] is not None else ''
                if not name or name.lower() in SKIP or len(name) < 3:
                    continue
                if any(sw in name.lower() for sw in ['total', 'subtotal', 'grand total']):
                    continue

                isin = None
                if ci['isin'] is not None:
                    raw = row_s[ci['isin']].strip()
                    if re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', raw):
                        isin = raw
                if isin and isin in seen_isins:
                    continue
                if isin:
                    seen_isins.add(isin)

                value = _inv_pn(row[ci['value']]) if ci['value'] is not None else None
                cost = _inv_pn(row[ci['cost']]) if ci['cost'] is not None else None
                gain_pct = _inv_pn(row[ci['gainpct']]) if ci['gainpct'] is not None else None
                qty = _inv_pn(row[ci['qty']]) if ci['qty'] is not None else None
                price = _inv_pn(row[ci['price']]) if ci['price'] is not None else None
                weight = _inv_pn(row[ci['weight']]) if ci['weight'] is not None else None
                exch_cur = row_s[ci['currency']].strip() if ci['currency'] is not None else ''

                if value is None or value == 0:
                    continue

                cl = _inv_classify_stock(name, isin or '')
                holdings.append({
                    'name': name, 'isin': isin, 'value': value, 'bookCost': cost,
                    'gainPct': gain_pct, 'qty': qty, 'price': price, 'weight': weight,
                    'exchCurrency': exch_cur or currency,
                    'category': cl['category'], 'risk': cl['risk'],
                    'riskLabel': cl['riskLabel'], 'comment': cl['comment'],
                })
    else:
        return {'error': f'Unsupported file type: .{ext}. Please upload PDF, XLSX, XLS, or CSV.'}

    # ── Post-processing ───────────────────────────────
    if not holdings:
        return {
            'error': (
                'Could not extract any holdings from this file. '
                'For PDFs: the document may be a scanned image (not machine-readable text). '
                'For Excel/CSV: ensure the file has recognisable column headers such as '
                '"Description", "Market Value", "Quantity", "ISIN".'
            )
        }

    # Normalise values — if any look like pence/cents (e.g. > 1000× others), scale down
    max_val = max((h['value'] for h in holdings if h.get('value')), default=0)
    min_val = min((h['value'] for h in holdings if h.get('value') and h['value'] > 0), default=0)
    if max_val > 0 and min_val > 0 and max_val / min_val > 1000:
        for h in holdings:
            if h.get('value'):
                h['value'] = round(h['value'] / 100, 2)
            if h.get('bookCost'):
                h['bookCost'] = round(h['bookCost'] / 100, 2)
            if h.get('price'):
                h['price'] = round(h['price'] / 100, 4)

    total_value = doc_total if (doc_type == 'kkr_report' and doc_total) \
        else round(sum(h['value'] for h in holdings if h.get('value')), 2)

    # Allocation %
    if total_value > 0:
        for h in holdings:
            h['pct'] = round((h.get('value', 0) / total_value) * 100, 2)

    # Gain in currency terms
    for h in holdings:
        if h.get('value') and h.get('bookCost'):
            h['gainAbs'] = round(h['value'] - h['bookCost'], 2)
        elif h.get('gainPct') and h.get('bookCost'):
            h['gainAbs'] = round(h['bookCost'] * h['gainPct'] / 100, 2)
        else:
            h.setdefault('gainAbs', None)

    # Attach best-guess tickers (no network fetch)
    _inv_try_enrich_tickers(holdings)

    asset_classes = _inv_asset_classes_from_holdings(holdings)

    # Build gain returns for chart
    gain_returns = {}
    for h in holdings:
        if h.get('gainPct') is not None:
            short = h['name'][:28] + ('…' if len(h['name']) > 28 else '')
            gain_returns[short] = h['gainPct']

    # Generate commentary
    sections, warnings_list, positives, highlights = _inv_generate_commentary(
        holdings, total_value, currency, asset_classes, perf, doc_type, report_date
    )

    return {
        'docType': doc_type,
        'reportDate': report_date,
        'filename': filename,
        'currency': currency,
        'totalValue': total_value,
        'holdings': holdings,
        'assetClasses': asset_classes,
        'performance': perf,
        'gainReturns': gain_returns,
        'sections': sections,
        'sectionsText': all_texts,   # raw page text, used by pension provider detection
        'warnings': warnings_list,
        'positives': positives,
        'highlights': highlights,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ── PORTFOLIO NEWS RADAR — map holdings to news-fetch targets ─────────────────
# ══════════════════════════════════════════════════════════════════════════════
# Most of these holdings are private (KKR PE funds, structured notes, Davy managed
# funds) so they have no ticker to hang a news feed on. Instead each holding is
# mapped to the *public* entity news actually discusses: the manager (KKR & Co),
# the note's underlying index, or a plain-language search. The actual fetching is
# done in app.py (it needs the OpenBB API + RSS helpers); this stays network-free.

# Underlying-index detection for structured notes → (display name, index ticker)
_NOTE_INDEX = [
    (('sx5e', 'stoxx', 'eurostoxx', 'wosx5', 'esmi', 'euro stoxx'), ('EuroStoxx 50', '^STOXX50E')),
    (('spx', 's&p 500', 'sp500', 'us500'), ('S&P 500', '^GSPC')),
    (('ukx', 'ftse 100', 'ftse100'), ('FTSE 100', '^FTSE')),
    (('ndx', 'nasdaq'), ('Nasdaq 100', '^NDX')),
    (('nky', 'nikkei'), ('Nikkei 225', '^N225')),
]

_NOTE_ISSUERS = ['bnp', 'barclays', 'citigroup', 'goldman sachs', 'morgan stanley',
                 'societe generale', 'deutsche bank', 'hsbc', 'jpmorgan']


def holdings_news_strategy(holdings):
    """Map a consolidated holdings list to deduplicated news targets.

    Returns a list of target dicts, each:
        {group, kind: 'ticker'|'query', value, label}
    `group` is one of: 'kkr_watch', 'performance', 'markets', 'funds'.
    The caller fetches: kind 'ticker' -> OpenBB news/company; kind 'query' ->
    Google-News RSS search. Targets are deduped by (kind, value).
    """
    targets = []
    seen = set()

    def add(group, kind, value, label):
        key = (kind, value.lower())
        if not value or key in seen:
            return
        seen.add(key)
        targets.append({'group': group, 'kind': kind, 'value': value, 'label': label})

    has_kkr = False
    for h in holdings:
        name = (h.get('name') or '').strip()
        n = name.lower()
        cat = (h.get('category') or h.get('fundType') or '').lower()
        ticker = h.get('ticker')

        # ── KKR private equity — firm-level + exit/sale watch ──
        if 'kkr' in n or 'kkr' in cat:
            has_kkr = True
            add('performance', 'ticker', 'KKR', 'KKR & Co (manager)')
            continue

        # ── Structured notes — track the underlying index + issuer ──
        if 'structured' in cat or 'note' in cat or any(k in n for k in _NOTE_ISSUERS) \
                or any(k in n for k in ('buffered', 'autocall', 'ace di', 'acs7')):
            idx = next((disp for keys, disp in _NOTE_INDEX if any(k in n for k in keys)), None)
            if idx:
                add('markets', 'query', idx[0], f'{idx[0]} — note underlying')
            issuer = next((iss for iss in _NOTE_ISSUERS if iss in n), None)
            if issuer:
                add('markets', 'query', f'{issuer.title()} structured note', f'{issuer.title()} (note issuer)')
            continue

        # ── Davy / Irish managed funds ──
        if 'davy' in n:
            add('funds', 'query', 'Davy Global Portfolio Strategies fund Ireland', 'Davy managed funds')
            continue
        if any(k in n for k in ('zurich', 'irish life', 'aviva', 'standard life', 'new ireland')):
            mgr = next(k for k in ('zurich', 'irish life', 'aviva', 'standard life', 'new ireland') if k in n)
            add('funds', 'query', f'{mgr.title()} pension fund Ireland', f'{mgr.title()} funds')
            continue

        # ── Anything with a resolvable public ticker ──
        if ticker:
            add('performance', 'ticker', ticker, name[:40])
            continue

        # ── Fallback: plain-language search on the holding name ──
        if len(name) >= 4:
            add('funds', 'query', name, name[:40])

    # The headline ask: a dedicated KKR exit/sale watch (only if KKR is held).
    if has_kkr:
        targets.insert(0, {
            'group': 'kkr_watch', 'kind': 'query',
            'value': 'KKR (exit OR sells OR IPO OR divests OR "portfolio company" OR acquires OR stake)',
            'label': 'KKR deals, exits & IPOs',
        })

    return targets
