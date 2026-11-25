import { useEffect, useMemo, useRef, useState } from "react";
import { ApiClientError, createClientRequest, type SessionInfo } from "./api/client";

type Capabilities = {
  parserIsolation: "unverified" | "in_process_dev" | "unavailable";
  externalVision: boolean;
  localOcr: string;
  note: string;
};

type Artifact = {
  artifactId: string;
  displayName: string;
  format: string;
  sha256: string;
  byteLength: number;
  quarantineState: string;
};

type Citation = {
  fieldKey: string;
  rawText?: string | null;
  valueState?: string;
  verification?: string;
  normalized?: Record<string, unknown> | null;
  conditions?: string[];
  missingReason?: string | null;
  locator?: Record<string, unknown> | null;
};

type Job = {
  jobId: string;
  artifactId: string;
  status: string;
  attempts: number;
  errorCode: string | null;
  outcome?: {
    status: string;
    warnings?: string[];
    missingFields?: string[];
    candidates?: Citation[];
  };
  citations?: Citation[];
};

type Answer = {
  status: string;
  answer: string;
  synthesis: string;
  citations: Citation[];
};

const CHUNK = 512 * 1024;
const MAX_BYTES = 20 * 1024 * 1024;

function hex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function locatorLabel(locator: Record<string, unknown> | null | undefined): string {
  if (!locator) return "location unavailable";
  const kind = String(locator.kind ?? "document");
  if (kind === "csv") return `record ${String(locator.record1)} · field ${String(locator.field1)}`;
  if (kind === "pdf") return `page ${String(locator.page1)}`;
  if (kind === "sheet") return `${String(locator.sheetName)}!${String(locator.cellA1)}`;
  if (kind === "office" && locator.format === "pptx") return `slide ${String(locator.slide1)}`;
  if (kind === "office") return `paragraph ${String(locator.paragraph1)}`;
  if (kind === "image") return "image region";
  return kind;
}

function readableError(error: unknown): string {
  if (error instanceof ApiClientError) return error.code;
  return error instanceof Error ? error.message : "UNKNOWN_ERROR";
}

