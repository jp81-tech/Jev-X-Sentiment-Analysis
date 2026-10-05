# Jev X Sentiment Analysis

![Jev X Sentiment Analysis](public/images/frontend.jpeg)

An on-demand crypto market intelligence and decision-support terminal powered by TypeSafe AI's System One model (Jev).

The tool allows you to search any cryptocurrency (such as BTC, SOL, or ETH), select how many tweets you want to analyze (from 50 up to 1,000 tweets), and receive an instant, data-backed trading decision (Buy, Sell, Hold, or Take Profit) based on real-time market data, perpetuals funding rates, and social sentiment.

The platform does not execute trades automatically. It generates a clear decision card with calculated entry ranges, stop losses, and target levels so you can review the reasoning and execute manually on whichever exchange or DEX you prefer.

---

## How Tweets Are Analyzed

When you request sentiment analysis across 50 to 1,000 tweets, dumping hundreds of raw tweets into an LLM would exceed token budgets and introduce latency. Instead, Jev X Sentiment Analysis uses an **intelligent three-stage pipeline**:

```text
User Search (e.g. "SOL", 500 tweets)
   │
   ├──> CCXT: Live Price, 24h Volume, RSI, Funding Rate, Open Interest
   └──> TwitterAPI.io: 500 tweets ingested via cursor pagination
            │
            ▼
   Tier 1: Python Statistical Pre-Processing
   - Computes total engagement velocity (likes, retweets per minute)
   - Measures author diversity ratio (detects bot farms vs organic retail)
   - Computes keyword sentiment polarity (fear/capitulation vs greed/hype)
   - Stratified extraction:
       • Top 25 highest-engaged tweets (KOL & market-moving opinions)
       • 25 most recent breaking tweets (current real-time narrative)
            │
            ▼
   Tier 2: Source-aware storage and pagination
   - Fetch until the requested unique sample, end of results, no cursor progress, or 100 pages.
   - Known IDs do not stop pagination; overlapping pages are deduplicated.
   - SQLite records are keyed by source and ID, with separate asset relationships.
   - Partial samples remain visible as partial; they do not produce a decision.
            │
            ▼
   Tier 3: TypeSafe Jev System One Evaluation (`typesafe-sdk`)
   Evaluates 4 typed questions concurrently on the combined state:
     1. Trade Action (Choice: Strong Buy, Buy, Hold, Take Profit, Sell, Strong Sell)
     2. Sentiment Spectrum (Score: Extreme Panic to Euphoria)
     3. Squeeze Risk (Noul: probability that negative funding + panic indicates a short squeeze)
     4. Catalyst Significance (Score: None, Minor, Moderate, Major)
            │
            ▼
   Decision Card Displayed in Web Terminal
   - Recommended action with provider confidence percentage (separate from action probabilities)
   - Macro sentiment gauge across all 500 tweets
   - Calculated entry range, stop loss, and target levels
   - User executes manually on their exchange of choice
```

---

## Configurable Social Fetching & Cost Breakdown

You can configure the tweet sample size in the terminal interface based on your needs:

| Sample Size | TwitterAPI.io Cost | TypeSafe Jev Cost | Total Cost per Search | Best For |
| :--- | :--- | :--- | :--- | :--- |
| **50 Tweets** | ~$0.0075 | ~$0.0008 | **< $0.009 (<1¢)** | Quick pulse check on immediate price moves |
| **100 Tweets** | ~$0.0150 | ~$0.0008 | **~$0.016 (1.6¢)** | Standard intraday trading check |
| **250 Tweets** | ~$0.0375 | ~$0.0008 | **~$0.038 (3.8¢)** | Multi-hour swing setup validation |
| **500 Tweets** | ~$0.0750 | ~$0.0008 | **~$0.076 (7.6¢)** | Comprehensive sentiment & news audit |
| **1,000 Tweets** | ~$0.1500 | ~$0.0008 | **~$0.151 (15¢)** | Major regime shift or ETF/catalyst investigation |

*Cost figures above are estimates, not measured bills. A complete social sample is cached for the configured TTL; market and model calls are separate and may still incur costs. Pagination overlap can increase provider billing.*

---

## Technical Architecture

