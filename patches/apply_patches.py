#!/usr/bin/env python3
"""
Patch Hermes Agent to show all providers (not just authenticated ones)
in the web UI model picker, and allow API key entry for unauthenticated providers.

Uses string replacement instead of unified diff patches for robustness
against upstream whitespace changes.
"""

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_AGENT_ROOT", "/opt/hermes-agent"))

# ── Patch 1: web_server.py — return all canonical providers ──────────────────

ws = ROOT / "hermes_cli" / "web_server.py"
ws_text = ws.read_text(encoding="utf-8")

# Replace the get_model_options function body.
# Find: "providers = list_authenticated_providers(" through the return dict.
old_ws_block = """\
        providers = list_authenticated_providers(
            current_provider=current_provider,
            current_base_url=current_base_url,
            current_model=current_model,
            user_providers=user_providers,
            custom_providers=custom_providers,
            max_models=50,
        )
        return {
            "providers": providers,
            "model": current_model,
            "provider": current_provider,
        }"""

new_ws_block = """\
        authenticated = list_authenticated_providers(
            current_provider=current_provider,
            current_base_url=current_base_url,
            current_model=current_model,
            user_providers=user_providers,
            custom_providers=custom_providers,
            max_models=50,
        )

        # Mark authenticated providers and build lookup by slug
        from hermes_cli.models import CANONICAL_PROVIDERS, _PROVIDER_LABELS
        from hermes_cli.auth import PROVIDER_REGISTRY as _auth_reg

        authed_map: dict = {}
        authed_extra: list = []
        canonical_slugs = {e.slug for e in CANONICAL_PROVIDERS}
        for p in authenticated:
            p["authenticated"] = True
            authed_map[p["slug"]] = p
            if p["slug"] not in canonical_slugs:
                authed_extra.append(p)

        # Build final list in CANONICAL_PROVIDERS order, merging auth data
        ordered: list = []
        for entry in CANONICAL_PROVIDERS:
            if entry.slug in authed_map:
                ordered.append(authed_map[entry.slug])
            else:
                pconfig = _auth_reg.get(entry.slug)
                auth_type = pconfig.auth_type if pconfig else "api_key"
                key_env = (
                    pconfig.api_key_env_vars[0]
                    if (pconfig and pconfig.api_key_env_vars)
                    else ""
                )
                if auth_type == "api_key" and key_env:
                    warning = f"paste {key_env} to activate"
                else:
                    warning = f"run `hermes model` to configure ({auth_type})"
                ordered.append(
                    {
                        "slug": entry.slug,
                        "name": _PROVIDER_LABELS.get(entry.slug, entry.label),
                        "is_current": entry.slug == current_provider,
                        "is_user_defined": False,
                        "models": [],
                        "total_models": 0,
                        "source": "built-in",
                        "authenticated": False,
                        "auth_type": auth_type,
                        "key_env": key_env,
                        "warning": warning,
                    }
                )

        # Append user-defined/custom providers not in canonical list
        ordered.extend(authed_extra)

        return {
            "providers": ordered,
            "model": current_model,
            "provider": current_provider,
        }"""

if old_ws_block not in ws_text:
    print("ERROR: Could not find target block in web_server.py")
    print("The upstream code may have changed. Manual patching required.")
    sys.exit(1)

ws_text = ws_text.replace(old_ws_block, new_ws_block)

# Update docstring
ws_text = ws_text.replace(
    '"""Return authenticated providers + their curated model lists.\n\n    '
    'REST equivalent of the ``model.options`` JSON-RPC on tui_gateway, so the\n    '
    'dashboard Models page can render the picker without a live chat session.\n    '
    'The response shape matches ``model.options`` 1:1 so ``ModelPickerDialog``\n    '
    'can share the same types.\n    """',
    '"""Return all providers (authenticated + unauthenticated) with auth metadata.\n\n    '
    'REST equivalent of the ``model.options`` JSON-RPC on tui_gateway.\n    '
    'Includes unauthenticated canonical providers so the dashboard Models page\n    '
    'can render the full picker and prompt for API keys.\n    """',
)

