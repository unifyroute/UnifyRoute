import useSWR from 'swr'

const API_BASE = '/api'

export function getAuthToken(): string {
    const token = localStorage.getItem('admin_token') || ''
    // Migration guard: old app used 'logged_in'
    if (token === 'logged_in') {
        localStorage.removeItem('admin_token')
        return ''
    }
    return token
}

export function setAuthToken(token: string) {
    if (token) {
        localStorage.setItem('admin_token', token)
    } else {
        localStorage.removeItem('admin_token')
    }
}

export async function adminLogin(adminKey: string): Promise<string> {
    // Left for backwards compatibility if needed, but Login.tsx handles its own fetch
    setAuthToken(adminKey)
    return adminKey
}

export async function adminLogout() {
    setAuthToken('')
    await fetch(`${API_BASE}/admin/logout`, { method: 'POST', credentials: 'include' }).catch(() => { })
}


const getHeaders = () => {
    const token = getAuthToken()
    const headers: Record<string, string> = {}
    if (token) {
        headers['Authorization'] = `Bearer ${token}`
    }
    return headers
}

export const fetcher = async (url: string) => {
    const res = await fetch(`${API_BASE}${url}`, {
        headers: getHeaders(),
        credentials: 'include'
    })
    if (res.status === 401) {
        // Session expired or invalid — clear state and redirect
        setAuthToken('')
        window.location.href = '/login'
        throw new Error('Session expired. Please log in again.')
    }
    if (!res.ok) {
        const info = await res.json().catch(() => ({}))
        const errorMsg = info.detail || info.error?.message || 'An error occurred while fetching the data.'
        const error: any = new Error(errorMsg)
        error.info = info
        error.status = res.status
        throw error
    }
    return res.json()
}


// Global SWR settings for App.tsx (we will configure SWR locally in api.ts to apply globally, or just return fetcher)

async function postJSON(path: string, body: object) {
    const res = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getHeaders() },
        credentials: 'include',
        body: JSON.stringify(body)
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Request failed')
    }
    return res.json()
}

