#!/usr/bin/env python3
"""OIE Daily Digest — chain the full engine into a rich HTML report.
Canonical copy at skills/oie-daily-digest/scripts/daily_digest.py.
Resolves the repo root dynamically (OIE_REPO env override or walk-up), so paths
stay flexible and this works both from scripts/ and from skills/oie-daily-digest/scripts/.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo(start: str) -> str:
    cur = start
    for _ in range(6):
        if os.path.exists(os.path.join(cur, 'config', 'rules.yaml')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for cand in [os.path.join(start, '..', '..', '..'),
                 os.path.join(start, '..'),
                 os.path.dirname(start)]:
        cand = os.path.abspath(cand)
        if os.path.exists(os.path.join(cand, 'config', 'rules.yaml')):
            return cand
    return os.path.dirname(start)


REPO = os.path.abspath(os.getenv('OIE_REPO', '')) or _find_repo(HERE)
LOGS = os.path.join(REPO, 'logs')

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>OIE Daily Digest</title>
<style>
body{{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;background:#0d1117;color:#ffffff;margin:0;padding:24px}}
.wrap{{max-width:920px;margin:0 auto}}
h1{{color:#79c0ff;border-bottom:2px solid #30363d;padding-bottom:8px;font-size:24px}}
.sub{{color:#d0d7de;font-size:13px;margin-bottom:18px}}
.card{{background:#161b27;border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin:14px 0}}
.card h2{{margin:0 0 8px;font-size:16px;color:#7ee787}}
.card h2.alert{{color:#f85149}}
pre{{background:#10151d;border:1px solid #2a303c;border-radius:8px;padding:12px;overflow-x:auto;line-height:1.45;font-size:12px;margin:8px 0 0;color:#ffffff}}
.metrics{{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}}
.metric{{flex:1 1 150px;background:#161b27;border:1px solid #30363d;border-radius:10px;padding:10px 12px}}
.metric .k{{font-size:11px;color:#d0d7de;text-transform:uppercase}}
.metric .v{{font-size:20px;font-weight:600;color:#79c0ff}}
.bullets li{{margin:6px 0;line-height:1.5}}
.foot{{color:#d0d7de;font-size:11px;text-align:center;margin-top:24px}}
</style></head><body><div class="wrap">
<h1>OIE Daily Digest</h1>
<div class="sub">{ts} &middot; {period} &middot; Generated from moomoo OpenD + yfinance</div>
"""

FOOT = """<div class="foot">Paper trades are simulated &mdash; never real orders &middot; Data: moomoo OpenD + yfinance</div>
</div></body></html>"""

CLAUDE_HINT = (
    "\n<!-- GENAI ABSTRACT: Claude reads the matching digest-<ts>.json facts file "
    "and replaces the bullets below with a concise 5-10 bullet Daily Decision "
    "Abstract. Deterministic engine untouched; narrative layer only. -->\n"
)


def run_cmd(cmd: list, timeout: int = 600) -> str:
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=REPO, env={**os.environ, 'PYTHONUNBUFFERED': '1'})
        out = (proc.stdout or '') + (proc.stderr or '')
        lines = [ln for ln in out.splitlines()
                 if 'open_context_base' not in ln and '_init_connect_sync' not in ln
                 and 'on_disconnect' not in ln and 'New connect' not in ln]
        text = '\n'.join(lines).strip()
        print(f"    [digest] {os.path.basename(cmd[0])} ... {time.time()-start:.0f}s", file=sys.stderr)
        return text
    except Exception as e:
        return f"[digest error] {cmd[0]} failed: {e}"


def extract_metrics(portfolio_text: str) -> dict:
    out = {}
    pats = {
        'Liquid': r'Liquid \(cash\+fund\):\s*\$\s*([\d,.]+)',
        'Net Liquidation': r'Net Liquidation:\s*\$\s*([\d,.]+)',
        'Cash Buying Power': r'Cash Buying Power:\s*\$\s*([\d,.]+)',
        'Buying Power': r'Buying Power:\s*\$\s*([\d,.]+)',
        'CSP Liability': r'CSP Liability:\s*\$\s*([\d,.]+)',
    }
    for k, pat in pats.items():
        m = re.search(pat, portfolio_text)
        if m:
            out[k] = float(m.group(1).replace(',', ''))
    m = re.search(r'(\d+) stocks, (\d+) options', portfolio_text)
    if m:
        out['stocks'] = int(m.group(1))
        out['options'] = int(m.group(2))
    return out


