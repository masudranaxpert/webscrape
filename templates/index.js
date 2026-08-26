/**
 * Anti API — Interactive Documentation & Live Playground Client
 */

// Colorful JSON Syntax Highlighter
function syntaxHighlight(json) {
  if (typeof json !== 'string') {
    json = JSON.stringify(json, undefined, 2);
  }
  json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function (match) {
    let cls = 'json-number';
    if (/^"/.test(match)) {
      if (/:$/.test(match)) {
        cls = 'json-key';
      } else {
        cls = 'json-string';
      }
    } else if (/true|false/.test(match)) {
      cls = 'json-boolean';
    } else if (/null/.test(match)) {
      cls = 'json-null';
    }
    return '<span class="' + cls + '">' + match + '</span>';
  });
}

// Colorful HTML & XML Syntax Highlighter
function highlightHtml(html) {
  if (!html) return '<span style="color:var(--text-muted)">&lt;!-- Empty body response --&gt;</span>';
  let escaped = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // HTML comments
  escaped = escaped.replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="json-comment">$1</span>');
  // Tags and attributes
  escaped = escaped.replace(/(&lt;\/?[a-zA-Z0-9\-:]+)([\s\S]*?)(&gt;)/g, function (m, tag, attrs, close) {
    let tagHtml = '<span class="json-key">' + tag + '</span>';
    let attrsHtml = attrs.replace(/([a-zA-Z0-9\-:]+)=(".*?"|'.*?'|[^\s>]+)/g, '<span class="json-boolean">$1</span>=<span class="json-string">$2</span>');
    let closeHtml = '<span class="json-key">' + close + '</span>';
    return tagHtml + attrsHtml + closeHtml;
  });
  return escaped;
}

// Response Tab Navigation Switcher
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

    btn.classList.add('active');
    const target = document.getElementById(btn.getAttribute('data-tab'));
    if (target) {
      target.classList.add('active');
      const container = target.closest('.tab-content');
      if (container) container.scrollTop = 0;
    }
  });
});

// Guide Tab Navigation Switcher
document.querySelectorAll('.guide-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.guide-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.guide-pane').forEach(p => p.classList.remove('active'));

    btn.classList.add('active');
    const target = document.getElementById(btn.getAttribute('data-guide'));
    if (target) target.classList.add('active');
  });
});

// Live Health Status Checker
async function checkHealth() {
  const statusText = document.getElementById('statusText');
  const liveStatusBadge = document.getElementById('liveStatusBadge');
  try {
    const res = await fetch('/health');
    const data = await res.json();
    if (data.status === 'ok') {
      statusText.textContent = `Online (${data.domains_cached} cached)`;
      liveStatusBadge.style.color = 'var(--accent-green)';
    }
  } catch (err) {
    statusText.textContent = 'Offline';
    liveStatusBadge.style.color = '#ef4444';
  }
}
checkHealth();
setInterval(checkHealth, 10000);

// Form Submission & Live Runner
const fetchForm = document.getElementById('fetchForm');
const targetUrlInput = document.getElementById('targetUrl');
const submitBtn = document.getElementById('submitBtn');
const btnLabel = document.getElementById('btnLabel');
const jsonOutput = document.getElementById('jsonOutput');
const htmlOutput = document.getElementById('htmlOutput');
const cookiesList = document.getElementById('cookiesList');
const headersList = document.getElementById('headersList');
const cookieCount = document.getElementById('cookieCount');
const logsList = document.getElementById('logsList');
const logsCount = document.getElementById('logsCount');

const statusPill = document.getElementById('statusPill');
const viaPill = document.getElementById('viaPill');
const cachePill = document.getElementById('cachePill');
const timePill = document.getElementById('timePill');

fetchForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = targetUrlInput.value.trim();
  if (!url) return;

  submitBtn.disabled = true;
  btnLabel.textContent = 'Processing...';

  statusPill.className = 'metric-pill amber';
  statusPill.textContent = 'Status: Fetching...';
  viaPill.className = 'metric-pill';
  viaPill.textContent = 'Engine: --';
  cachePill.className = 'metric-pill';
  cachePill.textContent = 'Cache: --';
  timePill.className = 'metric-pill';
  timePill.textContent = 'Latency: --';

  const responseTabContent = document.querySelector('.response-panel .tab-content');
  if (responseTabContent) responseTabContent.scrollTop = 0;

  if (logsCount) logsCount.textContent = '...';
  if (logsList) logsList.innerHTML = '<div style="color:#fb923c">// Executing stealth dual-engine pipeline...</div>';
  jsonOutput.innerHTML = '// Running dual-engine pipeline...';

  const t0 = performance.now();

  try {
    const res = await fetch('/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url })
    });

    const elapsed = Math.round(performance.now() - t0);
    const data = await res.json();

    // Update Status Pill
    if (data.status_code >= 200 && data.status_code < 300) {
      statusPill.className = 'metric-pill success';
      statusPill.textContent = `Status: ${data.status_code} OK`;
    } else {
      statusPill.className = 'metric-pill amber';
      statusPill.textContent = `Status: ${data.status_code || res.status}`;
    }

    // Engine Pill
    const engine = data.meta ? data.meta.via : 'http';
    viaPill.className = engine === 'http' ? 'metric-pill info' : 'metric-pill purple';
    viaPill.textContent = `Engine: ${engine}`;

    // Cache Hit Pill
    const isCacheHit = data.meta && data.meta.cache_hit;
    cachePill.className = isCacheHit ? 'metric-pill success' : 'metric-pill';
    cachePill.textContent = `Cache: ${isCacheHit ? 'Hit' : 'Miss'}`;

    // Timing Pill
    timePill.className = 'metric-pill info';
    timePill.textContent = `Latency: ${elapsed} ms`;

    // Format JSON View (syntax highlighted)
    const displayData = { ...data };
    if (typeof displayData.body === 'string') {
      const trimmed = displayData.body.trim();
      if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
        try {
          displayData.body = JSON.parse(displayData.body);
        } catch (e) {
          if (displayData.body.length > 500) {
            displayData.body = displayData.body.substring(0, 500) + `\n... [Truncated ${displayData.body.length} characters total]`;
          }
        }
      } else if (displayData.body.length > 500) {
        displayData.body = displayData.body.substring(0, 500) + `\n... [Truncated ${displayData.body.length} characters total]`;
      }
    }
    jsonOutput.innerHTML = syntaxHighlight(displayData);

    // Format HTML / Body Preview View (syntax highlighted)
    if (data.body) {
      const trimmed = data.body.trim();
      if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
        try {
          const parsed = JSON.parse(data.body);
          htmlOutput.innerHTML = syntaxHighlight(parsed);
        } catch (e) {
          htmlOutput.innerHTML = highlightHtml(data.body);
        }
      } else {
        htmlOutput.innerHTML = highlightHtml(data.body);
      }
    } else {
      htmlOutput.innerHTML = '<span style="color:var(--text-muted)">&lt;!-- Empty body response --&gt;</span>';
    }

    // Format Cookies Tab
    const cookies = data.cookies || {};
    const cookieKeys = Object.keys(cookies);
    if (cookieCount) cookieCount.textContent = cookieKeys.length;

    if (cookieKeys.length > 0) {
      let html = '<div style="display:flex;flex-direction:column;gap:12px;">';
      for (const k of cookieKeys) {
        html += '<div style="background:#202024;border:1px solid #2e2e34;border-radius:8px;padding:12px 16px;">' +
                '<div style="color:#38bdf8;font-weight:700;font-size:0.86rem;margin-bottom:8px;display:flex;align-items:center;gap:8px;">' +
                '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#38bdf8;"></span>' +
                k +
                '</div>' +
                '<div style="font-family:var(--font-mono);font-size:0.82rem;color:#e2e8f0;word-break:break-all;background:rgba(0,0,0,0.35);padding:8px 12px;border-radius:6px;line-height:1.6;user-select:all;border:1px solid rgba(255,255,255,0.05);">' +
                cookies[k] +
                '</div>' +
                '</div>';
      }
      html += '</div>';
      cookiesList.innerHTML = html;
    } else {
      cookiesList.innerHTML = '<div style="color:#71717a;padding:20px 0;font-family:var(--font-sans);font-size:0.9rem;" class="empty-tab-msg">No cookies present in response.</div>';
    }

    // Format Headers Tab
    const headers = data.headers || {};
    const headerKeys = Object.keys(headers);
    if (headerKeys.length > 0) {
      let html = '<div style="display:flex;flex-direction:column;gap:10px;">';
      for (const k of headerKeys) {
        html += '<div style="background:#202024;border:1px solid #2e2e34;border-radius:8px;padding:10px 14px;display:flex;flex-direction:column;gap:4px;">' +
                '<div style="color:#a78bfa;font-weight:700;font-size:0.84rem;">' + k + '</div>' +
                '<div style="font-family:var(--font-mono);font-size:0.82rem;color:#e2e8f0;word-break:break-all;background:rgba(0,0,0,0.25);padding:6px 10px;border-radius:4px;">' +
                headers[k] +
                '</div>' +
                '</div>';
      }
      html += '</div>';
      headersList.innerHTML = html;
    } else {
      headersList.innerHTML = '<div style="color:#71717a;padding:20px 0;font-family:var(--font-sans);font-size:0.9rem;" class="empty-tab-msg">No headers available.</div>';
    }

    // Format Execution Logs Tab
    const logs = data.logs || [];
    if (logsCount) logsCount.textContent = logs.length;
    if (logsList) {
      if (logs.length > 0) {
        let logHtml = '';
        for (const line of logs) {
          let colored = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
          colored = colored.replace(/(\[INIT\]|\[HTTP\]|\[RETRY\])/g, '<span style="color:#38bdf8;font-weight:700">$1</span>');
          colored = colored.replace(/(\[CACHE\]|\[EXTRACT\])/g, '<span style="color:#10b981;font-weight:700">$1</span>');
          colored = colored.replace(/(\[INJECT\]|\[HEADERS\])/g, '<span style="color:#a78bfa;font-weight:700">$1</span>');
          colored = colored.replace(/(\[HTTP-SUCCESS\]|\[RETRY-SUCCESS\]|\[DONE\])/g, '<span style="color:#4ade80;font-weight:700">$1</span>');
          colored = colored.replace(/(\[HTTP-BLOCKED\]|\[HTTP-INTERSTITIAL\]|\[DETECT\]|\[ESCALATE\]|\[FORCE\])/g, '<span style="color:#fbbf24;font-weight:700">$1</span>');
          colored = colored.replace(/(\[HTTP-FAIL\]|\[RETRY-FAIL\]|\[FAIL\]|\[ERROR\])/g, '<span style="color:#f43f5e;font-weight:700">$1</span>');
          colored = colored.replace(/(\[BROWSER\]|\[SOLVER\])/g, '<span style="color:#c084fc;font-weight:700">$1</span>');
          colored = colored.replace(/(\[REDIRECT\]|\[REDIRECT-CACHE\])/g, '<span style="color:#fb923c;font-weight:700">$1</span>');
          logHtml += `<div style="padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05);">${colored}</div>`;
        }
        logsList.innerHTML = logHtml;
      } else {
        logsList.innerHTML = '<span style="color:#71717a">No logs generated for this request.</span>';
      }
    }

    checkHealth();
  } catch (err) {
    const elapsed = Math.round(performance.now() - t0);
    statusPill.className = 'metric-pill amber';
    statusPill.textContent = 'Status: Failed';
    timePill.textContent = `Latency: ${elapsed} ms`;
    jsonOutput.innerHTML = `<span style="color:#ef4444">Error executing request: ${err.message}</span>`;
    if (logsList) logsList.innerHTML = `<span style="color:#ef4444">[ERROR] Pipeline failed: ${err.message}</span>`;
  } finally {
    submitBtn.disabled = false;
    btnLabel.textContent = 'Send Request';
  }
});

// Copy to clipboard helper
function copyCode(btn) {
  const pre = btn.closest('.code-block-wrapper').querySelector('pre');
  navigator.clipboard.writeText(pre.innerText).then(() => {
    const original = btn.innerHTML;
    btn.innerHTML = '<span>Copied!</span>';
    btn.style.color = 'var(--accent-green)';
    setTimeout(() => {
      btn.innerHTML = original;
      btn.style.color = '';
    }, 2000);
  });
}
