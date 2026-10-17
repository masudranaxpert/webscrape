/**
 * Anti API — Full-Featured Stealth Request Studio & Domain Cache Explorer
 */

// Global State
let currentResponseData = null;
let cachedDomainsData = {};
let ttlIntervalTimer = null;

// HTML entity escaping for XSS prevention
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// Syntax highlighters
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

function highlightHtml(html) {
  if (!html) return '<span style="color:var(--text-muted)">&lt;!-- Empty body response --&gt;</span>';
  let escaped = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  escaped = escaped.replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="json-comment">$1</span>');
  escaped = escaped.replace(/(&lt;\/?[a-zA-Z0-9\-:]+)([\s\S]*?)(&gt;)/g, function (m, tag, attrs, close) {
    let tagHtml = '<span class="json-key">' + tag + '</span>';
    let attrsHtml = attrs.replace(/([a-zA-Z0-9\-:]+)=(".*?"|'.*?'|[^\s>]+)/g, '<span class="json-boolean">$1</span>=<span class="json-string">$2</span>');
    let closeHtml = '<span class="json-key">' + close + '</span>';
    return tagHtml + attrsHtml + closeHtml;
  });
  return escaped;
}

// Authentication vault manager
const globalApiKeyInput = document.getElementById('globalApiKeyInput');
const btnSaveApiKey = document.getElementById('btnSaveApiKey');
const btnClearApiKey = document.getElementById('btnClearApiKey');
const btnToggleKeyVisibility = document.getElementById('btnToggleKeyVisibility');
const eyeIcon = document.getElementById('eyeIcon');
const vaultStatusPill = document.getElementById('vaultStatusPill');
const headerKeyBadge = document.getElementById('headerKeyBadge');
const headerKeyDot = document.getElementById('headerKeyDot');
const headerKeyText = document.getElementById('headerKeyText');

function getStoredApiKey() {
  try {
    return localStorage.getItem('anti_api_key') || localStorage.getItem('anti_playground_api_key') || '';
  } catch (e) {
    return '';
  }
}

function setStoredApiKey(key) {
  try {
    if (key) {
      localStorage.setItem('anti_api_key', key);
      localStorage.setItem('anti_playground_api_key', key);
    } else {
      localStorage.removeItem('anti_api_key');
      localStorage.removeItem('anti_playground_api_key');
    }
  } catch (e) {}
}

function updateApiKeyUI(key) {
  const hasKey = Boolean(key && key.trim());
  if (globalApiKeyInput) {
    globalApiKeyInput.value = key || '';
  }
  if (vaultStatusPill) {
    if (hasKey) {
      vaultStatusPill.className = 'vault-status-pill saved';
      vaultStatusPill.textContent = 'Key Active';
    } else {
      vaultStatusPill.className = 'vault-status-pill';
      vaultStatusPill.textContent = 'Key Required';
    }
  }
  if (headerKeyBadge && headerKeyText && headerKeyDot) {
    if (hasKey) {
      headerKeyBadge.style.background = 'rgba(22, 163, 74, 0.1)';
      headerKeyBadge.style.borderColor = 'rgba(22, 163, 74, 0.25)';
      headerKeyBadge.style.color = 'var(--accent-green)';
      headerKeyDot.style.background = 'var(--accent-green)';
      headerKeyDot.style.boxShadow = '0 0 8px var(--accent-green)';
      headerKeyText.textContent = 'Key Active';
    } else {
      headerKeyBadge.style.background = 'rgba(234, 88, 12, 0.1)';
      headerKeyBadge.style.borderColor = 'rgba(234, 88, 12, 0.25)';
      headerKeyBadge.style.color = 'var(--accent-orange)';
      headerKeyDot.style.background = 'var(--accent-orange)';
      headerKeyDot.style.boxShadow = '0 0 8px var(--accent-orange)';
      headerKeyText.textContent = 'Key Required';
    }
  }
}

// Initial Key Load
const initialKey = getStoredApiKey();
updateApiKeyUI(initialKey);

if (btnSaveApiKey && globalApiKeyInput) {
  btnSaveApiKey.addEventListener('click', () => {
    const val = globalApiKeyInput.value.trim();
    if (!val) {
      globalApiKeyInput.focus();
      return;
    }
    setStoredApiKey(val);
    updateApiKeyUI(val);
    btnSaveApiKey.textContent = 'Saved!';
    setTimeout(() => { btnSaveApiKey.textContent = 'Save Key'; }, 1800);
    loadCacheExplorer();
  });
}

if (btnClearApiKey && globalApiKeyInput) {
  btnClearApiKey.addEventListener('click', () => {
    setStoredApiKey('');
    updateApiKeyUI('');
    globalApiKeyInput.value = '';
    loadCacheExplorer();
  });
}

if (btnToggleKeyVisibility && globalApiKeyInput) {
  btnToggleKeyVisibility.addEventListener('click', () => {
    const isPassword = globalApiKeyInput.type === 'password';
    globalApiKeyInput.type = isPassword ? 'text' : 'password';
    if (eyeIcon) {
      eyeIcon.innerHTML = isPassword
        ? '<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/>'
        : '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>';
    }
  });
}

if (headerKeyBadge) {
  headerKeyBadge.addEventListener('click', () => {
    const vault = document.getElementById('apiVaultCard');
    if (vault) {
      vault.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (globalApiKeyInput) globalApiKeyInput.focus();
    }
  });
}

// Navigation & tab switchers
const btnToggleConfig = document.getElementById('btnToggleConfig');
const studioConfigCard = document.getElementById('studioConfigCard');
const btnCloseConfig = document.getElementById('btnCloseConfig');

function setConfigDrawerOpen(open) {
  if (!studioConfigCard) return;
  if (open) {
    studioConfigCard.classList.add('is-open');
    if (btnToggleConfig) {
      btnToggleConfig.classList.add('active');
      btnToggleConfig.setAttribute('aria-expanded', 'true');
    }
  } else {
    studioConfigCard.classList.remove('is-open');
    if (btnToggleConfig) {
      btnToggleConfig.classList.remove('active');
      btnToggleConfig.setAttribute('aria-expanded', 'false');
    }
  }
}

if (btnToggleConfig) {
  btnToggleConfig.addEventListener('click', () => {
    const isOpen = studioConfigCard && studioConfigCard.classList.contains('is-open');
    setConfigDrawerOpen(!isOpen);
  });
}

if (btnCloseConfig) {
  btnCloseConfig.addEventListener('click', () => {
    setConfigDrawerOpen(false);
  });
}

// Active Config Dot Indicator
function checkActiveConfigState() {
  const dot = document.getElementById('activeConfigDot');
  if (!dot) return;

  const mode = document.getElementById('cfgEngineMode')?.value || 'auto';
  const preset = document.getElementById('cfgPreset')?.value || 'chrome-latest-windows';
  const httpVer = document.getElementById('cfgHttpVersion')?.value || '';
  const proxy = document.getElementById('cfgProxy')?.value.trim() || '';
  const ua = document.getElementById('cfgUa')?.value.trim() || '';
  const body = document.getElementById('cfgBody')?.value.trim() || '';
  const turnstileToken = document.getElementById('cfgTurnstileToken')?.value.trim() || '';
  const waitSel = document.getElementById('cfgWaitForSelector')?.value.trim() || '';
  const clickSel = document.getElementById('cfgClickSelector')?.value.trim() || '';
  const extractSel = document.getElementById('selectorsKvContainer')?.querySelectorAll('.kv-row').length || 0;
  const customCookies = document.getElementById('cookiesKvContainer')?.querySelectorAll('.kv-row').length || 0;
  const customHeaders = document.getElementById('headersKvContainer')?.querySelectorAll('.kv-row').length || 0;

  const isCustom = (
    mode !== 'auto' ||
    preset !== 'chrome-latest-windows' ||
    httpVer !== '' ||
    proxy !== '' ||
    ua !== '' ||
    body !== '' ||
    turnstileToken !== '' ||
    waitSel !== '' ||
    clickSel !== '' ||
    extractSel > 0 ||
    customCookies > 0 ||
    customHeaders > 0
  );

  dot.style.display = isCustom ? 'inline-block' : 'none';
}