```text
.
├── app/
│   ├── api/
│   │   └── v1/
│   │       └── analyze.py        # Search and settings endpoints
│   ├── core/
│   │   ├── config.py             # App configuration and environment variables
│   │   ├── cache.py              # In-memory TTL caches
│   │   └── database.py           # Local SQLite tweet storage and retrieval
│   ├── services/
│   │   ├── market_service.py     # Exchange data client (CCXT Kraken/Kraken Futures)
│   │   ├── twitter_service.py    # TwitterAPI.io client with cursor pagination
│   │   ├── stats_service.py      # Tier 1 deterministic statistical pre-processing
│   │   └── typesafe_service.py   # TypeSafe Jev System One client and decision logic
│   ├── static/
│   │   ├── css/
│   │   │   └── style.css         # Dark quantitative terminal styling
│   │   └── js/
│   │       └── app.js            # Frontend controls and result rendering
│   ├── templates/
│   │   └── index.html            # Main web terminal interface
│   └── main.py                   # FastAPI application entrypoint
├── requirements.txt
└── README.md
```

---

## Setup and Installation

### Prerequisites
- Python 3.12 or higher
- A TypeSafe AI API key (from [console.typesafe.ai](https://console.typesafe.ai))
- A TwitterAPI.io API key (from [twitterapi.io](https://twitterapi.io))

### 1. Clone and Install Dependencies

```bash
git clone <repo-url>
cd Jev-X-Sentiment-Analysis

python -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create a `.env` file in the root directory:

```env
TYPESAFE_API_KEY=your_typesafe_api_key_here
TWITTER_API_KEY=your_twitterapi_io_key_here

# Optional configuration
PORT=8000
HOST=127.0.0.1
CACHE_TTL_SECONDS=600

# Security & Access Control
ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
# ADMIN_TOKEN=your_secret_admin_token_here
```

### 3. Run the Application

```bash
uvicorn app.main:app --reload --port 8000
```

Open your browser and navigate to:
```text
http://localhost:8000
```

### 4. Running the Automated Test Suite

```bash
python -m pytest -q
node --check app/static/js/app.js
```

---

## Data availability and local tests

Missing keys, provider errors, invalid market prices/candles, stale inputs, and partial social samples return `unavailable` or `degraded` with `decision: null`. There is no generated demo data or replacement decision. The UI clears previous decisions and disables copying on failure, asset change, and expiry. HOLD and TAKE_PROFIT have no entry/stop/target ticket. BUY/SELL levels use a fixed-percentage heuristic, rounded to the instrument tick size; they are not levels predicted by the model.

RSI uses closed hourly candles only, with 50 for a flat series and no value for insufficient history. An absent provider probability distribution remains null; confidence is never derived from that distribution. Keyword polarity is an uncalibrated word-count heuristic.

Present spot metadata must be finite and within its valid range: a 24h change cannot be below -100%, high/low must be positive and ordered, and volumes cannot be negative. Invalid provider values block analysis before a model call. Invalid cached entries are refetched; the model adapter separately rejects invalid numeric input. Missing optional metadata remains null; missing price change gives `momentum_bucket=unknown`. The model receives that missing value explicitly and the UI shows a dash rather than 0%. Invalid optional futures data is discarded as unavailable, without disabling an otherwise valid spot analysis.

The default pytest suite is offline: network/DNS are blocked before application imports, provider replies are synthetic, and database/config paths are temporary. Node is required for DOM contract tests. These tests do not establish live provider compatibility or economic effectiveness.

`JEV_CONFIG_FILE` overrides the same configuration path for reading and atomic settings writes. `JEV_DB_PATH` overrides SQLite storage. Importing the application does not initialize SQLite. On first storage access, existing `tweets` records remain quarantined in their original table. A private temporary SQLite backup must pass integrity, legacy schema and exact legacy-content checks (all values, storage types and duplicate counts, independent of insertion order) before it is fsynced and atomically published as `<database>.legacy-backup`; only then are v2 tables added. Incomplete existing backups are rebuilt from the preserved legacy data even if a former migration already created v2. Valid existing copies are preserved. Legacy entries are never promoted to a live source automatically. Historical storage is not used to silently fill current searches.

Ticker request start, receipt time, and source timestamp are preserved separately. Kraken may not provide a source timestamp: it remains null, with `freshness_basis=request_start` and `valid_until=request_started_at+120`. Observation/request age does not establish the source quotation age. When a valid source timestamp exists, the deadline is `min(request_started_at, source_timestamp)+120`; malformed or future source timestamps are rejected. Absolute `valid_until` deadlines are checked after market fetches and model completion for both market and social inputs, and also in the UI. Handled settings write failures remove their private temporary file; `.settings-*` is ignored by Git as crash protection. Settings writes are serialized within one application process and replace the file before changing runtime keys; use a single worker for this local settings workflow.

## Usage Workflow

1. **Enter a Symbol**: Type any supported cryptocurrency symbol (e.g. `BTC`, `SOL`, `ETH`).
2. **Select Sample Size**: Choose between 50, 100, 250, 500, or 1,000 tweets using the sample slider.
3. **Review Market & Derivatives Data**: View live spot price, 24-hour volume, perpetuals funding rate, and open interest delta (powered by Kraken & Kraken Futures).
4. **Inspect Social Sentiment**: Check engagement velocity, fear/greed polarity, and top-discussed catalysts across the sample.
5. **Read the Jev System One Decision**: Review the recommended action (`STRONG BUY`, `BUY`, `HOLD`, `TAKE PROFIT`, `SELL`), confidence percentage, and rationale.
6. **Execute Manually**: Review the calculated entry, stop-loss, and target levels and execute manually on your preferred exchange or DEX.

---

## ⚖️ Legal & Financial Disclaimer

**Not Financial Advice**: This software is created strictly for educational, research, and technical demonstration purposes. None of the quantitative models, sentiment scores, trade levels, or verdicts generated by this platform constitute financial, investment, or trading advice. Trading cryptocurrencies carries a significant risk of financial loss. Always perform your own due diligence and never trade with funds you cannot afford to lose.

---

Twitter ingestion retries HTTP 429 on the same cursor up to four total attempts per page. Valid Retry-After delta-seconds or HTTP dates are respected; absent/malformed values use 5/10/20-second backoff. Overlapping searches using the same credential share an in-process request lock and cooldown. The provider-fetch budget is 75 seconds, including waiting and requests; a required delay beyond that budget stops without an early retry.

Persistent HTTP 429 reports `rate_limited`, while cooldown exhaustion reports `rate_limit_timeout`. Provider-fetch deadline expiry without an active cooldown reports `fetch_timeout`; HTTP client exceptions, including read timeouts, report `provider_error`. Other 4xx responses are not retried. Page-call counts include retries. Exhausted searches return partial/unavailable data and cannot produce a model decision from an incomplete sample. Freshness starts at the original fetch start and is not renewed by retries. This coordination is within one worker/process and does not cover other applications using the same key.

## Local launch and bounded diagnostics

From the repository root, run `sh scripts/launch_local.sh --port 8787`. The launcher uses the existing sibling `../jev-test-env/bin/python`; set `JEV_PYTHON` to an absolute path to another installed virtualenv when needed. It binds only `127.0.0.1` with one worker, uses the existing `JEV_CONFIG_FILE`/environment configuration, and does not create or overwrite `.env`. Missing credentials are reported by name only: the local UI/public market can start, but full analysis remains blocked until both keys are configured. Stop the foreground server with Ctrl-C.

Using that same Python runtime, run either:

```sh
../jev-test-env/bin/python -B scripts/diagnose_local.py market --port 8787
../jev-test-env/bin/python -B scripts/diagnose_local.py analysis --port 8787
```

The market mode requests BTC public market data. Analysis mode checks credential-presence booleans through local `/health`, then submits exactly one BTC/50 analysis request; provider calls may incur usage. Neither mode retries or follows HTTP redirects. The probe reads no configuration file itself and prints only technical statuses, freshness timestamps, sample/page counts, versions installed in the probe interpreter (not proof of server versions) and safe error codes. It does not print recommendations, tweets, credential values, remote URLs or exception details. HTTP200 alone or a null decision cannot produce `LIVE_CHECK_OK`; missing keys produce `BLOCKED`, and failed/partial/expired/unverifiable results produce `UNKNOWN` (exit78). Reported elapsed time is the local HTTP request duration, not independent provider-stage latency. Model/provider-specific failure causes remain unknown when the application does not expose a safe code. A timeout is inconclusive and should not trigger an automatic retry; the server may still be completing that one request.

## License

This project is open-source software licensed under the [MIT License](LICENSE).
