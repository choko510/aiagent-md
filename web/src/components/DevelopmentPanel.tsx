import { FormEvent, useState } from "react";
import type {
  DevCycleItem,
  DevDebugFindingItem,
  DevDesignDecisionItem,
  DevPrRecordItem,
  DevProjectItem,
} from "../api/client";

type Props = {
  projects: DevProjectItem[];
  selectedProjectId: string | null;
  cycles: DevCycleItem[];
  selectedCycleId: string | null;
  designs: DevDesignDecisionItem[];
  debugFindings: DevDebugFindingItem[];
  prRecords: DevPrRecordItem[];
  busy: boolean;
  onSelectProject: (projectId: string) => void;
  onSelectCycle: (cycleId: string) => void;
  onCreateProject: (payload: {
    name: string;
    repository: string;
    defaultBranch: string;
    framework: string;
    preferences: {
      designPolicy: string;
      demoAcceptancePolicy: string;
      releasePolicy: string;
    };
  }) => Promise<boolean>;
  onSelectDesignCandidate: (cycleId: string, designRevision: number, candidateId: string) => Promise<boolean>;
  onDecideDemo: (
    cycleId: string,
    designRevision: number,
    decision: "APPROVED" | "REVISION_REQUESTED" | "REJECTED",
    demoSnapshotDigest: string,
    feedback?: string,
  ) => Promise<boolean>;
  onReconcilePr: (prRecordId: string) => Promise<boolean>;
  onGuardianCheckPr: (prRecordId: string) => Promise<boolean>;
};