async function deleteRequest(path: string) {
    const res = await fetch(`${API_BASE}${path}`, {
        method: 'DELETE',
        headers: getHeaders(),
        credentials: 'include'
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Delete request failed')
    }
    return res.json().catch(() => null)
}

export function useProviders() {
    const { data, error, isLoading, mutate } = useSWR('/admin/providers', fetcher)
    return { providers: data, isLoading, isError: error, mutate }
}

export function useCredentials() {
    const { data, error, isLoading, mutate } = useSWR('/admin/credentials', fetcher)
    return { credentials: data, isLoading, isError: error, mutate }
}

export function useModels() {
    const { data, error, isLoading, mutate } = useSWR('/admin/models', fetcher)
    return { models: data, isLoading, isError: error, mutate }
}

export function useRoutingConfig() {
    const { data, error, isLoading, mutate } = useSWR('/admin/routing', fetcher)
    return { routingConfig: data?.yaml_content, isLoading, isError: error, mutate }
}

export function useGatewayKeys() {
    const { data, error, isLoading, mutate } = useSWR('/admin/keys', fetcher)
    return { keys: data, isLoading, isError: error, mutate }
}

export function useLogs(page = 1, limit = 20, provider?: string, status?: string, tier?: string, search?: string) {
    const url = new URL('/admin/logs', window.location.origin)
    url.searchParams.set('page', page.toString())
    url.searchParams.set('limit', limit.toString())
    if (provider && provider !== "all") url.searchParams.set('provider', provider)
    if (status && status !== "all") url.searchParams.set('status', status)
    if (tier && tier !== "all") url.searchParams.set('tier', tier)
    if (search) url.searchParams.set('search', search)

    const { data, error, isLoading, mutate } = useSWR(url.pathname + url.search, fetcher)
    return {
        logs: data?.items || [],
        total: data?.total || 0,
        isLoading,
        isError: error,
        mutate
    }
}

export function useLogStats(hours: number = 24) {
    const { data, error, isLoading, mutate } = useSWR(`/admin/logs/stats?hours=${hours}`, fetcher)
    return { stats: data, isLoading, isError: error, mutate }
}

export function useLogsTimeline(hours: number = 24) {
    const { data, error, isLoading, mutate } = useSWR(`/admin/logs/timeline?hours=${hours}`, fetcher)
    return { timeline: data || [], isLoading, isError: error, mutate }
}

export function useUsageStats(days: number = 30, provider?: string) {
    let url = `/admin/usage?days=${days}`
    if (provider) url += `&provider=${provider}`
    const { data, error, isLoading, mutate } = useSWR(url, fetcher)
    return {
        usage: data?.items || [],
        totalCost: data?.total_cost || 0,
        totalRequests: data?.total_requests || 0,
        isLoading,
        isError: error,
        mutate
    }
}

export function useUsageDetails(days: number = 30, provider?: string, credentialId?: string) {
    let url = `/admin/usage/details?days=${days}`
    if (provider) url += `&provider=${provider}`
    if (credentialId) url += `&credential_id=${credentialId}`
    const { data, error, isLoading, mutate } = useSWR(url, fetcher)
    return {
        details: data?.items || [],
        totalCost: data?.total_cost || 0,
        totalRequests: data?.total_requests || 0,
        isLoading: isLoading,
        isError: error,
        mutate
    }
}

export async function saveRoutingConfig(yaml_content: string) {
    return postJSON('/admin/routing', { yaml_content })
}

export async function createProvider(data: { name: string; display_name: string; auth_type: string; enabled: boolean }) {
    return postJSON('/admin/providers', data)
}

export async function updateProvider(id: string, data: { name?: string; display_name?: string; auth_type?: string; enabled?: boolean }) {
    const res = await fetch(`${API_BASE}/admin/providers/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...getHeaders() },
        credentials: 'include',
        body: JSON.stringify(data)
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Update failed')
    }
    return res.json()
}

export async function deleteProvider(id: string) {
    return deleteRequest(`/admin/providers/${id}`)
}

export async function updateModel(id: string, data: { tier?: string; cost_in_1m?: number; cost_out_1m?: number; enabled?: boolean }) {
    const res = await fetch(`${API_BASE}/admin/models/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...getHeaders() },
        credentials: 'include',
        body: JSON.stringify(data)
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Update model failed')
    }
    return res.json()
}

export async function deleteModel(id: string) {
    return deleteRequest(`/admin/models/${id}`)
}

export async function createCredential(data: { provider_id: string; label: string; auth_type: string; secret_key?: string; enabled: boolean }) {
    return postJSON('/admin/credentials', data)
}

export async function deleteCredential(id: string) {
    return deleteRequest(`/admin/credentials/${id}`)
}

export async function updateCredential(id: string, data: { label?: string; enabled?: boolean }) {
    const res = await fetch(`${API_BASE}/admin/credentials/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...getHeaders() },
        credentials: 'include',
        body: JSON.stringify(data)
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Update credential failed')
    }
    return res.json()
}

export async function verifyCredential(id: string) {
    return fetcher(`/admin/credentials/${id}/verify`)
}

export async function getCredentialQuota(id: string) {
    return fetcher(`/admin/credentials/${id}/quota`)
}

/**
 * Kicks off the server-side OAuth2 flow for a given provider.
 * The backend should respond with an oauth_url to open in the browser.
 */
export async function startOAuthFlow(provider_id: string): Promise<string> {
    const res = await fetch(`${API_BASE}/oauth/start/${provider_id}`, {
        headers: getHeaders(),
        credentials: 'include'
    })
    if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `OAuth start failed (HTTP ${res.status})`)
    }
    const json = await res.json()
    return json.oauth_url as string
}

/**
 * Start the Google Antigravity OAuth flow.
 * Uses the same hardcoded credentials as gemini-cli — no user configuration needed.
 * Returns the Google accounts.google.com URL to open in the browser.
 */
export async function startAntigravityOAuth(): Promise<string> {
    const res = await fetch(`${API_BASE}/oauth/google-antigravity/start`, {
        headers: getHeaders(),
        credentials: 'include'
    })
    if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Failed to start Google Antigravity OAuth (HTTP ${res.status})`)
    }
    const json = await res.json()
    return json.oauth_url as string
}

