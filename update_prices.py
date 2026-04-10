#!/usr/bin/env python3
"""
Daily price updater for Pokemon Chase Card Price Predictor.
Fetches current raw prices from the Pokemon TCG API (TCGPlayer market data),
converts to PSA 10 using each card's stored gm.raw multiplier,
appends a new history point when the month changes or price moves >8%,
recomputes projections using the same model as the JavaScript page,
then writes the updated index.html back to disk for GitHub Pages to serve.
"""

import re, math, time, os, requests
from datetime import datetime, timezone

# ── Config ───────────────────────────────────────────────────────────────────
HTML_FILE   = 'index.html'
API_KEY     = os.environ.get('POKEMON_TCG_API_KEY', '')
HEADERS     = {'X-Api-Key': API_KEY} if API_KEY else {}
BASE_URL    = 'https://api.pokemontcg.io/v2'
CHANGE_THRESHOLD = 0.08   # Add new history point if price moved >8%
RATE_DELAY       = 0.15   # Seconds between API calls

POINT_PAT = re.compile(r"\{l:'([^']+)',v:([\d.]+)\}")
TREND_PAT = re.compile(r',\s*trend:-?\d+,')

# ── Helpers ──────────────────────────────────────────────────────────────────
def month_label() -> str:
    return datetime.now(timezone.utc).strftime('%b %Y')

