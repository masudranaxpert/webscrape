"""JavaScript snippets the browser engine injects to detect and solve Turnstile widgets.

Kept separate from the pool code: they are pure strings, tuned per Cloudflare behaviour.
"""

# Recursive shadow DOM walker to locate Turnstile checkbox within challenge iframes
FIND_CHECKBOX_JS = """() => {
    function find(root){
        if(!root) return null;
        const direct = root.querySelector && root.querySelector('input[type=checkbox]');
        if(direct) return direct;
        for(const el of (root.querySelectorAll ? root.querySelectorAll('*') : [])){
            const sr = el.fakeShadowRoot || el.shadowRoot;
            if(sr){ const r = find(sr); if(r) return r; }
        }
        return null;
    }
    const cb = find(document);
    if(!cb) return {found:false};
    const r = cb.getBoundingClientRect();
    return {found:true, checked:cb.checked, x:r.x+r.width/2, y:r.y+r.height/2, w:r.width};
}"""

# Detects an embedded Turnstile widget (not a full-page interstitial) and whether it has issued a token
TURNSTILE_STATE_JS = """() => {
    const widgets = document.querySelectorAll(
        '.cf-turnstile, iframe[src*="challenges.cloudflare.com"][src*="turnstile"]'
    );
    const inputs = document.querySelectorAll(
        'input[name*="turnstile-response"], textarea[name*="turnstile-response"]'
    );
    // Widgets rendered via turnstile.render() live in a shadow tree the selectors above cannot see, but they
    // always leave their hidden response input behind in the light DOM
    if(!widgets.length && !inputs.length) return {present:false, solved:false};
    const solved = inputs.length > 0 && Array.from(inputs).every(i => i.value && i.value.length > 0);
    return {present:true, solved:solved};
}"""

# Reads the Turnstile token: known response inputs, caller-supplied names/globs, then turnstile.getResponse()

# Fast in-browser DOM probe to determine if Cloudflare challenge wall has cleared
IS_BYPASSED_JS = """() => {
    try {
        const title = (document.title || '').toLowerCase();
        const challengeTitles = [
            'just a moment',
            'attention required',
            'security check',
            'checking your browser',
            'verifying you are human'
        ];
        if (challengeTitles.some(t => title.includes(t))) return false;

        // Interstitial challenge wall DOM containers
        if (document.querySelector(
            '#challenge-running, #challenge-form, form#challenge-form, #challenge-stage, #cf-wrapper, .cf-browser-verification, #trk_jschal_js'
        )) return false;

        // Challenge platform orchestrator globals
        if (window._cf_chl_opt || window.__cf_chl_opt || window._cf_chl_ctx) return false;

        // Challenge iframe inside challenge container
        if (document.querySelector('#challenge-stage iframe, #challenge-form iframe, #cf-wrapper iframe')) return false;

        // Explicit block / access denied messages in body text
        const bodyText = (document.body ? (document.body.innerText || '') : '').toLowerCase();
        if (
            bodyText.includes('please complete the captcha') ||
            bodyText.includes('you have been blocked') ||
            bodyText.includes('sorry, you have been blocked') ||
            bodyText.includes('error 1020') ||
            bodyText.includes('access denied')
        ) return false;

        return true;
    } catch (e) {
        return false;
    }
}"""

CLICK_ACTION_JS = """(sel) => {
        const btn = document.querySelector(sel);
        if (btn) {
            btn.disabled = false;
            btn.click();
            return true;
        }
        const f = document.querySelector('form');
        if (f) {
            f.submit();
            return true;
        }
        return false;
    }"""