export default function DevelopmentPanel({
  projects,
  selectedProjectId,
  cycles,
  selectedCycleId,
  designs,
  debugFindings,
  prRecords,
  busy,
  onSelectProject,
  onSelectCycle,
  onCreateProject,
  onSelectDesignCandidate,
  onDecideDemo,
  onReconcilePr,
  onGuardianCheckPr,
}: Props) {
  const [name, setName] = useState("");
  const [repository, setRepository] = useState("");
  const [defaultBranch, setDefaultBranch] = useState("main");
  const [framework, setFramework] = useState("web");
  const [designPolicy, setDesignPolicy] = useState("REQUIRE_USER_SELECTION");
  const [demoAcceptancePolicy, setDemoAcceptancePolicy] = useState("REQUIRE_USER_APPROVAL");
  const [releasePolicy, setReleasePolicy] = useState("PR_AFTER_APPROVED_SCOPE");
  const [viewportMode, setViewportMode] = useState<"desktop" | "mobile">("desktop");
  const [themeMode, setThemeMode] = useState<"light" | "dark">("light");
  const [feedback, setFeedback] = useState("");

  const activeCycle = cycles.find((c) => c.id === selectedCycleId) ?? null;
  const latestDesign = designs[0] ?? null;

  async function handleCreateProject(e: FormEvent) {
    e.preventDefault();
    if (!name.trim() || !repository.trim()) return;
    const ok = await onCreateProject({
      name: name.trim(),
      repository: repository.trim(),
      defaultBranch: defaultBranch.trim() || "main",
      framework,
      preferences: { designPolicy, demoAcceptancePolicy, releasePolicy },
    });
    if (ok) {
      setName("");
      setRepository("");
    }
  }

  return (
    <div className="console-split" data-testid="development-panel">
      <div className="card">
        <h3>開発プロジェクト (DEV-01 / DEV-09)</h3>
        <form onSubmit={handleCreateProject} className="stack-form">
          <label className="field-label">
            プロジェクト名
            <input
              aria-label="プロジェクト名"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例: Web Console App"
              disabled={busy}
            />
          </label>
          <label className="field-label">
            リポジトリ (owner/repo)
            <input
              aria-label="リポジトリ"
              value={repository}
              onChange={(e) => setRepository(e.target.value)}
              placeholder="例: org/demo-app"
              disabled={busy}
            />
          </label>
          <div className="action-row">
            <label className="field-label">
              既定ブランチ
              <input
                aria-label="既定ブランチ"
                value={defaultBranch}
                onChange={(e) => setDefaultBranch(e.target.value)}
                disabled={busy}
              />
            </label>
            <label className="field-label">
              フレームワーク
              <select aria-label="フレームワーク" value={framework} onChange={(e) => setFramework(e.target.value)}>
                <option value="web">web</option>
                <option value="python">python</option>
                <option value="node">node</option>
                <option value="android">android</option>
                <option value="desktop">desktop</option>
                <option value="generic">generic</option>
              </select>
            </label>
          </div>
          <div className="action-row">
            <label className="field-label">
              Design 方針
              <select
                aria-label="Design方針"
                value={designPolicy}
                onChange={(e) => setDesignPolicy(e.target.value)}
              >
                <option value="REQUIRE_USER_SELECTION">ユーザー選択必須</option>
                <option value="AUTO_RECOMMEND">推奨のみ自動提案</option>
              </select>
            </label>
            <label className="field-label">
              Demo 承認方針
              <select
                aria-label="Demo承認方針"
                value={demoAcceptancePolicy}
                onChange={(e) => setDemoAcceptancePolicy(e.target.value)}
              >
                <option value="REQUIRE_USER_APPROVAL">都度ユーザー承認</option>
                <option value="PREAUTHORIZED_BOUNDED_AUTO">有界事前承認</option>
              </select>
            </label>
          </div>
          <label className="field-label">
            公開ポリシー
            <select aria-label="公開ポリシー" value={releasePolicy} onChange={(e) => setReleasePolicy(e.target.value)}>
              <option value="LOCAL_ONLY">LOCAL_ONLY</option>
              <option value="PR_AFTER_APPROVED_SCOPE">PR_AFTER_APPROVED_SCOPE</option>
              <option value="REQUIRE_PER_ACTION_APPROVAL">REQUIRE_PER_ACTION_APPROVAL</option>
            </select>
          </label>
          <button type="submit" disabled={busy || !name.trim() || !repository.trim()}>
            プロジェクト登録
          </button>
        </form>

        <h4>登録済みプロジェクト ({projects.length})</h4>
        <ul className="item-list">
          {projects.map((p) => (
            <li key={p.id}>
              <button
                type="button"
                className={p.id === selectedProjectId ? "selected" : ""}
                data-testid={`dev-project-${p.id}`}
                onClick={() => onSelectProject(p.id)}
              >
                <strong>{p.name}</strong> ({p.repository}@{p.defaultBranch}) [{p.framework}]
              </button>
            </li>
          ))}
        </ul>

        <h4>開発サイクル ({cycles.length})</h4>
        <ul className="item-list">
          {cycles.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className={c.id === selectedCycleId ? "selected" : ""}
                data-testid={`dev-cycle-${c.id}`}
                onClick={() => onSelectCycle(c.id)}
              >
                <span>{c.id}</span> — <code>{c.phase}</code> (Round {c.roundNumber})
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="card">
        <h3>サイクル詳細・Design Picker・Browser Debug・PR 統合</h3>
        {!activeCycle ? (
          <p className="muted">左の一覧から開発サイクルを選択してください。</p>
        ) : (
          <div data-testid="dev-cycle-detail">
            <p>
              <strong>Cycle:</strong> <code>{activeCycle.id}</code> | <strong>Phase:</strong>{" "}
              <code>{activeCycle.phase}</code> | <strong>Commit:</strong>{" "}
              <code>{activeCycle.commitSha ? activeCycle.commitSha.slice(0, 12) : "未コミット"}</code>
            </p>
            <p className="muted">
              Repo: {activeCycle.repository} / Branch: {activeCycle.branch} / Tests: {activeCycle.tests.length} /
              Reviews: {activeCycle.reviews.length}
            </p>

            <section className="sub-section" data-testid="dev-design-picker">
              <h4>Design Picker &amp; Demo 承認 (DEV-03)</h4>
              <p className="muted">
                Design / Demo 承認は UI 仕様合意のみであり、外部 Git Push・PR 作成・デプロイの Tool 承認とは分離されています。
              </p>
              {!latestDesign ? (
                <p className="muted">このサイクルの Design 候補はまだありません。</p>
              ) : (
                <div>
                  <div className="action-row">
                    <span>
                      Rev: <strong>{latestDesign.designRevision}</strong> / Demo 状態:{" "}
                      <strong data-testid="dev-demo-status">{latestDesign.demoDecision}</strong>
                    </span>
                    <button
                      type="button"
                      onClick={() => setViewportMode(viewportMode === "desktop" ? "mobile" : "desktop")}
                    >
                      表示: {viewportMode}
                    </button>
                    <button type="button" onClick={() => setThemeMode(themeMode === "light" ? "dark" : "light")}>
                      テーマ: {themeMode}
                    </button>
                  </div>
                  <div className="action-row" style={{ flexWrap: "wrap", marginTop: "8px" }}>
                    {latestDesign.candidates.map((cand) => {
                      const isSel = latestDesign.selectedCandidateId === cand.candidateId;
                      return (
                        <div
                          key={cand.candidateId}
                          className="card"
                          data-testid={`design-candidate-${cand.candidateId}`}
                          style={{ flex: "1 1 180px", padding: "8px" }}
                        >
                          <div>
                            <strong>{cand.label}</strong>
                            {latestDesign.recommendedCandidateId === cand.candidateId && " [推奨]"}
                            {isSel && " [選択中]"}
                          </div>
                          <div className="muted">Fixture: {cand.fixtureRef}</div>
                          <div className="muted">
                            {viewportMode === "desktop" ? cand.desktopThumbRef : cand.mobileThumbRef} ({themeMode})
                          </div>
                          <p>{cand.summary}</p>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void onSelectDesignCandidate(
                                activeCycle.id,
                                latestDesign.designRevision,
                                cand.candidateId,
                              )
                            }
                          >
                            {isSel ? "選択済み" : "この候補を選択"}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                  <label className="field-label" style={{ marginTop: "8px" }}>
                    Demo レビューコメント
                    <input
                      aria-label="Demoレビューコメント"
                      value={feedback}
                      onChange={(e) => setFeedback(e.target.value)}
                      placeholder="修正要望や確認メモ"
                      disabled={busy}
                    />
                  </label>
                  <div className="action-row">
                    <button
                      type="button"
                      disabled={busy || !latestDesign.selectedCandidateId || !latestDesign.demoSnapshotDigest}
                      onClick={() =>
                        void onDecideDemo(
                          activeCycle.id,
                          latestDesign.designRevision,
                          "APPROVED",
                          latestDesign.demoSnapshotDigest ?? "",
                          feedback || undefined,
                        )
                      }
                    >
                      Demo を承認 (APPROVED)
                    </button>
                    <button
                      type="button"
                      disabled={busy || !latestDesign.demoSnapshotDigest}
                      onClick={() =>
                        void onDecideDemo(
                          activeCycle.id,
                          latestDesign.designRevision,
                          "REVISION_REQUESTED",
                          latestDesign.demoSnapshotDigest ?? "",
                          feedback || undefined,
                        )
                      }
                    >
                      修正依頼
                    </button>
                    <button
                      type="button"
                      disabled={busy || !latestDesign.demoSnapshotDigest}
                      onClick={() =>
                        void onDecideDemo(
                          activeCycle.id,
                          latestDesign.designRevision,
                          "REJECTED",
                          latestDesign.demoSnapshotDigest ?? "",
                          feedback || undefined,
                        )
                      }
                    >
                      却下
                    </button>
                  </div>
                </div>
              )}
            </section>

            <section className="sub-section" data-testid="dev-debug-findings">
              <h4>Browser Debug 証跡 &amp; Reproduction Lab (DEV-06 / DEV-12)</h4>
              {debugFindings.length === 0 ? (
                <p className="muted">Debug 証跡はまだありません（CDP URL・機密情報は非公開）。</p>
              ) : (
                <ul className="item-list">
                  {debugFindings.map((f) => (
                    <li key={f.id} data-testid={`debug-finding-${f.id}`}>
                      <strong>{f.status}</strong> — <code>{f.sourceUrlRedacted}</code> (Console:{" "}
                      {f.consoleErrors.length}, Net: {f.networkErrors.length}, Layout: {f.layoutIssues.length})
                      {f.reproTestId && (
                        <div className="muted">
                          Repro: {f.reproTestId} (Before FAIL: {f.reproBeforeFailDigest?.slice(0, 10) ?? "-"} / After
                          PASS: {f.reproAfterPassDigest?.slice(0, 10) ?? "-"})
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}

        <section className="sub-section" data-testid="dev-pr-records">
          <h4>PR 統合・PR Guardian 状態 (DEV-05 / DEV-11)</h4>
          {prRecords.length === 0 ? (
            <p className="muted">選択中プロジェクトの PR レコードはありません。</p>
          ) : (
            <ul className="item-list">
              {prRecords.map((pr) => (
                <li key={pr.id} data-testid={`dev-pr-${pr.id}`}>
                  <div>
                    <strong>{pr.repository}</strong>#{pr.prNumber ?? "?"} (<code>{pr.branch}</code>) — Effect:{" "}
                    <code>{pr.effectStatus}</code> / CI: <code>{pr.ciState}</code> / Repair: {pr.repairAttempts}/
                    {pr.maxRepairAttempts}
                  </div>
                  <div className="action-row">
                    {pr.effectStatus === "EFFECT_UNKNOWN" && (
                      <button type="button" disabled={busy} onClick={() => void onReconcilePr(pr.id)}>
                        Reconcile (照合)
                      </button>
                    )}
                    {pr.effectStatus === "CONFIRMED" && pr.prNumber && (
                      <button type="button" disabled={busy} onClick={() => void onGuardianCheckPr(pr.id)}>
                        PR Guardian CI 点検
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