def auto_abstract(facts: dict, sections: list) -> list:
    """Deterministic fallback abstract; replaced by the GenAI in the skill workflow."""
    bullets = []
    all_text = '\n'.join(b for _, _, b in sections)
    broken = re.findall(r'\N{HEAVY EXCLAMATION MARK SYMBOL}\s+([A-Z]{2,5})\s+\[\S+\].*?THESIS_BROKEN', all_text)
    if broken:
        bullets.append(f"Exit/broken thesis: {', '.join(sorted(set(broken)))} - close or reassign.")
    damaged = re.findall(r'\N{WARNING SIGN}\s+([A-Z]{2,5})\s+\[\S+\].*?TECHNICAL_DAMAGE', all_text)
    if damaged:
        bullets.append(f"Monitor damaged theses: {', '.join(sorted(set(damaged)))} - re-evaluate in 7 days.")
    if facts.get('CSP Liability') and facts.get('Liquid'):
        if facts['CSP Liability'] > facts['Liquid']:
            bullets.append(f"CSP liability ${facts['CSP Liability']:,.0f} exceeds liquid ${facts['Liquid']:,.0f} - reduce exposure.")
        else:
            bullets.append(f"CSPs covered: ${facts['Liquid']:,.0f} liquid vs ${facts['CSP Liability']:,.0f} liability.")
    m = re.search(r'^\s*\d+\s+([A-Z]{2,5})\s+(CSP|CC)\s+\$([\d,.]+)', all_text, re.M)
    if m:
        bullets.append(f"Top screen: {m.group(1)} {m.group(2)} ${m.group(3)} - review vs guardrails.")
    m = re.search(r'Regime:\s+(\w+)', all_text)
    if m:
        bullets.append(f"Regime: {m.group(1)} - sizing/eligibility per rules.yaml.")
    bullets.append("All recommendation logic is deterministic from config/rules.yaml; no AI computes scores or signals.")
    return bullets[:10]


def build_html(sections: list, period: str, facts: dict) -> tuple[str, str]:
    ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    parts = [PAGE.format(ts=ts, period=period)]
    if facts:
        parts.append('<div class="metrics">')
        for k, v in facts.items():
            if isinstance(v, float):
                parts.append(f'<div class="metric"><div class="k">{html.escape(k)}</div><div class="v">${v:,.0f}</div></div>')
            else:
                parts.append(f'<div class="metric"><div class="k">{html.escape(k)}</div><div class="v">{v}</div></div>')
        parts.append('</div>')
    parts.append(CLAUDE_HINT)
    parts.append('<div class="card" id="abstract"><h2>Daily Decision Abstract</h2><ul class="bullets">')
    for b in auto_abstract(facts, sections):
        parts.append(f'<li>{html.escape(b)}</li>')
    parts.append('</ul></div>')
    for icon, title, body in sections:
        if not body:
            continue
        header_cls = ' alert' if ('BROKEN' in body or '\N{HEAVY EXCLAMATION MARK SYMBOL}' in body) else ''
        parts.append(f'<div class="card"><h2 class="{header_cls}">{icon} {html.escape(title)}</h2><pre>{html.escape(body)}</pre></div>')
    parts.append(FOOT)
    subject = f"OIE Daily Digest - {period} {datetime.now().strftime('%Y-%m-%d')}"
    return '\n'.join(parts), subject


