import { FormEvent, useEffect, useState } from "react";

type Source = "all" | "mcp" | "clawhub";
type Kind = "all" | "MCP" | "SKILL";

type CatalogItem = {
  id: string;
  source: Exclude<Source, "all">;
  kind: Exclude<Kind, "all">;
  name: string;
  summary: string;
  publisher: string;
  version: string | null;
  url: string;
  identity: string;
  transport: string | null;
  catalogStatus: string;
  installable: false;
  notice: string;
};
type Warning = { source: string; code: string };
type ListResponse = {
  items: CatalogItem[];
  nextCursor: string | null;
  warnings: Warning[];
  partial: boolean;
};

const SOURCE_LABEL: Record<Source, string> = {
  all: "すべてのソース",
  mcp: "MCP Registry",
  clawhub: "ClawHub",
};
const WARNING_LABEL: Record<string, string> = {
  MCP_QUERY_TOO_SHORT: "MCPの検索には3文字以上必要です。Skillsのみを表示しています。",
  RATE_LIMITED: "レジストリの利用制限に達しました。時間をおいて再検索してください。",
  UPSTREAM_UNAVAILABLE: "レジストリに接続できません。",
  UPSTREAM_INVALID_SCHEMA: "レジストリの応答形式が変更されました。",
  UPSTREAM_INVALID_JSON: "レジストリの応答を読み取れません。",
  UPSTREAM_CURSOR_LOOP: "ページ移動を継続できません。",
};
const errorMessage = (code: string) =>
  WARNING_LABEL[code] ?? "検索を完了できませんでした。再試行してください。";

function safeSourceUrl(value: string) {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "https:") return null;
    if (
      parsed.hostname !== "clawhub.ai" &&
      parsed.hostname !== "registry.modelcontextprotocol.io"
    ) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