def fetch_raw_price(tcg_api_id: str) -> float | None:
    """Return current TCGPlayer NM market price for a card, or None."""
    try:
        r = requests.get(f'{BASE_URL}/cards/{tcg_api_id}',
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        prices = (r.json().get('data') or {}) \
                          .get('tcgplayer', {}) \
                          .get('prices', {})
        for ptype in ['holofoil', 'reverseHolofoil', 'normal',
                      '1stEditionHolofoil', 'unlimitedHolofoil']:
            p = prices.get(ptype, {}).get('market')
            if p and p > 0:
                return round(p, 2)
    except Exception as exc:
        print(f'    API error for {tcg_api_id}: {exc}')
    return None

# ── Projection model (mirrors the JavaScript version exactly) ─────────────
def ewlr_slope(hist, lam=0.3):
    n = len(hist)
    xs = list(range(n))
    ys = [math.log(max(v, 0.01)) for _, v in hist]
    ws = [lam ** (n - 1 - i) for i in range(n)]
    sw = sum(ws)
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    num = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
    den = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    return num / den if den else 0.0

def predict(hist, ath, steps):
    n = len(hist); cur = hist[-1][1]
    eff = min(ath, cur * 8); ar = cur / eff if eff > 0 else 1.0
    rc  = (cur - hist[-2][1]) / hist[-2][1] if n >= 2 else 0.0
    rc2 = (hist[-2][1] - hist[-3][1]) / hist[-3][1] if n >= 3 else rc
    sl  = ewlr_slope(hist)
    if sl > 0:
        if ar >= 0.90: sl = min(sl, 0.03)
        elif ar >= 0.80: sl = min(sl, 0.09)
        elif ar >= 0.70: sl = min(sl, 0.15)
    if sl < -0.25 and ar < 0.40:           sl = sl * 0.45 - 0.05
    if abs(rc) < 0.10 and abs(rc2) < 0.12 and rc <= 0.08: sl *= 0.45
    if ar < 0.45 and not (rc < -0.08 and rc2 < -0.08) and rc > -0.05:
        sl = max(sl, 0.04)
    if n <= 5 and sl > 0.25:               sl = 0.25 + (sl - 0.25) * 0.30
    sl = max(-0.50, min(0.50, sl))
    p  = cur * math.exp(sl * steps)
    p  = max(cur * 0.30, min(cur * 4.0, p))
    if eff == ath: p = min(p, ath * 1.10)
    return round(p)

# ── Per-card line updater ─────────────────────────────────────────────────
def update_line(line: str, new_raw: float) -> tuple[str, str]:
    """
    Given a card JS line and a fresh raw price, update history/proj/notes.
    Returns (updated_line, status_string).
    """
    gm_raw_m = re.search(r'gm:\{[^}]*raw:([\d.]+)', line)
    if not gm_raw_m:
        return line, 'skip (no gm.raw)'
    gm_raw = float(gm_raw_m.group(1))
    if gm_raw <= 0:
        return line, 'skip (gm.raw=0)'

    new_psa10 = round(new_raw / gm_raw)

    hist_m = re.search(r'history:\[(.*?)\]', line)
    if not hist_m:
        return line, 'skip (no history)'
    hist = [(l, float(v)) for l, v in POINT_PAT.findall(hist_m.group(1))]
    if not hist:
        return line, 'skip (empty history)'

    cur_psa10   = hist[-1][1]
    change_pct  = abs(new_psa10 - cur_psa10) / cur_psa10 if cur_psa10 else 0
    lbl         = month_label()
    last_lbl    = hist[-1][0]
    new_month   = lbl != last_lbl
    big_move    = change_pct > CHANGE_THRESHOLD

    if not (new_month or big_move):
        return line, f'no-op (${cur_psa10:.0f}→${new_psa10}, {change_pct:.1%} change)'

    # Update history
    if new_month:
        hist.append((lbl, new_psa10))
    else:
        hist[-1] = (lbl, new_psa10)

    # Update ATH
    ath_m = re.search(r'ath:(\d+)', line)
    ath   = int(ath_m.group(1)) if ath_m else int(cur_psa10 * 1.5)
    if new_psa10 > ath:
        ath  = new_psa10
        line = re.sub(r'ath:\d+', f'ath:{ath}', line)

    # Rebuild history[]
    hist_str = 'history:[' + ','.join(
        f"{{l:'{l}',v:{int(v)}}}" for l, v in hist) + ']'
    line = re.sub(r'history:\[[^\]]*\]', hist_str, line)

    # Recompute proj[]
    proj_m = re.search(r'proj:\[(.*?)\]', line)
    if proj_m:
        old_lbls = [p[0] for p in POINT_PAT.findall(proj_m.group(1))]
        new_proj = [
            {'l': old_lbls[i] if i < len(old_lbls) else f'+{(i+1)*6}mo',
             'v': predict(hist, ath, i + 1)}
            for i in range(3)
        ]
        proj_str = 'proj:[' + ','.join(
            f"{{l:'{p['l']}',v:{p['v']}}}" for p in new_proj) + ']'
        line = re.sub(r'proj:\[[^\]]*\]', proj_str, line)

        # Update trend
        p12 = new_proj[1]['v'] if len(new_proj) > 1 else new_psa10
        trend = round((p12 - new_psa10) / max(new_psa10, 1) * 100)
        line  = TREND_PAT.sub(f', trend:{trend},', line)

        # Update outlook
        p18   = new_proj[2]['v'] if len(new_proj) > 2 else p12
        g18   = (p18 - new_psa10) / max(new_psa10, 1)
        new_o = 'bull' if g18 > 0.10 else 'bear' if g18 < -0.06 else 'neutral'
        line  = re.sub(r"outlook:'(\w+)'", f"outlook:'{new_o}'", line)

    # Update note prices
    line = re.sub(r'PSA 10 (?:est )?~?\$([\d,]+)',
                  f'PSA 10 ~${new_psa10:,}', line)
    line = re.sub(r'Raw ~?\$([\d,]+)',
                  f'Raw ~${round(new_raw):,}', line)

    action = 'new month' if new_month else f'big move {change_pct:.1%}'
    return line, f'updated (${cur_psa10:.0f}→${new_psa10}) [{action}]'

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f'=== Pokemon Price Updater  {run_date} ===\n')

    with open(HTML_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    lines     = content.split('\n')
    new_lines = []
    counts    = {'updated': 0, 'no_op': 0, 'no_price': 0, 'skip': 0}

    for line in lines:
        # Only process curated card lines
        if "{ id:'" not in line or "tcgApiId:'" not in line \
                or 'history:[' not in line:
            new_lines.append(line)
            continue

        cid_m = re.search(r"id:'([^']+)'", line)
        tcg_m = re.search(r"tcgApiId:'([^']+)'", line)
        if not (cid_m and tcg_m):
            new_lines.append(line)
            continue

        cid    = cid_m.group(1)
        tcg_id = tcg_m.group(1)

        raw = fetch_raw_price(tcg_id)
        time.sleep(RATE_DELAY)

        if raw is None:
            print(f'  {cid}: no TCGPlayer price — skipping')
            counts['no_price'] += 1
            new_lines.append(line)
            continue

        new_line, status = update_line(line, raw)
        print(f'  {cid}: {status}')

        if status.startswith('updated'):
            counts['updated'] += 1
        elif status.startswith('no-op'):
            counts['no_op'] += 1
        else:
            counts['skip'] += 1

        new_lines.append(new_line)

    # Inject a visible "last updated" timestamp into the page
    updated_content = '\n'.join(new_lines)
    ts_tag = f'<!-- auto-updated:{run_date} -->'
    if '<!-- auto-updated:' in updated_content:
        updated_content = re.sub(
            r'<!-- auto-updated:[^-]*-->', ts_tag, updated_content)
    else:
        # Insert just before </head>
        updated_content = updated_content.replace(
            '</head>', f'{ts_tag}\n</head>', 1)

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(updated_content)

    print(f'\n=== Summary ===')
    print(f"  Updated  : {counts['updated']}")
    print(f"  No change: {counts['no_op']}")
    print(f"  No price : {counts['no_price']}")
    print(f"  Skipped  : {counts['skip']}")
    print(f'\nDone — {HTML_FILE} written.')

if __name__ == '__main__':
    main()
