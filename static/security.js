window.__security = (function () {
    'use strict';

    const tokenMeta = document.querySelector('meta[name="csrf-token"]');
    const csrfToken = tokenMeta ? tokenMeta.content : '';
    const unsafeMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
    const legacyEventAttributes = [
        'onclick', 'onsubmit', 'oninput', 'onchange', 'onkeydown', 'onkeypress', 'onkeyup',
        'onfocus', 'onblur', 'onmouseenter', 'onmouseleave', 'onmouseover', 'onmouseout',
        'onerror', 'onload', 'onloadstart', 'onloadedmetadata', 'onloadeddata',
        'onscroll', 'onseeked', 'ontimeupdate'
    ];

    window.escapeHtml = function (value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    };

    window.secureLogout = function (event) {
        if (event) event.preventDefault();
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = '/logout';
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'csrf_token';
        input.value = csrfToken;
        form.appendChild(input);
        document.body.appendChild(form);
        form.submit();
        return false;
    };

    const originalFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        const options = Object.assign({}, init || {});
        const requestMethod = options.method || (input instanceof Request ? input.method : 'GET');
        const url = new URL(input instanceof Request ? input.url : input, window.location.href);

        if (unsafeMethods.has(requestMethod.toUpperCase()) && url.origin === window.location.origin) {
            const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
            headers.set('X-CSRF-Token', csrfToken);
            options.headers = headers;
        }
        return originalFetch(input, options);
    };

    function addCsrfInput(form) {
        const method = (form.method || 'GET').toUpperCase();
        if (!unsafeMethods.has(method) || form.querySelector('input[name="csrf_token"]')) return;
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'csrf_token';
        input.value = csrfToken;
        form.appendChild(input);
    }

    function normalizeEventAttribute(attrName) {
        if (attrName === 'onload') return 'load';
        if (attrName === 'onerror') return 'error';
        return attrName.slice(2);
    }

    function compileLegacyHandler(node, attrName, code) {
        try {
            return new Function('event', 'node', 'csrfToken', `"use strict";\n${code}`);
        } catch (error) {
            console.error(`security.js: invalid inline handler in ${attrName}`, error);
            return null;
        }
    }

    function bindLegacyEventListeners(root = document) {
        const selector = legacyEventAttributes.map((attribute) => `[${attribute}]`).join(',');
        const nodes = root.querySelectorAll(selector);

        for (const node of nodes) {
            for (const attribute of legacyEventAttributes) {
                const code = node.getAttribute(attribute);
                if (!code) continue;

                const eventType = normalizeEventAttribute(attribute);
                const handler = compileLegacyHandler(node, attribute, code);
                if (!handler) {
                    node.removeAttribute(attribute);
                    continue;
                }

                node.addEventListener(eventType, function (event) {
                    const result = handler.call(node, event, node, csrfToken);
                    if (result === false) {
                        event.preventDefault();
                        event.stopPropagation();
                    }
                });
                node.removeAttribute(attribute);
            }
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            document.querySelectorAll('form').forEach(addCsrfInput);
            bindLegacyEventListeners();
        }, { once: true });
    } else {
        document.querySelectorAll('form').forEach(addCsrfInput);
        bindLegacyEventListeners();
    }

    document.addEventListener('submit', function (event) {
        addCsrfInput(event.target);
    }, true);

    return { bindLegacyEventListeners };
})();