export default function App() {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<Kind>("all");
  const [source, setSource] = useState<Source>("all");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [data, setData] = useState<ListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CatalogItem | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    if (!detailId) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = document.querySelector<HTMLElement>(".detail-panel");
    const close = panel?.querySelector<HTMLButtonElement>(".close-button");
    close?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setDetailId(null);
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      const controls = Array.from(panel.querySelectorAll<HTMLElement>("button:not([disabled]),a[href]"));
      if (controls.length === 0) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        last.focus();
        event.preventDefault();
      } else if (!event.shiftKey && document.activeElement === last) {
        first.focus();
        event.preventDefault();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocus?.focus();
    };
  }, [detailId]);

  const resetPaging = () => {
    setCursor(null);
    setHistory([]);
    setDetailId(null);
  };

  useEffect(() => {
    const abort = new AbortController();
    const params = new URLSearchParams({ q: query, kind, source, limit: "20" });
    if (cursor) params.set("cursor", cursor);
    setLoading(true);
    setError(null);
    fetch("/api/v1/marketplace/items?" + params.toString(), { signal: abort.signal })
      .then(async (res) => {
        if (!res.ok) {
          const payload = (await res.json().catch(() => ({}))) as { detail?: string };
          throw new Error(errorMessage(payload.detail ?? String(res.status)));
        }
        return res.json() as Promise<ListResponse>;
      })
      .then((result) => {
        if (!abort.signal.aborted) setData(result);
      })
      .catch((cause: unknown) => {
        if (!abort.signal.aborted) {
          setData(null);
          setError(cause instanceof Error ? cause.message : "検索に失敗しました");
        }
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [query, kind, source, cursor, refresh]);

  useEffect(() => {
    if (!detailId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    const abort = new AbortController();
    setDetail(null);
    setDetailError(null);
    fetch("/api/v1/marketplace/items/" + encodeURIComponent(detailId), { signal: abort.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error("詳細情報を取得できませんでした");
        return res.json() as Promise<CatalogItem>;
      })
      .then((item) => {
        if (!abort.signal.aborted) setDetail(item);
      })
      .catch((cause: unknown) => {
        if (!abort.signal.aborted) setDetailError(cause instanceof Error ? cause.message : "取得に失敗しました");
      });
    return () => abort.abort();
  }, [detailId]);

  const search = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    resetPaging();
    setQuery(draft.trim());
  };

  const chooseKind = (next: Kind) => {
    setKind(next);
    if (next === "MCP" && source === "clawhub") setSource("all");
    if (next === "SKILL" && source === "mcp") setSource("all");
    resetPaging();
  };

  const chooseSource = (next: Source) => {
    setSource(next);
    if (next === "mcp" && kind === "SKILL") setKind("all");
    if (next === "clawhub" && kind === "MCP") setKind("all");
    resetPaging();
  };

  const nextPage = () => {
    if (!data?.nextCursor || loading) return;
    setHistory((old) => [...old, cursor]);
    setCursor(data.nextCursor);
    setDetailId(null);
  };
  const previousPage = () => {
    if (!history.length || loading) return;
    setCursor(history[history.length - 1]);
    setHistory((old) => old.slice(0, -1));
    setDetailId(null);
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="logo"><span className="logo-mark">✦</span><span>aiagent<span className="logo-muted">.md</span></span></div>
        <div className="section-label">ワークスペース</div>
        <div className="nav-item muted"><span>▦</span> ダッシュボード</div>
        <div className="nav-item muted"><span>◷</span> タスク</div>
        <div className="section-label second-label">拡張機能</div>
        <div className="nav-item active"><span>◈</span> Marketplace</div>
        <div className="nav-item muted" title="MKT-2で実装予定"><span>▤</span> 追加済み <small>準備中</small></div>
        <div className="sidebar-bottom">Marketplace Preview <span>MKT-1</span></div>
      </aside>

      <main className="main">
        <header className="topbar">
          <span>拡張機能 <span className="breadcrumb">/ Marketplace</span></span>
          <span className="preview-pill">閲覧専用プレビュー</span>
        </header>
        <div className="content">
          <div className="eyebrow">EXTENSION MARKETPLACE</div>
          <h1>エージェントに、新しい可能性を。</h1>
          <p className="lead">SkillsとMCPサーバーを検索して、できることを広げましょう。</p>
          <form className="search-form" onSubmit={search}>
            <span className="magnify" aria-hidden="true">⌕</span>
            <input
              aria-label="拡張機能を検索"
              placeholder="拡張機能を検索... 例: GitHub、browser、search"
              value={draft}
              maxLength={200}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button type="submit" className="primary-button">検索</button>
          </form>
          <div className="filters" aria-label="種類で絞り込み">
            {(["all", "SKILL", "MCP"] as Kind[]).map((value) => (
              <button key={value} type="button" className={"filter " + (kind === value ? "selected" : "")} onClick={() => chooseKind(value)}>
                {value === "all" ? "すべて" : value === "SKILL" ? "Skills" : "MCP Servers"}
              </button>
            ))}
            <label className="source-select">
              ソース
              <select aria-label="検索元" value={source} onChange={(event) => chooseSource(event.target.value as Source)}>
                {(Object.keys(SOURCE_LABEL) as Source[]).map((value) => (
                  <option key={value} value={value}>{SOURCE_LABEL[value]}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="results-head">
            <h2>{query ? "「" + query + "」の検索結果" : "見つける"}</h2>
            <span>{loading ? "読み込み中..." : data ? data.items.length + " 件を表示" : ""}</span>
          </div>
          {data?.warnings.map((warning, index) => (
            <p className="warning" role="status" key={warning.source + index}>
              <strong>{warning.source === "mcp" ? "MCP Registry" : "ClawHub"}:</strong> {errorMessage(warning.code)}
            </p>
          ))}
          {error && <div role="alert" className="error">{error} <button type="button" onClick={() => setRefresh((value) => value + 1)}>再試行</button></div>}
          {loading ? (
            <div className="loading" role="status">拡張機能を検索しています...</div>
          ) : data?.items.length ? (
            <div className="card-grid">
              {data.items.map((item) => (
                <article className="extension-card" key={item.id}>
                  <div className="card-top">
                    <span className={"card-icon " + (item.kind === "SKILL" ? "skill" : "mcp")} aria-hidden="true">{item.kind === "SKILL" ? "✳" : "⬡"}</span>
                    <span className={"kind " + (item.kind === "SKILL" ? "skill-kind" : "")}>{item.kind}</span>
                  </div>
                  <h3>{item.name}</h3>
                  <div className="publisher">{item.publisher} · {item.source === "mcp" ? "MCP Registry" : "ClawHub"}</div>
                  <p className="summary">{item.summary || "説明は登録されていません。"}</p>
                  <div className="card-footer">
                    <span className="version">{item.version ? "v" + item.version : "バージョン未取得"}</span>
                    <button type="button" className="subtle-button" onClick={() => setDetailId(item.id)}>詳細を見る</button>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="empty">
              <div className="empty-icon">⌕</div>
              <strong>{data?.partial ? "検索元に接続できませんでした" : "該当する拡張機能が見つかりませんでした"}</strong>
              <p>別のキーワードや検索元を試してください。</p>
            </div>
          )}
          <div className="pagination">
            <button type="button" disabled={!history.length || loading} onClick={previousPage}>前へ</button>
            <span>ページ {history.length + 1}</span>
            <button type="button" disabled={!data?.nextCursor || loading} onClick={nextPage}>次へ</button>
          </div>
          <div className="note"><strong>現在は検索・閲覧のみ</strong> — インストール、認証、更新、削除はMKT-2以降で対応します。外部レジストリに掲載された内容の安全性は保証されません。</div>
        </div>
      </main>

      {detailId && (
        <div className="detail-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setDetailId(null); }}>
          <section className="detail-panel" role="dialog" aria-modal="true" aria-labelledby="detail-title">
            <button type="button" className="close-button" aria-label="詳細を閉じる" onClick={() => setDetailId(null)}>×</button>
            {detailError && <p className="error" role="alert">{detailError}</p>}
            {!detail && !detailError && <p className="loading">詳細を読み込み中...</p>}
            {detail && (
              <>
                <div className="eyebrow">{detail.kind} · {detail.source === "mcp" ? "MCP Registry" : "ClawHub"}</div>
                <h2 id="detail-title">{detail.name}</h2>
                <p className="detail-by">{detail.publisher}</p>
                <p className="detail-summary">{detail.summary || "説明はありません。"}</p>
                <dl className="detail-list">
                  <dt>識別子</dt><dd>{detail.identity}</dd>
                  <dt>バージョン</dt><dd>{detail.version || "未取得"}</dd>
                  <dt>利用方式</dt><dd>{detail.kind === "MCP" ? detail.transport || "不明" : "Skill document"}</dd>
                  <dt>掲載状態</dt><dd>{detail.catalogStatus}</dd>
                </dl>
                <p className="notice">{detail.notice}</p>
                {safeSourceUrl(detail.url) && (
                  <a href={safeSourceUrl(detail.url)!} target="_blank" rel="noopener noreferrer" className="external-link">登録元を確認 ↗</a>
                )}
                <button type="button" className="install-disabled" disabled title="MKT-2で実装予定。現在は追加できません。">追加（MKT-2で対応）</button>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
