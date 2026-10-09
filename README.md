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
   - Computes mean weighted engagement per post: (likes + 2 × retweets) / sampled posts
   - Measures unique author-name ratio; this is not bot detection
   - Computes keyword sentiment polarity (fear/capitulation vs greed/hype)
   - Stratified extraction:
       • Top 25 highest-engaged tweets (KOL & market-moving opinions)
       • 25 first returned valid posts from the configured publication window
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

*Cost figures above are estimates, not measured bills. A complete social sample is cached for the configured TTL; market and model calls are separate and may still incur costs. Pagination overlap can increase provider billing. Retries add requests; billing of HTTP 429 responses is unverified, as is billing of other failed requests. This table is not a cap on actual charges.*

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
sh scripts/launch_local.sh --port 8787
```

Open your browser and navigate to:
```text
http://127.0.0.1:8787
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
4. **Inspect Social Sentiment**: Check mean engagement per post, fear/greed polarity, and top-discussed catalysts across the sample.
5. **Read the Jev System One Decision**: Review the recommended action (`STRONG BUY`, `BUY`, `HOLD`, `TAKE PROFIT`, `SELL`), confidence percentage, and rationale.
6. **Execute Manually**: Review the calculated entry, stop-loss, and target levels and execute manually on your preferred exchange or DEX.

---

## ⚖️ Legal & Financial Disclaimer

**Not Financial Advice**: This software is created strictly for educational, research, and technical demonstration purposes. None of the quantitative models, sentiment scores, trade levels, or verdicts generated by this platform constitute financial, investment, or trading advice. Trading cryptocurrencies carries a significant risk of financial loss. Always perform your own due diligence and never trade with funds you cannot afford to lose.

---

Twitter ingestion retries HTTP 429 on the same cursor up to four total attempts per page. Valid Retry-After delta-seconds or HTTP dates are respected; absent/malformed values use 5/10/20-second backoff. Sequential and overlapping searches using the same credential share an in-process request lock and cooldown. The provider-fetch budget is 75 seconds, including waiting and requests; a required delay beyond that budget stops without an early retry.

Persistent HTTP 429 reports `rate_limited`, while cooldown exhaustion reports `rate_limit_timeout`. Provider-fetch deadline expiry without an active cooldown reports `fetch_timeout`; HTTP client timeouts also report `fetch_timeout`. HTTP 402 reports `payment_required`, other 4xx/5xx report `http_error`, malformed JSON/schema reports `malformed_response`, HTTP decoding errors report `malformed_response`, and other non-timeout httpx request errors (including too many redirects) report `transport_error`. Unclassified exceptions retain the neutral `provider_error` category. Other 4xx responses are not retried. Page-call counts include retries. Exhausted searches return partial/unavailable data and cannot produce a model decision from an incomplete sample. Freshness starts at the original fetch start and is not renewed by retries. This coordination is within one worker/process and does not cover other applications using the same key.

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


### Local access and publication window (08 October 2026)

The launcher explicitly binds 127.0.0.1, one worker, with forwarded-header interpretation disabled. `HOST` in the configuration is descriptive; it does not override this launcher or a manually invoked uvicorn command. Prefer the launcher: it configures exact local Origins for the chosen `--port` before importing the app. For direct uvicorn use `uvicorn app.main:app --host 127.0.0.1 --port 8787 --no-proxy-headers` and explicitly configure matching ALLOWED_ORIGINS/ALLOWED_HOSTS. No public deployment is included.

Every analysis, market and settings API request uses the same access policy. If `ADMIN_TOKEN` is configured, it is required even on loopback, in `X-Admin-Token` (constant-time comparison). Enter it in the UI access-token field; it stays only in page memory, with no localStorage/sessionStorage or cookies, and is lost on reload. Clearing the field removes it from future requests. API keys remain separate server configuration.

Without a token, only a numeric loopback client with no Forwarded/X-Forwarded-*/X-Real-IP headers is accepted. `localhost` and `testclient` are not trusted client addresses. Host must match `ALLOWED_HOSTS` (default localhost,127.0.0.1,::1); Origin, when present, must match `ALLOWED_ORIGINS`. CORS is not authentication. A proxy hiding all forwarding evidence is indistinguishable from a direct local client: **every proxy requires ADMIN_TOKEN**, even if the backend is loopback. Explicitly configure its Host/Origin allowlists and protect its transport; the app does not claim to detect such a hidden proxy.

`SOCIAL_WINDOW_HOURS` defaults to 24, configurable from 1 to 168. Search includes explicit since_time/until_time bounds; local checks reject unknown, naive, invalid, future and too-old publication dates. Missing dates remain unknown, never receive the fetch time. The response provides publication-window bounds, oldest/newest accepted publication time and rejection counts. Only the requested number of unique valid posts permits model evaluation. Publication age is checked again for cache reuse and after the model; the response deadline is at most the oldest post's publication time plus the window length. Thus cached/model results cannot extend a post's admissible lifetime. Fetch freshness and publication age are separate.

