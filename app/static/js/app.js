// Jev X Sentiment Analysis Terminal Client Controller

let currentDecision = null;
let accessToken = "";
const apiHeaders = () => ({ "Content-Type": "application/json", ...(accessToken ? { "X-Admin-Token": accessToken } : {}) });
const formatMetric = (value, digits = 1) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
let currentMarketPair = null;
let requestGeneration = 0;
let requestFetchId = 0;
let decisionExpiresAt = 0;
let expiryTimer = null;
const formatSigned = value => typeof value === "number" && Number.isFinite(value) ? `${value > 0 ? "+" : ""}${value.toFixed(2)}` : "—";
const formatPrice = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumSignificantDigits: 15 }) : "—";
// Set from the last response's market block: true when Kraken failed and the
// prices in it are placeholder constants rather than quotes.
let currentPriceIsFallback = false;

// Cost lookup per sample size
const COST_MAP = {
    50: "$0.0075",
    100: "$0.0150",
    250: "$0.0375",
    500: "$0.0750",
    1000: "$0.1500"
};

document.addEventListener("DOMContentLoaded", () => {
    setupEventListeners();
    // Await user clicking 'Analyze Asset' before running any analysis
});

function setupEventListeners() {
    const symbolInput = document.getElementById("symbol-input");
    const slider = document.getElementById("sample-size-slider");
    const sliderVal = document.getElementById("sample-size-val");
    const estimatedCost = document.getElementById("estimated-cost");
    const analyzeBtn = document.getElementById("analyze-btn");
    const copyBtn = document.getElementById("copy-levels-btn");

    symbolInput.addEventListener("input", () => { ++requestGeneration; invalidateDecision("Asset changed — analyze again"); });

    const tokenInput = document.getElementById("access-token-input");
    if (tokenInput) tokenInput.addEventListener("input", () => { accessToken = tokenInput.value; });

    // Slider change
    slider.addEventListener("input", (e) => {
        const val = parseInt(e.target.value);
        sliderVal.textContent = `${val} Tweets`;
        const costStr = COST_MAP[val] || `~$${(val * 0.00015).toFixed(4)}`;
        estimatedCost.textContent = `(${costStr})`;
    });

    // Quick chips
    document.querySelectorAll(".chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
            chip.classList.add("active");
            symbolInput.value = chip.dataset.symbol;
            ++requestGeneration;
            invalidateDecision("Asset changed — analyze again");
        });
    });

    // Search enter key
    symbolInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            const sym = symbolInput.value.trim().toUpperCase();
            if (sym) runAnalysis(sym, parseInt(slider.value));
        }
    });

    // Analyze button
    analyzeBtn.addEventListener("click", () => {
        const sym = symbolInput.value.trim().toUpperCase();
        if (sym) runAnalysis(sym, parseInt(slider.value));
    });

    // Copy Levels button
    copyBtn.addEventListener("click", () => {
        if (!currentDecision || !currentDecision.trade_levels || Date.now() >= decisionExpiresAt) {
            invalidateDecision("Stale or unavailable");
            return;
        }
        if (currentPriceIsFallback) {
            const orig = copyBtn.textContent;
            copyBtn.textContent = "No live price";
            setTimeout(() => { copyBtn.textContent = orig; }, 2000);
            return;
        }
        const d = currentDecision;
        const lvls = d.trade_levels;
        const text = [
            `--- JEV X SENTIMENT ANALYSIS TRADE TICKET ---`,
            `Asset: ${currentMarketPair}`,
            `Action: ${d.action} (${formatMetric(d.confidence_pct)}% Confidence)`,
            `Entry Range: $${formatPrice(lvls.entry_range[0])} - $${formatPrice(lvls.entry_range[1])}`,
            `Stop Loss: $${formatPrice(lvls.stop_loss)} (${formatMetric(lvls.stop_loss_pct, 2)}%)`,
            `Target 1: $${formatPrice(lvls.target_1)} (${formatSigned(lvls.target_1_pct)}%)`,
            `Target 2: $${formatPrice(lvls.target_2)} (${formatSigned(lvls.target_2_pct)}%)`,
            `Risk/Reward: ${formatMetric(lvls.risk_reward_ratio, 2)} R:R`,
            `Rationale: ${d.rationale}`
        ].join("\n");

        navigator.clipboard.writeText(text).then(() => {
            const orig = copyBtn.textContent;
            copyBtn.textContent = "✅ Copied!";
            setTimeout(() => { copyBtn.textContent = orig; }, 2000);
        });
    });

    // Settings Modal
    const modal = document.getElementById("settings-modal");
    const openSettingsBtn = document.getElementById("open-settings-btn");
    const closeSettingsBtn = document.getElementById("close-settings-btn");
    const cancelSettingsBtn = document.getElementById("cancel-settings-btn");
    const saveSettingsBtn = document.getElementById("save-settings-btn");
    const typesafeKeyInput = document.getElementById("typesafe-key-input");
    const twitterKeyInput = document.getElementById("twitter-key-input");

    if (openSettingsBtn && modal) {
        openSettingsBtn.addEventListener("click", () => {
            modal.classList.remove("hidden");
        });

        const closeModal = () => modal.classList.add("hidden");
        closeSettingsBtn.addEventListener("click", closeModal);
        cancelSettingsBtn.addEventListener("click", closeModal);
        modal.addEventListener("click", (e) => {
            if (e.target === modal) closeModal();
        });

        saveSettingsBtn.addEventListener("click", async () => {
            const typesafeKey = typesafeKeyInput.value.trim();
            const twitterKey = twitterKeyInput.value.trim();

            if (!typesafeKey && !twitterKey) {
                alert("Please enter at least one API key to activate live mode.");
                return;
            }

            saveSettingsBtn.disabled = true;
            saveSettingsBtn.textContent = "Saving...";

            try {
                const res = await fetch("/api/v1/settings", {
                    method: "POST",
                    headers: apiHeaders(),
                    body: JSON.stringify({
                        typesafe_api_key: typesafeKey || null,
                        twitter_api_key: twitterKey || null
                    })
                });

                if (!res.ok) throw new Error("Failed to save settings");
                const data = await res.json();

                // Update navbar pills
                if (data.has_typesafe_key) {
                    const pill = document.getElementById("typesafe-status");
                    pill.className = "status-pill status-active";
                    document.getElementById("typesafe-status-text").textContent = "TypeSafe AI: Configured";
                }
                if (data.has_twitter_key) {
                    const pill = document.getElementById("twitter-status");
                    pill.className = "status-pill status-active";
                    document.getElementById("twitter-status-text").textContent = "TwitterAPI: Configured";
                }

                closeModal();
                alert("API keys saved! Running live analysis...");
                const sym = symbolInput.value.trim().toUpperCase() || "BTC";
                runAnalysis(sym, parseInt(slider.value));

            } catch (err) {
                alert("Error saving keys: " + err.message);
            } finally {
                saveSettingsBtn.disabled = false;
                saveSettingsBtn.textContent = "Save & Go Live";
            }
        });
    }
}

