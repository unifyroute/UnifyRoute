import { useState, useEffect } from "react"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter
} from "@/components/ui/dialog"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { Plus, ExternalLink, Trash2, ChevronRight, Activity, ShieldCheck, RefreshCw, AlertCircle, CheckCircle2 } from "lucide-react"
import {
    useCredentials, useProviders,
    createCredential, deleteCredential, updateCredential,
    startOAuthFlow, startAntigravityOAuth,
    verifyCredential, getCredentialQuota, syncProviderModels
} from "@/lib/api"
import { ErrorState } from "@/components/error-state"

// ── helpers ────────────────────────────────────────────────────────────────

function providerIcon(name: string) {
    if (name === "google-antigravity" || name?.includes("google")) {
        return (
            <svg className="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none">
                <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
                <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
            </svg>
        )
    }
    return null
}

/** Pull account info out of oauth_meta for display */
function oauthAccount(cred: any): string | null {
    const m = cred.oauth_meta
    if (!m) return null
    const ui = m._userinfo
    if (ui?.email) return ui.email
    if (ui?.name) return ui.name
    // fallback: try top-level fields some providers return
    if (m.email) return m.email
    return null
}

// ── component ──────────────────────────────────────────────────────────────

type Step = "pick-provider" | "api-key" | "oauth-start" | "oauth-waiting" | null