Cooldown entries survive completed requests, are keyed by credential hashes, and expire when unused and past their deadlines. At the 256-entry capacity, a new key is refused (`rate_limit_capacity`) rather than evicting an active cooldown. This is in-process coordination only.

`constraints-tested.txt` records the installed dedicated Python 3.12 environment used for offline tests. To reproduce in a separately prepared environment use `python -m pip install -r requirements.txt -c constraints-tested.txt`; this patch did not install or update packages. Constraints constrain dependency versions, not platform binaries. The probability/confidence fields come from the provider and are not evidence of calibration, profitability or a forward-tested strategy. Decision-history logging and investment evaluation remain separate work.


The launcher narrows ALLOWED_HOSTS to 127.0.0.1/localhost and ALLOWED_ORIGINS to those two HTTP origins with the selected port. It changes only runtime settings, not configuration files. For authenticated diagnostics, use `python scripts/diagnose_local.py market --token-env` to read an already-exported ADMIN_TOKEN from the process environment, or `--token-stdin` to read one line from stdin. No token values are accepted as command arguments; no .env is read by diagnostics. A missing/incorrect token produces BLOCKED/ACCESS_DENIED on HTTP401/403. Do not put secrets in shell history; use your secure input mechanism. Neither option persists or reports the token.

Future publications remain rejected, including clock-skewed provider dates; this is deliberate, so keep the local clock accurate. Failure responses carry an X-Correlation-ID generated locally; logs retain only a closed category and stage with that ID, never raw exceptions.


## Decision journal and offline research (phase4)

Normal analysis responses now report `decision_logged` and, on success, `record_id`. The private append-only journal defaults to `app/data/decisions.jsonl` (`JEV_DECISION_LOG` overrides it). It contains typed allowlisted numbers/enums and a hash of post IDs, never tweet text, author names, API keys or exception messages. Handled logging failures preserve the HTTP result and expose only `log_error_category`; keep the journal directory writable. Prompt version hashes the actual questions/criteria/state definitions; changing them starts a different study. Dirty source checkouts are labeled rather than presented as release commits.

[RESEARCH-PROTOCOL.md](RESEARCH-PROTOCOL.md) and `research_protocol.json` define the frozen study and commands. Settlement/evaluation are offline; the separate candle collector defaults to dry-run and requires explicit `--execute` for public Kraken requests. Collect at least every 48h (recommended 24h): Kraken recent OHLC does not provide unlimited historical pagination. H72 with placebo+48h needs roughly 120h after the signal and complete exact candle windows. Missing/conflicting data never becomes a fabricated result. No research command places orders, reads .env or requires account keys.

A verdict requires the full preregistered cohort gates and nonoverlap checks. H24 is descriptive; previews are explicitly not verdicts. Returns are hypothetical holding returns, separate from SL/TP diagnostics, not realized trading profits. This implementation has not collected or evaluated a live investment sample.

The collector retains each pair for a final catch-up through 180h after its signal, including the first run after the 120h horizon. This is bounded by the recent-candle retention and does not recover earlier missing history. Older records are counted as `past_catchup_records` with `coverage_status=INCOMPLETE`; actual coverage is determined by offline settlement.

Journal failures remain visible in the UI until a later confirmed write, with a neutral category and correlation ID. The backend never silently trims a damaged journal. Offline inspection: `python scripts/repair_decision_log.py --log /explicit/path/decisions.jsonl`; only `--apply` authorizes repair of a recognizable truncated tail. The script requires a regular non-symlink file with mode0600, validates the complete prefix, locks it, and durably preserves the tail in a unique adjacent0600 quarantine before truncation. Corruption in earlier lines is blocked. Do not delete quarantine files before reviewing recovery. Logging append/flock/fsync runs outside the asyncio loop; provenance is captured at module initialization.

The collector attempts XBT/USD first and isolates failures per pair, continuing the remaining pairs. Any failure gives a neutral per-pair report, PARTIAL/BLOCKED, and a nonzero CLI exit; corrupted cache is not overwritten. Identical INCOMPLETE audit observations are deduplicated; changed coverage remains in the history.

## Scheduled analysis client (step5)

`scripts/auto_analyze.py` is a standalone local client, not an installed scheduler. Example offline plan:

```sh
python scripts/auto_analyze.py --log /explicit/path/decisions.jsonl --run-log /explicit/path/auto_analyze_runs.jsonl
```

Defaults: ETH,SOL,XRP, 50 posts, 5h minimum gap, at most 3 attempts/run and 16 attempts in a rolling 24h. Without `--execute`, it opens no sockets, writes no files, does not read a token, and reports `requests:0`, `health:NOT_CHECKED`. Missing journals are treated as empty; corrupt/truncated/nonfinite/future records block processing. `--now` is only for dry-run; combining it with `--execute` exits 2 before requests. Changing the protocol or prompt is not part of this client.