['cfgEngineMode', 'cfgPreset', 'cfgHttpVersion', 'cfgProxy', 'cfgUa', 'cfgBody', 'cfgWaitForSelector', 'cfgClickSelector'].forEach(id => {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener('input', checkActiveConfigState);
    el.addEventListener('change', checkActiveConfigState);
  }
});
checkActiveConfigState();

// Config Tabs
document.querySelectorAll('.config-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.config-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.config-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const target = document.getElementById(btn.getAttribute('data-cfg'));
    if (target) target.classList.add('active');
    setConfigDrawerOpen(true);
  });
});

// Response Tabs
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const parentPanel = btn.closest('.response-panel') || document;
    parentPanel.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    parentPanel.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const target = document.getElementById(btn.getAttribute('data-tab'));
    if (target) {
      target.classList.add('active');
      const container = target.closest('.tab-content');
      if (container) container.scrollTop = 0;
    }
  });
});

// Guide Tabs
document.querySelectorAll('.guide-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.guide-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.guide-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const target = document.getElementById(btn.getAttribute('data-guide'));
    if (target) target.classList.add('active');
  });
});

// In-text Guide Links
document.querySelectorAll('[data-open-guide]').forEach(link => {
  link.addEventListener('click', () => {
    const btn = document.querySelector(`.guide-tab-btn[data-guide="${link.getAttribute('data-open-guide')}"]`);
    if (btn) btn.click();
  });
});

// Method Selector Styling
const requestMethod = document.getElementById('requestMethod');
if (requestMethod) {
  requestMethod.addEventListener('change', () => {
    requestMethod.className = `method-select method-${requestMethod.value.toLowerCase()}`;
    const isBodyMethod = ['POST', 'PUT', 'PATCH'].includes(requestMethod.value);
    if (isBodyMethod) {
      setConfigDrawerOpen(true);
      const headersTabBtn = document.querySelector('.config-tab-btn[data-cfg="cfgHeaders"]');
      if (headersTabBtn && !document.getElementById('cfgHeaders').classList.contains('active')) {
        headersTabBtn.click();
      }
    }
  });
}

// Clear URL Button
const btnClearUrl = document.getElementById('btnClearUrl');
const targetUrl = document.getElementById('targetUrl');
if (btnClearUrl && targetUrl) {
  btnClearUrl.addEventListener('click', () => {
    targetUrl.value = '';
    targetUrl.focus();
  });
}

// Key-value rows editor
function createKvRow(key = '', val = '') {
  const row = document.createElement('div');
  row.className = 'kv-row';
  row.innerHTML = `
    <input type="text" class="kv-key" placeholder="Header name" value="${key.replace(/"/g, '&quot;')}" />
    <input type="text" class="kv-val" placeholder="Value" value="${val.replace(/"/g, '&quot;')}" />
    <button type="button" class="btn-remove-kv" title="Remove row">&times;</button>
  `;
  row.querySelector('.btn-remove-kv').addEventListener('click', () => {
    row.remove();
    checkActiveConfigState();
  });
  row.querySelectorAll('input').forEach(input => {
    input.addEventListener('input', checkActiveConfigState);
  });
  return row;
}

function getKvPairs(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return {};
  const pairs = {};
  container.querySelectorAll('.kv-row').forEach(row => {
    const k = row.querySelector('.kv-key').value.trim();
    const v = row.querySelector('.kv-val').value.trim();
    if (k) pairs[k] = v;
  });
  return pairs;
}

const btnAddHeaderRow = document.getElementById('btnAddHeaderRow');
if (btnAddHeaderRow) {
  btnAddHeaderRow.addEventListener('click', () => {
    document.getElementById('headersKvContainer').appendChild(createKvRow());
    checkActiveConfigState();
  });
}

const btnAddCookieRow = document.getElementById('btnAddCookieRow');
if (btnAddCookieRow) {
  btnAddCookieRow.addEventListener('click', () => {
    const row = createKvRow();
    row.querySelector('.kv-key').placeholder = 'Cookie name';
    document.getElementById('cookiesKvContainer').appendChild(row);
    checkActiveConfigState();
  });
}

// Format JSON Body
const btnFormatBodyJson = document.getElementById('btnFormatBodyJson');
const cfgBody = document.getElementById('cfgBody');
if (btnFormatBodyJson && cfgBody) {
  btnFormatBodyJson.addEventListener('click', () => {
    const text = cfgBody.value.trim();
    if (!text) return;
    try {
      const parsed = JSON.parse(text);
      cfgBody.value = JSON.stringify(parsed, null, 2);
    } catch (e) {
      alert('Invalid JSON: ' + e.message);
    }
  });
}

// Paste Saved Proxy Helper
const btnTestUserProxy = document.getElementById('btnTestUserProxy');
const cfgProxy = document.getElementById('cfgProxy');
if (btnTestUserProxy && cfgProxy) {
  btnTestUserProxy.addEventListener('click', () => {
    const saved = localStorage.getItem('anti_playground_proxy') || '';
    if (saved) {
      cfgProxy.value = saved;
    }
  });
}

// Preset configurations

document.querySelectorAll('.btn-preset').forEach(btn => {
  btn.addEventListener('click', () => {
    const preset = btn.getAttribute('data-preset');
    if (!targetUrl || !requestMethod) return;

    if (preset === 'httpbin') {
      requestMethod.value = 'GET';
      requestMethod.className = 'method-select method-get';
      targetUrl.value = 'https://httpbin.org/get';
      document.getElementById('cfgEngineMode').value = 'auto';
    } else if (preset === 'redirects') {
      requestMethod.value = 'GET';
      requestMethod.className = 'method-select method-get';
      targetUrl.value = 'https://httpbin.org/redirect/3';
      document.getElementById('cfgEngineMode').value = 'auto';
    } else if (preset === 'turnstile') {
      requestMethod.value = 'GET';
      requestMethod.className = 'method-select method-get';
      targetUrl.value = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
      document.getElementById('cfgWaitForSelector').value = 'body';
      document.querySelector('.config-tab-btn[data-cfg="cfgTurnstile"]').click();
    } else if (preset === 'post_json') {
      requestMethod.value = 'POST';
      requestMethod.className = 'method-select method-post';
      targetUrl.value = 'https://httpbin.org/post';
      cfgBody.value = JSON.stringify({ message: 'Hello from Anti Stealth Studio', timestamp: new Date().toISOString() }, null, 2);
      document.querySelector('.config-tab-btn[data-cfg="cfgHeaders"]').click();
    }
    checkActiveConfigState();
  });
});

// HTML preview: code vs sandboxed iframe

const btnPreviewCode = document.getElementById('btnPreviewCode');
const btnPreviewFrame = document.getElementById('btnPreviewFrame');
const htmlCodeContainer = document.getElementById('htmlCodeContainer');
const htmlFrameContainer = document.getElementById('htmlFrameContainer');
const previewIframe = document.getElementById('previewIframe');
const htmlSizeNotice = document.getElementById('htmlSizeNotice');

if (btnPreviewCode && btnPreviewFrame) {
  btnPreviewCode.addEventListener('click', () => {
    btnPreviewCode.classList.add('active');
    btnPreviewFrame.classList.remove('active');
    htmlCodeContainer.style.display = 'block';
    htmlFrameContainer.style.display = 'none';
  });

  btnPreviewFrame.addEventListener('click', () => {
    btnPreviewFrame.classList.add('active');
    btnPreviewCode.classList.remove('active');
    htmlCodeContainer.style.display = 'none';
    htmlFrameContainer.style.display = 'block';
  });
}

function updateHtmlPreview(html) {
  const output = document.getElementById('htmlOutput');
  const size = html ? new Blob([html]).size : 0;
  if (htmlSizeNotice) {
    htmlSizeNotice.textContent = size > 1024 ? `${(size / 1024).toFixed(1)} KB` : `${size} bytes`;
  }
  if (!html) {
    if (output) output.innerHTML = '<span style="color:var(--text-muted)">&lt;!-- Empty response body --&gt;</span>';
    if (previewIframe) previewIframe.srcdoc = '<html><body style="font-family:sans-serif;color:#888;padding:20px;">Empty Body</body></html>';
    return;
  }
  if (output) {
    output.innerHTML = highlightHtml(html);
  }
  if (previewIframe) {
    // Inject base href and render securely in sandboxed frame
    try {
      const url = targetUrl.value.trim();
      let baseTag = '';
      if (url) {
        baseTag = `<base href="${url}">`;
      }
      previewIframe.srcdoc = html.includes('<head>') ? html.replace('<head>', `<head>${baseTag}`) : baseTag + html;
    } catch (e) {
      previewIframe.srcdoc = html;
    }
  }
}