async function runAnalysis(symbol, sampleSize) {
    const sym = symbol.toUpperCase().replace("$", "");
    const analyzeBtn = document.getElementById("analyze-btn");
    const spinner = document.getElementById("loading-spinner");
    const btnText = analyzeBtn.querySelector(".btn-text");

    const generation = ++requestGeneration;
    const fetchId = ++requestFetchId;
    invalidateDecision("Loading");
    analyzeBtn.disabled = true;
    spinner.classList.remove("hidden");
    btnText.textContent = "Ingesting & Analyzing...";

    try {
        const response = await fetch("/api/v1/analyze", {
            method: "POST",
            headers: apiHeaders(),
            body: JSON.stringify({ symbol: sym, sample_size: sampleSize }),
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || "Analysis request failed");
        }

        const data = await response.json();
        if (generation === requestGeneration) updateUI(data);

    } catch (err) {
        if (generation !== requestGeneration) return;
        invalidateDecision("Unavailable");
        console.error("Error analyzing asset:", err);
        alert(`Analysis Error: ${err.message}`);
    } finally {
        if (fetchId === requestFetchId) {
            analyzeBtn.disabled = false;
            spinner.classList.add("hidden");
            btnText.textContent = "Analyze Asset";
        }
    }
}

function updateUI(data) {
    const journalWarning = document.getElementById("journal-warning");
    if (data.decision_logged === false) {
        const categories = new Set(["permission", "storage_full", "io", "invalid_record"]);
        const category = categories.has(data.log_error_category) ? data.log_error_category : "unknown";
        const correlation = /^[a-f0-9]{32}$/.test(data.correlation_id || "") ? data.correlation_id : "unavailable";
        journalWarning.textContent = `Decision NOT recorded — ${category}. Reference: ${correlation}. Check the journal before further analyses.`;
        journalWarning.hidden = false;
    } else if (data.decision_logged === true) {
        journalWarning.textContent = "";
        journalWarning.hidden = true;
    }
    const market = data.market || {};
    const stats = data.social_stats || {};
    const decision = data.decision || {};
    const tweets = data.tweets_sample || [];

    const fresh = input => Number.isFinite(input?.fetched_at) && Number.isFinite(input?.valid_until)
        && input.fetched_at <= Date.now() / 1000 && Date.now() / 1000 < input.valid_until;
    const marketClockValid = fresh(market) && Number.isFinite(market.request_started_at)
        && market.request_started_at <= market.fetched_at && Date.now() / 1000 - market.request_started_at < 120
        && (market.source_timestamp === null
            ? market.freshness_basis === "request_start" && market.valid_until <= market.request_started_at + 120
            : market.freshness_basis === "source_timestamp" && Number.isFinite(market.source_timestamp)
              && Date.now() / 1000 - market.source_timestamp >= 0 && Date.now() / 1000 - market.source_timestamp < 120
              && market.valid_until <= Math.min(market.request_started_at, market.source_timestamp) + 120);
    const valid = data.status === "success" && data.model_status === "ok" && !data.is_twitter_mock && !data.is_typesafe_mock
        && !decision.is_mock && !!decision.action && market.status === "ok" && !market.is_fallback
        && marketClockValid
        && data.social?.status === "ok" && fresh(data.social);
    if (!valid) {
        invalidateDecision(data.social?.status === "partial" ? "Partial sample — no decision" : "Unavailable or stale");
        document.getElementById("twitter-status-text").textContent = `TwitterAPI: ${data.social?.status || "unavailable"}`;
        document.getElementById("tweet-count-badge").textContent = `${stats.sample_size || 0} / ${data.social?.target_count || "?"} tweets`;
        return;
    }
    currentDecision = decision;
    currentMarketPair = typeof market.pair === "string" && /^[A-Z0-9]+\/[A-Z0-9]+$/.test(market.pair)
        ? market.pair : `${data.symbol}/USD`;
    decisionExpiresAt = Math.min(market.valid_until, data.social.valid_until) * 1000;
    clearTimeout(expiryTimer);
    expiryTimer = setTimeout(() => invalidateDecision("Stale — refresh analysis"), Math.max(0, decisionExpiresAt - Date.now()));
    document.getElementById("typesafe-status-text").textContent = "TypeSafe AI: Available";
    document.getElementById("twitter-status-text").textContent = "TwitterAPI: Complete";

    // 1. Ticker Ribbon
    // Kraken failed for this symbol and market_service returned placeholder
    // constants. The response says so; the interface has to say so too, and it
    // must not turn a placeholder into entry, stop and target prices.
    const isFallback = market.is_fallback === true;
    currentPriceIsFallback = isFallback;

    document.getElementById("ticker-symbol").textContent = currentMarketPair;
    const priceEl = document.getElementById("ticker-price");
    priceEl.textContent = isFallback ? "unavailable" : `$${formatPrice(market.price)}`;
    priceEl.classList.toggle("val-unavailable", isFallback);

    const changeEl = document.getElementById("ticker-change");
    const change = market.change_24h_pct;
    const hasChange = !isFallback && Number.isFinite(change);
    changeEl.textContent = hasChange ? `${change > 0 ? '+' : ''}${change}%` : "—";
    changeEl.className = !hasChange
        ? "ticker-val font-mono val-unavailable"
        : `ticker-val font-mono ${change >= 0 ? 'text-success' : 'text-danger'}`;

    const fundingEl = document.getElementById("ticker-funding");
    if (isFallback || !Number.isFinite(market.funding_rate_pct)) {
        fundingEl.textContent = "—";
        fundingEl.className = "ticker-val font-mono val-unavailable";
    } else {
        const funding = market.funding_rate_pct;
        fundingEl.textContent = `${funding > 0 ? '+' : ''}${funding.toFixed(4)}%`;
        fundingEl.className = `ticker-val font-mono ${funding < 0 ? 'text-success' : funding > 0.03 ? 'text-danger' : ''}`;
    }

    const oiEl = document.getElementById("ticker-oi");
    if (oiEl) {
        if (isFallback || !Number.isFinite(market.open_interest_usd)) {
            oiEl.textContent = "—";
            oiEl.classList.toggle("val-unavailable", true);
        } else {
            oiEl.textContent = `$${(market.open_interest_usd / 1e6).toFixed(1)}M`;
            oiEl.classList.toggle("val-unavailable", false);
        }
    }

    const rsiEl = document.getElementById("ticker-rsi");
    rsiEl.textContent = isFallback ? "—" : (market.rsi_14 ?? "--");
    rsiEl.classList.toggle("val-unavailable", isFallback);

    const volEl = document.getElementById("ticker-vol");
    const hasVolume = !isFallback && Number.isFinite(market.volume_24h_usd);
    volEl.textContent = hasVolume ? `$${(market.volume_24h_usd / 1e6).toFixed(1)}M` : "—";
    volEl.classList.toggle("val-unavailable", !hasVolume);

    // 2. Decision Hero
    const badge = document.getElementById("action-badge");
    badge.textContent = decision.action || "HOLD";
    badge.className = "action-badge";

    if (decision.action.includes("BUY")) {
        badge.classList.add("badge-buy");
    } else if (decision.action.includes("SELL")) {
        badge.classList.add("badge-sell");
    } else {
        badge.classList.add("badge-hold");
    }

    document.getElementById("confidence-val").textContent = `${formatMetric(decision.confidence_pct)}%`;
    document.getElementById("rationale-box").textContent = decision.rationale || "No rationale available.";

    // Render 6-way Signal Probability Distribution
    renderProbabilityDistribution(decision);

    // 3. Trade Levels
    const banner = document.getElementById("market-fallback-banner");
    if (banner) banner.hidden = !isFallback;
    const copyBtn = document.getElementById("copy-levels-btn");
    if (copyBtn) copyBtn.disabled = isFallback || !decision.trade_levels;
    const levelCells = ["lvl-entry", "lvl-sl", "lvl-tp1", "lvl-tp2", "lvl-rr"];
    if (isFallback || !decision.trade_levels) {
        for (const id of levelCells) {
            const el = document.getElementById(id);
            el.textContent = "—";
            el.classList.add("val-unavailable");
        }
    } else {
        for (const id of levelCells) document.getElementById(id).classList.remove("val-unavailable");
        const lvls = decision.trade_levels || {};
        const entry = lvls.entry_range || [market.price, market.price];
        document.getElementById("lvl-entry").textContent = `$${formatPrice(entry[0])} - $${formatPrice(entry[1])}`;
        document.getElementById("lvl-sl").textContent = `$${formatPrice(lvls.stop_loss)} (${formatMetric(lvls.stop_loss_pct, 2)}%)`;
        document.getElementById("lvl-tp1").textContent = `$${formatPrice(lvls.target_1)} (${formatSigned(lvls.target_1_pct)}%)`;
        document.getElementById("lvl-tp2").textContent = `$${formatPrice(lvls.target_2)} (${formatSigned(lvls.target_2_pct)}%)`;
        document.getElementById("lvl-rr").textContent = `${formatMetric(lvls.risk_reward_ratio, 2)}:1 Expected R:R`;
    }

    // 4. Metrics Radar
    document.getElementById("sentiment-label-val").textContent = stats.sentiment_label || decision.sentiment_label || "Neutral";
    document.getElementById("sentiment-sub").textContent = `Polarity Score: ${formatMetric(stats.polarity_score)} across ${stats.sample_size} tweets`;
    document.getElementById("squeeze-val").textContent = `${formatMetric(decision.squeeze_risk_pct)}%`;
    document.getElementById("diversity-val").textContent = `${formatMetric(stats.author_diversity_pct)}%`;
    document.getElementById("catalyst-val").textContent = `${formatMetric(decision.catalyst_impact_score)} / 3.0`;

    // 5. Update Exchange Links
    const row = document.getElementById("exchange-links-row");
    row.innerHTML = `
        <a href="https://www.binance.com/en/trade/${data.symbol}_USDT" target="_blank" class="ext-link">Binance</a>
        <a href="https://www.bybit.com/trade/usdt/${data.symbol}USDT" target="_blank" class="ext-link">Bybit</a>
        <a href="https://www.coinbase.com/advanced-trade/spot/${data.symbol}-USD" target="_blank" class="ext-link">Coinbase</a>
        <a href="https://jup.ag/swap/USDC-${data.symbol}" target="_blank" class="ext-link">Jupiter DEX</a>
    `;

    // 6. Update Tweet Explorer List
    const tweetCountBadge = document.getElementById("tweet-count-badge");
    tweetCountBadge.textContent = `${tweets.length} of ${stats.sample_size} Analyzed Tweets`;

    const tweetList = document.getElementById("tweet-list");
    if (tweets.length === 0) {
        tweetList.innerHTML = `<div class="empty-state">No tweets available for this asset.</div>`;
    } else {
        tweetList.innerHTML = tweets.map((t) => `
            <div class="tweet-item">
                <div class="tweet-header">
                    <div class="author-meta">
                        <span class="author-name">@${escapeHtml(t.author_username || "user")}</span>
                        ${t.author_verified ? '<span class="verified-badge">✓</span>' : ''}
                    </div>
                    <span class="tweet-tag">${t.likes > 100 ? 'High Engagement' : 'Recent'}</span>
                </div>
                <div class="tweet-text">${escapeHtml(t.text || "")}</div>
                <div class="tweet-stats">
                    <span>❤️ ${(t.likes || 0).toLocaleString()}</span>
                    <span>🔁 ${(t.retweets || 0).toLocaleString()}</span>
                    <span>💬 ${(t.replies || 0).toLocaleString()}</span>
                </div>
            </div>
        `).join("");
    }
}

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function renderProbabilityDistribution(decision) {
    const container = document.getElementById("dist-bars-container");
    const winnerNote = document.getElementById("dist-winner-note");
    const footerEl = document.getElementById("dist-footer");
    const confSub = document.getElementById("conf-sub-text");
    if (!container) return;

    const winnerAction = decision.action || "HOLD";
    const rawProbs = decision.action_probabilities || {};

    const allActions = ["STRONG_BUY", "BUY", "HOLD", "TAKE_PROFIT", "SELL", "STRONG_SELL"];

    // Build raw item list
    let items = allActions.map((act) => ({
        action: act,
        pct: typeof rawProbs[act] === "number" ? rawProbs[act] : 0.0,
        isWinner: act === winnerAction,
    }));

    const totalSum = items.reduce((acc, it) => acc + it.pct, 0);
    if (!allActions.every(act => Number.isFinite(rawProbs[act]) && rawProbs[act] >= 0 && rawProbs[act] <= 100) || Math.abs(totalSum - 100) > 0.0001) {
        container.textContent = "Distribution unavailable";
        if (winnerNote) winnerNote.textContent = "No distribution supplied";
        if (footerEl) footerEl.textContent = "";
        if (confSub) confSub.textContent = "Provider confidence (separate from action probability)";
        return;
    }

    // Sort descending by percentage so highest probability signal is on top
    items.sort((a, b) => b.pct - a.pct);

    // Sum and difference over runner-up
    const calculatedSum = items.reduce((acc, it) => acc + it.pct, 0).toFixed(1);
    const winnerItem = items.find((it) => it.isWinner) || items[0];
    const runnerUp = items.find((it) => !it.isWinner) || { action: "HOLD", pct: 0 };
    const margin = winnerItem.pct - runnerUp.pct;

    if (winnerNote) {
        winnerNote.innerHTML = `Selected: <span class="highlight-action">${winnerAction}</span> (${formatMetric(winnerItem.pct)}%) · Total: ${calculatedSum}%`;
    }

    if (confSub) {
        confSub.textContent = `Provider confidence (separate from action probability)`;
    }

    container.innerHTML = items
        .map((item) => {
            let fillClass = "fill-other";
            if (item.isWinner) {
                if (item.action.includes("BUY")) fillClass = "fill-winner-buy";
                else if (item.action.includes("SELL")) fillClass = "fill-winner-sell";
                else if (item.action === "TAKE_PROFIT") fillClass = "fill-winner-tp";
                else fillClass = "fill-winner-hold";
            } else {
                if (item.action.includes("BUY")) fillClass = "fill-subtle-buy";
                else if (item.action.includes("SELL")) fillClass = "fill-subtle-sell";
                else if (item.action === "TAKE_PROFIT") fillClass = "fill-subtle-tp";
                else fillClass = "fill-subtle-hold";
            }

            const friendlyName = item.action.replace(/_/g, " ");
            const barWidth = Math.min(100, Math.max(item.pct, 0));

            return `
                <div class="dist-row ${item.isWinner ? 'is-winner-row' : ''}">
                    <div class="dist-row-label">
                        <span class="dist-action-name ${item.isWinner ? 'is-winner' : ''}">
                            <span class="action-text">${friendlyName}</span>
                            ${item.isWinner ? '<span class="winner-pill">✓ CHOSEN</span>' : ''}
                        </span>
                        <span class="dist-pct font-mono ${item.isWinner ? 'is-winner' : ''}">${item.pct.toFixed(1)}%</span>
                    </div>
                    <div class="dist-bar-track">
                        <div class="dist-bar-fill ${fillClass}" style="width: ${barWidth}%;"></div>
                    </div>
                </div>
            `;
        })
        .join("");

    if (footerEl) {
        footerEl.innerHTML = `
            <div class="dist-footer-item">
                <span class="footer-label">Sum Across 6 Signals:</span>
                <span class="footer-val font-mono" style="color: var(--accent-cyan);">${calculatedSum}%</span>
            </div>
            <div class="dist-footer-item">
                <span class="footer-label">Selection Logic:</span>
                <span class="footer-val font-mono" style="color: var(--text-secondary);">${winnerAction} selected; difference (${formatSigned(margin)}% over runner-up ${runnerUp.action})</span>
            </div>
        `;
    }
}