ws.write_text(ws_text, encoding="utf-8")
print("OK Patched web_server.py")

# ── Patch 2: ModelPickerDialog.tsx ───────────────────────────────────────────

tsx = ROOT / "web" / "src" / "components" / "ModelPickerDialog.tsx"
tsx_text = tsx.read_text(encoding="utf-8")

# 2a. Add useCallback to imports
tsx_text = tsx_text.replace(
    'import { useEffect, useMemo, useRef, useState } from "react";',
    'import { useCallback, useEffect, useMemo, useRef, useState } from "react";',
)

# 2b. Add new interface fields
tsx_text = tsx_text.replace(
    "  warning?: string;\n}\n\ninterface ModelOptionsResponse {",
    "  warning?: string;\n  authenticated?: boolean;\n  auth_type?: string;\n  key_env?: string;\n}\n\ninterface ModelOptionsResponse {",
)

# 2c. Add state variables after 'const closedRef = useRef(false);'
tsx_text = tsx_text.replace(
    "  const closedRef = useRef(false);\n",
    "  const closedRef = useRef(false);\n\n"
    "  const [activatingSlug, setActivatingSlug] = useState<string | null>(null);\n"
    "  const [keyInput, setKeyInput] = useState(\"\");\n"
    "  const [savingKey, setSavingKey] = useState(false);\n"
    "  const [keyError, setKeyError] = useState<string | null>(null);\n\n",
)

# 2d. Replace the useEffect that loads providers with useCallback + useEffect
old_effect = """\
  // Load providers + models on open.
  useEffect(() => {
    closedRef.current = false;

    const promise = standalone
      ? (loader as () => Promise<ModelOptionsResponse>)()
      : (gw as GatewayClient).request<ModelOptionsResponse>(
          "model.options",
          sessionId ? { session_id: sessionId } : {},
        );

    promise
      .then((r) => {
        if (closedRef.current) return;
        const next = r?.providers ?? [];
        setProviders(next);
        setCurrentModel(String(r?.model ?? ""));
        setCurrentProviderSlug(String(r?.provider ?? ""));
        setSelectedSlug(
          (next.find((p) => p.is_current) ?? next[0])?.slug ?? "",
        );
        setSelectedModel("");
        setLoading(false);
      })
      .catch((e) => {
        if (closedRef.current) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });

    return () => {
      closedRef.current = true;
    };
    // Deliberately omit props from deps — stable for the dialog's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);"""

new_code = """\
  const loadProviders = useCallback(() => {
    closedRef.current = false;
    setLoading(true);
    setError(null);

    const promise = standalone
      ? (loader as () => Promise<ModelOptionsResponse>)()
      : (gw as GatewayClient).request<ModelOptionsResponse>(
          "model.options",
          sessionId ? { session_id: sessionId } : {},
        );

    promise
      .then((r) => {
        if (closedRef.current) return;
        const next = r?.providers ?? [];
        setProviders(next);
        setCurrentModel(String(r?.model ?? ""));
        setCurrentProviderSlug(String(r?.provider ?? ""));
        setSelectedSlug(
          (next.find((p) => p.is_current) ?? next[0])?.slug ?? "",
        );
        setSelectedModel("");
        setLoading(false);
      })
      .catch((e) => {
        if (closedRef.current) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });

    return () => {
      closedRef.current = true;
    };
  }, [standalone, loader, gw, sessionId]);

  // Load providers + models on open.
  useEffect(() => {
    const cleanup = loadProviders();
    return cleanup;
    // Deliberately omit props from deps — stable for the dialog's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);"""

if old_effect not in tsx_text:
    print("ERROR: Could not find provider loading useEffect in ModelPickerDialog.tsx")
    sys.exit(1)

tsx_text = tsx_text.replace(old_effect, new_code)