// Copy JSON Response Helper
const btnCopyJson = document.getElementById('btnCopyJson');
if (btnCopyJson) {
  btnCopyJson.addEventListener('click', () => {
    if (!currentResponseData) return;
    navigator.clipboard.writeText(JSON.stringify(currentResponseData, null, 2)).then(() => {
      const span = btnCopyJson.querySelector('span');
      if (span) {
        const orig = span.textContent;
        span.textContent = 'Copied!';
        setTimeout(() => { span.textContent = orig; }, 1800);
      }
    });
  });
}

// Copy HTML Response Helper
const btnCopyHtml = document.getElementById('btnCopyHtml');
if (btnCopyHtml) {
  btnCopyHtml.addEventListener('click', () => {
    if (!currentResponseData || typeof currentResponseData.body !== 'string') return;
    navigator.clipboard.writeText(currentResponseData.body).then(() => {
      const span = btnCopyHtml.querySelector('span');
      if (span) {
        const orig = span.textContent;
        span.textContent = 'Copied!';
        setTimeout(() => { span.textContent = orig; }, 1800);
      }
    });
  });
}

// Copy Extracted Content Helper
const btnCopyExtracted = document.getElementById('btnCopyExtracted');
if (btnCopyExtracted) {
  btnCopyExtracted.addEventListener('click', () => {
    if (!currentResponseData || currentResponseData.extracted === null || currentResponseData.extracted === undefined) return;
    const textToCopy = typeof currentResponseData.extracted === 'string'
      ? currentResponseData.extracted
      : JSON.stringify(currentResponseData.extracted, null, 2);
    navigator.clipboard.writeText(textToCopy).then(() => {
      const span = btnCopyExtracted.querySelector('span');
      if (span) {
        const orig = span.textContent;
        span.textContent = 'Copied!';
        setTimeout(() => { span.textContent = orig; }, 1800);
      }
    });
  });
}

// Keyboard Shortcut: Ctrl+Enter / Cmd+Enter sends form
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    const fetchForm = document.getElementById('fetchForm');
    if (fetchForm && !document.getElementById('submitBtn').disabled) {
      fetchForm.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    }
  }
});

// Studio request runner

const fetchForm = document.getElementById('fetchForm');
const submitBtn = document.getElementById('submitBtn');
const btnLabel = document.getElementById('btnLabel');
const jsonOutput = document.getElementById('jsonOutput');
const statusPill = document.getElementById('statusPill') || document.getElementById('respStatus');
const viaPill = document.getElementById('viaPill') || document.getElementById('respEngine');
const cachePill = document.getElementById('cachePill');
const protocolPill = document.getElementById('protocolPill');
const cookiesUsedPill = document.getElementById('cookiesUsedPill');
const timePill = document.getElementById('timePill') || document.getElementById('respTiming');
const logsList = document.getElementById('logsList');
const logsCount = document.getElementById('logsCount');
const browserStateList = document.getElementById('browserStateList');
const cookieCount = document.getElementById('cookieCount');
const headersList = document.getElementById('headersList');
const extractedList = document.getElementById('extractedList');
const extractedCount = document.getElementById('extractedCount');
const tabBtnTurnstile = document.getElementById('tabBtnTurnstile');
const turnstileContent = document.getElementById('turnstileContent');

fetchForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = targetUrl.value.trim();
  if (!url) return;

  const apiKey = (globalApiKeyInput ? globalApiKeyInput.value.trim() : '') || getStoredApiKey();
  if (!apiKey) {
    if (statusPill) {
      statusPill.className = 'metric-pill amber';
      statusPill.textContent = 'API Key Required';
    }
    if (jsonOutput) {
      jsonOutput.innerHTML = `<span style="color:#f43f5e">// [401 Unauthorized]\nX-API-Key is strictly required.\nPlease enter your secret API key in the Authentication Vault above.</span>`;
    }
    if (logsList) {
      logsList.innerHTML = `<div style="color:#f43f5e;font-weight:700">[AUTH] Aborted: X-API-Key missing. Set your key in the Authentication Vault.</div>`;
    }
    const vault = document.getElementById('apiVaultCard');
    if (vault) {
      vault.classList.add('vault-attention-shake');
      setTimeout(() => vault.classList.remove('vault-attention-shake'), 700);
      vault.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (globalApiKeyInput) globalApiKeyInput.focus();
    }
    return;
  }

  // Save proxy if provided
  const proxyInput = document.getElementById('cfgProxy');
  const proxyVal = proxyInput ? proxyInput.value.trim() : '';
  if (proxyVal) {
    try { localStorage.setItem('anti_playground_proxy', proxyVal); } catch (e) {}
  }

  if (submitBtn) submitBtn.disabled = true;
  if (btnLabel) btnLabel.textContent = 'Executing...';

  if (statusPill) {
    statusPill.className = 'metric-pill amber';
    statusPill.textContent = 'Status: Routing...';
  }
  if (viaPill) {
    viaPill.className = 'metric-pill';
    viaPill.textContent = 'Engine: --';
  }
  if (cachePill) {
    cachePill.className = 'metric-pill';
    cachePill.textContent = 'Cache: --';
  }
  if (protocolPill) protocolPill.textContent = 'Proto: --';
  if (cookiesUsedPill) cookiesUsedPill.textContent = 'Cookies: --';
  if (timePill) timePill.textContent = 'Latency: -- ms';

  if (jsonOutput) jsonOutput.innerHTML = '// Dispatching request through stealth pipeline...';
  if (logsList) logsList.innerHTML = '<div style="color:#fb923c">// Negotiating wire handshake...</div>';
  if (logsCount) logsCount.textContent = '...';

  // Build Payload Schema
  const method = requestMethod.value;
  const engineMode = document.getElementById('cfgEngineMode').value;
  const preset = document.getElementById('cfgPreset').value;
  const httpVersion = document.getElementById('cfgHttpVersion').value;
  const timeout = parseInt(document.getElementById('cfgTimeout').value, 10) || 30;
  const userAgent = document.getElementById('cfgUserAgent').value.trim();

  const customHeaders = getKvPairs('headersKvContainer');
  const customCookies = getKvPairs('cookiesKvContainer');
  const bodyText = cfgBody.value.trim();

  const waitForSelector = document.getElementById('cfgWaitForSelector').value.trim();
  const clickSelector = document.getElementById('cfgClickSelector').value.trim();
  const cfWait = parseFloat(document.getElementById('cfgCfWait').value) || 25.0;
  const pageLoadState = document.getElementById('cfgPageLoadState').value;

  const selector = document.getElementById('cfgSelector').value.trim();
  const selectorAttr = document.getElementById('cfgSelectorAttr').value.trim() || 'text';
  const selectorAll = document.getElementById('cfgSelectorAll').value === 'true';

  const sessionId = document.getElementById('cfgSessionId').value.trim();
  const cookieTtl = parseInt(document.getElementById('cfgCookieTtl').value, 10) || 86400;
  const cookieLimit = parseInt(document.getElementById('cfgCookieLimit').value, 10) || 35;

  const payload = {
    url: url,
    method: method,
    preset: preset,
    timeout: timeout,
    cf_wait: cfWait,
    page_load_state: pageLoadState,
    cookie_ttl: cookieTtl,
    cookie_limit: cookieLimit
  };

  if (engineMode === 'browser') payload.force_browser = true;
  if (engineMode === 'http') payload.force_http = true;
  if (proxyVal) payload.proxy = proxyVal;
  if (httpVersion) payload.http_version = httpVersion;
  if (userAgent) payload.user_agent = userAgent;
  if (Object.keys(customHeaders).length > 0) payload.headers = customHeaders;
  if (Object.keys(customCookies).length > 0) payload.inject_cookies = customCookies;
  if (['POST', 'PUT', 'PATCH'].includes(method) && bodyText) payload.body = bodyText;

  if (waitForSelector) payload.wait_for_selector = waitForSelector;
  if (clickSelector) payload.click_selector = clickSelector;

  if (selector) {
    payload.selector = selector;
    payload.selector_attr = selectorAttr;
    payload.selector_all = selectorAll;
  }
  if (sessionId) payload.session_id = sessionId;

  const t0 = performance.now();

  // The server caps a browser job at cf_wait + 90s; give up a little later instead of hanging on a dropped connection
  const clientTimeoutSec = (Number(payload.cf_wait) || 45) + 110;
  const abort = new AbortController();
  const abortTimer = setTimeout(() => abort.abort(), clientTimeoutSec * 1000);

  try {
    let res;
    try {
      res = await fetch('/fetch', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': apiKey
        },
        body: JSON.stringify(payload),
        signal: abort.signal
      });
    } catch (netErr) {
      if (netErr.name === 'AbortError') {
        throw new Error(`No response after ${clientTimeoutSec}s; the request was abandoned client-side.`);
      }
      throw netErr;
    } finally {
      clearTimeout(abortTimer);
    }

    const elapsed = Math.round(performance.now() - t0);
    // A reverse proxy that times out returns an empty or HTML body (e.g. 502/504), which res.json() cannot parse
    const rawText = await res.text();
    let data;
    try {
      data = JSON.parse(rawText);
    } catch (parseErr) {
      throw new Error(
        `Gateway returned HTTP ${res.status} with ${rawText ? 'a non-JSON' : 'an empty'} body after ${elapsed} ms. ` +
        'The upstream connection was dropped (reverse proxy timeout or server restart); retry the request.'
      );
    }
    currentResponseData = data;

    // Handle 401 Unauthorized
    if (res.status === 401) {
      if (statusPill) {
        statusPill.className = 'metric-pill amber';
        statusPill.textContent = 'Status: 401 Unauthorized';
      }
      if (viaPill) {
        viaPill.className = 'metric-pill';
        viaPill.textContent = 'Engine: REJECTED';
      }
      if (timePill) timePill.textContent = `Latency: ${elapsed} ms`;

      const errorMsg = data && data.detail ? data.detail : 'Invalid X-API-Key provided.';
      if (jsonOutput) {
        jsonOutput.innerHTML = `<div style="background:rgba(244,63,94,0.08);border:1px solid rgba(244,63,94,0.3);border-radius:8px;padding:12px 16px;margin-bottom:12px;color:#fca5a5;font-size:0.84rem;line-height:1.6;">
  <div style="display:flex;align-items:center;gap:8px;font-weight:700;color:#f43f5e;font-size:0.9rem;margin-bottom:4px;">
    <svg class="svg-icon" viewBox="0 0 24 24" style="width:16px;height:16px;display:inline-block;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    Authentication Failed (401 Unauthorized)
  </div>
  <span>${escapeHtml(errorMsg)}. Please verify your secret API key in the <strong>Authentication Vault</strong> above.</span>
</div>` + syntaxHighlight(data);
      }
      if (logsList) {
        logsList.innerHTML = `<div style="color:#f43f5e;font-weight:700">[AUTH] 401 Unauthorized: Invalid X-API-Key credentials. Request rejected by gateway.</div>`;
      }
      const vault = document.getElementById('apiVaultCard');
      if (vault) {
        vault.classList.add('vault-attention-shake');
        setTimeout(() => vault.classList.remove('vault-attention-shake'), 700);
      }
      return;
    }

    // Update Status Pill
    const code = data.status_code || res.status;
    if (statusPill) {
      if (code >= 200 && code < 300) {
        statusPill.className = 'metric-pill success';
        statusPill.textContent = `Status: ${code} OK`;
      } else if (code === 429) {
        statusPill.className = 'metric-pill amber';
        statusPill.textContent = `Status: 429 Rate Limited`;
      } else {
        statusPill.className = 'metric-pill amber';
        statusPill.textContent = `Status: ${code}`;
      }
    }

    // Engine & Metadata Pills
    const meta = data.meta || {};
    const via = meta.via || 'http';
    if (viaPill) {
      viaPill.className = via === 'http' ? 'metric-pill info' : 'metric-pill purple';
      viaPill.textContent = `Engine: ${via.toUpperCase()}`;
    }

    const isHit = Boolean(meta.cache_hit);
    if (cachePill) {
      cachePill.className = isHit ? 'metric-pill success' : 'metric-pill';
      cachePill.textContent = `Cache: ${isHit ? 'HIT' : 'MISS'}`;
    }

    if (protocolPill) protocolPill.textContent = `Proto: ${data.protocol || 'h2'}`;
    if (cookiesUsedPill) cookiesUsedPill.textContent = `Cookies: ${meta.cookies_used || 0}`;
    if (timePill) {
      timePill.className = 'metric-pill info';
      timePill.textContent = `Latency: ${elapsed} ms`;
    }

    // JSON View (with truncation safeguard for huge bodies)
    const jsonToDisplay = { ...data };
    if (typeof jsonToDisplay.body === 'string' && jsonToDisplay.body.length > 500) {
      const trimmed = jsonToDisplay.body.trim();
      if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) {
        jsonToDisplay.body = jsonToDisplay.body.substring(0, 500) + `\n... [Truncated ${jsonToDisplay.body.length} chars total — see HTML tab]`;
      }
    }
    jsonOutput.innerHTML = syntaxHighlight(jsonToDisplay);

    // HTML Preview View
    updateHtmlPreview(data.body || '');

    // Extracted Tab
    if (data.extracted !== null && data.extracted !== undefined) {
      const items = Array.isArray(data.extracted) ? data.extracted : [data.extracted];
      if (extractedCount) extractedCount.textContent = items.length;
      if (items.length > 0) {
        let extHtml = '<div style="display:flex;flex-direction:column;gap:8px;">';
        items.forEach((item, idx) => {
          extHtml += `<div class="extracted-item"><span style="color:var(--text-muted);margin-right:8px;">#${idx + 1}</span>${escapeHtml(item)}</div>`;
        });
        extHtml += '</div>';
        extractedList.innerHTML = extHtml;
      } else {
        extractedList.innerHTML = '<div class="empty-tab-msg">No matches found for selector in HTML.</div>';
      }
    } else {
      if (extractedCount) extractedCount.textContent = '0';
      extractedList.innerHTML = '<div class="empty-tab-msg">No extraction selector specified in request.</div>';
    }

    // Cookies Tab
    const cookies = data.cookies || {};
    const cKeys = Object.keys(cookies);
    if (cookieCount) cookieCount.textContent = cKeys.length;
    if (cKeys.length > 0) {
      let cHtml = '<div style="display:flex;flex-direction:column;gap:12px;">';
      cKeys.forEach(k => {
        const isClearance = k.toLowerCase().includes('cf_clearance');
        const safeKey = escapeHtml(k);
        const safeVal = escapeHtml(cookies[k]);
        cHtml += `
          <div style="background:#202024;border:1px solid ${isClearance ? 'rgba(234,88,12,0.4)' : '#2e2e34'};border-radius:8px;padding:12px 16px;">
            <div style="color:${isClearance ? '#fb923c' : '#38bdf8'};font-weight:700;font-size:0.86rem;margin-bottom:8px;display:flex;align-items:center;gap:8px;">
              <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${isClearance ? '#fb923c' : '#38bdf8'};"></span>
              ${safeKey}
              ${isClearance ? '<span style="font-size:0.7rem;padding:2px 6px;border-radius:4px;background:rgba(234,88,12,0.15);color:#fb923c;">Cloudflare Clearance</span>' : ''}
            </div>
            <div style="font-family:var(--font-mono);font-size:0.82rem;color:#e2e8f0;word-break:break-all;background:rgba(0,0,0,0.35);padding:8px 12px;border-radius:6px;line-height:1.6;user-select:all;border:1px solid rgba(255,255,255,0.05);">
              ${safeVal}
            </div>
          </div>
        `;
      });
      cHtml += '</div>';
      browserStateList.innerHTML = cHtml;
    } else {
      browserStateList.innerHTML = '<div class="empty-tab-msg">No cookies harvested in this response.</div>';
    }

    // Headers Tab
    const headers = data.headers || {};
    const hKeys = Object.keys(headers);
    if (hKeys.length > 0) {
      let hHtml = '<div style="display:flex;flex-direction:column;gap:8px;">';
      hKeys.forEach(k => {
        const safeHeader = escapeHtml(k);
        const safeVal = escapeHtml(headers[k]);
        hHtml += `
          <div style="background:#202024;border:1px solid #2e2e34;border-radius:6px;padding:8px 12px;display:flex;flex-direction:column;gap:3px;">
            <div style="color:#a78bfa;font-weight:700;font-size:0.82rem;">${safeHeader}</div>
            <div style="font-family:var(--font-mono);font-size:0.8rem;color:#e2e8f0;word-break:break-all;">${safeVal}</div>
          </div>
        `;
      });
      hHtml += '</div>';
      headersList.innerHTML = hHtml;
    } else {
      headersList.innerHTML = '<div class="empty-tab-msg">No headers available.</div>';
    }

    // Pipeline Logs Tab
    const logs = data.logs || [];
    if (logsCount) logsCount.textContent = logs.length;
    if (logsList) {
      if (logs.length > 0) {
        let logHtml = '';
        logs.forEach(line => {
          let colored = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
          colored = colored.replace(/(\[INIT\]|\[HTTP\]|\[RETRY\]|\[PROXY\]|\[IDENTITY\])/g, '<span style="color:#38bdf8;font-weight:700">$1</span>');
          colored = colored.replace(/(\[CACHE\]|\[EXTRACT\])/g, '<span style="color:#10b981;font-weight:700">$1</span>');
          colored = colored.replace(/(\[INJECT\]|\[HEADERS\])/g, '<span style="color:#a78bfa;font-weight:700">$1</span>');
          colored = colored.replace(/(\[HTTP-SUCCESS\]|\[RETRY-SUCCESS\]|\[DONE\])/g, '<span style="color:#4ade80;font-weight:700">$1</span>');
          colored = colored.replace(/(\[HTTP-BLOCKED\]|\[HTTP-INTERSTITIAL\]|\[DETECT\]|\[ESCALATE\]|\[FORCE\])/g, '<span style="color:#fbbf24;font-weight:700">$1</span>');
          colored = colored.replace(/(\[HTTP-FAIL\]|\[RETRY-FAIL\]|\[FAIL\]|\[ERROR\])/g, '<span style="color:#f43f5e;font-weight:700">$1</span>');
          colored = colored.replace(/(\[BROWSER\]|\[SOLVER\])/g, '<span style="color:#c084fc;font-weight:700">$1</span>');
          logHtml += `<div style="padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05);">${colored}</div>`;
        });
        logsList.innerHTML = logHtml;
      } else {
        logsList.innerHTML = '<span style="color:#71717a">No logs generated for this request.</span>';
      }
    }

    // Turnstile Tab
    if (data.turnstile_token) {
      if (tabBtnTurnstile) tabBtnTurnstile.style.display = 'flex';
      let tsHtml = `
        <div style="background:#202024;border:1px solid rgba(234,88,12,0.4);border-radius:8px;padding:16px;">
          <div style="color:#fb923c;font-weight:700;font-size:0.95rem;margin-bottom:8px;">Turnstile Clearance Token</div>
          <div style="font-family:var(--font-mono);font-size:0.84rem;color:#e2e8f0;word-break:break-all;background:rgba(0,0,0,0.4);padding:10px 14px;border-radius:6px;user-select:all;">
            ${escapeHtml(data.turnstile_token)}
          </div>
          <div style="margin-top:12px;display:flex;gap:16px;font-size:0.8rem;color:var(--text-muted);">
            <div>Sitekey: <code style="color:#e4e4e7;">${escapeHtml(data.turnstile_sitekey || 'auto')}</code></div>
            <div>Solved At: <code style="color:#e4e4e7;">${data.turnstile_solved_at ? new Date(data.turnstile_solved_at * 1000).toLocaleTimeString() : 'N/A'}</code></div>
          </div>
        </div>
      `;
      if (data.token_submissions && data.token_submissions.length > 0) {
        tsHtml += '<div style="margin-top:16px;font-weight:700;color:#e4e4e7;font-size:0.88rem;">Captured Postback Submissions:</div>';
        data.token_submissions.forEach(sub => {
          tsHtml += `
            <div style="margin-top:8px;background:#202024;border:1px solid #2e2e34;border-radius:6px;padding:10px 14px;font-size:0.82rem;">
              <div style="color:#38bdf8;font-weight:600;">${escapeHtml(sub.method || 'POST')} ${escapeHtml(sub.url)} (${escapeHtml(sub.response_status)})</div>
              <pre style="margin-top:6px;color:#a1a1aa;font-size:0.78rem;"><code>${escapeHtml(sub.response_body || '')}</code></pre>
            </div>
          `;
        });
      }
      if (turnstileContent) turnstileContent.innerHTML = tsHtml;
    } else {
      if (tabBtnTurnstile) tabBtnTurnstile.style.display = 'none';
    }

    // Refresh Live Cache Explorer entries
    loadCacheExplorer();
    checkHealth();
  } catch (err) {
    const elapsed = Math.round(performance.now() - t0);
    if (statusPill) {
      statusPill.className = 'metric-pill amber';
      statusPill.textContent = 'Status: Failed';
    }
    if (timePill) timePill.textContent = `Latency: ${elapsed} ms`;
    if (jsonOutput) jsonOutput.innerHTML = `<span style="color:#ef4444">Error executing request: ${escapeHtml(err.message)}</span>`;
    if (logsList) logsList.innerHTML = `<span style="color:#ef4444">[ERROR] Pipeline failed: ${escapeHtml(err.message)}</span>`;
  } finally {
    if (submitBtn) submitBtn.disabled = false;
    if (btnLabel) btnLabel.textContent = 'Send Request';
  }
});