/** Fetch the full UnifyRouter-aligned provider catalog. */
export async function getProviderSeeds(): Promise<any[]> {
    return fetcher('/admin/providers/seeds')
}

/** Seed the DB with selected providers (or full catalog if none provided). */
export async function seedProviders(providers?: string[]): Promise<{ inserted: string[]; skipped: number }> {
    const res = await fetch(`${API_BASE}/admin/providers/seed`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getHeaders() },
        credentials: 'include',
        body: JSON.stringify({ providers: providers || null })
    })
    if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Seed failed (HTTP ${res.status})`)
    }
    return res.json()
}

export async function createGatewayKey(data: { label: string; scopes?: string[] }) {
    return postJSON('/admin/keys', data)
}

export async function deleteGatewayKey(id: string) {
    return deleteRequest(`/admin/keys/${id}`)
}

export async function syncProviderModels(providerId: string) {
    return postJSON(`/admin/providers/${providerId}/sync-models`, {})
}

export async function updateGatewayKey(id: string, data: { label?: string }) {
    const res = await fetch(`${API_BASE}/admin/keys/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...getHeaders() },
        credentials: 'include',
        body: JSON.stringify(data)
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Update failed')
    }
    return res.json()
}

export async function revealGatewayKey(id: string, password?: string) {
    return postJSON(`/admin/keys/${id}/reveal`, { password })
}

// ── Chat Playground helpers ─────────────────────────────────────────────

export interface ChatMessage {
    role: 'user' | 'assistant' | 'system'
    content: string
}

export interface ChatCompletionResponse {
    id: string
    choices: Array<{
        message: { role: string; content: string }
        finish_reason: string
    }>
    usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number }
    model?: string
    _unifyroute?: { provider: string; latency_ms: number }
}

/** Non-streaming chat completion. */
export async function sendChatMessage(
    model: string,
    messages: ChatMessage[],
    options?: { temperature?: number; max_tokens?: number }
): Promise<ChatCompletionResponse> {
    const res = await fetch(`${API_BASE}/v1/chat/completions`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(typeof getHeaders !== 'undefined' ? getHeaders() : {})
        },
        credentials: 'include',
        body: JSON.stringify({
            model,
            messages,
            stream: false,
            ...options,
        }),
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        const msg = err?.error?.message || err?.detail || `Chat request failed (${res.status})`
        throw new Error(msg)
    }
    return res.json()
}

/**
 * Streaming chat completion via SSE.
 * Yields content delta strings as they arrive.
 * Returns a cleanup function to abort the stream.
 */