# 2e. Add submitApiKey and handleProviderSelect before the return statement
tsx_text = tsx_text.replace(
    "    }\n  };\n\n  return (",
    """\
    }
  };

  const submitApiKey = async (provider: ModelOptionProvider) => {
    if (!provider.key_env || !keyInput.trim()) return;
    setSavingKey(true);
    setKeyError(null);
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const token = (window as any).__HERMES_SESSION_TOKEN__;
      if (token) headers["X-Hermes-Session-Token"] = token;
      const base = (window as any).__HERMES_BASE_PATH__ || "";
      const res = await fetch(`${base}/api/env`, {
        method: "PUT",
        headers,
        body: JSON.stringify({ key: provider.key_env, value: keyInput.trim() }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      setActivatingSlug(null);
      setKeyInput("");
      loadProviders();
    } catch (e) {
      setKeyError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingKey(false);
    }
  };

  const handleProviderSelect = (slug: string) => {
    const provider = providers.find((p) => p.slug === slug);
    if (
      provider &&
      provider.authenticated === false &&
      provider.auth_type === "api_key" &&
      provider.key_env
    ) {
      setActivatingSlug(slug);
      setKeyInput("");
      setKeyError(null);
      return;
    }
    setSelectedSlug(slug);
    setSelectedModel("");
  };

  return (""",
)

# 2f. Change ProviderColumn props in JSX
tsx_text = tsx_text.replace(
    """\
            total={providers.length}
            selectedSlug={selectedSlug}
            query={needle}
            onSelect={(slug) => {
              setSelectedSlug(slug);
              setSelectedModel("");
            }}""",
    """\
            total={providers.length}
            selectedSlug={selectedSlug}
            query={needle}
            activatingSlug={activatingSlug}
            onSelect={handleProviderSelect}
            onActivateSubmit={submitApiKey}
            onActivateCancel={() => {
              setActivatingSlug(null);
              setKeyInput("");
              setKeyError(null);
            }}
            keyInput={keyInput}
            onKeyInputChange={setKeyInput}
            savingKey={savingKey}
            keyError={keyError}""",
)

# 2g. Update ProviderColumn function signature
tsx_text = tsx_text.replace(
    """\
function ProviderColumn({
  loading,
  error,
  providers,
  total,
  selectedSlug,
  query,
  onSelect,
}: {
  loading: boolean;
  error: string | null;
  providers: ModelOptionProvider[];
  total: number;
  selectedSlug: string;
  query: string;
  onSelect(slug: string): void;
}) {""",
    """\
function ProviderColumn({
  loading,
  error,
  providers,
  total,
  selectedSlug,
  query,
  activatingSlug,
  onSelect,
  onActivateSubmit,
  onActivateCancel,
  keyInput,
  onKeyInputChange,
  savingKey,
  keyError,
}: {
  loading: boolean;
  error: string | null;
  providers: ModelOptionProvider[];
  total: number;
  selectedSlug: string;
  query: string;
  activatingSlug: string | null;
  onSelect(slug: string): void;
  onActivateSubmit(provider: ModelOptionProvider): void;
  onActivateCancel(): void;
  keyInput: string;
  onKeyInputChange(value: string): void;
  savingKey: boolean;
  keyError: string | null;
}) {""",
)

# 2h. Change "no authenticated providers" to "no providers"
tsx_text = tsx_text.replace(
    '"no authenticated providers"',
    '"no providers"',
)

# 2i. Add activation UI and unauthenticated styling in the provider list
# Use a regex-based replacement for the providers.map block since
# the exact indentation can vary.
old_map_pattern = (
    r'\{providers\.map\(\(p\) => \{\s*'
    r'const active = p\.slug === selectedSlug;\s*'
    r'return \(\s*'
    r'<ListItem\s*'
    r'key=\{p\.slug\}\s*'
    r'active=\{active\}\s*'
    r'onClick=\{\(\) => onSelect\(p\.slug\)\}\s*'
    r'className=\{\`items-start text-xs border-l-2 \$\{\s*'
    r'active \? "border-l-primary" : "border-l-transparent"\s*'
    r'\}\`\}\s*'
    r'>\s*'
    r'<div className="flex-1 min-w-0">\s*'
    r'<div className="flex items-center gap-1\.5">\s*'
    r'<span className="font-medium truncate">\{p\.name\}</span>\s*'
    r'\{p\.is_current && <CurrentTag />\}\s*'
    r'</div>\s*'
    r'<div className="text-\[0\.65rem\] text-muted-foreground/80 font-mono truncate">\s*'
    r'\{p\.slug\} · \{p\.total_models \?\? p\.models\?\.length \?\? 0\} models\}\s*'  # approximate
    r'</div>\s*'
    r'</div>\s*'
    r'</ListItem>\s*'
    r'\);\s*'
    r'\}\)\}'
)

