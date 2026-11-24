# Regulatory & Market-Rule Reference Register

**Last verified:** 2026-11-23 (web research during build). Anything not re-verified that day is marked.
**Scope:** NSE/BSE cash equities, delivery basis, research and paper trading only.

> This register is a research aid, not legal advice. Exchange circulars and statutory rates change. Check the primary source before relying on any item, and re-verify this register at least quarterly (the monthly workflow reminds you through the cost-schedule `verification` flags).

## 1. How the rules apply to this platform

| Topic | Applies? | Why |
|---|---|---|
| SEBI retail algorithmic-trading framework (Feb-2025 circular and its extensions) | **Not triggered** | The framework governs algorithmic orders that reach an exchange through a broker's API. This platform has no broker connection, generates no exchange orders and has no code path that could place one. |
| Exchange implementation standards (algo ID tagging, static IP, order-per-second threshold, registration) | Not triggered | Same reason. If the platform were ever connected to a broker, every one of these would apply. It would also need broker empanelment and exchange approval, and that would be a separate, regulated project. |
| Data licensing (exchange market data, vendor terms) | **Applies** | Prices come from the exchanges' **public download archives** (bhavcopy and index files), fetched at a low rate with an honest User-Agent and no login or bot-protection circumvention, for **personal research** under the exchanges' website terms of use. Do not redistribute raw files or the database. **Commercial use or redistribution needs a data licence** (NSE Data & Analytics / NSE Indices, or BSE). Provenance is recorded for every import. |
| Statutory charges (STT, stamp duty, GST, SEBI fee) and exchange charges | Modelled | They are used to make simulated costs realistic. The schedule is date-effective (`config/transaction_costs.yaml`). |
| Price bands, circuit breakers, holidays, settlement | Modelled | They are used for execution realism and calendar logic. |

## 2. Register