export function sendChatMessageStream(
    model: string,
    messages: ChatMessage[],
    onDelta: (text: string) => void,
    onDone: (info: { model?: string; provider?: string }) => void,
    onError: (err: Error) => void,
    options?: { temperature?: number; max_tokens?: number }
): () => void {
    const controller = new AbortController()

        ; (async () => {
            try {
                const res = await fetch(`${API_BASE}/v1/chat/completions`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        ...(typeof getHeaders !== 'undefined' ? getHeaders() : {})
                    },
                    credentials: 'include',
                    body: JSON.stringify({
                        model,
                        messages,
                        stream: true,
                        ...options,
                    }),
                    signal: controller.signal,
                })

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}))
                    const msg = err?.error?.message || err?.detail || `Chat stream failed (${res.status})`
                    throw new Error(msg)
                }

                const reader = res.body?.getReader()
                if (!reader) throw new Error('No readable stream')

                const decoder = new TextDecoder()
                let buffer = ''
                let modelName: string | undefined
                let providerName: string | undefined

                while (true) {
                    const { done, value } = await reader.read()
                    if (done) break

                    buffer += decoder.decode(value, { stream: true })
                    const lines = buffer.split('\n')
                    buffer = lines.pop() || ''

                    for (const line of lines) {
                        if (!line.startsWith('data: ')) continue
                        const data = line.slice(6).trim()
                        if (data === '[DONE]') {
                            onDone({ model: modelName, provider: providerName })
                            return
                        }
                        let chunk;
                        try {
                            chunk = JSON.parse(data)
                        } catch {
                            continue // skip malformed chunk strings
                        }

                        if (chunk.error) {
                            throw new Error(chunk.error)
                        }
                        if (chunk.model) modelName = chunk.model
                        if (chunk._unifyroute?.provider) providerName = chunk._unifyroute.provider
                        const delta = chunk.choices?.[0]?.delta?.content
                        if (delta) onDelta(delta)
                    }
                }
                onDone({ model: modelName, provider: providerName })
            } catch (err: any) {
                if (err.name !== 'AbortError') onError(err)
            }
        })()

    return () => controller.abort()
}

// ── Brain Module API ──────────────────────────────────────────────────────────

export interface BrainHealth {
    ok: boolean | null
    latency_ms: number | null
    message: string
    tested_at: number | null
}

export interface BrainProvider {
    id: string
    provider: string
    provider_display: string
    credential_label: string
    credential_id: string
    model_id: string
    priority: number
    enabled: boolean
    health: BrainHealth
}

export interface BrainRankedEntry {
    rank: number
    brain_config_id: string
    provider: string
    credential_label: string
    model_id: string
    priority: number
    score: number
    health_ok: boolean
    health_message: string
    latency_ms: number
    quota_remaining: number
}

export interface BrainSelection {
    ok: boolean
    provider: string | null
    credential_id: string | null
    credential_label: string
    model_id: string | null
    score: number
    reason: string
}

export interface BrainTestResult {
    brain_config_id: string
    provider: string
    credential_label: string
    model_id: string
    ok: boolean
    message: string
    latency_ms: number
}

export function useBrainStatus() {
    const { data, error, isLoading, mutate } = useSWR('/admin/brain/status', fetcher)
    return {
        providers: (data?.brain_providers ?? []) as BrainProvider[],
        total: data?.total ?? 0,
        isLoading,
        isError: error,
        mutate,
    }
}

export function useBrainRanking() {
    const { data, error, isLoading, mutate } = useSWR('/admin/brain/ranking', fetcher)
    return {
        ranking: (data?.ranking ?? []) as BrainRankedEntry[],
        isLoading,
        isError: error,
        mutate,
    }
}

export async function brainAssignProvider(data: {
    provider_id: string
    credential_id: string
    model_id: string
    priority: number
}) {
    return postJSON('/admin/brain/providers', data)
}

export async function updateBrainProvider(entryId: string, data: { priority?: number; enabled?: boolean }) {
    const res = await fetch(`${API_BASE}/admin/brain/providers/${entryId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...(typeof getHeaders !== 'undefined' ? getHeaders() : {}) },
        credentials: 'include',
        body: JSON.stringify(data)
    })
    if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Update brain provider failed')
    }
    return res.json()
}

export async function brainRemoveProvider(entryId: string) {
    return deleteRequest(`/admin/brain/providers/${entryId}`)
}

export async function brainImport(format: 'yaml' | 'json', content: string) {
    return postJSON('/admin/brain/import', { format, content })
}

export async function brainRunTests(): Promise<{ tested: number; healthy: number; failed: number; results: BrainTestResult[] }> {
    return postJSON('/admin/brain/test', {})
}

export async function brainSelect(): Promise<BrainSelection> {
    return postJSON('/admin/brain/select', {})
}
