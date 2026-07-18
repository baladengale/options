# Loss Management Playbook — CC/CSP Wheel

**For**: 40-year-old Singapore-resident wheel trader. Covered calls + cash-secured puts only — no spreads, no naked options, no margin. Quality US large-caps, 30–45 DTE entries, 15–30 min/day management, deterministic rules engine (`rules.yaml` + `GOAL.md`).

**Thesis of this playbook**: the wheel's losses don't come from the options — they come from the stock you end up holding. Manage the option leg mechanically, manage the stock leg with pre-written thesis-break and backstop rules, and size so that a correlated bear market can't force your hand.

---

## 1. What the Evidence Says About Wheel Losses

- 94–99% of the wheel's total return is attributable to the underlying stock, not option premium (SPY 45-DTE backtest, 2007–2024, 2,200+ trades, 10 configurations). The options leg is a risk-damper, not a return engine — so **loss management is stock-leg management**. — [Spintwig](https://spintwig.com/spy-wheel-45-dte-options-backtest/)
- 30+ years of CBOE benchmark data: BXM 9.0%, PUT 10.1%, BXMD 10.8% annualized vs. S&P 500 10.3% — roughly market returns with ~⅓ less volatility (9.9–10.6% vs 14.9%) and ~24% smaller average max drawdowns. Premium selling buys smoother, not bigger. — [CBOE](https://www.cboe.com/insights/posts/benchmark-indices-series-income-generation-and-smoother-returns-with-cboes-bxm-bxmd-put-and-cmbo-indices)
- 2022 bear: SPY wheel (25Δ, 30 DTE, 50% profit-take) returned −2% vs SPY −19%. 2020 V-recovery: wheel +4% vs SPY +18%. Full 5 years: +41% vs +58%. The insurance is real and it costs upside. — [ApexVol backtest](https://apexvol.com/strategies/wheel-strategy/backtest)
- BXM (covered-call index) 2022: −11.37% vs S&P −18.17%; 2008: −28.65% vs −37%. — [YCharts BXM](https://ycharts.com/indices/%5EBXM)
- Real-money confirmation: a $100k wheel account returned +0.34% Oct 2021–Oct 2022 (even after $7,200 withdrawals) vs S&P −14.28%. — [Seeking Alpha](https://seekingalpha.com/article/4545696-how-i-survived-the-bear-market)
- **The win-rate illusion**: across 20 wheel parameter combos (2018–2025), average win rate was 96% — even the worst config won 89% of trades while returning 2.1% annualized. High win rate is a structural feature of short premium, not evidence your system works. The rare losses decide everything. — [The Intrinsic Investor](https://theintrinsicinvestor.com/research/wheel-strategy/)
- Loss distribution for 0.20–0.30Δ short puts: skewness −3.27, kurtosis ~12, worst single loss −1,486% of premium collected. One tail event can erase years of premium. — [HKBU academic study](https://scholars.hkbu.edu.hk/ws/portalfiles/portal/55023608/OA-0307.pdf)
- **The fatal regime is the long bear, not the crash**: 2000–2013 left the S&P underwater 13 years (−56.8% max DD). Calls struck at old highs collect near-zero premium for a decade. 2022 was "unusually short and shallow" — do not extrapolate the wheel's 2022 win. — [Early Retirement Now](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/)
- The wheel's second-worst path is gap-down-then-V: 2020 assigned puts near the low, then the CC leg capped the entire rebound. Plan specifically for this scenario (see §4 on strike resets). — [The Intrinsic Investor](https://theintrinsicinvestor.com/research/wheel-strategy/)

## 2. Loss Anatomy: Delta Creep and Hidden Leverage

- Assignment converts a ~0.20Δ position into a 1.00Δ position — exactly as the price falls. "The more the stock falls, the more Delta." The wheel structurally adds exposure into declines; your sizing must assume this. — [Early Retirement Now](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/)
- "Cash-secured" is not low-risk: it is full-notional equity exposure deferred until assignment. Count every open CSP's notional as equity exposure when sizing. — [Early Retirement Now](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/)
- After assignment, shares (1.0Δ) minus a 25Δ CC still ≈ 0.75Δ net — roughly 3× the exposure you originally chose. Run a post-assignment delta/concentration check before selling the next call. — [Early Retirement Now](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/)
- Correlation is regime-dependent: pairs averaging 0.3 correlation reach 0.7–0.8 on the worst 5% of days; March 2020 intra-equity correlation hit 0.95+. Eight large-cap wheel positions behave like 2–3 independent bets in a crash — assume simultaneous assignment across the book. — [Pomegra](https://pomegra.io/learn/library/track-e-trading-risk/risk-management/chapter-05-portfolio-risk/hidden-correlations-in-crashes)
- 2022 flipped stock-bond correlation to +0.30, breaking the classic hedge — for a premium seller, **cash reserves are the only reliable dry powder** when everything falls together. — [CFA Institute](https://rpc.cfainstitute.org/blogs/enterprising-investor/2026/why-static-portfolios-fail-when-risk-regimes-change)

## 3. Option-Leg Rules: Loss Discipline Before Assignment

**The evidence-backed core is profit-taking and time management, not stop-losses.**

- Close at 50% of max profit: 16Δ 45-DTE backtest (2005–2018) showed P&L per day of $2.04 vs $1.18 holding to expiration (+73%), with duration falling from 45 to ~15 days and risk flat or lower. — [eDeltaPro](https://www.edeltapro.com/blog/managing-winners)
- Why it works: past 50% captured, risk/reward inverts — at 50% profit you risk ~4:1 against you for the remainder; at 80% profit, ~11.5:1. — [ApexVol](https://apexvol.com/strategies/wheel-strategy)
- Manage at 21 DTE: gamma roughly doubles between 21 and 7 DTE for ATM options; a 200k+ trade study found closing at 21 DTE improved risk-adjusted returns 15–20% vs holding to expiration. — [Days to Expiry](https://www.daystoexpiry.com/blog/the-21-dte-rule-explained-when-and-why-to-close-options-positions-early)
- Managing at 21 DTE collapses realized probability-of-touch to ~0.8× delta for puts (vs ~2× delta held to expiry) — most "tested" positions never actually get touched if you exit early. — [tastylive](https://www.tastylive.com/news-insights/options-trading-exploring-probability-touch-various-deltas)
- **Stop-losses on short premium are contested — know both sides.** tastytrade's own studies found managing losers at 2× credit *underperformed* holding to expiration ([Market Measures 2018](https://www.tastylive.com/shows/market-measures/episodes/managing-losers-in-spreads-09-27-2018)), and a 1×–5× stop-loss sweep on 16Δ SPY puts found 50%-profit management alone beat every stop variant ([Market Measures 2015](https://www.tastylive.com/shows/market-measures/episodes/short-puts-managing-winners-losers-09-01-2015)). Stops trigger on IV noise and bid-ask spread, and small credits hit "2×" almost by default. — [Harsha Zampi](https://harshazampi.com/options-trading-guide/ref-management-adjustments/)
- The synthesis: use **2× credit received as an alert-level review trigger, not an automatic close**. It is the point where you must actively choose: roll for credit, take assignment, or exit — tastytrade frames 2× as the defensive decision point with rolls executed 7–21 DTE. — [tastylive: Defending Positions](https://www.tastylive.com/concepts-strategies/defending-positions)

**Rolling discipline (the anti-"roll forever" rules):**

- Roll for net credit only — paying a debit to roll worsens breakeven and re-opens a below-50%-POP trade. — [tastylive](https://www.tastylive.com/concepts-strategies/defending-positions)
- Act early: start planning defense when the stock is within 5% of your short strike, and treat 0.40Δ as "decision time" — beyond 0.50Δ rolling gets expensive and options narrow. — [Options Trading IQ](https://optionstradingiq.com/put-rolling-strategies/)
- Cap the campaign: maximum 2–3 rolls on the same position; extend 30–45 days minimum per roll (weeklies churn commissions); if only a 90+ DTE roll produces a credit, the position is broken — close it. — [Options Trading IQ](https://optionstradingiq.com/put-rolling-strategies/)
- Account for the whole chain as one campaign (sum of all leg cash flows). Three or more rolls with successively lower strikes = broken thesis, not bad luck. — [Options Trading IQ](https://optionstradingiq.com/put-rolling-strategies/)

**Taking assignment vs buying back:**

- Take assignment when ALL hold: thesis intact, adjusted basis acceptable, no roll-for-credit available, position within size limits. "Exit the wheel when the thesis breaks, not when the price drops." — [ApexVol checklist](https://apexvol.com/learn/wheel-strategy-checklist)
- The algebra: buy-to-close breakeven = current price + remaining extrinsic value. Near expiry extrinsic ≈ 0, so buy-back and assignment converge; deep ITM (>10%) has no extrinsic and rolling needs 60+ DTE for a marginal credit — usually take the assignment. — [practitioner framework](https://github.com/ssandy33/regress/issues/319)

## 4. Post-Assignment Playbook: Underwater Shares

- **Default action**: immediately sell a 25Δ, 30–45 DTE covered call at or above adjusted cost basis. Deviate only if the strike-at-basis pays nothing (deep underwater) or the thesis broke (exit instead). — [ApexVol checklist](https://apexvol.com/learn/wheel-strategy-checklist)
- Adjusted basis = strike − Σ(all premiums collected, puts and calls). Example: $250 assignment reduced to $232.45 effective basis after $17.45 of accumulated premium. Track this yourself — brokers don't. — [Barchart](https://www.barchart.com/education/wheel-strategy)
- **Beware the basis-reduction sunk-cost trap**: collected premium is already yours; it is not a shield against future losses. If the stock fell $100→$60 and your "adjusted basis" is $92, the decision input is the $60 stock's forward prospects — not the $92 anchor. The adjusted-basis frame makes traders hold broken positions too long. — [Early Retirement Now](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/)
- Moderately underwater (≤10% below basis): sell the CC at basis even for thin premium — small credits still grind the basis down and the ceiling only binds if the stock recovers to it. — [ApexVol checklist](https://apexvol.com/learn/wheel-strategy-checklist)
- Deeply underwater (15%+ below basis) is the **dead zone**: a basis-strike call pays ~$0.01–0.05. Choices are (a) hold unencumbered and wait, or (b) thesis-check → exit and redeploy. Sitting in the dead zone generates no income while tying up capital that could wheel another name. — [ApexVol checklist](https://apexvol.com/learn/wheel-strategy-checklist)
- **Anchoring to old basis is mathematically destructive**: a 2020–2026 backtest comparing basis-anchored strikes vs dynamically reset strikes (105% of current price) returned +593% vs +1,347% — the anchored version collected nothing in drawdowns, then got called at breakeven in the V-reversal and missed the rally (crypto data; directionally instructive for equities). — [ApexVol backtest](https://apexvol.com/strategies/wheel-strategy/backtest)
- Never ratchet a CC strike *down* to harvest more premium on a falling stock — the death-spiral guard. The widely used thetagang bot enforces this as `maintain_high_water_mark = true`. — [thetagang bot](https://explore.market.dev/ecosystems/python/projects/thetagang)
- Averaging down with more CSPs on the same name is justified only if: the drop is macro/sector-wide (peers down too, not idiosyncratic), the thesis is intact, and the ≤15% position cap still holds after assignment. Otherwise you're catching the falling knife at 1.0Δ. — [Early Retirement Now](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/)
- **Capacity math** — compute before deciding to "wheel it back": `months_to_recover = price_gap_per_share / avg_monthly_CC_premium_per_share`. At 1–2% monthly premium, a 20% gap takes 10–20 uninterrupted months. "Larger accounts can hold the losing trade and roll it into perpetuity" — smaller accounts cannot; if months_to_recover > 12, flag for redeployment. — [tastylive](https://www.tastylive.com/concepts-strategies/defending-positions)
- Breached CC (stock rallies through strike): <5 DTE and deep ITM → let assignment happen; transitory rally → roll up-and-out for a credit; durable rally (strong quarter, upgrades) → let it go and redeploy; never roll for a net debit. — [Snider Advisors](https://www.snideradvisors.com/blog/when-should-you-roll-a-covered-call/)

## 5. Thesis-Break Exit Criteria (Codable)

Every gate below is expressible as a threshold on fetchable data. A stock failing gates here gets sold *despite* the loss — this is the "thesis + backstop" half of your exit philosophy.

| Gate | Codable rule | Evidence | Source |
|---|---|---|---|
| Growth stall | YoY revenue growth < 0 for 2 consecutive quarters → `THESIS_BREAK` | 93% of stalled companies never regain 2% growth; 69% lose ≥half their market cap | [Forbes/Hartung](https://www.forbes.com/sites/adamhartung/2014/08/13/mcdonalds-growth-stall-predicts-deadly-problems/) |
| Dual deceleration | Revenue AND EPS growth both decelerating 3 consecutive quarters → `SELL_FLAG` | Confirmed sell signal, not noise | [IBD](https://www.investors.com/how-to-invest/investors-corner/prolonged-falling-profit-growth-can-mark-sell-signal-for-stocks/) |
| Moat erosion | Gross margin −100bps over 2 years AND declining YoY 3 consecutive quarters → `MOAT_EROSION` | Margin compression = pricing-power loss | [Pomegra](https://pomegra.io/learn/library/track-c-strategies/growth-investing/chapter-07-moats/moat-narrowing-red-flags) |
| Value destruction | ROIC < WACC with worsening 4-quarter trend → `VALUE_DESTRUCTION` | Flagged names fell ~47% while S&P rose 14% | [New Constructs](https://www.newconstructs.com/wp-content/uploads/2019/05/NewConstructs_Danger_Zone_IBKR-2019-04-30.pdf) |
| Balance sheet | Debt/EBITDA > 4.5× → `BALANCE_SHEET_STRESS` | >6× leaves no margin; 10% revenue drop breaches covenants | [CreditPulse](https://www.creditpulse.com/blog/bankruptcy-prediction-b2b-credit) |
| Estimate break | Net analyst revision breadth < −25% or bottom quintile of revisions → `ESTIMATE_BREAK` | Bottom quintile returned 4.5%/yr vs 11.2% top quintile (2006–2026); drift persists months | [Accelerate](https://accelerateshares.com/research/alpharank-top-stocks-the-predictive-power-of-changing-expectations/) |
| Guidance | ≥1 downward guidance cut in trailing 4 quarters + falling forward EPS → `GUIDANCE_FLAG` | A single cut carries a −9.3% penalty persisting 3+ quarters | [Woolridge study](https://psc.ky.gov/pscecf/2016-00371/rateintervention%40ky.gov/03312017042942/Woolridge_R-S.pdf) |
| Cash flow | FCF margin < 20th percentile of industry OR (FCF yield < 2% AND net capital raising) → `CASH_FLOW_BREAK` | Strongest combined negative signal in the accounting literature | [GMT Research](https://www.gmtresearch.com/en/accounting-ratio/free-cash-flow-margin/) |
| Pre-break dashboard | 3 of 4 leading indicators (inventory/revenue, DSO, SG&A/revenue, gross margin) deteriorating 2 consecutive quarters → `PRE_BREAK_WARNING` | Signals rise almost monotonically in the 3 quarters before earnings strings break; sales growth fades 12.65% → 5.41% | [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1475770219000036) |
| Cyclical vs structural | Peers drawing down similarly → cyclical (hold bias); idiosyncratic drawdown → structural (exit bias) | Standard tools repair cyclical weakness, not disruption | [Howard Marks](https://advisoranalyst.com/2020/10/14/howard-marks-latest-memo-coming-into-focus.html/) |

- **Kill criteria at entry**: before any CSP is sold, write 3–5 falsifiable thesis pillars per ticker, each with `{metric, kill_threshold, deadline}` plus a pre-mortem ("it's 2 years later and this failed — why?"). Exit conditions written at entry are immune to sunk-cost drift. — [Annie Duke](https://www.annieduke.com/the-investors-podcast-the-art-of-decision-making-w-annie-duke/)
- Operationalized as a per-ticker scorecard: pillars with green/amber/red status, a catalyst calendar, and a logged action per new data point (No Change / Increase / Trim / Exit); review at least quarterly. — [thesis-tracker skill](https://skillsmp.com/creators/changhochien/pi-financial-services/plugins-vertical-plugins-equity-research-skills-thesis-tracker)

*Data availability*: revenue, EPS, margins, D/E, FCF are already in your fundamental module (yfinance). Revision breadth and guidance-cut counts need an estimates source — implement those two gates only when data is wired.

## 6. Price Backstops & Recovery Base Rates

- Stop-losses only add value in trending (momentum) markets: a 10% trailing stop added 50–100bps/month during stopped-out periods in momentum regimes but *reduces* expected return in random-walk conditions. Make the stop conditional on regime. — [Kaminski & Lo](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338)
- Regime exit rule: close below 200-day SMA **and** 200-day SMA slope negative → `REGIME_EXIT`. (S&P above its 200-day: +13.2% annualized; below: −0.6%, since 1950. Death cross alone false-signals 30–35% of the time — require the slope condition.) — [TradeAlgo](https://www.tradealgo.com/trading-guides/technical-analysis/moving-averages-guide)
- Hard stop for assigned quality large-caps: **−30% from adjusted basis** — "premium won't save you"; a 40% decline needs years of premium to recover. (O'Neil's famous 7–8% rule is for new momentum buys, not wheel assignments — but its asymmetry math stands: −7% needs +7.5%; −30% needs +43%; −50% needs +100%.) — [ApexVol](https://apexvol.com/strategies/wheel-strategy), [IBD](https://www.investors.com/how-to-invest/investors-corner/why-you-cut-losses-in-stocks/)
- **Recovery base rates** (6,500+ US stocks, 1985–2024, Mauboussin/Callahan): from a 50–55% drawdown, 77% recover (median 2.0 yrs); 70–75%: 62% recover (3.4 yrs); 80–85%: 49% (4.2 yrs); 95%+: 16% (8.0 yrs). Below ~80% drawdown the odds still favor recovery for the median name — beyond it, holding is statistically wrong. — [Acquirer's Multiple/Mauboussin](https://acquirersmultiple.com/2025/05/michael-mauboussin-the-hard-truth-about-drawdowns/)
- Sobering complement: the median stock only ever recovers to 89.5% of its prior peak — permanent partial loss is the *norm*; aggregate market recoveries are driven by outliers. Even the top-20 stocks of 1985–2024 had a median max drawdown of 72% (4.3 yrs to recover). Quality selection is what earns the right to hold. — [Alpha Architect](https://alphaarchitect.com/buy-and-hold-on-for-dear-life-think-again/)
- Time stop: 6–12 months with no progress toward thesis → review against opportunity cost. Codable: if `position_total_return(N months) < CC/CSP_premium_yield_alternative(N months)` by 2:1, flag `OPPORTUNITY_COST_EXIT`. — [Suresh Gopalan](https://sureshgopalan.in/education/advanced_stop_loss_strategies.html)

## 7. Portfolio Drawdown Control

- Converging practitioner caps: single CSP ≤5% of account, per-trade risk ≤2%, one ticker/theme ≤20%, cash reserve ≥20%, total short-premium exposure 30–60% of buying power in normal vol, scale down when VIX >25. Your existing 15% single-position rule is a *ceiling* — 5–10% per CSP name is the working target. — [The Option Premium](https://www.theoptionpremium.com/p/options-trading-risk-management-playbook), [Options Trading IQ](https://optionstradingiq.com/put-rolling-strategies/)
- Sizing math: use fractional Kelly (¼–½), never full Kelly — premium selling's fat tails violate Kelly's known-probability assumption. A 2025 academic test of put-writing sizing found a hybrid Kelly × VIX-percentile method best balanced return vs drawdown (and VIX9D beat VIX30D for short-dated risk). — [Wysocki, arXiv](https://arxiv.org/html/2508.16598v1)
- **The low-VIX paradox — your BULLISH row is the dangerous one**: when VIX <15, a 1% market drop spikes VIX far harder (probability VIX falls on a down day: 1-in-25 low-VIX vs 1-in-4 above 20), and thin premium doesn't pay for the asymmetric jump risk. Practitioner scaling actually *reduces* size 30–50% below VIX 15; optimal zone is VIX 15–22. Your current table allows 100% size at VIX <15 — this contradicts the evidence and is flagged in §11. — [Volatility Box](https://volatilitybox.com/research/position-sizing-with-volatility/)
- Wheel drawdown reference points: SPY wheel −13% max DD vs −22% buy-and-hold; but single names run hotter — AAPL wheel −24%, KO −8%. Budget single-name drawdowns at ~2× the index wheel number. — [ApexVol backtest](https://apexvol.com/strategies/wheel-strategy/backtest)
- Lowest-drawdown configurations cluster at 10–20Δ / 60 DTE (max DD −11.0 to −11.6%, best Sharpe 0.65) — lower delta and longer DTE is the defensive dial when you want to stay active but cut risk. — [The Intrinsic Investor](https://theintrinsicinvestor.com/research/wheel-strategy/)
- Panic-selling is the most expensive mistake in the whole system: an investor who sold after a −30% loss in 2008 and re-entered in 2012 ended with 43% less wealth than one who stayed invested (through end-2015, both withdrawing $50k/yr). Exits belong to the *rules* (§5, §6), never to the drawdown itself. — [Advisor Perspectives](https://www.advisorperspectives.com/commentaries/2016/02/11/think-before-you-sell)
- Income is natural sequence-risk mitigation: in 2008, S&P dividends fell far less than prices, letting income investors avoid selling the bottom — wheel premium plays the same role (it kept flowing through 2022), reducing forced liquidation risk. — [Por Dividendos](https://www.pordividendos.com/en/wiki/drawdown), [QueenVest](https://queenvestllc.com/blog-posts/the-retirement-risk-no-one-talks-about-jim-kathy-roger-and-sally)

## 8. Singapore-Resident Structural Notes

**The good news first — then the two traps.**

- No capital gains tax on personal investment gains; symmetrically, **losses are not deductible**. There is no tax-loss harvesting and no wash-sale rule — every exit decision is purely economic, with zero tax distortion on when or how you roll. This simplifies the entire playbook: §5/§6 rules run on economics alone. — [PwCo](https://www.pwco.com.sg/guides/capital-gain-tax-singapore/)
- Option premium received by a non-US person is not FDAP income and faces **no US withholding** — premium arrives 100% gross. — [StashAway](https://www.stashaway.sg/r/dividend-withholding-tax-estate-tax-us-equities-singapore)
- Section 871(m) dividend-equivalent withholding does not apply to standard listed options (transition relief through 2026, Notice 2024-44; only delta-one instruments in scope). — [PwC](https://www.pwc.com/us/en/services/tax/library/section-871m-dividend-equivalent-rules-phasein-period-extended-2.html)

**Trap 1 — IRAS "badges of trade":**

- Gains are non-taxable only while you're an *investor*. IRAS applies the badges of trade (frequency, holding period, systematic organization, intent) holistically — a wheel running 60–100+ option transactions/year across 5–8 names with rule-based triggers has a non-trivial resemblance to a trade/business, which would make all of it (premium included) taxable income. — [IRAS](https://www.iras.gov.sg/taxes/individual-income-tax/basics-of-individual-income-tax/what-is-taxable-what-is-not/gains-from-sale-of-property-shares-and-financial-instruments), [OANDA/Grant Thornton](https://www.oanda.com/sg-en/skills-and-insights/news-and-views/tax-implications--retail-trading/)
- Case law confirms the holistic test: *Comptroller v BBO* [2014] SGCA (no single factor decisive); *NP v Comptroller* [2007] (8 transactions in 8 years partially assessed as trading). Protective practices: document long-term investment intent per holding, hold assigned shares for months rather than days, keep the universe limited and quality-based. — [ISCA](https://ca-lab.isca.org.sg/technicalities/gains-or-losses-from-sale-of-foreign-assets-part-1/)
- No IRAS ruling specifically addresses individual CC/CSP writers — the premium's character follows the overall activity's character. Given 20+ years of intended activity, a one-time consult with an SG tax advisor who understands options is cheap insurance. — [EBOS](https://ebos-sg.com/are-gains-from-property-shares-and-financial-instruments-taxable-in-singapore/)

**Trap 2 — the US side:**

- Dividends: 30% flat withholding, permanently — no US–SG tax treaty exists. W-8BEN (renew every 3 years) prevents backup withholding but cannot reduce the 30%. A 3% yielder nets 2.1% — factor this into every "hold the dividend payer through the drawdown" decision and into §4 capacity math. — [StashAway](https://www.stashaway.sg/r/dividend-withholding-tax-estate-tax-us-equities-singapore), [Saxo SG](https://www.help.saxo/hc/en-sg/articles/19502740551197-Do-I-need-to-submit-W-8BEN-form)
- **US estate tax**: US-listed stocks are US-situs assets regardless of broker; above a $60,000 exemption (frozen since 1976), rates run 18–40%. A six-figure wheel portfolio at age 40 is materially exposed; executors must file Form 706-NA within 9 months and accounts can freeze 6–18 months. Mitigations to research separately: term insurance sized to the liability, holding structures, and Ireland-domiciled UCITS for the non-wheel sleeve. — [StashAway](https://www.stashaway.sg/r/dividend-withholding-tax-estate-tax-us-equities-singapore), [US Tax FS](https://www.ustaxfs.com/insights/us-estate-tax-for-non-residents-2026/)
- The core-sleeve fix: Ireland-domiciled UCITS ETFs (CSPX/VWRA) cut the dividend leak to 15% at fund level and eliminate US estate exposure — but moomoo SG doesn't carry LSE-listed UCITS; a second broker (commonly IBKR) is the standard answer for the passive sleeve while the wheel stays at moomoo. — [StashAway UCITS guide](https://www.stashaway.sg/r/guide-ireland-domiciled-etfs-singapore)
- FX drag: moomoo SG's SGD↔USD spread runs ~0.1–0.3% (to 0.6% in stress). Wheel cash cycles through conversions repeatedly — keep the account in USD and convert deliberately, not per-trade. — [The Kopi Notes](https://thekopinotes.com/articles/etfs/moomoo-trading-fees-singapore-etf-guide/)

## 9. [FLAGGED] Beyond the Wheel — Outside Your Constraints

*Awareness only. These are the tools you are consciously not using; here is what they cost, so the constraint stays a decision rather than a blind spot.*

- Collars (long stock + protective put + short call): SPY 5-year backtest +6.1%/yr vs +9.0% buy-and-hold with max DD −8% vs −22% — essentially all of the edge came from 2022; in every up year the collar trailed by 5–15 points. — [ApexVol collar backtest](https://apexvol.com/strategies/collar/backtest)
- The 107-year Dimson-Marsh-Staunton evidence: a 5%-floor collar cut US equity returns from 9.77% to ~6.2%/yr; a tight 1% floor left just 0.4%/yr — protection priced away nearly the whole equity risk premium. "It is relatively expensive to engineer a protected portfolio." — [Swedroe/ETF.com](https://www.etf.com/sections/index-investor-corner/swedroe-beware-drag-collar-strategies)
- AQR (Israelov & Klein): the CBOE collar index matched a simple reduced-equity portfolio's return with ~2× its volatility — "an investor is better off simply reducing her equity exposure." — [Swedroe/ETF.com](https://www.etf.com/sections/index-investor-corner/swedroe-beware-drag-collar-strategies)
- Protective puts (CBOE PPUT, monthly 5% OTM): brilliant in the sharp 2020 crash (−11.8% max DD vs −33%) but moved in lock-step with the S&P through 2022's slow grind — monthly OTM puts expire worthless when losses stay inside the 5% deductible, and re-hedging at elevated IV is ruinous. Sustained protection needs LEAPS. — [Swan Global](https://www.swanglobalinvestments.com/short-term-vs-long-term-hedging/)
- Why you skip them: your regime-scaled sizing, cash reserve, and quality screens target the same drawdown reduction these overlays buy — without the ~3%/yr drag documented above. The trade-off you accept in exchange: no protection against overnight gap catastrophes in individual names (§6's diversification and sizing rules are the only defense).

## 10. The Daily 15–30 Minute Routine

Ordered checklist — most items produce no action ("give yourself permission to do nothing"):

1. **Delta & distance scan** — every short option's delta and price-vs-strike; flag ≥0.40Δ or within 5% of strike → run §3 rolling decision. — [The Option Premium](https://www.theoptionpremium.com/p/options-trading-risk-management-playbook)
2. **Profit orders** — check GTC 50%-profit closes; re-arm any fills with new positions only if regime and sizing gates pass. — [The Option Premium](https://www.theoptionpremium.com/p/options-trading-risk-management-playbook)
3. **DTE scan** — anything ≤21 DTE gets a decision *today*: close, roll (credit-only), or accept assignment. — [Days to Expiry](https://www.daystoexpiry.com/blog/the-21-dte-rule-explained-when-and-why-to-close-options-positions-early)
4. **Loss alerts** — any option at ≥2× credit received → run the roll / assign / exit tree (§3). — [tastylive](https://www.tastylive.com/concepts-strategies/defending-positions)
5. **Thesis dashboard** — any §5 pillar flipped green→amber or amber→red on new filings/data.
6. **Cash, reserve, regime** — reserve ≥ regime minimum; VIX band unchanged?
7. **Weekly add-ons** (one session): refresh IV Rank and expected move across the watchlist, rebalance so no ticker/theme >20%, plan rolls *before* they're forced, log all trades Friday. — [MyATMM](https://www.myatmm.com/blog/wheel-strategy-portfolio-management-multiple-positions.html)

## 11. Proposed Rule Changes — rules.yaml / GOAL.md

**Status: UNVALIDATED — backtest pending.** Per your own architecture principle, these should pass `pytest` + a historical backtest before live orders depend on them.

| # | Proposal | Where | Basis |
|---|---|---|---|
| 1 | Extend 50% profit-take to CSPs (currently CC-only in GOAL.md §6) with GTC orders at entry | `options.exits.profit_take_pct: 50` | [eDeltaPro](https://www.edeltapro.com/blog/managing-winners), [ApexVol](https://apexvol.com/strategies/wheel-strategy) |
| 2 | Make 21 DTE a universal management point (close/roll/decide), not just "underwater → consider rolling" | `options.exits.management_dte: 21` | [Days to Expiry](https://www.daystoexpiry.com/blog/the-21-dte-rule-explained-when-and-why-to-close-options-positions-early), [tastylive](https://www.tastylive.com/news-insights/options-trading-exploring-probability-touch-various-deltas) |
| 3 | Loss alert (not auto-close) at 2× credit received; alert also at 0.40Δ or price within 5% of strike | `options.exits.loss_alert_multiple: 2.0`, `decision_delta: 0.40` | [tastylive](https://www.tastylive.com/concepts-strategies/defending-positions), [Options Trading IQ](https://optionstradingiq.com/put-rolling-strategies/) |
| 4 | Roll campaign limits: net-credit only (exists), max 2 rolls, ≥30-day extension, broken-position test (credit requires >90 DTE → close) | `options.rolling.*` (new block) | [Options Trading IQ](https://optionstradingiq.com/put-rolling-strategies/) |
| 5 | Thesis-break gates from §5 as new constraint checks on *held* positions (start with the 6 yfinance-implementable ones) | new `holdings.thesis_gates` | §5 sources |
| 6 | Price backstop on assigned shares: exit at −30% from adjusted basis **if** below 200d SMA with negative slope; unconditional circuit breaker at −40% | new `holdings.backstop` | [ApexVol](https://apexvol.com/strategies/wheel-strategy), [Kaminski & Lo](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338), [Mauboussin](https://acquirersmultiple.com/2025/05/michael-mauboussin-the-hard-truth-about-drawdowns/) |
| 7 | Recovery-capacity flag: `months_to_recover > 12` → REDEPLOY review; time stop at 12 months stagnant vs premium-yield alternative | new `holdings.capacity` | [tastylive](https://www.tastylive.com/concepts-strategies/defending-positions), [Suresh Gopalan](https://sureshgopalan.in/education/advanced_stop_loss_strategies.html) |
| 8 | Per-name CSP notional target 10% (15% stays as hard ceiling); count open CSP notional as equity exposure in concentration checks | `sizing.csp_notional_target_pct: 10` | [The Option Premium](https://www.theoptionpremium.com/p/options-trading-risk-management-playbook), [ERN](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/) |
| 9 | ⚠️ **DECISION NEEDED**: cap BULLISH (VIX<15) position size at ~75–80% instead of 100% — evidence says low-VIX is the asymmetric-risk zone; contradicts your current regime table | `regime.position_mult.BULLISH` | [Volatility Box](https://volatilitybox.com/research/position-sizing-with-volatility/), [Wysocki](https://arxiv.org/html/2508.16598v1) |
| 10 | ⚠️ **DECISION NEEDED**: "never CC below basis" refinement — keep for drawdowns ≤15%, but for deeper drawdowns require an explicit thesis-check outcome: thesis broken → exit; thesis intact → either hold unencumbered *or* consciously reset strikes below basis with the anchoring evidence in view | GOAL.md §6 | [ApexVol](https://apexvol.com/strategies/wheel-strategy/backtest), [ERN](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/) |
| 11 | Ops (non-engine): W-8BEN renewal reminder (3-yearly), annual estate-tax exposure review, USD-balance FX policy, one-time SG tax consult on badges-of-trade documentation | ops checklist | §8 sources |

---

## Sources

1. https://spintwig.com/spy-wheel-45-dte-options-backtest/
2. https://www.cboe.com/insights/posts/benchmark-indices-series-income-generation-and-smoother-returns-with-cboes-bxm-bxmd-put-and-cmbo-indices
3. https://apexvol.com/strategies/wheel-strategy/backtest
4. https://apexvol.com/strategies/wheel-strategy
5. https://apexvol.com/learn/wheel-strategy-checklist
6. https://apexvol.com/strategies/collar/backtest
7. https://theintrinsicinvestor.com/research/wheel-strategy/
8. https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/
9. https://scholars.hkbu.edu.hk/ws/portalfiles/portal/55023608/OA-0307.pdf
10. https://ycharts.com/indices/%5EBXM
11. https://seekingalpha.com/article/4545696-how-i-survived-the-bear-market
12. https://www.barchart.com/education/wheel-strategy
13. https://explore.market.dev/ecosystems/python/projects/thetagang
14. https://www.snideradvisors.com/blog/when-should-you-roll-a-covered-call/
15. https://github.com/ssandy33/regress/issues/319
16. https://www.tastylive.com/concepts-strategies/defending-positions
17. https://www.tastylive.com/shows/market-measures/episodes/managing-losers-in-spreads-09-27-2018
18. https://www.tastylive.com/shows/market-measures/episodes/short-puts-managing-winners-losers-09-01-2015
19. https://www.tastylive.com/news-insights/options-trading-exploring-probability-touch-various-deltas
20. https://www.daystoexpiry.com/blog/the-21-dte-rule-explained-when-and-why-to-close-options-positions-early
21. https://www.edeltapro.com/blog/managing-winners
22. https://harshazampi.com/options-trading-guide/ref-management-adjustments/
23. https://optionstradingiq.com/put-rolling-strategies/
24. https://www.theoptionpremium.com/p/options-trading-risk-management-playbook
25. https://www.myatmm.com/blog/wheel-strategy-portfolio-management-multiple-positions.html
26. https://arxiv.org/html/2508.16598v1
27. https://pomegra.io/learn/library/track-e-trading-risk/risk-management/chapter-05-portfolio-risk/hidden-correlations-in-crashes
28. https://rpc.cfainstitute.org/blogs/enterprising-investor/2026/why-static-portfolios-fail-when-risk-regimes-change
29. https://volatilitybox.com/research/position-sizing-with-volatility/
30. https://www.advisorperspectives.com/commentaries/2016/02/11/think-before-you-sell
31. https://queenvestllc.com/blog-posts/the-retirement-risk-no-one-talks-about-jim-kathy-roger-and-sally
32. https://www.pordividendos.com/en/wiki/drawdown
33. https://www.forbes.com/sites/adamhartung/2014/08/13/mcdonalds-growth-stall-predicts-deadly-problems/
34. https://www.investors.com/how-to-invest/investors-corner/prolonged-falling-profit-growth-can-mark-sell-signal-for-stocks/
35. https://www.investors.com/how-to-invest/investors-corner/why-you-cut-losses-in-stocks/
36. https://pomegra.io/learn/library/track-c-strategies/growth-investing/chapter-07-moats/moat-narrowing-red-flags
37. https://www.newconstructs.com/wp-content/uploads/2019/05/NewConstructs_Danger_Zone_IBKR-2019-04-30.pdf
38. https://www.creditpulse.com/blog/bankruptcy-prediction-b2b-credit
39. https://accelerateshares.com/research/alpharank-top-stocks-the-predictive-power-of-changing-expectations/
40. https://psc.ky.gov/pscecf/2016-00371/rateintervention%40ky.gov/03312017042942/Woolridge_R-S.pdf
42. https://www.gmtresearch.com/en/accounting-ratio/free-cash-flow-margin/
43. https://www.sciencedirect.com/science/article/abs/pii/S1475770219000036
44. https://advisoranalyst.com/2020/10/14/howard-marks-latest-memo-coming-into-focus.html/
45. https://www.annieduke.com/the-investors-podcast-the-art-of-decision-making-w-annie-duke/
46. https://skillsmp.com/creators/changhochien/pi-financial-services/plugins-vertical-plugins-equity-research-skills-thesis-tracker
47. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338
48. https://www.tradealgo.com/trading-guides/technical-analysis/moving-averages-guide
49. https://acquirersmultiple.com/2025/05/michael-mauboussin-the-hard-truth-about-drawdowns/
50. https://alphaarchitect.com/buy-and-hold-on-for-dear-life-think-again/
51. https://sureshgopalan.in/education/advanced_stop_loss_strategies.html
52. https://www.pwco.com.sg/guides/capital-gain-tax-singapore/
53. https://www.iras.gov.sg/taxes/individual-income-tax/basics-of-individual-income-tax/what-is-taxable-what-is-not/gains-from-sale-of-property-shares-and-financial-instruments
54. https://www.oanda.com/sg-en/skills-and-insights/news-and-views/tax-implications--retail-trading/
55. https://ca-lab.isca.org.sg/technicalities/gains-or-losses-from-sale-of-foreign-assets-part-1/
56. https://ebos-sg.com/are-gains-from-property-shares-and-financial-instruments-taxable-in-singapore/
57. https://www.stashaway.sg/r/dividend-withholding-tax-estate-tax-us-equities-singapore
58. https://www.help.saxo/hc/en-sg/articles/19502740551197-Do-I-need-to-submit-W-8BEN-form
59. https://www.ustaxfs.com/insights/us-estate-tax-for-non-residents-2026/
60. https://www.pwc.com/us/en/services/tax/library/section-871m-dividend-equivalent-rules-phasein-period-extended-2.html
61. https://www.stashaway.sg/r/guide-ireland-domiciled-etfs-singapore
62. https://thekopinotes.com/articles/etfs/moomoo-trading-fees-singapore-etf-guide/
63. https://www.etf.com/sections/index-investor-corner/swedroe-beware-drag-collar-strategies
64. https://www.swanglobalinvestments.com/short-term-vs-long-term-hedging/

---
*Captured: 2026-07-17*
*Method: 5 parallel research agents (assignment management, exit discipline, portfolio drawdown, thesis-break criteria, Singapore structural) → adversarial filter (unsourced/contradictory findings dropped) → synthesis. This is research information, not financial or tax advice; §8 items warrant professional confirmation.*
