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
    const isLocalHost = ['localhost', '127.0.0.1'].includes(window.location.hostname);

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

    function topLevelSplit(code, delimiter) {
        const parts = [];
        let depth = 0;
        let inString = null;
        let inTemplate = false;
        let escaped = false;
        let start = 0;

        for (let i = 0; i < code.length; i++) {
            const ch = code[i];

            if (escaped) {
                escaped = false;
                continue;
            }
            if (ch === '\\') {
                escaped = true;
                continue;
            }
            if (inTemplate) {
                if (ch === '`') {
                    inTemplate = false;
                }
                continue;
            }
            if (inString) {
                if (ch === inString) {
                    inString = null;
                }
                continue;
            }

            if (ch === '"' || ch === "'" || ch === '`') {
                inString = ch;
                inTemplate = (ch === '`');
                continue;
            }
            if (ch === '(' || ch === '[' || ch === '{') depth++;
            else if (ch === ')' || ch === ']' || ch === '}') depth--;
            else if (ch === delimiter && depth === 0) {
                parts.push(code.slice(start, i));
                start = i + 1;
            }
        }
        if (start <= code.length) {
            parts.push(code.slice(start));
        }
        return parts;
    }

    function parseValue(raw) {
        const text = raw.trim();
        if (!text) return undefined;
        if (text === 'event') return { kind: 'event' };
        if (text === 'node' || text === 'this') return { kind: 'node' };
        if (text === 'false') return false;
        if (text === 'true') return true;
        if (text === 'null') return null;
        if ((text[0] === "'" && text[text.length - 1] === "'") || (text[0] === '"' && text[text.length - 1] === '"')) {
            try {
                return { kind: 'literal', value: JSON.parse(text) };
            } catch (_error) {
                return { kind: 'literal', value: text.slice(1, -1) };
            }
        }
        const regexMatch = text.match(/^\/(.+)\/([gimsuy]*)$/);
        if (regexMatch) {
            try {
                return { kind: 'literal', value: new RegExp(regexMatch[1], regexMatch[2] || '') };
            } catch (_error) {
                return null;
            }
        }
        if (/^-?\d+(\.\d+)?$/.test(text)) return { kind: 'literal', value: Number(text) };
        return null;
    }

    function parseArguments(raw) {
        const parts = topLevelSplit(raw, ',').map((part) => part.trim()).filter(Boolean);
        return parts.map((part) => parseValue(part));
    }

    function resolveArgument(arg, event, node) {
        if (arg === undefined || arg === null) return arg;
        if (arg && arg.kind === 'event') return event;
        if (arg && arg.kind === 'node') return node;
        if (arg && arg.kind === 'literal') return arg.value;
        return arg;
    }

    function describeFunctionRef(path) {
        const clean = path.trim();
        const parts = clean.split('.');
        let start = 0;
        if (!/^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$/.test(clean)) {
            return null;
        }
        if (parts[0] === 'this' || parts[0] === 'node') {
            start = 1;
        } else if (parts[0] === 'window') {
            start = 1;
        }
        if (parts[0] === 'event') {
            start = 1;
        }
        if (start >= parts.length) return null;
        return {
            parts,
            start,
            rootKind: parts[0] === 'this' ? 'node' : parts[0] === 'node' ? 'node' : parts[0] === 'event' ? 'event' : 'window',
            fnName: parts[parts.length - 1],
        };
    }

    function resolveFunctionRefAtRuntime(fnRef, event, node) {
        let context = null;
        if (fnRef.rootKind === 'node') {
            context = node;
        } else if (fnRef.rootKind === 'event') {
            context = event;
        } else {
            context = window;
        }

        if (!context) return null;

        for (let i = fnRef.start; i < fnRef.parts.length - 1; i++) {
            const key = fnRef.parts[i];
            if (!(key in context)) return null;
            context = context[key];
            if (context == null) return null;
        }

        if (typeof context !== 'object' || context === null) return null;
        const fn = context[fnRef.fnName];
        if (typeof fn !== 'function') return null;
        return { fn, context };
    }

    function invokeFunctionRef(fnRef, event, node, args) {
        const resolved = resolveFunctionRefAtRuntime(fnRef, event, node);
        if (!resolved) return;
        return resolved.fn.apply(resolved.context, args);
    }

    function normalizePathLikePath(raw) {
        return raw.replace(/\s+/g, '').trim();
    }

    function compileLegacyHandler(node, attrName, code) {
        const originalCode = String(code || '').trim();
        if (!originalCode) return null;

        const statements = topLevelSplit(originalCode, ';').map((statement) => statement.trim()).filter(Boolean);
        if (!statements.length) return null;

        const handlers = [];

        for (const statement of statements) {
            const hasReturn = /^return\b/.test(statement);
            const withoutReturn = hasReturn ? statement.replace(/^return\s+/, '').trim() : statement;

            if (hasReturn && (withoutReturn === 'true' || withoutReturn === 'false')) {
                const literalValue = withoutReturn === 'true';
                handlers.push(() => literalValue);
                continue;
            }

            // Statements like `return checkMyPage(event)` fall through to the
            // normal matchers below using the return-stripped expression, so a
            // real function call is invoked and its actual return value (not a
            // literal-text guess) decides whether the default action is cancelled.
            const normalized = withoutReturn;

            const ifMatch = normalized.match(/^if\s*\((.+)\)\s*\(?\s*(.+)\s*\)?$/);
            if (ifMatch) {
                const condition = normalizePathLikePath(ifMatch[1]);
                const body = ifMatch[2];
                if (
                    condition === 'event.target===node' ||
                    condition === 'event.target==node' ||
                    condition === 'event.target===this' ||
                    condition === 'event.target==this'
                ) {
                    handlers.push((event, targetNode) => {
                        if (event.target !== targetNode) return;
                        const inner = compileLegacyHandler(targetNode, attrName, body);
                        if (!inner) return;
                        const result = inner(event, targetNode);
                        if (result === false) {
                            event.preventDefault();
                            event.stopPropagation();
                        }
                    });
                    continue;
                }
            }

            const locationMatch = normalized.match(/^((?:window\.)?location\.href)\s*=\s*(.+)$/);
            if (locationMatch) {
                const value = parseValue(locationMatch[2]);
                if (value === null) {
                    console.error(`security.js: unsupported inline handler in ${attrName}`, statement);
                    return null;
                }
                handlers.push((event) => {
                    const resolvedValue = resolveArgument(value, event, node);
                    window.location.href = String(resolvedValue);
                    event.preventDefault();
                    event.stopPropagation();
                });
                continue;
            }

            const replaceMatch = normalizePathLikePath(normalized).match(/^((?:this|node)\.value)=(?:this|node)\.value\.replace\((.+)\)$/);
            if (replaceMatch) {
                const args = topLevelSplit(replaceMatch[2], ',').map((part) => parseValue(part.trim()));
                if (args.length >= 1) {
                    handlers.push((event, targetNode) => {
                        if (typeof targetNode.value !== 'string') return;
                        const [patternExpr, replacementExpr] = args;
                        const pattern = resolveArgument(patternExpr, event, targetNode);
                        const replacement = resolveArgument(replacementExpr, event, targetNode) || '';
                        if (pattern instanceof RegExp) {
                            targetNode.value = targetNode.value.replace(pattern, replacement);
                        }
                    });
                    continue;
                }
            }

            const trailingCallMatch = normalized.match(/^((?:[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*))\s*\($/);
            if (trailingCallMatch) {
                const fnRef = describeFunctionRef(trailingCallMatch[1]);
                if (fnRef) {
                    handlers.push((event, targetNode) => {
                        return invokeFunctionRef(fnRef, event, targetNode, []);
                    });
                    continue;
                }
            }

            const callMatch = normalized.match(/^((?:[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*))\s*\((.*)\)$/);
            if (callMatch) {
                const fnRef = describeFunctionRef(callMatch[1]);
                if (!fnRef) {
                    console.error(`security.js: unsupported inline handler in ${attrName}`, statement);
                    return null;
                }
                const args = parseArguments(callMatch[2]);
                if (args.some((arg) => arg === null)) {
                    console.error(`security.js: unsupported inline handler argument in ${attrName}`, statement);
                    return null;
                }
                handlers.push((event, targetNode) => {
                    const finalArgs = args.map((arg) => resolveArgument(arg, event, targetNode));
                    return invokeFunctionRef(fnRef, event, targetNode, finalArgs);
                });
                continue;
            }

            const assignmentMatch = normalized.match(/^(?:node|this)\.([A-Za-z_$][\w$]*)\s*=\s*(.+)$/);
            if (assignmentMatch) {
                const prop = assignmentMatch[1];
                const value = parseValue(assignmentMatch[2]);
                if (value === null) {
                    console.error(`security.js: unsupported inline handler in ${attrName}`, statement);
                    return null;
                }
                handlers.push((_, targetNode) => {
                    targetNode[prop] = resolveArgument(value, null, targetNode);
                });
                continue;
            }

            console.error(`security.js: unsupported inline handler in ${attrName}`, statement);
            return null;
        }

        return function (event) {
            for (const handler of handlers) {
                const result = handler(event, node);
                if (result === false) {
                    event.preventDefault();
                    event.stopPropagation();
                    return;
                }
            }
        };
    }

    function bindLegacyEventListeners(root = document) {
        const selector = legacyEventAttributes.map((attribute) => `[${attribute}]`).join(',');
        const nodes = root.querySelectorAll(selector);

        for (const node of nodes) {
            for (const attribute of legacyEventAttributes) {
                const code = node.getAttribute(attribute);
                if (!code) continue;

                const eventType = normalizeEventAttribute(attribute);
            if (!isLocalHost) {
                const handler = compileLegacyHandler(node, attribute, code);
                if (!handler) {
                    // Keep unknown attributes as-is to avoid emptying the node's event wiring on partial parser misses.
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
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            document.querySelectorAll('form').forEach(addCsrfInput);
            if (!isLocalHost) {
                bindLegacyEventListeners();
            }
        }, { once: true });
    } else {
        document.querySelectorAll('form').forEach(addCsrfInput);
        if (!isLocalHost) {
            bindLegacyEventListeners();
        }
    }

    document.addEventListener('submit', function (event) {
        addCsrfInput(event.target);
    }, true);

    if ('MutationObserver' in window) {
        const observer = new MutationObserver((records) => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node && node.nodeType === Node.ELEMENT_NODE) {
                        if (!isLocalHost) {
                            bindLegacyEventListeners(node);
                        }
                    }
                }
            }
        });
        observer.observe(document.documentElement, {
            childList: true,
            subtree: true,
        });
    }

    return { bindLegacyEventListeners };
})();
