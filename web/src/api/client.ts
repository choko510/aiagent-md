import type { TaskState, TaskV1 } from "./generated/domain";

export type SessionInfo = {
  authenticated: boolean;
  authMode?: "session" | "bearer";
  tenantId?: string;
  userId?: string;
  workspaceId?: string;
  csrfToken?: string | null;
  expiresAt?: string;
};

export type TaskSummary = {
  id: string;
  state: TaskState;
  profile: string;
  goalRevision: string;
  goalSummary?: string;
  activeRunId: string | null;
  runState: string | null;
  createdAt: string;
  updatedAt: string;
};

export type ApprovalRow = {
  id: string;
  tool: string;
  summary: string;
  decision: string;
  requestSha256: string;
  recordVersion: string;
  goalRevision: string;
};

export type FullTaskView = {
  id: TaskV1["id"];
  state: TaskV1["state"];
  goalRevision: TaskV1["goalRevision"];
  recordVersion: TaskV1["recordVersion"];
  run: {
    id: string | null;
    state: string | null;
    attemptNo: number | null;
    leaseEpoch: string;
    headRevision: string;
    result: string | null;
    errorCode: string | null;
  };
  approvals: ApprovalRow[];
  effects: Array<{ tool: string; status: string }>;
};

export type BrowserSessionItem = {
  id: string;
  taskId: string;
  runId: string;
  state: string;
  mode: string;
  generation: number;
  goalRevision: string;
  leaseEpoch: string;
  currentUrl?: string | null;
  takeoverReason?: string | null;
  hasStorageState?: boolean;
  updatedAt: string;
};

export type BrowserObservation = {
  sessionId: string;
  taskId: string;
  runId: string;
  state: string;
  mode: string;
  generation: number;
  currentUrl: string | null;
  takeoverReason: string | null;
  observation: string;
  truncated: boolean;
  observedAt: string | null;
  observeOnly: true;
};

export type MemoryItem = {
  id: string;
  recipient: string;
  consentBasis: string;
  observedAt: string;
  expiresAt: string;
  revision: number;
  revoked: boolean;
  sourceArtifactRef: string;
  projectionArtifactRef: string | null;
};

export type MemoryRevisionItem = {
  revision: number;
  recipient: string;
  observedAt: string;
  expiresAt: string;
  revokedAt: string | null;
  editedAt: string;
};

export type ConsentItem = {
  recipient: string;
  consentBasis: string;
  granted: boolean;
  recordVersion: number;
  grantedAt: string;
};

export type ContextPolicyItem = {
  recipient: string;
  model: string;
  enabled: boolean;
  semanticVerifierMode: "HEURISTIC" | "LLM_GATED";
  recordVersion: number;
  updatedAt: string;
};

export type CampaignItem = {
  id: string;
  state: string;
  controlVersion: number;
  goalRevision: string;
  updatedAt: string;
};

export type WatchItem = {
  watchId: string;
  timezoneId: string;
  localHour: number;
  localMinute: number;
  maxOccurrences: number;
  revision: number;
  active: boolean;
  createdAt: string;
};

export type NotificationBinding = {
  id: string;
  channel: "webhook" | "telegram";
  label: string;
  destinationScheme: string;
  destinationHost: string;
  destinationPort: number;
  redactedTarget: string;
  bindingVersion: number;
  consentGranted: boolean;
  consentVersion: number;
  quietStartHour: number | null;
  quietEndHour: number | null;
  timezoneId: string;
  optedOutAt: string | null;
  revokedAt: string | null;
  createdAt: string;
  updatedAt: string;
};

export type NotificationIntentItem = {
  id: string;
  bindingId: string;
  bindingVersion: number;
  consentVersion: number;
  semanticEventKey: string;
  deliveryPurpose: string;
  sourceKind: string;
  sourceRef: string;
  evidenceUrls: string[];
  observedAt: string;
  priority: string;
  title: string;
  summaryText: string;
  status: string;
  attemptCount: number;
  lastErrorCode: string | null;
  providerReceiptRef: string | null;
  notBeforeAt: string;
  deliveredAt: string | null;
  readAt: string | null;
  createdAt: string;
};

export type DryRunRecipient = {
  bindingId: string;
  label: string;
  channel: "webhook" | "telegram";
  redactedTarget: string;
  eligible: boolean;
  blockedReason: string | null;
  wouldDeferUntil: string | null;
};