Only explicit `--execute` enables GET `/health` and POST `/api/v1/analyze` at numeric 127.0.0.1 on `--port` (8787 default). Health must be healthy with both provider flags exactly true. No redirects, proxies, retry, Origin header, app import or .env read. Optional `--token-env` reads ADMIN_TOKEN from the process environment; `--token-stdin` reads one line. Never put a token in arguments. Tokens must fit latin-1, contain no CR/LF, and be at most 4096 characters. No token is logged or printed.

Execute holds a nonblocking exclusive lock on the run-log for its entire run; a competing process exits 78/RUN_LOCKED without network requests. The private 0600 append-only log contains `attempt_reserved`, `result` and `skipped` events. Each reservation has a unique attempt_id and is fsynced before POST; the file and containing directory are also synced before any POST. Only unique reservations count toward the daily budget, including orphan reservations after a crash or uncertain timeout. Result events do not double-count, and skips consume no attempt. Gap uses the latest decision of any status or reservation for that symbol, whichever is later. A failure writing the local run-log stops further POSTs; a failed reservation never authorizes POST. No refund or retry is attempted, even if a reservation consumed budget without reaching the server. Preserve the run-log: manual deletion/replacement or using a different run-log bypasses its coordination and is outside this guarantee. The log and journal cannot be the same inode; symlinks, FIFOs and directories are rejected.

A result records only allowlisted status/action/IDs, counts/times, HTTP code and a closed log-error category. It contains no posts, levels, headers or exception text. Outcome `LOGGING_FAILED` means the backend returned `decision_logged:false`: the attempt remains counted and the client exits 1/PARTIAL. This backend journal failure does not itself stop attempts for the remaining symbols when the local run-log is still writable. Exit 0 means every selected symbol returned HTTP 200 with a recognized status and confirmed record ID; degraded/unavailable can satisfy this without a decision. Exit 1 means skips or post-attempt failure; exit 78 means blocked before a POST. Counts include results and skips, never reservation events. Run-log malformed data/duplicate reservations/unmatched results produce RUN_LOG_CORRUPT; journal corruption, including an identical duplicate record ID, produces JOURNAL_CORRUPT, without modifying corrupt content. No automatic tail repair is performed.

The limit is a hard cap on locally reserved attempts using the same protected run-log, **not a guaranteed dollar cap**. Provider pages/retries and pricing can change costs. This patch does not install launchd, activate scheduled requests or measure costs. If scheduling is later enabled, `trigger:scheduled` and `record_id` in result events identify scheduled records; the research protocol, its ID and evaluator remain unchanged.

The journal parser deliberately rejects even identical duplicate record IDs (more conservative than the research evaluator). A blocked execute may create an empty private run-log while preserving any corrupt input unchanged. The defaults of 3/run and 16/day are configurable limits, not immutable ceilings; the selected values bound that run.

## Sample-yield diagnostics (stage A)

Provider classification is diagnostic only: the model still requires the full requested sample (minimum request 50) and all existing freshness/provenance gates. No query filters, prompt, research protocol or evaluator changed. Logs contain only a closed failure category and, for HTTP errors, the numeric status code; response bodies, URLs and exception messages are not included. Historical `provider_error` remains recognized.

Scheduler result events now include `reason`, taken only from the recognized `social.reason` values; missing or unrecognized reasons become JSON null. Reservations/skips have null reason. Old run-log events without the field remain valid and continue consuming the same budget. `reason_counts` counts persisted result events in this run, not reservations/skips; the literal key `unknown` counts null reasons (never a string `null` key). No other provider fields are added to the log.

Offline diagnostic command:

```sh
python scripts/sample_yield_report.py --log /explicit/path/decisions.jsonl
```

It prints one JSON line followed by a compact Markdown table. Output contains only aggregate technical data: unique-record counts, status/reason/sample-count distributions, symbol, UTC date/hour, and shares with explicit numerators/denominators. `end_of_results_below_50` uses all unique valid records in its group as denominator; `below_50_among_end_of_results` uses only end_of_results. The latter short-sample counts also have median/min. Legacy provider_error, payment_required and combined provider-failure shares are reported separately. Null or unknown reasons aggregate under `unknown`; source strings are never echoed.

Identical duplicate record IDs count once; conflicting IDs, malformed/truncated JSONL or invalid technical types block the whole report with exit 78/INVALID_LOG, without a partial denominator. Empty files report EMPTY with zero counts and null shares/medians (exit 0). Missing files block. No journal is modified and no network or application configuration is read. Free-form source fields are ignored in output; full-record hashes are used only in memory to distinguish identical duplicates from conflicts. This is descriptive reporting, not a threshold recommendation or evidence of a completed study. Stage B requires a separately reviewed cohort of at least 36 records across 3 complete days; no such runtime gate or sample-threshold change is introduced here.

Provider text must be a string when present (explicit null is malformed); missing text retains an empty string. Username aliases must be strings or null; null/missing/empty values retain the anonymous fallback. Invalid records are stopped before statistics or model evaluation, while previously validated records remain a partial sample. The offline yield-report input is intended to be a regular JSONL file; special files such as FIFOs are outside its supported input contract.