new_map_block = """\
{providers.map((p) => {
          const active = p.slug === selectedSlug;
          const isActivating = activatingSlug === p.slug;
          const unauthenticated = p.authenticated === false;

          if (isActivating) {
            return (
              <div key={p.slug} className="p-2 border-b border-border bg-muted/30">
                <div className="text-xs font-medium mb-1 truncate">{p.name}</div>
                <div className="text-[0.65rem] text-muted-foreground mb-2">
                  {p.warning}
                </div>
                <Input
                  type="password"
                  autoFocus
                  placeholder={`${p.key_env}…`}
                  value={keyInput}
                  onChange={(e) => onKeyInputChange(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      onActivateSubmit(p);
                    }
                    if (e.key === "Escape") {
                      onActivateCancel();
                    }
                  }}
                  className="h-7 text-xs mb-1"
                />
                {keyError && (
                  <div className="text-[0.65rem] text-destructive mb-1">{keyError}</div>
                )}
                <div className="flex gap-1">
                  <Button
                    size="xs"
                    onClick={() => onActivateSubmit(p)}
                    disabled={savingKey || !keyInput.trim()}
                    className="h-6 text-[0.65rem] px-2"
                  >
                    {savingKey ? <Spinner className="text-xs" /> : "Save"}
                  </Button>
                  <Button
                    ghost
                    size="xs"
                    onClick={onActivateCancel}
                    disabled={savingKey}
                    className="h-6 text-[0.65rem] px-2"
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            );
          }

          return (
            <ListItem
              key={p.slug}
              active={active}
              onClick={() => onSelect(p.slug)}
              className={`items-start text-xs border-l-2 ${
                active ? "border-l-primary" : "border-l-transparent"
              } ${unauthenticated ? "opacity-60" : ""}`}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="font-medium truncate">{p.name}</span>
                  {p.is_current && <CurrentTag />}
                </div>
                <div className="text-[0.65rem] text-muted-foreground/80 font-mono truncate">
                  {p.slug} · {p.total_models ?? p.models?.length ?? 0} models
                </div>
                {unauthenticated && p.warning && (
                  <div className="text-[0.65rem] text-muted-foreground/60 truncate">
                    {p.warning}
                  </div>
                )}
              </div>
            </ListItem>
          );
        })}"""

# Try regex first for flexibility
import re
match = re.search(old_map_pattern, tsx_text, re.DOTALL)
if match:
    tsx_text = tsx_text[:match.start()] + new_map_block + tsx_text[match.end():]
else:
    # Fallback: exact string match with flexible whitespace
    # Read the file line by line and find the block
    lines = tsx_text.split('\n')
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if '{providers.map((p) =>' in line and 'selectedSlug' in lines[i+1] if i+1 < len(lines) else False:
            start_idx = i
        if start_idx is not None and '</ListItem>' in line:
            # Look for the closing )};
            for j in range(i, min(i+5, len(lines))):
                if ')}' in lines[j]:
                    end_idx = j
                    break
            if end_idx:
                break
    if start_idx is not None and end_idx is not None:
        old_block_text = '\n'.join(lines[start_idx:end_idx+1])
        tsx_text = tsx_text.replace(old_block_text, new_map_block)
    else:
        print("ERROR: Could not find providers.map block in ModelPickerDialog.tsx")
        print("The upstream code may have changed. Manual patching required.")
        sys.exit(1)

tsx.write_text(tsx_text, encoding="utf-8")
print("OK Patched ModelPickerDialog.tsx")

print("\nAll patches applied successfully!")