export type DryRunPreview = {
  dryRun: true;
  evaluatedAt: string;
  recipientCount: number;
  sendableCount: number;
  recipients: DryRunRecipient[];
};

export type CapabilityItem = {
  component: string;
  integrationState: string;
  memoryEgressPath: string;
  externalActionsEnabled: boolean;
  ownerConsentRequired: boolean;
  hostVerified: boolean;
  productionReady: boolean;
};

export type DevProjectItem = {
  id: string;
  workspaceId: string;
  name: string;
  repository: string;
  defaultBranch: string;
  framework: string;
  repositoryBindingId: string | null;
  githubApiBase: string | null;
  preferences: {
    designPolicy?: string;
    demoAcceptancePolicy?: string;
    styleCandidates?: number;
    expertAdvisorEnabled?: boolean;
    releasePolicy?: string;
  };
  status: string;
  revision: number;
  createdAt: string;
  updatedAt: string;
};

export type DevCycleItem = {
  id: string;
  projectId: string;
  workspaceId: string;
  taskId: string;
  runId: string;
  version: number;
  phase: string;
  roundNumber: number;
  changeRequestRef: string;
  repository: string;
  branch: string;
  baseSha: string;
  commitSha: string | null;
  tests: Array<Record<string, unknown>>;
  reviews: Array<Record<string, unknown>>;
  pushReceipt: string | null;
  prReceipt: string | null;
  createdAt: string;
  updatedAt: string;
};

export type DevDesignCandidate = {
  candidateId: string;
  label: string;
  fixtureRef: string;
  summary: string;
  desktopThumbRef: string;
  mobileThumbRef: string;
  viewports: string[];
  themes: string[];
  tokens?: { theme?: string; accent?: string };
};

export type DevDesignDecisionItem = {
  id: string;
  projectId: string;
  cycleId: string;
  goalRevision: number;
  designRevision: number;
  designPolicy: string;
  demoAcceptancePolicy: string;
  candidates: DevDesignCandidate[];
  recommendedCandidateId: string | null;
  selectedCandidateId: string | null;
  demoDecision: string;
  demoSnapshotDigest: string | null;
  feedback: string | null;
  decidedAt: string | null;
  createdAt: string;
  updatedAt: string;
};

export type DevDebugFindingItem = {
  id: string;
  projectId: string;
  cycleId: string;
  previewLeaseId: string | null;
  commitSha: string;
  sourceUrlRedacted: string;
  consoleErrors: string[];
  networkErrors: string[];
  layoutIssues: string[];
  reproTestId: string | null;
  reproBeforeFailDigest: string | null;
  reproAfterPassDigest: string | null;
  status: string;
  createdAt: string;
};

export type DevPrRecordItem = {
  id: string;
  projectId: string;
  cycleId: string;
  repository: string;
  branch: string;
  headSha: string;
  prNumber: number | null;
  prUrl: string | null;
  idempotencyKey: string;
  intentId: string | null;
  effectStatus: string;
  ciState: string;
  repairAttempts: number;
  maxRepairAttempts: number;
  lastErrorCode: string | null;
  createdAt: string;
  updatedAt: string;
};

export class ApiClientError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string) {
    super(`HTTP ${status}: ${code}`);
    this.status = status;
    this.code = code;
  }
}

export function extractErrorCode(rawText: string, status: number): string {
  try {
    const parsed = JSON.parse(rawText) as { error?: unknown; detail?: { error?: unknown } | unknown };
    if (typeof parsed.error === "string") return parsed.error;
    if (parsed.detail && typeof parsed.detail === "object" && "error" in parsed.detail) {
      return String((parsed.detail as { error: unknown }).error);
    }
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    // fallback below
  }
  return rawText.trim() || `HTTP_${status}`;
}

export function createClientRequest(
  getCsrf: () => string | null,
  onUnauthorized: () => void,
) {
  return async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers ?? {});
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const csrf = getCsrf();
    if (csrf && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      headers.set("X-CSRF-Token", csrf);
    }
    const res = await fetch(path, {
      ...init,
      headers,
      credentials: "include",
    });
    const text = await res.text();
    if (!res.ok) {
      if (res.status === 401) {
        onUnauthorized();
      }
      throw new ApiClientError(res.status, extractErrorCode(text, res.status));
    }
    return (text ? JSON.parse(text) : {}) as T;
  };
}