// Domain cache explorer

const cacheGrid = document.getElementById('cacheGrid');
const cacheSearchInput = document.getElementById('cacheSearchInput');
const btnRefreshCache = document.getElementById('btnRefreshCache');
const refreshCacheIcon = document.getElementById('refreshCacheIcon');
const statCachedDomains = document.getElementById('statCachedDomains');
const statActiveTabs = document.getElementById('statActiveTabs');
const statTotalCookies = document.getElementById('statTotalCookies');

function formatTtl(seconds) {
  if (seconds <= 0) return 'Expired';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

async function loadCacheExplorer() {
  const apiKey = (globalApiKeyInput ? globalApiKeyInput.value.trim() : '') || getStoredApiKey();

  if (refreshCacheIcon) refreshCacheIcon.style.animation = 'spin 1s linear infinite';

  try {
    // 1. Health Status
    const hRes = await fetch('/health');
    const hData = await hRes.json();
    if (hData.status === 'ok') {
      const pool = hData.browser_pool || {};
      if (statActiveTabs) {
        statActiveTabs.textContent = `${pool.active_tabs || 0} / ${pool.max_browsers || 1}`;
      }
    }

    // 2. Cookies List
    if (!apiKey) {
      if (statCachedDomains) statCachedDomains.textContent = '0';
      if (statTotalCookies) statTotalCookies.textContent = '0';
      if (cacheGrid) {
        cacheGrid.innerHTML = `
          <div class="cache-loading-msg">
            <div class="cache-lock-badge" id="cacheLockBadge">
              <svg class="svg-icon" viewBox="0 0 24 24">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
            </div>
            <div style="font-weight:700;color:var(--text-primary);margin-top:10px;">API Key Required to Inspect Domain Cache</div>
          </div>
        `;
      }
      return;
    }

    const cRes = await fetch('/cookies', {
      headers: { 'X-API-Key': apiKey }
    });

    if (cRes.status === 401) {
      if (cacheGrid) {
        cacheGrid.innerHTML = `
          <div class="cache-loading-msg">
            <div class="cache-lock-badge is-error">
              <svg class="svg-icon" viewBox="0 0 24 24">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
            </div>
            <div style="color:var(--accent-rose);font-weight:700;margin-top:10px;font-size:1.05rem;">401 Unauthorized</div>
            <p style="font-size:0.84rem;color:var(--text-secondary);margin-top:4px;">The provided API key is invalid. Please verify your X-API-Key.</p>
          </div>
        `;
      }
      return;
    }

    const domains = await cRes.json();
    cachedDomainsData = domains;
    const domainKeys = Object.keys(domains);

    // Update Stats
    if (statCachedDomains) statCachedDomains.textContent = domainKeys.length;
    let totalCookies = 0;
    domainKeys.forEach(k => {
      totalCookies += domains[k].cookie_count || Object.keys(domains[k].cookies || {}).length || 0;
    });
    if (statTotalCookies) statTotalCookies.textContent = totalCookies;

    renderDomainCards();
  } catch (err) {
    if (cacheGrid) {
      cacheGrid.innerHTML = `<div class="cache-loading-msg"><p style="color:#ef4444;">Error connecting to cache service: ${escapeHtml(err.message)}</p></div>`;
    }
  } finally {
    if (refreshCacheIcon) refreshCacheIcon.style.animation = '';
  }
}

function renderDomainCards() {
  if (!cacheGrid) return;
  const filter = (cacheSearchInput ? cacheSearchInput.value.trim().toLowerCase() : '');
  const allKeys = Object.keys(cachedDomainsData);
  const filteredKeys = allKeys.filter(k => k.toLowerCase().includes(filter));

  if (allKeys.length === 0) {
    cacheGrid.innerHTML = `
      <div class="cache-loading-msg">
        <div class="cache-empty-icon">
          <svg class="svg-icon" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
        </div>
        <div style="font-weight:700;color:var(--text-primary);margin-bottom:6px;">No Cached Domain Sessions</div>
        <p style="font-size:0.84rem;color:var(--text-secondary);max-width:440px;margin:0 auto;">When you make a request to a Cloudflare-protected site, solved <code>cf_clearance</code> tokens are saved here for subsequent sub-second visits.</p>
      </div>
    `;
    return;
  }

  if (filteredKeys.length === 0) {
    cacheGrid.innerHTML = `
      <div class="cache-loading-msg">
        <p>No cached domains match your filter "<strong>${filter}</strong>".</p>
      </div>
    `;
    return;
  }

  let html = '';
  filteredKeys.forEach(domainKey => {
    const entry = cachedDomainsData[domainKey];
    const cookies = entry.cookies || {};
    const cookieNames = Object.keys(cookies);
    const ttl = entry.ttl_remaining !== undefined ? entry.ttl_remaining : 0;
    const ua = entry.user_agent || 'Preset User-Agent';

    // Parse Identity and Host
    let identity = '';
    let host = domainKey;
    if (domainKey.includes('@')) {
      const parts = domainKey.split('@');
      identity = parts[0];
      host = parts[1];
    }

    const safeHost = escapeHtml(host);
    const safeIdentity = escapeHtml(identity);
    const safeUa = escapeHtml(ua);
    const safeDomainKey = escapeHtml(domainKey);
    const safeCardId = 'card-' + domainKey.replace(/[^a-zA-Z0-9_-]/g, '_');
    const safeTtlId = 'ttl-' + domainKey.replace(/[^a-zA-Z0-9_-]/g, '_');

    html += `
      <div class="domain-card" id="${safeCardId}">
        <div class="domain-card-header">
          <div class="domain-title-wrap">
            <div class="domain-host">
              <span>${safeHost}</span>
              ${identity ? `<span class="domain-identity-badge">${safeIdentity}</span>` : ''}
            </div>
            <span style="font-size:0.75rem;color:var(--text-muted);">${cookieNames.length} session cookie${cookieNames.length === 1 ? '' : 's'}</span>
          </div>
          <div class="domain-ttl-pill" data-ttl="${ttl}" id="${safeTtlId}">
            ${formatTtl(ttl)}
          </div>
        </div>

        <div class="domain-body">
          <div class="domain-ua-row">
            <span class="domain-ua-label">User-Agent Profile</span>
            <div class="domain-ua-text" title="${safeUa}">${safeUa}</div>
          </div>

          <div class="domain-cookies-wrap">
            <span class="domain-ua-label">Stored Cookies</span>
            <div class="domain-cookies-chips">
              ${cookieNames.map(name => {
                const isClearance = name.toLowerCase().includes('cf_clearance');
                return `<span class="domain-cookie-chip ${isClearance ? 'highlight' : ''}">${escapeHtml(name)}</span>`;
              }).join('')}
            </div>
          </div>
        </div>

        <div class="domain-card-footer">
          <span style="font-size:0.75rem;color:var(--text-muted);">LRU Stored</span>
          <button type="button" class="btn-purge-domain" data-domain="${safeDomainKey}">
            <svg class="svg-icon" style="width:13px;height:13px;" viewBox="0 0 24 24">
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
            </svg>
            <span>Purge Domain</span>
          </button>
        </div>
      </div>
    `;
  });

  cacheGrid.innerHTML = html;

  // Wire Purge Buttons
  cacheGrid.querySelectorAll('.btn-purge-domain').forEach(btn => {
    btn.addEventListener('click', async () => {
      const domain = btn.getAttribute('data-domain');
      const apiKey = (globalApiKeyInput ? globalApiKeyInput.value.trim() : '') || getStoredApiKey();
      if (!domain || !apiKey) return;

      btn.disabled = true;
      btn.innerHTML = '<span>Purging...</span>';

      try {
        const res = await fetch(`/cookies/${encodeURIComponent(domain)}`, {
          method: 'DELETE',
          headers: { 'X-API-Key': apiKey }
        });
        if (res.ok) {
          delete cachedDomainsData[domain];
          renderDomainCards();
          checkHealth();
        } else {
          alert(`Failed to purge domain cache: HTTP ${res.status}`);
          btn.disabled = false;
          btn.innerHTML = '<span>Purge Domain</span>';
        }
      } catch (err) {
        alert('Network error purging domain: ' + err.message);
        btn.disabled = false;
        btn.innerHTML = '<span>Purge Domain</span>';
      }
    });
  });
}

// Live TTL Countdown Timer (Decrements every second)
if (ttlIntervalTimer) clearInterval(ttlIntervalTimer);
ttlIntervalTimer = setInterval(() => {
  document.querySelectorAll('.domain-ttl-pill[data-ttl]').forEach(el => {
    let cur = parseInt(el.getAttribute('data-ttl'), 10);
    if (!isNaN(cur) && cur > 0) {
      cur -= 1;
      el.setAttribute('data-ttl', cur);
      el.textContent = formatTtl(cur);
      if (cur <= 0) {
        el.style.background = 'rgba(239,68,68,0.1)';
        el.style.borderColor = 'rgba(239,68,68,0.25)';
        el.style.color = '#ef4444';
      }
    }
  });
}, 1000);

if (cacheSearchInput) {
  cacheSearchInput.addEventListener('input', () => {
    renderDomainCards();
  });
}

if (btnRefreshCache) {
  btnRefreshCache.addEventListener('click', () => {
    loadCacheExplorer();
  });
}

// Health and connection check

async function checkHealth() {
  const statusText = document.getElementById('statusText');
  const liveStatusBadge = document.getElementById('liveStatusBadge');
  try {
    const res = await fetch('/health');
    const data = await res.json();
    if (data.status === 'ok') {
      if (statusText) statusText.textContent = 'Online';
      if (liveStatusBadge) liveStatusBadge.style.color = 'var(--accent-green)';
    }
  } catch (err) {
    if (statusText) statusText.textContent = 'Offline';
    if (liveStatusBadge) liveStatusBadge.style.color = '#ef4444';
  }
}

// Initial Invocations
checkHealth();
loadCacheExplorer();
setInterval(checkHealth, 15000);

// Auto-navigate to section based on URL hash or path
window.addEventListener('DOMContentLoaded', () => {
  const hash = window.location.hash;
  const path = window.location.pathname;
  if (hash === '#cache' || path === '/cache') {
    const cacheSec = document.getElementById('cache');
    if (cacheSec) cacheSec.scrollIntoView({ behavior: 'smooth' });
  } else if (hash === '#playground' || path === '/playground') {
    const pgSec = document.getElementById('playground');
    if (pgSec) pgSec.scrollIntoView({ behavior: 'smooth' });
  }
});

// ================= Studio Mode Tabs Switcher (Scraping vs Turnstile) =================
document.querySelectorAll('.studio-mode-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.studio-mode-tab').forEach(t => {
      t.classList.remove('active');
      t.setAttribute('aria-selected', 'false');
    });
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    const mode = tab.getAttribute('data-mode');
    const fetchPane = document.getElementById('paneModeFetch');
    const turnstilePane = document.getElementById('paneModeTurnstile');
    if (mode === 'fetch') {
      if (fetchPane) fetchPane.style.display = 'block';
      if (turnstilePane) turnstilePane.style.display = 'none';
    } else {
      if (fetchPane) fetchPane.style.display = 'none';
      if (turnstilePane) turnstilePane.style.display = 'block';
    }
  });
});