function invalidateDecision(reason) {
    currentDecision = null;
    currentMarketPair = null;
    currentPriceIsFallback = true;
    decisionExpiresAt = 0;
    clearTimeout(expiryTimer);
    for (const id of ["ticker-symbol", "confidence-val", "squeeze-val", "catalyst-val", "lvl-entry", "lvl-sl", "lvl-tp1", "lvl-tp2", "lvl-rr", "ticker-price", "ticker-change", "ticker-funding", "ticker-oi", "ticker-rsi", "ticker-vol", "sentiment-label-val", "diversity-val", "dist-winner-note", "conf-sub-text"]) {
        document.getElementById(id).textContent = "—";
    }
    document.getElementById("action-badge").textContent = reason;
    document.getElementById("action-badge").className = "action-badge badge-hold";
    document.getElementById("rationale-box").textContent = "No current decision available.";
    document.getElementById("copy-levels-btn").disabled = true;
    document.getElementById("dist-bars-container").textContent = "Distribution unavailable";
    document.getElementById("dist-footer").textContent = "";
    document.getElementById("tweet-list").textContent = "No current sample";
    document.getElementById("tweet-count-badge").textContent = "0 Tweets";
    document.getElementById("sentiment-sub").textContent = "Keyword heuristic unavailable";
    for (const provider of ["typesafe", "twitter"]) {
        document.getElementById(`${provider}-status`).className = "status-pill status-warning";
        document.getElementById(`${provider}-status-text`).textContent = `${provider}: ${reason}`;
    }
}
