#!/usr/bin/env python3
"""
Patch Hermes Agent's ModelPickerDialog.tsx to add API key entry UI for
unauthenticated providers, so users can activate them from the dashboard
instead of editing .env manually.

NOTE: The backend half of this feature (web_server.py returning
include_unconfigured=True) was merged upstream and is no longer patched here.
Only the frontend activation UI remains.

Uses string replacement instead of unified diff patches for robustness
against upstream whitespace changes.
"""

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_AGENT_ROOT", "/opt/hermes-agent"))

# ── Patch: ModelPickerDialog.tsx ───────────────────────────────────────────

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

# 2e. Add submitApiKey and handleProviderSelect before the return statement.
# Upstream now has a `confirm()` function + portal comment block before `return (`,
# so we anchor on the portal comment which is stable.
old_pre_return = """\
  const confirm = () => {
    if (!canConfirm) return;
    void applySelection();
  };

  // Portal to document.body: the main dashboard column in App.tsx is"""

new_pre_return = """\
  const confirm = () => {
    if (!canConfirm) return;
    void applySelection();
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

  // Portal to document.body: the main dashboard column in App.tsx is"""

if old_pre_return not in tsx_text:
    print("ERROR: Could not find confirm() + portal comment block in ModelPickerDialog.tsx")
    print("The upstream code may have changed. Manual patching required.")
    sys.exit(1)

tsx_text = tsx_text.replace(old_pre_return, new_pre_return)

# 2f. Change ProviderColumn props in JSX.
# Upstream renamed `providers` -> `filteredProviders` and `needle` -> `trimmedQuery`.
tsx_text = tsx_text.replace(
    """\
            total={providers.length}
            selectedSlug={selectedSlug}
            query={trimmedQuery}
            onSelect={(slug) => {
              setSelectedSlug(slug);
              setSelectedModel("");
            }}""",
    """\
            total={providers.length}
            selectedSlug={selectedSlug}
            query={trimmedQuery}
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
# Use a regex-based replacement for flexibility against upstream whitespace/class changes.
# Upstream now uses `text-text-secondary` instead of `text-muted-foreground/80`.
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
    # Match either the old or new secondary text class
    r'<div className="text-\[0\.65rem\] (?:text-muted-foreground/80|text-text-secondary) font-mono truncate">\s*'
    r'\{p\.slug\} · \{p\.total_models \?\? p\.models\?\.length \?\? 0\} models\}\s*'
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