// ================= Feature Guides: Usage Examples Sub-navigation =================
document.querySelectorAll('.examples-subnav .btn-sub-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.examples-subnav .btn-sub-toggle').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const lang = btn.getAttribute('data-lang');
    const map = { py: 'exPython', curl: 'exCurl', node: 'exNode', solve: 'exSolve' };
    document.querySelectorAll('.example-lang-pane').forEach(p => p.style.display = 'none');
    const target = document.getElementById(map[lang]);
    if (target) target.style.display = 'block';
  });
});

// ================= Dedicated Turnstile Solver Logic =================
const tsForm = document.getElementById('turnstileSolveForm');
if (tsForm) {
  // Test Presets
  document.querySelectorAll('.btn-preset-solver').forEach(btn => {
    btn.addEventListener('click', () => {
      const sitekey = btn.getAttribute('data-sitekey');
      const siteurl = btn.getAttribute('data-siteurl');
      if (sitekey) document.getElementById('tsSitekey').value = sitekey;
      if (siteurl) document.getElementById('tsSiteurl').value = siteurl;
    });
  });

  // Action Buttons
  document.getElementById('btnPasteSitekey')?.addEventListener('click', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) document.getElementById('tsSitekey').value = text.trim();
    } catch (e) {
      document.getElementById('tsSitekey').focus();
    }
  });

  document.getElementById('btnClearSiteurl')?.addEventListener('click', () => {
    document.getElementById('tsSiteurl').value = '';
    document.getElementById('tsSiteurl').focus();
  });

  document.getElementById('btnPasteSavedProxyTs')?.addEventListener('click', () => {
    const saved = localStorage.getItem('anti_user_proxy');
    if (saved) {
      document.getElementById('tsProxy').value = saved;
    } else {
      alert('No saved proxy found. Enter a proxy in the Scraping Options tab to save one.');
    }
  });

  const solverStatusPill = document.getElementById('solverStatusPill');
  const solverEnginePill = document.getElementById('solverEnginePill');
  const solverSitekeyPill = document.getElementById('solverSitekeyPill');
  const solverProxyPill = document.getElementById('solverProxyPill');
  const solverTimePill = document.getElementById('solverTimePill');
  const turnstileTokenOutput = document.getElementById('turnstileTokenOutput');
  const tokenLenBadge = document.getElementById('tokenLenBadge');
  const solverRawJson = document.getElementById('solverRawJson');
  const tsSubmissionsList = document.getElementById('tsSubmissionsList');
  const tsSubmissionsCount = document.getElementById('tsSubmissionsCount');
  const tsLogsList = document.getElementById('tsLogsList');
  let currentSolverData = null;

  document.getElementById('btnResetSolver')?.addEventListener('click', () => {
    tsForm.reset();
    currentSolverData = null;
    if (solverStatusPill) {
      solverStatusPill.className = 'metric-pill';
      solverStatusPill.textContent = 'Status: Idle';
    }
    if (solverEnginePill) solverEnginePill.textContent = 'Engine: BROWSER';
    if (solverSitekeyPill) solverSitekeyPill.textContent = 'Sitekey: --';
    if (solverProxyPill) solverProxyPill.textContent = 'Proxy: Direct';
    if (solverTimePill) {
      solverTimePill.className = 'metric-pill';
      solverTimePill.textContent = 'Latency: -- s';
    }
    if (tokenLenBadge) tokenLenBadge.textContent = '0 chars';
    if (turnstileTokenOutput) turnstileTokenOutput.innerHTML = '// Configure sitekey and target URL above and click "Turnstile Token"...';
    if (solverRawJson) solverRawJson.textContent = '// Awaiting solver execution...';
    if (tsSubmissionsList) tsSubmissionsList.innerHTML = 'No postback form submissions captured yet.';
    if (tsSubmissionsCount) tsSubmissionsCount.textContent = '0';
    if (tsLogsList) tsLogsList.innerHTML = '// Headless browser challenge solver logs stream here...';
  });

  // 1-Click Copy Token
  document.getElementById('btnCopyTurnstileToken')?.addEventListener('click', () => {
    const tokenVal = turnstileTokenOutput?.textContent?.trim();
    if (!tokenVal || tokenVal.startsWith('//')) return;
    navigator.clipboard.writeText(tokenVal).then(() => {
      const btnText = document.getElementById('btnCopyTokenText');
      if (btnText) {
        const orig = btnText.textContent;
        btnText.textContent = 'Copied!';
        setTimeout(() => { btnText.textContent = orig; }, 1800);
      }
    });
  });

  // 1-Click Copy Solver JSON
  document.getElementById('btnCopySolverJson')?.addEventListener('click', () => {
    if (!currentSolverData) return;
    navigator.clipboard.writeText(JSON.stringify(currentSolverData, null, 2)).then(() => {
      const span = document.getElementById('btnCopySolverJson')?.querySelector('span');
      if (span) {
        const orig = span.textContent;
        span.textContent = 'Copied!';
        setTimeout(() => { span.textContent = orig; }, 1800);
      }
    });
  });

  // 1-Click Copy Integration Snippet
  document.getElementById('btnCopyIntegrationSnippet')?.addEventListener('click', () => {
    const snippet = document.getElementById('tokenCurlSnippet')?.textContent;
    if (!snippet) return;
    navigator.clipboard.writeText(snippet).then(() => {
      const span = document.getElementById('btnCopyIntegrationSnippet')?.querySelector('span');
      if (span) {
        const orig = span.textContent;
        span.textContent = 'Copied!';
        setTimeout(() => { span.textContent = orig; }, 1800);
      }
    });
  });

  // Solver Form Submission
  tsForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const sitekey = document.getElementById('tsSitekey').value.trim();
    const siteurl = document.getElementById('tsSiteurl').value.trim();
    const proxy = document.getElementById('tsProxy').value.trim();
    const timeout = parseInt(document.getElementById('tsTimeout').value, 10) || 30;

    const apiKey = (globalApiKeyInput ? globalApiKeyInput.value.trim() : '') || getStoredApiKey();
    if (!apiKey) {
      if (solverStatusPill) {
        solverStatusPill.className = 'metric-pill amber';
        solverStatusPill.textContent = 'API Key Required';
      }
      if (turnstileTokenOutput) {
        turnstileTokenOutput.innerHTML = `<span style="color:#f43f5e">// [401 Unauthorized]\nX-API-Key is strictly required.\nPlease enter your secret API key in the Authentication Vault above.</span>`;
      }
      if (tsLogsList) {
        tsLogsList.innerHTML = `<div style="color:#f43f5e;font-weight:700">[AUTH] Aborted: X-API-Key missing. Set your key in the Authentication Vault above.</div>`;
      }
      const vault = document.getElementById('apiVaultCard');
      if (vault) {
        vault.classList.add('vault-attention-shake');
        setTimeout(() => vault.classList.remove('vault-attention-shake'), 700);
        vault.scrollIntoView({ behavior: 'smooth', block: 'center' });
        if (globalApiKeyInput) globalApiKeyInput.focus();
      }
      return;
    }

    const submitBtn = document.getElementById('btnSubmitSolve');
    const submitLabel = document.getElementById('solverBtnLabel');

    if (submitBtn) submitBtn.disabled = true;
    if (submitLabel) submitLabel.textContent = 'Solving Token...';

    if (solverStatusPill) {
      solverStatusPill.className = 'metric-pill amber';
      solverStatusPill.textContent = 'Status: Solving...';
    }
    if (solverEnginePill) solverEnginePill.textContent = 'Engine: BROWSER';
    if (solverSitekeyPill) solverSitekeyPill.textContent = 'Sitekey: ' + (sitekey.length > 14 ? sitekey.substring(0, 10) + '...' : sitekey);
    if (solverProxyPill) solverProxyPill.textContent = 'Proxy: ' + (proxy ? proxy.replace(/:[^:@]+@/, ':***@') : 'Default');
    if (solverTimePill) {
      solverTimePill.className = 'metric-pill';
      solverTimePill.textContent = 'Latency: 0.0s';
    }

    if (turnstileTokenOutput) {
      turnstileTokenOutput.innerHTML = '<span style="color:#fb923c">// Negotiating Turnstile challenge in headless Chromium pool...</span>';
    }
    if (solverRawJson) solverRawJson.textContent = '// Dispatching request to /solve-turnstile...';
    if (tsLogsList) tsLogsList.innerHTML = '<div style="color:#fb923c">// Allocating browser tab & navigating to challenge target...</div>';

    const t0 = performance.now();
    const timerInterval = setInterval(() => {
      const curElapsed = ((performance.now() - t0) / 1000).toFixed(1);
      if (solverTimePill) solverTimePill.textContent = `Latency: ${curElapsed}s`;
    }, 100);

    const payload = { sitekey, siteurl, timeout };
    if (proxy) payload.proxy = proxy;

    try {
      const res = await fetch('/solve-turnstile', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': apiKey,
        },
        body: JSON.stringify(payload),
      });

      clearInterval(timerInterval);
      const totalElapsed = ((performance.now() - t0) / 1000).toFixed(2);
      if (solverTimePill) {
        solverTimePill.className = 'metric-pill info';
        solverTimePill.textContent = `Latency: ${totalElapsed}s`;
      }

      const rawText = await res.text();
      let data;
      try {
        data = JSON.parse(rawText);
      } catch (err) {
        data = { error: 'Invalid response from server', raw: rawText };
      }
      currentSolverData = data;
      // The server reports the egress it actually used (credentials masked): default browser, direct or the proxy
      if (solverProxyPill && data && typeof data.proxy === 'string') {
        solverProxyPill.textContent = 'Proxy: ' + (data.proxy === 'direct' ? 'Direct' : data.proxy);
      }

      if (solverRawJson) {
        solverRawJson.innerHTML = syntaxHighlight(data);
      }

      // Handle 401 Unauthorized
      if (res.status === 401) {
        if (solverStatusPill) {
          solverStatusPill.className = 'metric-pill amber';
          solverStatusPill.textContent = 'Status: 401 Unauthorized';
        }
        if (solverEnginePill) solverEnginePill.textContent = 'Engine: REJECTED';
        const errorMsg = data && data.detail ? data.detail : 'Invalid X-API-Key provided.';
        if (turnstileTokenOutput) {
          turnstileTokenOutput.innerHTML = `<span style="color:#f43f5e">// [401 Unauthorized] ${escapeHtml(errorMsg)}\nPlease check your secret key in the Authentication Vault above.</span>`;
        }
        if (tsLogsList) {
          tsLogsList.innerHTML = `<div style="color:#f43f5e;font-weight:700">[AUTH] 401 Unauthorized: Invalid X-API-Key credentials. Request rejected by gateway.</div>`;
        }
        const vault = document.getElementById('apiVaultCard');
        if (vault) {
          vault.classList.add('vault-attention-shake');
          setTimeout(() => vault.classList.remove('vault-attention-shake'), 700);
        }
        return;
      }

      if (res.ok && data.solved && data.token) {
        if (solverStatusPill) {
          solverStatusPill.className = 'metric-pill success';
          solverStatusPill.textContent = 'Status: 200 Solved';
        }
        if (tokenLenBadge) tokenLenBadge.textContent = `${data.token.length} chars`;
        if (turnstileTokenOutput) {
          turnstileTokenOutput.textContent = data.token;
        }

        // Generate cURL & Python snippet
        const curlSnippet = `# 1. Direct cURL submission:\ncurl -X POST "${siteurl}" \\\n  -H "Content-Type: application/x-www-form-urlencoded" \\\n  --data-urlencode "cf-turnstile-response=${data.token}"\n\n# 2. Python requests snippet:\nimport requests\nres = requests.post("${siteurl}", data={"cf-turnstile-response": "${data.token}"})\nprint(res.status_code, res.text[:200])`;
        const snippetEl = document.getElementById('tokenCurlSnippet');
        if (snippetEl) snippetEl.textContent = curlSnippet;

        // Submissions
        if (data.token_submissions && data.token_submissions.length > 0) {
          if (tsSubmissionsCount) tsSubmissionsCount.textContent = data.token_submissions.length;
          let subHtml = '<div style="display:flex;flex-direction:column;gap:10px;">';
          data.token_submissions.forEach((sub, idx) => {
            subHtml += `
              <div style="background:#202024;border:1px solid #2e2e34;border-radius:6px;padding:12px 14px;font-size:0.84rem;">
                <div style="color:#38bdf8;font-weight:600;margin-bottom:6px;">#${idx + 1} — ${escapeHtml(sub.method || 'POST')} ${escapeHtml(sub.url)} (${escapeHtml(sub.response_status || '200')})</div>
                <pre style="margin:0;color:#a1a1aa;font-size:0.78rem;background:transparent;padding:0;"><code>${escapeHtml(sub.response_body || '')}</code></pre>
              </div>`;
          });
          subHtml += '</div>';
          if (tsSubmissionsList) tsSubmissionsList.innerHTML = subHtml;
        } else {
          if (tsSubmissionsCount) tsSubmissionsCount.textContent = '0';
          if (tsSubmissionsList) tsSubmissionsList.innerHTML = '<div style="color:var(--text-muted);font-family:var(--font-mono);font-size:0.84rem;">No automatic postback form submission was captured. You can pass the solved token manually via the Integration Code tab.</div>';
        }

        if (tsLogsList) {
          tsLogsList.innerHTML = `<div style="color:#4ade80;font-weight:700">[TURNSTILE] Solved successfully in ${totalElapsed}s.</div>
<div style="color:#e4e4e7;margin-top:4px;">Sitekey: ${escapeHtml(sitekey)}</div>
<div style="color:#e4e4e7;">Token length: ${data.token.length} chars</div>
<div style="color:#38bdf8;margin-top:4px;">Solved At: ${data.turnstile_solved_at ? new Date(data.turnstile_solved_at * 1000).toLocaleTimeString() : new Date().toLocaleTimeString()}</div>`;
        }
      } else {
        if (solverStatusPill) {
          solverStatusPill.className = 'metric-pill amber';
          solverStatusPill.textContent = `Status: ${res.status || 'Failed'}`;
        }
        const detailMsg = typeof data.detail === 'string' ? data.detail : (data.detail ? JSON.stringify(data.detail) : (data.error || 'Turnstile challenge could not be verified.'));
        if (turnstileTokenOutput) {
          turnstileTokenOutput.innerHTML = `<span style="color:#ef4444">// Solving Failed: ${escapeHtml(detailMsg)}</span>`;
        }
        if (tsLogsList) {
          tsLogsList.innerHTML = `<div style="color:#ef4444;font-weight:700">[ERROR] HTTP ${res.status}: ${escapeHtml(detailMsg)}</div>`;
        }
      }
    } catch (netErr) {
      clearInterval(timerInterval);
      if (solverStatusPill) {
        solverStatusPill.className = 'metric-pill amber';
        solverStatusPill.textContent = 'Status: Failed';
      }
      if (turnstileTokenOutput) {
        turnstileTokenOutput.innerHTML = `<span style="color:#ef4444">// Network or Gateway Error: ${escapeHtml(netErr.message)}</span>`;
      }
      if (tsLogsList) {
        tsLogsList.innerHTML = `<div style="color:#ef4444;font-weight:700">[ERROR] Request Failed: ${escapeHtml(netErr.message)}</div>`;
      }
      if (solverRawJson) solverRawJson.textContent = JSON.stringify({ error: netErr.message }, null, 2);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
      if (submitLabel) submitLabel.textContent = 'Turnstile Token';
    }
  });
}
window.toggleEndpointCard = function (bodyId, headerEl) {
  const body = document.getElementById(bodyId);
  const btn = headerEl.querySelector('.btn-endpoint-toggle');
  const label = headerEl.querySelector('.endpoint-toggle-label');
  if (!body) return;
  const isCollapsed = body.classList.toggle('is-collapsed');
  if (btn) btn.classList.toggle('is-collapsed', isCollapsed);
  if (label) label.textContent = isCollapsed ? 'Show' : 'Hide';
};