export default function MultimodalView() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [question, setQuestion] = useState("What is the price?");
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const csrfRef = useRef<string | null>(null);

  const apiFetch = useMemo(
    () =>
      createClientRequest(() => csrfRef.current, () => {
        csrfRef.current = null;
        setSession(null);
      }),
    []
  );

  useEffect(() => {
    let active = true;
    void apiFetch<SessionInfo>("/v1/auth/session")
      .then((info) => {
        if (!active) return;
        csrfRef.current = info.csrfToken ?? null;
        setSession(info);
      })
      .catch(() => {
        if (active) setSession({ authenticated: false });
      });
    void apiFetch<Capabilities>("/v1/multimodal/capabilities")
      .then((value) => {
        if (active) setCapabilities(value);
      })
      .catch(() => {
        if (active) setCapabilities(null);
      });
    return () => {
      active = false;
    };
  }, [apiFetch]);

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setArtifact(null);
    setJob(null);
    setAnswer(null);
    try {
      if (file.size === 0 || file.size > MAX_BYTES) {
        throw new Error("FILE_SIZE_REQUIRES_1_BYTE_TO_20_MIB");
      }
      setStatus("Hashing the file locally…");
      const bytes = await file.arrayBuffer();
      const digest = hex(await crypto.subtle.digest("SHA-256", bytes));
      setStatus("Opening a quarantined upload…");
      const started = await apiFetch<{ uploadId: string }>("/v1/multimodal/uploads", {
        method: "POST",
        body: JSON.stringify({
          fileName: file.name,
          declaredMediaType: file.type || "application/octet-stream",
          byteLength: file.size,
          sha256: digest,
          idempotencyKey: `web-${digest.slice(0, 32)}`,
        }),
      });
      for (let offset = 0, index = 0; offset < file.size; offset += CHUNK, index += 1) {
        const part = bytes.slice(offset, Math.min(offset + CHUNK, file.size));
        setStatus(`Sending chunk ${index + 1} of ${Math.ceil(file.size / CHUNK)}…`);
        await apiFetch(`/v1/multimodal/uploads/${started.uploadId}/chunks/${index}`, {
          method: "PUT",
          headers: {
            "Content-Type": "application/octet-stream",
            "X-Chunk-Sha256": hex(await crypto.subtle.digest("SHA-256", part)),
          },
          body: part,
        });
      }
      setStatus("Completing integrity checks…");
      const completed = await apiFetch<Artifact>(
        `/v1/multimodal/uploads/${started.uploadId}/complete`,
        { method: "POST", body: "{}" }
      );
      setArtifact(completed);
      setStatus("Queuing a parse job…");
      const created = await apiFetch<Job>(`/v1/multimodal/artifacts/${completed.artifactId}/jobs`, {
        method: "POST",
        body: JSON.stringify({
          requestedFields: ["amount", "effective_price", "date"],
          allowLocalOcr: false,
          idempotencyKey: `web-job-${digest.slice(0, 32)}`,
        }),
      });
      setStatus("Parsing and independently verifying anchors…");
      const ran = await apiFetch<Job>(`/v1/multimodal/jobs/${created.jobId}/run`, {
        method: "POST",
        body: "{}",
      });
      setJob(ran);
      setStatus(
        ran.status === "succeeded"
          ? "Parse complete. Only independently anchored values are usable; other values remain unknown."
          : `Parse ended with status ${ran.status}.`
      );
    } catch (caught) {
      setError(readableError(caught));
      setStatus("");
    } finally {
      setBusy(false);
    }
  };

  const ask = async () => {
    if (!artifact) return;
    setBusy(true);
    setError(null);
    try {
      const response = await apiFetch<Answer>(`/v1/multimodal/artifacts/${artifact.artifactId}/answer`, {
        method: "POST",
        body: JSON.stringify({ question }),
      });
      setAnswer(response);
    } catch (caught) {
      setError(readableError(caught));
    } finally {
      setBusy(false);
    }
  };

  const citations = job?.citations ?? job?.outcome?.candidates ?? [];

  return (
    <div className="multimodal-view">
      <section className="mmi-card">
        <h2>1. Upload a document</h2>
        <p className="mmi-hint">
          The original filename is untrusted display metadata. Bytes are staged in quarantine, checked for length and
          SHA-256, then detected from content rather than from the extension.
        </p>
        <input
          type="file"
          aria-label="Document to ingest"
          accept=".pdf,.png,.jpg,.jpeg,.webp,.csv,.tsv,.xlsx,.docx,.pptx"
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            setArtifact(null);
            setJob(null);
            setAnswer(null);
          }}
        />
        <button type="button" disabled={!file || busy || !session?.authenticated} onClick={() => void upload()}>
          {busy ? "Working…" : "Ingest and verify"}
        </button>
        {!session?.authenticated && <p className="mmi-warn">Sign in from the Control Console before uploading.</p>}
        {status && <p role="status" className="mmi-status">{status}</p>}
        {error && <p role="alert" className="error">Error: {error}</p>}
      </section>

      {capabilities && (
        <section className="mmi-card">
          <h2>Execution boundary</h2>
          <dl className="mmi-facts">
            <dt>Parser isolation</dt>
            <dd>{capabilities.parserIsolation}</dd>
            <dt>External vision</dt>
            <dd>{capabilities.externalVision ? "enabled" : "disabled"}</dd>
            <dt>Local OCR</dt>
            <dd>{capabilities.localOcr}</dd>
          </dl>
          {capabilities.parserIsolation === "in_process_dev" && (
            <p className="mmi-warn">
              This deployment uses the development-only in-process parser. It is not a verified isolation boundary
              and must not be used for untrusted production input.
            </p>
          )}
          {capabilities.parserIsolation === "unverified" && (
            <p className="mmi-warn">
              A parser executor is configured, but this deployment has not attested its OS-level isolation. Treat parse
              output as untrusted.
            </p>
          )}
          {capabilities.parserIsolation === "unavailable" && (
            <p className="mmi-warn">No parser executor is configured. Parse jobs will fail closed.</p>
          )}
        </section>
      )}

      {artifact && (
        <section className="mmi-card">
          <h2>2. Artifact and parse job</h2>
          <dl className="mmi-facts">
            <dt>Display name</dt>
            <dd>{artifact.displayName}</dd>
            <dt>Detected format</dt>
            <dd>{artifact.format}</dd>
            <dt>SHA-256</dt>
            <dd className="mmi-mono">{artifact.sha256}</dd>
            <dt>Bytes</dt>
            <dd>{artifact.byteLength}</dd>
            <dt>Quarantine</dt>
            <dd>{artifact.quarantineState}</dd>
            {job && <><dt>Job</dt><dd>{job.status} · attempts {job.attempts}</dd></>}
            {job?.errorCode && <><dt>Error</dt><dd>{job.errorCode}</dd></>}
          </dl>
          {job?.outcome?.warnings?.length ? (
            <p className="mmi-warn">Warnings: {job.outcome.warnings.join(", ")}</p>
          ) : null}
          {job?.outcome?.missingFields?.length ? (
            <p className="mmi-warn">Missing (not guessed): {job.outcome.missingFields.join(", ")}</p>
          ) : null}
        </section>
      )}

      {citations.length > 0 && (
        <section className="mmi-card">
          <h2>3. Extracted fields</h2>
          <div className="mmi-table-wrap">
            <table className="mmi-table">
              <thead>
                <tr><th>Field</th><th>Value</th><th>Verification</th><th>Location</th></tr>
              </thead>
              <tbody>
                {citations.map((citation, index) => (
                  <tr key={`${citation.fieldKey}-${index}`}>
                    <td>{citation.fieldKey}</td>
                    <td className="mmi-mono">
                      {citation.normalized ? JSON.stringify(citation.normalized) : citation.rawText ?? "unknown"}
                    </td>
                    <td>{citation.verification ?? citation.valueState ?? "unverified"}</td>
                    <td>{locatorLabel(citation.locator)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mmi-hint">
            OCR confidence is not truth. A candidate is “anchored” only after independent source verification;
            unanchored or contradicted values stay unknown.
          </p>
        </section>
      )}

      {job?.status === "succeeded" && artifact && (
        <section className="mmi-card">
          <h2>4. Ask from grounded context</h2>
          <p className="mmi-hint">
            This answer is deterministic and extractive. It uses only anchored candidates and cites the page, record,
            cell, slide, or image region. Excerpts are marked untrusted before any model sees them.
          </p>
          <label className="mmi-question">
            Question
            <input value={question} maxLength={2000} onChange={(event) => setQuestion(event.target.value)} />
          </label>
          <button type="button" disabled={busy || !question.trim()} onClick={() => void ask()}>
            Ask with citations
          </button>
          {answer && (
            <div className="mmi-answer">
              <p>{answer.answer}</p>
              <p className="mmi-hint">
                status {answer.status} · synthesis {answer.synthesis}
              </p>
              <ul>
                {answer.citations.map((citation, index) => (
                  <li key={`answer-${index}`}>
                    {citation.fieldKey}: {citation.normalized ? JSON.stringify(citation.normalized) : citation.rawText}
                    {" — "}{locatorLabel(citation.locator)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