export function Credentials() {
    const { credentials, isLoading, isError, mutate } = useCredentials()
    const { providers } = useProviders()

    const [open, setOpen] = useState(false)
    const [step, setStep] = useState<Step>(null)
    const [selectedProviderId, setSelectedProviderId] = useState("")
    const [apiKey, setApiKey] = useState("")
    const [apiKeyLabel, setApiKeyLabel] = useState("")
    const [oauthUrl, setOauthUrl] = useState<string | null>(null)
    const [saving, setSaving] = useState(false)
    const [error, setError] = useState<string | null>(null)

    const [actionId, setActionId] = useState<string | null>(null)
    const [actionMsg, setActionMsg] = useState<string | null>(null)
    const [actionError, setActionError] = useState<boolean>(false)

    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
    const [bulkActionMsg, setBulkActionMsg] = useState<string | null>(null)
    const [bulkActionError, setBulkActionError] = useState<boolean>(false)

    function toggleSelectAll() {
        if (!credentials) return
        if (selectedIds.size === credentials.length) {
            setSelectedIds(new Set())
        } else {
            setSelectedIds(new Set(credentials.map((c: any) => c.id)))
        }
    }

    function toggleSelect(id: string) {
        const newSet = new Set(selectedIds)
        if (newSet.has(id)) newSet.delete(id)
        else newSet.add(id)
        setSelectedIds(newSet)
    }

    async function handleBulkStatus(enable: boolean) {
        setBulkActionMsg(enable ? "Enabling..." : "Disabling...")
        setBulkActionError(false)
        try {
            await Promise.all(
                Array.from(selectedIds).map(id => updateCredential(id, { enabled: enable }))
            )
            await mutate()
            setBulkActionMsg(`Successfully ${enable ? 'enabled' : 'disabled'} selected credentials.`)
            setSelectedIds(new Set())
            setTimeout(() => setBulkActionMsg(null), 3000)
        } catch (err: any) {
            setBulkActionError(true)
            setBulkActionMsg(err.message || "Bulk action failed")
        }
    }

    async function handleBulkDelete() {
        if (!confirm("Are you sure you want to delete the selected credentials?")) return
        setBulkActionMsg("Deleting...")
        setBulkActionError(false)
        try {
            await Promise.all(
                Array.from(selectedIds).map(id => deleteCredential(id))
            )
            await mutate()
            setBulkActionMsg("Successfully deleted selected credentials.")
            setSelectedIds(new Set())
            setTimeout(() => setBulkActionMsg(null), 3000)
        } catch (err: any) {
            setBulkActionError(true)
            setBulkActionMsg(err.message || "Bulk delete failed")
        }
    }

    async function handleBulkVerify() {
        setBulkActionMsg("Verifying keys...")
        setBulkActionError(false)
        try {
            await Promise.all(
                Array.from(selectedIds).map(id => verifyCredential(id))
            )
            await mutate()
            setBulkActionMsg("Successfully verified selected credentials.")
            setSelectedIds(new Set())
            setTimeout(() => setBulkActionMsg(null), 3000)
        } catch (err: any) {
            setBulkActionError(true)
            setBulkActionMsg(err.message || "Bulk verification failed")
        }
    }

    // ── useEffect for oauth popup auto-close ──
    useEffect(() => {
        if (step !== "oauth-waiting") return
        function handleMessage(e: MessageEvent) {
            if (e.data === "oauth_success") {
                mutate()
                setOpen(false)
                setStep(null)
            }
        }
        window.addEventListener("message", handleMessage)
        return () => window.removeEventListener("message", handleMessage)
    }, [step, mutate])

    async function handleVerify(id: string) {
        setActionId(id)
        setActionError(false)
        setActionMsg("Verifying...")
        try {
            const res = await verifyCredential(id)
            if (res.status === 'error') {
                setActionError(true)
                setActionMsg(res.message)
            } else {
                setActionMsg(res.message)
            }
            await mutate() // refresh to show updated status pill
        } catch (err: any) {
            setActionError(true)
            setActionMsg(err.message || "Failed to verify")
        }
    }

    async function handleCheckQuota(id: string) {
        setActionId(id)
        setActionError(false)
        setActionMsg("Checking quota...")
        try {
            const res = await getCredentialQuota(id)
            // If no data has been polled yet, the API returns message + null fields
            if (res.tokens_remaining == null && res.requests_remaining == null) {
                setActionMsg(res.message || "No quota data yet — quota is polled automatically.")
            } else {
                const tokens = res.tokens_remaining != null ? res.tokens_remaining.toLocaleString() : "N/A"
                const reqs = res.requests_remaining != null ? res.requests_remaining.toLocaleString() : "N/A"
                setActionMsg(`Tokens: ${tokens} | Req: ${reqs}`)
            }
        } catch (err: any) {
            setActionError(true)
            setActionMsg(err.message || "Failed to get quota")
        }
    }

    async function handleSyncModels(providerId: string, credId: string) {
        setActionId(credId)
        setActionError(false)
        setActionMsg("Syncing models...")
        try {
            const res = await syncProviderModels(providerId)
            setActionMsg(`Synced ${res.total} models (${res.inserted} new)`)
        } catch (err: any) {
            setActionError(true)
            setActionMsg(err.message || "Failed to sync models")
        }
    }

    function openDialog() {
        setStep("pick-provider")
        setSelectedProviderId("")
        setApiKey("")
        setApiKeyLabel("")
        setOauthUrl(null)
        setError(null)
        setOpen(true)
    }

    function closeDialog() {
        setOpen(false)
        setStep(null)
    }

    const allProviders: any[] = providers ?? []
    const selectedProvider = allProviders.find(p => p.id === selectedProviderId)

    async function handleProviderChosen() {
        if (!selectedProvider) return
        const { auth_type, name } = selectedProvider

        if (auth_type === "api_key") {
            setApiKeyLabel(selectedProvider.display_name + " key")
            setStep("api-key")
            return
        }

        // OAuth — for google-antigravity use zero-config flow
        setStep("oauth-start")
        setSaving(true)
        setError(null)
        try {
            const url = name === "google-antigravity"
                ? await startAntigravityOAuth()
                : await startOAuthFlow(selectedProviderId)
            setOauthUrl(url)
            setStep("oauth-waiting")
        } catch (err: any) {
            setError(err.message || "Failed to start OAuth flow")
            setStep("pick-provider")
        } finally {
            setSaving(false)
        }
    }

    async function handleSaveApiKey(e: React.FormEvent) {
        e.preventDefault()
        if (!apiKey || !apiKeyLabel) return
        setSaving(true)
        setError(null)
        try {
            await createCredential({
                provider_id: selectedProviderId,
                label: apiKeyLabel,
                auth_type: "api_key",
                secret_key: apiKey,
                enabled: true,
            })
            await mutate()
            closeDialog()
        } catch (err: any) {
            setError(err.message || "Failed to save API key")
        } finally {
            setSaving(false)
        }
    }

    const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
    const [deleteError, setDeleteError] = useState<string | null>(null)

    async function handleDelete(id: string) {
        setDeleteError(null)
        setPendingDeleteId(id)
    }

    async function confirmDelete(id: string) {
        try {
            await deleteCredential(id)
            await mutate()
            setPendingDeleteId(null)
        } catch (err: any) {
            setDeleteError(err.message || "Delete failed")
            setPendingDeleteId(null)
        }
    }

    if (isLoading) return <div className="p-8">Loading credentials...</div>
    if (isError) return <ErrorState />

    return (
        <div className="p-8 space-y-8">
            {/* ── header ── */}
            <div className="flex justify-between items-center">
                <div>
                    <h2 className="text-3xl font-bold tracking-tight">Credentials</h2>
                    <p className="text-muted-foreground pt-1">API keys and OAuth logins for your providers.</p>
                </div>
                <Button onClick={openDialog}>
                    <Plus className="mr-2 h-4 w-4" /> Add Credential
                </Button>
            </div>
            {deleteError && (
                <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-2 text-sm text-destructive">
                    Delete failed: {deleteError}
                </div>
            )}

            {/* ── bulk actions ── */}
            {selectedIds.size > 0 && (
                <div className="flex items-center gap-4 bg-muted/40 p-3 rounded-md border">
                    <span className="text-sm font-medium">{selectedIds.size} selected</span>
                    <div className="flex items-center gap-2 ml-auto">
                        {bulkActionMsg && (
                            <span className={`text-sm mr-4 ${bulkActionError ? 'text-destructive' : 'text-green-600'}`}>
                                {bulkActionMsg}
                            </span>
                        )}
                        <Button size="sm" variant="outline" onClick={handleBulkVerify}>
                            Verify Keys
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => handleBulkStatus(true)}>
                            Enable Selected
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => handleBulkStatus(false)}>
                            Disable Selected
                        </Button>
                        <Button size="sm" variant="destructive" onClick={handleBulkDelete}>
                            Delete Selected
                        </Button>
                    </div>
                </div>
            )}

            {/* ── table ── */}
            <div className="rounded-md border">
                <Table>
                    <TableHeader>
                        <TableRow>
                            <TableHead className="w-[40px] text-center">
                                <input
                                    type="checkbox"
                                    className="cursor-pointer"
                                    checked={credentials?.length > 0 && selectedIds.size === credentials.length}
                                    onChange={toggleSelectAll}
                                />
                            </TableHead>
                            <TableHead>Provider</TableHead>
                            <TableHead>Account / Label</TableHead>
                            <TableHead>Auth</TableHead>
                            <TableHead>Expires</TableHead>
                            <TableHead>Status</TableHead>
                            <TableHead className="text-right">Actions</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {credentials?.map((cred: any) => {
                            const provider = allProviders.find(p => p.id === cred.provider_id)
                            const account = oauthAccount(cred)
                            return (
                                <TableRow key={cred.id} data-state={selectedIds.has(cred.id) ? "selected" : undefined}>
                                    <TableCell className="text-center">
                                        <input
                                            type="checkbox"
                                            className="cursor-pointer"
                                            checked={selectedIds.has(cred.id)}
                                            onChange={() => toggleSelect(cred.id)}
                                        />
                                    </TableCell>
                                    <TableCell className="font-medium">
                                        <div className="flex items-center gap-1.5">
                                            {providerIcon(provider?.name)}
                                            {provider?.display_name ?? cred.provider_id}
                                        </div>
                                    </TableCell>
                                    <TableCell>
                                        <div className="space-y-0.5">
                                            <p className="font-medium text-sm">{cred.label}</p>
                                            {account && (
                                                <p className="text-xs text-muted-foreground">{account}</p>
                                            )}
                                        </div>
                                    </TableCell>
                                    <TableCell>
                                        <Badge variant="outline">{cred.auth_type}</Badge>
                                    </TableCell>
                                    <TableCell className="text-muted-foreground text-sm">
                                        {cred.expires_at ? new Date(cred.expires_at).toLocaleDateString() : "Never"}
                                    </TableCell>
                                    <TableCell>
                                        <div className="flex flex-col gap-1.5 items-start">
                                            {cred.enabled
                                                ? <Badge className="bg-green-500 hover:bg-green-600">Active</Badge>
                                                : <Badge variant="secondary">Disabled</Badge>
                                            }
                                            {cred.status === "ok" && (
                                                <Badge variant="outline" className="text-green-600 border-green-200 bg-green-50 flex gap-1 px-1.5">
                                                    <CheckCircle2 className="w-3 h-3" /> OK
                                                </Badge>
                                            )}
                                            {cred.status === "error" && (
                                                <TooltipProvider delayDuration={200}>
                                                    <Tooltip>
                                                        <TooltipTrigger asChild>
                                                            <Badge variant="outline" className="text-red-500 border-red-200 bg-red-50 flex gap-1 px-1.5 cursor-help">
                                                                <AlertCircle className="w-3 h-3" /> Error
                                                            </Badge>
                                                        </TooltipTrigger>
                                                        <TooltipContent className="max-w-sm">
                                                            <p className="text-xs whitespace-pre-wrap">{cred.error_message || "Connection failed"}</p>
                                                        </TooltipContent>
                                                    </Tooltip>
                                                </TooltipProvider>
                                            )}
                                        </div>
                                    </TableCell>
                                    <TableCell className="text-right">
                                        {actionId === cred.id && actionMsg && (
                                            <div className={`text-xs mb-2 text-right ${actionError ? 'text-destructive' : 'text-green-600'}`}>
                                                {actionMsg}
                                            </div>
                                        )}
                                        {pendingDeleteId === cred.id ? (
                                            <div className="flex gap-1 justify-end">
                                                <Button
                                                    variant="destructive"
                                                    size="sm"
                                                    onClick={() => confirmDelete(cred.id)}
                                                >
                                                    Confirm
                                                </Button>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={() => setPendingDeleteId(null)}
                                                >
                                                    Cancel
                                                </Button>
                                            </div>
                                        ) : (
                                            <div className="flex gap-1 justify-end">
                                                <Button
                                                    variant="outline"
                                                    size="sm"
                                                    title="Verify Key"
                                                    onClick={() => handleVerify(cred.id)}
                                                    className="h-8 w-8 p-0"
                                                >
                                                    <ShieldCheck className="h-4 w-4" />
                                                </Button>
                                                <Button
                                                    variant="outline"
                                                    size="sm"
                                                    title="Sync Models"
                                                    onClick={() => handleSyncModels(cred.provider_id, cred.id)}
                                                    className="h-8 w-8 p-0"
                                                >
                                                    <RefreshCw className="h-4 w-4" />
                                                </Button>
                                                <Button
                                                    variant="outline"
                                                    size="sm"
                                                    title="Check Limits"
                                                    onClick={() => handleCheckQuota(cred.id)}
                                                    className="h-8 w-8 p-0"
                                                >
                                                    <Activity className="h-4 w-4" />
                                                </Button>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    className="text-destructive hover:text-destructive px-2"
                                                    onClick={() => handleDelete(cred.id)}
                                                >
                                                    <Trash2 className="h-4 w-4" />
                                                </Button>
                                            </div>
                                        )}
                                    </TableCell>
                                </TableRow>
                            )
                        })}
                        {credentials?.length === 0 && (
                            <TableRow>
                                <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                                    No credentials yet. Add one to get started.
                                </TableCell>
                            </TableRow>
                        )}
                    </TableBody>
                </Table>
            </div>

            {/* ── unified Add Credential dialog ── */}
            <Dialog open={open} onOpenChange={(v: boolean) => !v && closeDialog()}>
                <DialogContent className="sm:max-w-md">
                    {/* ── Step 1: pick provider ── */}
                    {step === "pick-provider" && (
                        <>
                            <DialogHeader>
                                <DialogTitle>Add Credential</DialogTitle>
                                <DialogDescription>
                                    Choose a provider — the form will adapt to its auth type automatically.
                                </DialogDescription>
                            </DialogHeader>
                            <div className="py-2 space-y-4">
                                <div className="space-y-1.5">
                                    <Label>Provider</Label>
                                    <Select value={selectedProviderId} onValueChange={setSelectedProviderId}>
                                        <SelectTrigger>
                                            <SelectValue placeholder="Select a provider..." />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {allProviders.length > 0 ? allProviders.map(p => (
                                                <SelectItem key={p.id} value={p.id}>
                                                    <div className="flex items-center gap-2">
                                                        {providerIcon(p.name)}
                                                        <span>{p.display_name}</span>
                                                        <Badge variant="outline" className="ml-1 text-xs">
                                                            {p.auth_type === "api_key" ? "API Key" : "OAuth"}
                                                        </Badge>
                                                    </div>
                                                </SelectItem>
                                            )) : (
                                                <SelectItem value="_none" disabled>No providers configured</SelectItem>
                                            )}
                                        </SelectContent>
                                    </Select>
                                </div>

                                {/* Preview what will happen */}
                                {selectedProvider && (
                                    <div className="rounded-md bg-muted/40 border px-3 py-2.5 text-sm">
                                        {selectedProvider.auth_type === "api_key" ? (
                                            <p>→ You'll enter an API key for <strong>{selectedProvider.display_name}</strong>.</p>
                                        ) : selectedProvider.name === "google-antigravity" ? (
                                            <p>→ Opens a Google login page. No configuration needed.</p>
                                        ) : (
                                            <p>→ Opens the {selectedProvider.display_name} OAuth consent screen.</p>
                                        )}
                                    </div>
                                )}

                                {error && <p className="text-sm text-destructive">{error}</p>}
                            </div>
                            <DialogFooter>
                                <Button variant="outline" onClick={closeDialog}>Cancel</Button>
                                <Button
                                    disabled={!selectedProviderId || saving}
                                    onClick={handleProviderChosen}
                                >
                                    {saving ? "Loading..." : <>Continue <ChevronRight className="ml-1 h-4 w-4" /></>}
                                </Button>
                            </DialogFooter>
                        </>
                    )}

                    {/* ── Step 2a: API key entry ── */}
                    {step === "api-key" && (
                        <>
                            <DialogHeader>
                                <DialogTitle>Add API Key — {selectedProvider?.display_name}</DialogTitle>
                                <DialogDescription>Enter your API key. It will be stored encrypted.</DialogDescription>
                            </DialogHeader>
                            <form onSubmit={handleSaveApiKey} className="py-2 space-y-4">
                                <div className="space-y-1.5">
                                    <Label htmlFor="cred-label">Label</Label>
                                    <Input
                                        id="cred-label"
                                        value={apiKeyLabel}
                                        onChange={e => setApiKeyLabel(e.target.value)}
                                        placeholder="e.g. Production key"
                                    />
                                </div>
                                <div className="space-y-1.5">
                                    <Label htmlFor="cred-key">API Key</Label>
                                    <Input
                                        id="cred-key"
                                        type="password"
                                        value={apiKey}
                                        onChange={e => setApiKey(e.target.value)}
                                        placeholder="sk-..."
                                    />
                                    <p className="text-xs text-muted-foreground">Stored encrypted, never shown again.</p>
                                </div>
                                {error && <p className="text-sm text-destructive">{error}</p>}
                                <DialogFooter>
                                    <Button type="button" variant="outline" onClick={() => setStep("pick-provider")}>Back</Button>
                                    <Button type="submit" disabled={saving || !apiKey || !apiKeyLabel}>
                                        {saving ? "Saving..." : "Save API Key"}
                                    </Button>
                                </DialogFooter>
                            </form>
                        </>
                    )}

                    {/* ── Step 2b: OAuth loading ── */}
                    {step === "oauth-start" && (
                        <>
                            <DialogHeader>
                                <DialogTitle>Connecting…</DialogTitle>
                                <DialogDescription>Preparing the OAuth login URL.</DialogDescription>
                            </DialogHeader>
                            <div className="py-6 text-center text-muted-foreground text-sm">Loading authorization URL…</div>
                        </>
                    )}

                    {/* ── Step 2c: OAuth open browser ── */}
                    {step === "oauth-waiting" && oauthUrl && (
                        <>
                            <DialogHeader>
                                <DialogTitle>
                                    {selectedProvider?.name === "google-antigravity"
                                        ? "Sign in with Google"
                                        : `Connect ${selectedProvider?.display_name}`}
                                </DialogTitle>
                                <DialogDescription>
                                    {selectedProvider?.name === "google-antigravity"
                                        ? "A Google login window is ready. Click below to sign in — the window will close automatically when done."
                                        : "Click below to open the OAuth consent screen."}
                                </DialogDescription>
                            </DialogHeader>
                            <div className="py-4 space-y-4">
                                <Button
                                    className="w-full"
                                    onClick={() => {
                                        window.open(oauthUrl, 'oauth', 'width=600,height=700')
                                    }}
                                >
                                    <ExternalLink className="mr-2 h-4 w-4" />
                                    {selectedProvider?.name === "google-antigravity"
                                        ? "Open Google Login"
                                        : "Open Authorization Screen"}
                                </Button>
                            </div>
                            <DialogFooter>
                                <Button variant="outline" onClick={async () => { await mutate(); closeDialog() }}>
                                    Done
                                </Button>
                            </DialogFooter>
                        </>
                    )}
                </DialogContent>
            </Dialog>
        </div>
    )
}