| # | Item | Issuer / date | Key content (as used here) | Source | Verification |
|---|---|---|---|---|---|
| R1 | Safer participation of retail investors in algorithmic trading | SEBI, 04-Feb-2025 | Framework for retail algo trading through brokers: APIs, algo identification, broker responsibility, registration above an order-rate threshold. | https://www.sebi.gov.in (Legal → Circulars, Feb-2025) | Confirmed via secondary sources and SEBI extension circulars |
| R2 | Extension of timeline (first) | SEBI, 29-Jul-2025 (CIR/2025/108) | Effective date moved from 01-Aug-2025 to 01-Oct-2025. | Business Standard / Angel One reports | Secondary |
| R3 | Extension of timeline (glide path) | SEBI, 30-Sep-2025 (CIR/2025/132) | Phased glide path. Brokers missing milestones could not onboard new retail API-algo clients from 05-Jan-2026. Full applicability **01-Apr-2026**. | https://www.sebi.gov.in/legal/circulars/sep-2025/extension-of-timeline-for-implementation-of-sebi-circular-dated-february-04-2025-on-safer-participation-of-retail-investors-in-algorithmic-trading-_96979.html | Primary URL located |
| R4 | Implementation standards for retail algo participation | NSE, 05-May-2025 | 10 orders/sec per exchange segment threshold. Algos above it need exchange registration. All algo orders are tagged. Static IP per API key. Daily logout of API sessions. | Zerodha Z-Connect summary; NSE circular | Secondary |
| R5 | FAQ on retail algo framework | NSE, 03-Nov-2025 | Clarifications, e.g. a vendor algo hosted on a broker uses the broker's static IP. | https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail_Algo_03112025_NSE.pdf | Primary URL located |
| R6 | Adherence to milestones (retail algo) | BSE, 24-Nov-2025 | BSE counterpart notices on the glide path. | Circular mirror (rhnvrm.github.io/stock-market-circulars) | Secondary |
| R7 | Revision in transaction charges | NSE, 27-Feb-2026 (FA73061), effective 01-Mar-2026 | IPFT contribution cut to ₹0.01/crore. Transaction charge raised so the cash-market total stays about ₹307/crore. | https://nsearchives.nseindia.com/content/circulars/FA73061.pdf | Primary URL located; figures from summary |
| R8 | Uniform exchange charges (SEBI) → NSE cash ₹297/crore | NSE/BSE revisions effective 01-Oct-2024 | Flat transaction charges replacing slab structures. | Outlook Business / Business Standard reports | Secondary |
| R9 | Changes in rates of STT | NSE, 31-Mar-2026; Finance Act 2026 | Delivery equity STT **unchanged at 0.1% buy and sell**. Intraday sell 0.025% unchanged. F&O raised from 01-Apr-2026 (futures 0.05%, options 0.15%). F&O is out of scope here. | NSE circular mirror; Business Standard 01-Feb-2026 | Secondary (multiple consistent) |
| R10 | SEBI turnover fee | SEBI / NSE investor page | 0.0001% (₹10/crore), plus 18% GST. | https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies | Primary |
| R11 | Stamp duty on securities | Indian Stamp Act 1899 as amended by Finance Act 2019; effective 01-Jul-2020 | 0.015% of delivery purchase value, buyer only. Collected through the exchange/clearing corporation. | NSE investor page (R10) | Primary |
| R12 | GST on brokerage and exchange charges | CGST/IGST Acts 2017 | 18% on brokerage, exchange transaction charges, SEBI fee and IPFT. | Broker charge schedules | Secondary |
| R13 | Equity price bands | NSE | Daily bands of 2%, 5%, 10% or 20%. No static band for stocks with derivatives, which have a dynamic operating range instead. | https://www.nseindia.com/static/products-services/equity-market-price-bands | Primary URL located |
| R14 | Index-based market-wide circuit breakers | SEBI / exchanges | Triggers at 10%, 15% and 20% index moves, causing a coordinated halt across equity and equity-derivative markets. Halt duration depends on the time of trigger. Sources differ on exact durations, so check the latest circular. | Geojit / Upstox help pages | Secondary (durations inconsistent) |
| R15 | Trading holidays 2026 | NSE circular, 12-Dec-2025 (plus 19-Dec-2025 notice) | About 15 weekday holidays for the equity segment. Muhurat session on Sunday 08-Nov-2026. A 15-Jan-2026 holiday was F&O only. | https://nsearchives.nseindia.com/content/circulars/CD71962.pdf and NSE equity circular | **Load the official list yourself.** The platform ships no real holiday file. |
| R16 | T+1 settlement for equities | SEBI; phased rollout completed Jan-2023 | Rolling T+1 settlement for cash equities. Optional T+0 beta started Mar-2024. | SEBI circulars | **Not re-verified in this session** |
| R17 | Deflated Sharpe Ratio | Bailey & López de Prado, J. Portfolio Mgmt 40(5) 2014 | Implemented in `bql.validation.stats`. | https://papers.ssrn.com/abstract=2460551 | Primary |
| R18 | Probability of Backtest Overfitting (CSCV) | Bailey, Borwein, López de Prado & Zhu, J. Computational Finance 2017 | Implemented in `bql.validation.stats`. | https://papers.ssrn.com/abstract=2326253 | Primary |

| R19 | NSE / BSE website terms of use | NSE, BSE | Website content is for personal, non-commercial use. Automated bulk extraction and redistribution are restricted. The platform downloads only published archive files at a polite rate and does not redistribute them. | https://www.nseindia.com (Terms of Use), https://www.bseindia.com (Disclaimer / Terms) | **Read the current terms yourself.** Not re-verified in this session because the NSE front end refuses non-browser clients. |
| R20 | NSE bhavcopy UDiFF format change | NSE, effective 08-Jul-2024 | The CM bhavcopy moved to the UDiFF format (`BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`). The legacy format is available up to 05-Jul-2024. | Observed in the archives on 2026-11-24 | Empirical |

## 3. Items to verify before using real data

1. The current NSE and BSE transaction-charge circulars. BSE rates in the cost file are placeholders marked `verify`.
2. STT and stamp-duty rates for the date range you backtest. Rates before 2013 and before 2020 are approximations, marked `verify` or `assumption`.
3. The holiday lists for every year you backtest. Import them as `holidays` with `verified=true`.
4. Your broker's actual brokerage and DP charges. The defaults are deliberately conservative assumptions.
5. Licence terms of every data source, including whether redistribution or derived-data storage is permitted.