def write_output(html_text: str, facts: dict) -> str:
    os.makedirs(LOGS, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M')
    html_path = os.path.join(LOGS, f'digest-{stamp}.html')
    with open(html_path, 'w') as f:
        f.write(html_text)
    facts_path = os.path.join(LOGS, f'digest-{stamp}.json')
    with open(facts_path, 'w') as f:
        json.dump({'generated': datetime.now().isoformat(), 'facts': facts}, f, indent=2)
    return html_path, facts_path


def load_email_cfg() -> dict:
    try:
        import yaml
        p = os.path.join(REPO, 'config', 'email.yaml')
        if os.path.exists(p):
            with open(p) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def send_email(html_text: str, subject: str, to: str, cfg: dict):
    smtp_host = cfg.get('smtp_host', 'smtp.gmail.com')
    smtp_port = int(cfg.get('smtp_port', 587))
    username, password = cfg.get('username', ''), cfg.get('password', '')
    if not username or not password:
        print("ERROR: --send requires config/email.yaml (see config/email.yaml.example)")
        sys.exit(1)
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = username
    msg['To'] = to or cfg.get('to', username)
    msg.attach(MIMEText(html_text, 'html'))
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)
    print(f"Email sent to {msg['To']}")


def main():
    ap = argparse.ArgumentParser(description='OIE Daily Digest - rich HTML of the full engine')
    ap.add_argument('--morning', action='store_true', help='Label as morning (07:00)')
    ap.add_argument('--evening', action='store_true', help='Label as evening (19:00)')
    ap.add_argument('--send', action='store_true', help='Deliver via SMTP (needs config/email.yaml). ' +
                                                       'Requires --html: emails the GENAI-EDITED file only (no duplicate).')
    ap.add_argument('--html', default='', help='Path to a previously generated digest HTML to email (send-only mode)')
    ap.add_argument('--to', default='', help='Override recipient')
    ap.add_argument('--skip-screener', action='store_true')
    ap.add_argument('--skip-oie', action='store_true')
    ap.add_argument('--no-external', action='store_true')
    args = ap.parse_args()

    # Send-only mode: email a previously generated (GenAI-edited) digest HTML.
    # The digest run itself NEVER sends — this prevents duplicate emails.
    if args.send:
        if not args.html:
            print("ERROR: --send requires --html <path> to the GenAI-edited digest HTML.")
            print("       The digest run does not email; edit the abstract first, then send ONCE:")
            print("       python3 skills/oie-daily-digest/scripts/daily_digest.py --send --html logs/digest-<ts>.html")
            sys.exit(1)
        if not os.path.exists(args.html):
            print(f"ERROR: --html file not found: {args.html}")
            sys.exit(1)
        with open(args.html) as f:
            html_text = f.read()
        subject = f"OIE Daily Digest - {args.html.split('/')[-1]}"
        send_email(html_text, subject, args.to, load_email_cfg())
        return

    period = 'Morning (07:00)' if args.morning else 'Evening (19:00)' if args.evening else 'Ad-hoc'
    ext = '--no-external' if args.no_external else ''
    py = sys.executable
    print(f"OIE Digest - {period} (repo: {REPO})", file=sys.stderr)

    sections = []
    sections.append(('💰', 'Portfolio (Real Account)',
                     run_cmd([py, 'scripts/portfolio.py'] + ([ext] if ext else []))))
    sections.append(('🌍', 'Market Sentiment (Macro + Watchlist)',
                     run_cmd([py, 'scripts/market_sentiment.py', '--watchlist'])))
    md_out = []
    for t in ['V', 'AAPL', 'MSFT', 'NVDA', 'GOOG']:
        md_out.append(run_cmd([py, 'scripts/market_data.py', t], timeout=180))
    sections.append(('📈', 'Market Data (Key Holdings)', '\n\n'.join(md_out)))
    if not args.skip_screener:
        sections.append(('🎯', 'Screener (Top Candidates)',
                         run_cmd([py, 'scripts/screener.py', '--top', '5'] + ([ext] if ext else []))))
    if not args.skip_oie:
        sections.append(('🧪', 'OIE Paper Engine (Simulated Cycle)',
                         run_cmd([py, 'scripts/oie_engine.py', 'once', '--dry-run', '--force'])))

    facts = extract_metrics(sections[0][2])
    html_text, subject = build_html(sections, period, facts)
    html_path, facts_path = write_output(html_text, facts)
    print(f"\nHTML: {html_path}")
    print(f"Facts: {facts_path}  (feed this to the GenAI abstract step)")
    print("\nNext: let GenAI replace the <div id=\"abstract\"> bullets, then send ONE email:")
    print(f"  python3 skills/oie-daily-digest/scripts/daily_digest.py --send --html {html_path}")


if __name__ == '__main__':
    main()