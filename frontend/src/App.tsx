import { useEffect, useMemo, useState, type FormEvent } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { AgentPlatformConsole } from "./AgentPlatformConsole";
import { api } from "./api";
import type { ArticleAuthoritySource, AuthoritySearchResult, AuthoritySource, CompetitorLearningDashboard, ContentAsset, ContentAssetDetail, ContentBrief, ContentDraft, ContentEffectiveness, ContentGenerationResult, ContentLearningMemory, ContentLearningMemoryDetail, ContentMemoryItem, ContentOutline, ExpansionResult, LibraryKeyword, Review, Score, SerpTitle, TitleCandidate, WordPressPublication } from "./types";

type RunState = { seeds: string[]; language: string; country: string; result: ExpansionResult } | null;
type ScoreInputs = Record<"keyword" | "volume" | "authority" | "domains" | "titleMatch" | "authoritySites" | "intent" | "relevance" | "businessValue", string>;

const projectKey = "seo-keyword-project-id";
type AiProvider = "openai" | "gemini" | "deepseek";
type AiProfile = { baseUrl: string; model: string; apiKey: string };
type AiAssignments = { keyword_review: AiProvider; title_generation: AiProvider };
type ProjectKnowledgeDocument = { id: number; project_id: number; title: string; source_type: string; url: string; content: string; knowledge_type: string; status: string; summary?: string; tags?: string[]; organizer_provider?: string | null; organizer_model?: string | null; created_at: string; updated_at: string };
type KnowledgeCrawlResult = { id: number; status: string; message: string; accepted_count: number; skipped_count: number; failed_count: number; pages: Array<{ url: string; title: string; knowledge_type: string; status: string; reason: string }> };
async function requestApprovedWordPressPublish(assetId: number, projectId: number, status: "draft" | "publish"): Promise<WordPressPublication> {
  let prepared = await api.prepareWordPressPublish(assetId, { project_id: projectId, status });
  if (prepared.status === "blocked" && prepared.report.issues.length === 1 && prepared.report.issues[0].code === "images") {
    if (window.confirm("当前正文没有 H2 配图。确认无图继续进入发布审批吗？")) {
      prepared = await api.prepareWordPressPublish(assetId, { project_id: projectId, status, allow_without_images: true });
    }
  }
  if (prepared.status !== "ready") throw new Error(prepared.report.issues.map((issue) => issue.message).join("；") || "发布门禁未通过。");
  const confirmation = status === "publish" ? "确认公开发布到网站吗？文章会立即对外可见。" : "确认创建 WordPress 草稿吗？";
  if (!window.confirm(confirmation)) throw new Error("已取消发布确认。");
  if (typeof prepared.approval_id !== "number") throw new Error("发布审批请求未创建。");
  await api.decideAgentApproval(prepared.approval_id, { project_id: projectId, decision: "approved", decided_by: "user" });
  return api.publishWordPress(assetId, { project_id: projectId, status, approval_id: prepared.approval_id });
}
function RecentTitleLibrary({ titles }: { titles: TitleCandidate[] }) { return <section className="recent-title-library"><div><strong>最近入库标题</strong><p>每次生成完成后会自动写入标题库，不会因为刷新或切换页面而丢失。</p></div><NavLink className="primary" to="/title-library">打开标题库（{titles.length} 条）</NavLink>{titles.length ? <div className="recent-title-list">{titles.slice(0, 5).map((title) => <article key={title.id}><ProviderBadge reason={title.reason} /><strong>{title.title}</strong><span>{title.keyword || "—"}</span></article>)}</div> : <p className="empty">暂时还没有标题。生成后会自动写入标题库。</p>}</section>; }
function ContentLibrary({ assets, onDelete }: { assets: ContentAsset[]; onDelete: (assetIds: number[]) => Promise<void> }) {
  const completedAssets = assets.filter((asset) => Boolean(asset.current_draft_id));
  const [selectedAssetIds, setSelectedAssetIds] = useState<number[]>([]);
  const [publishingAssetId, setPublishingAssetId] = useState<number | null>(null);
  const [publishMode, setPublishMode] = useState<"draft" | "publish">("draft");
  const [publishedPosts, setPublishedPosts] = useState<Record<number, WordPressPublication>>({});
  const [publishErrors, setPublishErrors] = useState<Record<number, string>>({});
  useEffect(() => setSelectedAssetIds((current) => current.filter((assetId) => completedAssets.some((asset) => asset.id === assetId))), [assets]);
  const toggleAsset = (assetId: number) => setSelectedAssetIds((current) => current.includes(assetId) ? current.filter((id) => id !== assetId) : [...current, assetId]);
  const deleteSelected = async () => { if (!selectedAssetIds.length || !window.confirm(`确认删除 ${selectedAssetIds.length} 篇已完成内容及其版本吗？`)) return; await onDelete(selectedAssetIds); setSelectedAssetIds([]); };
  const publishAsset = async (asset: ContentAsset) => {
    if (publishingAssetId === asset.id) return;
    setPublishingAssetId(asset.id);
    setPublishErrors((current) => ({ ...current, [asset.id]: "" }));
    try {
      const result = await requestApprovedWordPressPublish(asset.id, asset.project_id, publishMode);
      if (typeof result.wordpress_post_id !== "number") throw new Error("WordPress 未返回文章编号。");
      setPublishedPosts((current) => ({ ...current, [asset.id]: result }));
    } catch (error) {
      setPublishErrors((current) => ({ ...current, [asset.id]: error instanceof Error ? error.message : "发布失败，请先检查内容发布配置。" }));
    } finally {
      setPublishingAssetId(null);
    }
  };
  if (!completedAssets.length) return <div className="content-library-empty"><strong>暂无已完成正文</strong><p>内容资产完成 AI 正文生成后才会显示在这里。可前往“内容系统”继续完成 Brief、大纲和正文。</p><NavLink className="primary" to="/content">前往内容系统</NavLink></div>;
  return <><div className="content-library-bulk"><label><input type="checkbox" checked={selectedAssetIds.length === completedAssets.length} onChange={(event) => setSelectedAssetIds(event.target.checked ? completedAssets.map((asset) => asset.id) : [])} /> 全选已完成内容</label><button className="danger" disabled={!selectedAssetIds.length} onClick={() => void deleteSelected}>删除已选内容（{selectedAssetIds.length}）</button></div><div className="content-library-grid">{completedAssets.map((asset) => <article className={`content-library-card ${selectedAssetIds.includes(asset.id) ? "is-selected" : ""}`} key={asset.id}><div className="content-library-card-top"><label className="asset-checkbox"><input type="checkbox" checked={selectedAssetIds.includes(asset.id)} onChange={() => toggleAsset(asset.id)} aria-label={`选择 ${asset.title_snapshot}`} /></label><span className="content-completion-chip">✓ {asset.content_status_label || "内容完成"}</span><span className="content-outline-chip">✓ {asset.outline_status_label || "大纲完成"}</span></div><h3>{asset.title_snapshot}</h3><p>{asset.keyword || "—"} · {asset.locale}</p>{asset.tags?.length ? <div className="content-asset-tags" aria-label="内容标签">{asset.tags.map((tag) => <span key={tag}>{tag}</span>)}</div> : null}<div className="content-library-card-footer"><span>{asset.status === "completed" ? "已完成" : "已生成"}</span><div className="actions"><select aria-label="选择发布方式" value={publishMode} onChange={(event) => setPublishMode(event.target.value as "draft" | "publish")}><option value="draft">发布到草稿箱</option><option value="publish">直接发布</option></select>{publishedPosts[asset.id] ? <span className="wordpress-publication-result">{publishedPosts[asset.id].status === "publish" ? "发布成功" : "草稿已创建"} #{publishedPosts[asset.id].wordpress_post_id}{publishedPosts[asset.id].wordpress_url ? <a href={publishedPosts[asset.id].wordpress_url!} target="_blank" rel="noreferrer">打开页面 ↗</a> : null}</span> : <button className="primary" disabled={publishingAssetId === asset.id} onClick={() => void publishAsset(asset)}>{publishingAssetId === asset.id ? "正在发布…" : "一键发布"}</button>}<button className="link danger" onClick={() => void onDelete([asset.id])}>删除</button><NavLink className="primary" to={`/content-library/${asset.id}`}>阅读全文</NavLink></div>{publishErrors[asset.id] ? <p className="content-library-publish-error" role="status">发布失败：{publishErrors[asset.id]}</p> : null}</div></article>)}</div></>;
}
function articleWordCount(markdown: string) { return markdown.replace(/```[\s\S]*?```/g, " ").match(/\b[\w]+(?:['’-][\w]+)?\b/g)?.length || 0; }
function downloadArticleHtml(draft: ContentDraft, authoritySources: ArticleAuthoritySource[] = [], sectionImages: import("./types").SectionImage[] = []) {
  const title = draft.title || "SEO Article";
  const references = authoritySources.length ? `<section class="references"><h2>Authority Sources &amp; Verification</h2><ol>${authoritySources.map((source) => `<li>${source.url ? `<a href="${escapeHtml(source.url)}" rel="nofollow noopener noreferrer" target="_blank">${escapeHtml(source.title)}</a>` : escapeHtml(source.title)}${source.publisher ? ` — ${escapeHtml(source.publisher)}` : ""}${source.claim_topic ? `<br><small>Supports: ${escapeHtml(source.claim_topic)}</small>` : ""}</li>`).join("")}</ol></section>` : "";
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="${escapeHtml(draft.meta_description || "")}"><title>${escapeHtml(title)}</title><style>body{margin:0;background:#f4f7fb;color:#33435e;font-family:"Segoe UI Variable","PingFang SC",Arial,sans-serif;line-height:1.82}.article{max-width:790px;margin:40px auto;padding:42px;background:#fff;border:1px solid #e2e8f1;border-radius:18px}.eyebrow{color:#7089c6;font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.article h1,.article h2,.article h3{color:#243e6b;line-height:1.25}.article h1{font-size:2.5rem;letter-spacing:-.04em}.article h2{margin-top:2.1em;font-size:1.55rem}.article h3{margin-top:1.7em}.article figure{margin:1.5em 0}.article figure img{width:100%;border-radius:12px}.article figcaption{display:flex;align-items:flex-start;gap:7px;padding-top:8px;font-size:.85rem;color:#647591;line-height:1.45}.article-image-caption-label{display:inline-block;flex:0 0 auto;padding:2px 6px;border-radius:999px;background:#e2f2ee;color:#176b5a;font-size:10px;font-weight:750;line-height:1.35;white-space:nowrap}.article table{width:100%;border-collapse:collapse;margin:1.6em 0}.article th,.article td{padding:10px 12px;border:1px solid #dce4ef;text-align:left;vertical-align:top}.article th{background:#eff3ff}.article li{margin:.35em 0}.references{margin-top:3em;padding-top:1.5em;border-top:2px solid #dce4ef}.references small{color:#647591}@media(max-width:700px){.article{margin:0;border:0;border-radius:0;padding:24px}.article h1{font-size:2rem}}</style></head><body><main class="article"><p class="eyebrow">${escapeHtml(draft.provider || "AI")} · v${draft.version}</p><h1>${escapeHtml(title)}</h1>${draft.meta_description ? `<p>${escapeHtml(draft.meta_description)}</p>` : ""}<hr>${renderMarkdownPreview(draft.markdown, sectionImages, title)}${references}</main></body></html>`;
  const blob = new Blob([html], { type: "text/html;charset=utf-8" }); const url = URL.createObjectURL(blob); const link = document.createElement("a");
  link.href = url; link.download = `${title.replace(/[<>:"/\\|?*\x00-\x1F]/g, "-").slice(0, 100) || "article"}.html`; document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
}
function AuthoritySourceLibrary({ projectId }: { projectId: number | null }) {
  const [items, setItems] = useState<AuthoritySource[]>([]); const [title, setTitle] = useState(""); const [content, setContent] = useState(""); const [url, setUrl] = useState(""); const [publisher, setPublisher] = useState(""); const [sourceType, setSourceType] = useState<AuthoritySource["source_type"]>("first_party"); const [notice, setNotice] = useState(""); const [saving, setSaving] = useState(false);
  const load = async () => { if (projectId) setItems(await api.listAuthoritySources(projectId)); };
  useEffect(() => { void load(); }, [projectId]);
  const save = async () => { if (!projectId || !title.trim() || !content.trim()) return; setSaving(true); try { await api.createAuthoritySource({ project_id: projectId, title: title.trim(), source_type: sourceType, url: url.trim(), publisher: publisher.trim(), content: content.trim(), provider: "openai" }); setTitle(""); setContent(""); setUrl(""); setPublisher(""); setNotice("来源已归类并写入当前网站的长期来源库。"); await load(); } catch (error) { setNotice(error instanceof Error ? error.message : "来源入库失败。"); } finally { setSaving(false); } };
  const remove = async (id: number) => { if (!projectId || !window.confirm("删除这条长期来源吗？")) return; await api.deleteAuthoritySource(id, projectId); await load(); };
  if (!projectId) return <p className="empty">请先选择网站项目。</p>;
  return <section className="authority-library"><PanelTitle eyebrow="Authority Source Library" title="权威来源库" tag={`${items.length} 条长期记忆`} /><p className="hint">保存官网规格书、标准、认证、政府资料或行业研究。AI 会分析可信度、标签和可支持的主题；后续一键生成内容会优先读取当前网站的这些资料。</p><div className="authority-source-form"><Input label="来源标题" value={title} onChange={setTitle} /><Select label="来源类型" value={sourceType} onChange={(value) => setSourceType(value as AuthoritySource["source_type"])} options={[["first_party", "第一方规格书 / 报告"], ["standard", "标准 / 规范"], ["certification", "认证 / 检测"], ["government", "政府 / 监管"], ["industry_research", "行业研究"]]} /><Input label="权威 URL（可选）" value={url} onChange={setUrl} /><Input label="发布者（可选）" value={publisher} onChange={setPublisher} /><label>来源正文 / 摘录<textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder="粘贴规格书、证书内容、官方说明或权威研究正文。AI 将分类入库，不会伪造来源。" /></label><button className="primary" disabled={saving || !title.trim() || !content.trim()} onClick={() => void save()}>{saving ? "AI 归类中…" : "AI 归类并长期入库"}</button></div><p className="tag">{notice}</p><div className="authority-source-grid">{items.map((item) => <article key={item.id}><div><span className={`authority-level ${item.authority_level}`}>{item.authority_level}</span><span>{item.source_type}</span></div><h3>{item.title}</h3><p>{item.summary || "尚未生成摘要。"}</p><small>{item.publisher || "未标注发布者"}{item.url ? ` · ${item.url}` : ""}</small>{item.tags.length ? <div className="authority-tags">{item.tags.map((tag) => <i key={tag}>{tag}</i>)}</div> : null}<button className="link danger" onClick={() => void remove(item.id)}>删除</button></article>)}</div>{items.length ? null : <p className="empty">还没有来源。先把产品规格书、认证或权威标准资料粘贴进来。</p>}</section>;
}
function ArticleAuthorityReferences({ sources }: { sources: ArticleAuthoritySource[] }) {
  return <section className="article-authority-references"><p className="eyebrow">Verification References</p><h2>权威来源与验证依据</h2>{sources.length ? <ol>{sources.map((source) => <li key={source.id}><div><span className={`authority-level ${source.authority_level}`}>{source.authority_level}</span>{source.url ? <a href={source.url} target="_blank" rel="nofollow noopener noreferrer">{source.title}</a> : <strong>{source.title}</strong>}</div><p>{source.publisher || "已验证公开来源"}{source.claim_topic ? ` · 支持论断：${source.claim_topic}` : ""}</p>{source.summary ? <small>{source.summary}</small> : null}</li>)}</ol> : <p className="empty">暂无已验证的权威来源。点击“寻找权威来源”后，只有实际可打开且通过相关性与权威性检查的链接才会显示在这里。</p>}</section>;
}
function AuthoritySearchCandidates({ results }: { results: AuthoritySearchResult[] }) {
  if (!results.length) return null;
  return <section className="authority-search-candidates"><p className="eyebrow">Authority Search Audit</p><h2>本次权威来源候选页面</h2><p>Google 优先；可用 HTML 候选不足时自动补充 Bing。每条候选和跳过原因都会保留，PDF、表格与其他下载文件不会引用。</p><ol>{results.map((result) => { const searchUrl = `https://www.google.com/search?q=${encodeURIComponent(result.search_query)}`; return <li key={result.id} className={result.status}><span>{result.status === "accepted" ? "已引用" : result.status === "skipped" ? "已跳过" : result.status === "search_error" ? "搜索失败" : "待验证"}</span><div>{result.url ? <a href={result.url} target="_blank" rel="nofollow noopener noreferrer">{result.title || result.url}</a> : <a className="authority-search-query" href={searchUrl} target="_blank" rel="nofollow noopener noreferrer">{result.section_heading || "打开 Google 搜索"}</a>}<small>{result.url ? result.domain : "点击标题可打开本次 Google 搜索"}</small>{result.error_summary ? <p>原因：{result.error_summary}</p> : null}</div></li>; })}</ol></section>;
}
function GscIntegrationCard({ projectId }: { projectId: number | null }) {
  const [anchors, setAnchors] = useState<Array<{ query: string; page_url: string; clicks: number; impressions: number; position: number }>>([]);
  const [performance, setPerformance] = useState<Array<{ id: number; content_asset_id: number; title_snapshot: string; clicks: number; impressions: number; ctr: number; average_position: number; query_count: number; learning_status: "observing" | "qualified" | "insufficient"; summary: string }>>([]);
  const [effectiveness, setEffectiveness] = useState<ContentEffectiveness | null>(null);
  const [status, setStatus] = useState("打开 Chrome 后，在 Google 官方页面自行登录并进入 Search Console 报表。");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!projectId) { setAnchors([]); setPerformance([]); setEffectiveness(null); return; }
    api.listProjectGscAnchors(projectId).then(({ anchors: savedAnchors }) => {
      setAnchors(savedAnchors);
      if (savedAnchors.length) setStatus(`已保存 ${savedAnchors.length} 条当前网站的 GSC 锚文本建议。`);
    }).catch((error: unknown) => setStatus(error instanceof Error ? error.message : "读取已采集的 GSC 数据失败。"));
    api.listPublishedGscPerformance(projectId).then(setPerformance).catch(() => setPerformance([]));
    api.getContentEffectiveness(projectId).then(setEffectiveness).catch(() => setEffectiveness(null));
  }, [projectId]);
  if (!projectId) return <p className="empty">请先选择网站项目。</p>;
  const openBrowser = async () => { setSaving(true); try { const result = await api.openProjectGscBrowser(projectId); setStatus(`${result.message} 系统不会读取密码、Cookie 或浏览器保存的登录信息。`); } catch (error) { setStatus(error instanceof Error ? error.message : "无法打开 GSC 浏览器。"); } finally { setSaving(false); } };
  const captureBrowser = async () => { setSaving(true); setStatus("正在读取 Chrome 中当前可见的 Search Console 英语查询表格…"); try { const result = await api.captureProjectGscBrowser(projectId); setAnchors(result.anchors); setStatus(`已从当前 GSC 页面采集 ${result.synced || 0} 条英语查询记录，并保存为当前网站的锚文本建议。`); } catch (error) { setStatus(error instanceof Error ? error.message : "GSC 浏览器采集失败。"); } finally { setSaving(false); } };
  const captureRankedPages = async () => { if (!projectId) return; setSaving(true); setAnchors([]); setStatus("已清空当前网站旧的 GSC 查询与网页链接记录，正在从美国最近 7 天查询表筛选英语且平均排名 40 以内的词…"); try { const result = await api.captureProjectGscRankedPages(projectId); setAnchors(result.anchors); const audit = result.capture; setStatus(`已清空 ${audit?.cleared || 0} 条旧记录，并补齐 ${result.synced || 0} 条“英语查询词 → 网页 URL”记录；检查 ${audit?.queries_checked || 0} 个排名 40 以内的英语查询词，排除 ${audit?.queries_non_english || 0} 个非英语或混合语言查询词，${audit?.queries_without_page || 0} 个暂无网页数据。`); } catch (error) { setStatus(error instanceof Error ? error.message : "GSC 排名与网页链接采集失败。"); } finally { setSaving(false); } };
  const learnPublishedContent = async () => { if (!projectId) return; setSaving(true); setStatus("正在将已发布内容与当前 GSC 网页数据匹配，并保存可追溯表现快照…"); try { const result = await api.learnFromPublishedGscContent(projectId, { days: 7 }); const [nextPerformance, nextEffectiveness] = await Promise.all([api.listPublishedGscPerformance(projectId), api.getContentEffectiveness(projectId)]); setPerformance(nextPerformance); setEffectiveness(nextEffectiveness); setStatus(`${result.message} 本次：${result.snapshots.length} 篇内容已记录，${result.memories_created} 条形成效果记忆。`); } catch (error) { setStatus(error instanceof Error ? error.message : "发布后效果学习失败。请先采集 GSC 查询与网页链接。"); } finally { setSaving(false); } };
  const score = (value: number | null, maximum: number) => value === null ? "观察中" : `${value} / ${maximum}`;
  const performanceStatus = (value: "qualified" | "observing" | "insufficient") => value === "qualified" ? "数据合格" : value === "insufficient" ? "样本不足" : "观察中";
  return <section className="gsc-integration-card">
    <div><p className="eyebrow">Google Search Console</p><h2>Google 登录、排名与网页链接</h2><p>系统只读取你在专用 Chrome 中已打开的 GSC 效果报表，不读取密码、Cookie 或令牌。默认读取美国最近 7 天数据，只保留英语查询，并将平均排名 40 以内的查询词逐个切换到“网页”维度，保存真实的关键词与 URL 对应关系。</p></div>
    <section className="gsc-browser-mode"><div><strong>GSC 浏览器采集</strong><p>1. 打开 Chrome 并自行登录 Google；2. 在 Search Console 打开“效果 / Performance”；3. 系统固定查询美国最近 7 天，并开启点击、展示、平均排名；4. 自动排除中文及其他语言或混合语言查询；5. 点击“补齐排名≤40的网页链接”。当前查询表应尽量显示更多行（建议每页 100 或 500 行）。</p></div><div className="actions"><button className="primary" onClick={() => void openBrowser()} disabled={saving}>打开 Chrome 登录 GSC</button><button onClick={() => void captureRankedPages()} disabled={saving}>补齐排名≤40的网页链接</button><button onClick={() => void captureBrowser()} disabled={saving}>采集当前表格</button>{anchors.length ? <a className="gsc-export" href={`/api/projects/${projectId}/gsc/export.csv`}>导出 Excel（CSV）</a> : null}</div></section>
    <section className="gsc-browser-mode"><div><strong>发布后内容学习</strong><p>先采集 GSC 查询与网页链接，再匹配已公开发布的文章。首次仅建立观察快照；至少间隔 7 天的第二次有效快照，且展现与查询量足够时，才会写入可用于后续内容生成的效果记忆。</p></div><div className="actions"><button className="primary" onClick={() => void learnPublishedContent()} disabled={saving}>更新发布后效果学习</button></div></section>
    <p className="gsc-status">{status}</p>
    <section className="content-effectiveness" aria-labelledby="content-effectiveness-title">
      <header><div><p className="eyebrow">Content Quality × Search Performance</p><h3 id="content-effectiveness-title">内容质量与实际排名效果</h3><p>质量分来自当前已发布版本的 QA、待验证事实、权威来源、H2 结构和正文深度；表现分仅使用合格的 GSC 快照。相关性只描述同向变化，不证明内容质量单独导致排名变化。</p></div><span className={`content-effectiveness-state ${effectiveness?.summary.correlation_state || "insufficient"}`}>{effectiveness?.summary.correlation_state === "descriptive" ? "关联性已可观察" : "样本观察中"}</span></header>
      {effectiveness ? <>
        <dl className="content-effectiveness-summary"><div><dt>已公开发布</dt><dd>{effectiveness.summary.published_articles}</dd><span>篇文章</span></div><div><dt>合格表现数据</dt><dd>{effectiveness.summary.qualified_articles}</dd><span>篇文章</span></div><div><dt>Spearman 相关系数</dt><dd>{effectiveness.summary.spearman_correlation === null ? "—" : effectiveness.summary.spearman_correlation.toFixed(3)}</dd><span>{effectiveness.summary.spearman_correlation === null ? "至少需要 5 篇" : "仅作描述性观察"}</span></div></dl>
        <p className="content-effectiveness-note">{effectiveness.summary.note}</p>
        {effectiveness.articles.length ? <div className="table-wrap content-effectiveness-table"><table><thead><tr><th>已发布内容</th><th>质量分</th><th>GSC 表现分</th><th>综合分</th><th>平均排名</th><th>CTR</th><th>展示</th><th>查询数</th><th>评分明细</th></tr></thead><tbody>{effectiveness.articles.map((article) => <tr key={article.content_asset_id}><td><a href={article.published_url} target="_blank" rel="noreferrer">{article.title}</a><small>{performanceStatus(article.performance_status)}</small></td><td><strong>{score(article.quality_score, 100)}</strong></td><td>{score(article.performance_score, 45)}</td><td>{article.combined_score === null ? "—" : `${article.combined_score} / 100`}</td><td>{article.latest_snapshot?.average_position ? article.latest_snapshot.average_position.toFixed(1) : "—"}</td><td>{article.latest_snapshot ? `${(article.latest_snapshot.ctr * 100).toFixed(1)}%` : "—"}</td><td>{article.latest_snapshot?.impressions ?? "—"}</td><td>{article.latest_snapshot?.query_count ?? "—"}</td><td><details><summary>查看</summary><ul>{article.quality_breakdown.map((item) => <li key={`q-${item.key}`}><b>{item.label}</b><span>{item.points}/{item.max} · {item.note}</span></li>)}{article.performance_breakdown.length ? article.performance_breakdown.map((item) => <li key={`p-${item.key}`}><b>{item.label}</b><span>{item.points}/{item.max} · {item.note}</span></li>) : <li><span>等待第二次合格 GSC 快照后计算表现分。</span></li>}</ul></details></td></tr>)}</tbody></table></div> : <p className="empty">还没有公开发布的文章。发布后采集 GSC 数据，即可在这里建立质量与表现的观察记录。</p>}
      </> : <p className="empty">正在读取当前项目的已发布内容与 GSC 快照…</p>}
    </section>
    {performance.length ? <section className="gsc-performance-list"><h3>已发布内容的效果学习</h3>{performance.slice(0, 8).map((item) => <article key={item.id}><div><strong>{item.title_snapshot}</strong><p>{item.summary}</p></div><span className={`tag ${item.learning_status}`}>{item.learning_status === "qualified" ? "已形成效果记忆" : item.learning_status === "observing" ? "首次观察" : "数据暂不足"}</span></article>)}</section> : null}
    {anchors.length ? <div className="table-wrap gsc-anchor-table"><table><thead><tr><th>建议锚文本（真实查询）</th><th>对应网页 URL</th><th>点击</th><th>展现</th><th>平均排名</th></tr></thead><tbody>{anchors.slice(0, 20).map((item) => <tr key={`${item.query}-${item.page_url}`}><td><strong>{item.query}</strong></td><td><a href={item.page_url} target="_blank" rel="noreferrer">{item.page_url}</a></td><td>{item.clicks}</td><td>{item.impressions}</td><td>{item.position.toFixed(1)}</td></tr>)}</tbody></table></div> : null}
  </section>;
}
function ContentImageStudio({ projectId }: { projectId: number | null }) {
  const { assetId } = useParams(); const [images, setImages] = useState<import("./types").SectionImage[]>([]); const [status, setStatus] = useState(""); const [generating, setGenerating] = useState(false); const [publishMode, setPublishMode] = useState<"draft" | "publish">("draft"); const [publication, setPublication] = useState<WordPressPublication | null>(null);
  const load = async () => { if (projectId && assetId) { const item = await api.getContentAsset(Number(assetId), projectId); setImages(item.section_images || []); } };
  useEffect(() => { void load(); }, [projectId, assetId]);
  const generateAll = async () => { if (!projectId || !assetId || generating) return; setGenerating(true); setStatus("正在为全部 H2 生成图片、SEO 文件名和 alt 标签…"); try { const result = await api.generateAllSectionImages(Number(assetId), projectId); await load(); window.dispatchEvent(new Event("section-images-updated")); setStatus(`已生成 ${result.generated.length} 张并自动插入正文；${result.failed.length ? `${result.failed.length} 张失败，可单独重试。` : ""}`); } catch (error) { setStatus(error instanceof Error ? error.message : "一键生成图片失败；请确认 OpenAI 兼容接口支持 images/generations。"); await load(); } finally { setGenerating(false); } };
  const generate = async (id: number) => { if (!projectId) return; try { await api.generateSectionImage(id, projectId); await load(); window.dispatchEvent(new Event("section-images-updated")); setStatus("图片已生成并插入对应 H2。"); } catch (error) { setStatus(error instanceof Error ? error.message : "图片生成失败。"); await load(); } };
  const publish = async () => { if (!projectId || !assetId) return; try { const result = await requestApprovedWordPressPublish(Number(assetId), projectId, publishMode); setPublication(result); setStatus(result.status === "publish" ? `文章已发布到 WordPress #${result.wordpress_post_id}。` : `已创建 WordPress 草稿 #${result.wordpress_post_id}。`); } catch (error) { setStatus(error instanceof Error ? error.message : "WordPress 发布失败；请先在当前网站的“内容发布”中保存并测试后台登录。"); } };
  return <section className="content-image-studio"><div><p className="eyebrow">SEO Article Images</p><h2>一键生成并自动插入 H2 配图</h2><p>每个 H2 自动生成一张原创图片，保存 SEO 友好英文文件名并写入自然 alt 标签；图片会显示在对应正文小节下方。</p></div><div className="actions"><button className="primary" disabled={generating} onClick={() => void generateAll()}>{generating ? "正在批量生成图片…" : "一键生成全文配图"}</button><select aria-label="选择发布方式" value={publishMode} onChange={(event) => setPublishMode(event.target.value as "draft" | "publish")}><option value="draft">发布到草稿箱</option><option value="publish">直接发布</option></select><button onClick={() => void publish()}>一键发布</button></div><p className="tag">{status}</p>{publication ? <div className={`wordpress-publication-banner ${publication.status}`} role="status"><strong>{publication.status === "publish" ? "发布成功" : "草稿已创建"}</strong><span>WordPress 文章 #{publication.wordpress_post_id}</span>{publication.wordpress_url ? <a href={publication.wordpress_url} target="_blank" rel="noreferrer">{publication.status === "publish" ? "打开已发布页面 ↗" : "打开草稿编辑页 ↗"}</a> : null}</div> : null}{images.map((image) => <article key={image.id}><div><strong>{image.position}. {image.section_heading}</strong><small>alt: {image.alt_text}</small><small>文件名: {image.seo_filename || "生成中"}</small>{image.error_summary ? <p className="verify-warning">{image.error_summary}</p> : null}</div><div>{image.image_url ? <img src={image.image_url} alt={image.alt_text} /> : <button onClick={() => void generate(image.id)}>{image.status === "failed" ? "重试此图片" : "生成此图片"}</button>}<span className="tag">{image.status}</span></div></article>)}</section>;
}
function ContentReader({ projectId }: { projectId: number | null }) {
  const { assetId } = useParams();
  const [detail, setDetail] = useState<ContentAssetDetail | null>(null);
  const [readerError, setReaderError] = useState("");
  const [researchingSources, setResearchingSources] = useState(false);
  const [generatingImages, setGeneratingImages] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publishMode, setPublishMode] = useState<"draft" | "publish">("draft");
  const [publication, setPublication] = useState<WordPressPublication | null>(null);
  const [sourceResearchNotice, setSourceResearchNotice] = useState("");
  useEffect(() => { const refresh = () => { if (projectId && assetId) { setReaderError(""); api.getContentAsset(Number(assetId), projectId).then(setDetail).catch((error: unknown) => setReaderError(error instanceof Error ? error.message : "文章读取失败。")); } }; refresh(); window.addEventListener("section-images-updated", refresh); return () => window.removeEventListener("section-images-updated", refresh); }, [assetId, projectId]);
  if (readerError) return <p className="empty">{readerError}</p>;
  if (!detail?.current_draft) return <p className="empty">正在加载文章，或该内容尚未生成正文。</p>;
  const draft = detail.current_draft;
  const wordCount = articleWordCount(draft.markdown); const characterCount = draft.markdown.replace(/\s/g, "").length; const authoritySources = detail.authority_sources || []; const authoritySearchResults = detail.authority_search_results || []; const sectionImages = detail.section_images || [];
  const researchSources = async () => {
    if (!projectId || !assetId || researchingSources) return;
    setResearchingSources(true);
    setSourceResearchNotice("正在检索白名单权威来源，并逐条验证网页是否可打开、可读取且适合引用。");
    try {
      const result = await api.researchAuthoritySources({ project_id: projectId, asset_id: Number(assetId), provider: "gemini" });
      setSourceResearchNotice(`Gemini 已推荐并逐条复核 ${result.candidates_checked} 个候选页面，已入库 ${result.saved.length} 条可引用来源；跳过 ${result.skipped.length} 条无关、不可读取、非正文或未通过 AI 验证的页面。`);
      setDetail(await api.getContentAsset(Number(assetId), projectId));
    } catch (error) {
      setSourceResearchNotice(`权威来源研究失败：${error instanceof Error ? error.message : "请稍后重试。"}`);
    } finally { setResearchingSources(false); }
  };
  const generateAllImages = async () => { if (!projectId || !assetId || generatingImages) return; setGeneratingImages(true); setSourceResearchNotice("正在为全部 H2 生成配图、SEO 文件名和 alt 标签…"); try { const result = await api.generateAllSectionImages(Number(assetId), projectId); setDetail(await api.getContentAsset(Number(assetId), projectId)); window.dispatchEvent(new Event("section-images-updated")); setSourceResearchNotice(`已生成 ${result.generated.length} 张 H2 配图并自动插入正文；${result.failed.length ? `${result.failed.length} 张失败，可在下方配图区单独重试。` : ""}`); } catch (error) { setSourceResearchNotice(`配图生成失败：${error instanceof Error ? error.message : "请稍后重试。"}`); } finally { setGeneratingImages(false); } };
  const publishToWordPress = async () => {
    if (!projectId || !assetId || publishing) return;
    setPublishing(true);
    setSourceResearchNotice(publishMode === "publish" ? "正在检查发布门禁并发布到 WordPress…" : "正在检查发布门禁并创建 WordPress 草稿…");
    try {
      const result = await requestApprovedWordPressPublish(Number(assetId), projectId, publishMode);
      setPublication(result);
      setSourceResearchNotice(result.status === "publish" ? `文章已发布到 WordPress #${result.wordpress_post_id}。` : `已创建 WordPress 草稿 #${result.wordpress_post_id}。`);
      setDetail(await api.getContentAsset(Number(assetId), projectId));
    } catch (error) {
      setSourceResearchNotice(`WordPress 发布失败：${error instanceof Error ? error.message : "请先在当前网站的内容发布页保存并测试后台登录。"}`);
    } finally {
      setPublishing(false);
    }
  };
  const latestPublication = publication || detail.wordpress_publications?.[0] || null;
  return <article className="content-reader"><header className="content-reader-header"><NavLink to="/content-library">← 返回所有内容</NavLink><div className="content-reader-actions"><span title="按英文单词规则统计正文">{wordCount.toLocaleString()} words · {characterCount.toLocaleString()} chars</span><select aria-label="选择发布方式" value={publishMode} onChange={(event) => setPublishMode(event.target.value as "draft" | "publish")}><option value="draft">发布到草稿箱</option><option value="publish">直接发布</option></select><button type="button" className="primary" disabled={publishing} onClick={() => void publishToWordPress()}>{publishing ? "正在发布…" : "一键发布"}</button><button type="button" disabled={researchingSources} onClick={() => void researchSources()}>{researchingSources ? "验证权威链接中…" : "寻找权威来源"}</button><button type="button" className="primary" disabled={generatingImages} onClick={() => void generateAllImages()}>{generatingImages ? "正在一键配图…" : "一键生成全文配图"}</button><button type="button" onClick={() => downloadArticleHtml(draft, authoritySources, sectionImages)}>下载 HTML</button></div>{sourceResearchNotice ? <p className="source-research-notice" role="status">{sourceResearchNotice}</p> : null}{latestPublication ? <div className={`wordpress-publication-banner ${latestPublication.status}`} role="status"><strong>{latestPublication.status === "publish" ? "发布成功" : "草稿已创建"}</strong><span>WordPress 文章 #{latestPublication.wordpress_post_id}</span>{latestPublication.wordpress_url ? <a href={latestPublication.wordpress_url} target="_blank" rel="noreferrer">{latestPublication.status === "publish" ? "打开已发布页面 ↗" : "打开草稿编辑页 ↗"}</a> : null}</div> : null}<AuthoritySearchCandidates results={authoritySearchResults} /><p className="eyebrow">{draft.provider || "AI"} · v{draft.version} · {draft.qa_status}</p><h1>{draft.title}</h1>{detail.tags?.length ? <div className="content-asset-tags content-reader-tags" aria-label="内容标签">{detail.tags.map((tag) => <span key={tag}>{tag}</span>)}</div> : null}<p className="content-reader-description">{draft.meta_description}</p></header><article className="markdown-preview" dangerouslySetInnerHTML={{ __html: renderMarkdownPreview(draft.markdown, sectionImages, draft.title) }} />{draft.unresolved_verify?.length ? <p className="verify-warning">待验证：{draft.unresolved_verify.join("；")}</p> : null}<ArticleAuthorityReferences sources={authoritySources} /></article>;
}
function escapeHtml(value: string) { return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;").replace(/'/g, "&#39;"); }
function renderInlineMarkdown(value: string) { return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/__(.+?)__/g, "<strong>$1</strong>"); }
function tableCells(line: string) { return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim()); }
function isTableDivider(line: string) { const cells = tableCells(line); return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell)); }
function renderMarkdownPreview(markdown: string, sectionImages: import("./types").SectionImage[] = [], articleTitle = "") {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const output: string[] = [];
  const isList = (line: string) => /^[-*+]\s+/.test(line) || /^\d+[.)]\s+/.test(line);
  const isBlockStart = (index: number) => /^#{1,3}\s+/.test(lines[index]) || isList(lines[index]) || (lines[index].includes("|") && isTableDivider(lines[index + 1] || ""));
  for (let index = 0; index < lines.length;) {
    const line = lines[index].trim();
    if (!line) { index += 1; continue; }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) { const level = heading[1].length === 1 ? 2 : heading[1].length; const normaliseHeading = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); const isRepeatedArticleTitle = normaliseHeading(heading[2]) === normaliseHeading(articleTitle); output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`); const image = level === 2 && !isRepeatedArticleTitle ? sectionImages.find((item) => item.status === "ready" && item.image_url && item.section_heading.trim().toLowerCase() === heading[2].trim().toLowerCase()) : undefined; if (image?.image_url) output.push(`<figure class="article-section-image"><img src="${escapeHtml(image.image_url)}" alt="${escapeHtml(image.alt_text)}" loading="lazy"><figcaption><span class="article-image-caption-label">图片说明</span><span>${escapeHtml(image.alt_text)}</span></figcaption></figure>`); index += 1; continue; }
    if (line.includes("|") && isTableDivider(lines[index + 1] || "")) {
      const header = tableCells(line); index += 2;
      const rows: string[][] = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) { rows.push(tableCells(lines[index])); index += 1; }
      output.push(`<table><thead><tr>${header.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${header.map((_, column) => `<td>${renderInlineMarkdown(row[column] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table>`); continue;
    }
    if (isList(line)) {
      const ordered = /^\d+[.)]\s+/.test(line); const pattern = ordered ? /^\d+[.)]\s+/ : /^[-*+]\s+/; const items: string[] = [];
      while (index < lines.length && pattern.test(lines[index].trim())) { items.push(`<li>${renderInlineMarkdown(lines[index].trim().replace(pattern, ""))}</li>`); index += 1; }
      output.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`); continue;
    }
    const paragraph: string[] = [line]; index += 1;
    while (index < lines.length && lines[index].trim() && !isBlockStart(index)) { paragraph.push(lines[index].trim()); index += 1; }
    output.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
  }
  return output.join("");
}
function contentProviderLabel(provider: AiProvider | string | undefined) { return ({ openai: "ChatGPT", gemini: "Gemini", deepseek: "DeepSeek", auto_collaborate: "自动协作（DeepSeek → ChatGPT）" } as Record<string, string>)[provider || ""] || provider || "AI"; }
function contentStageLabel(stage: string | null | undefined) { return ({ semantic: "语义分析", title: "标题与元信息", outline: "文章大纲", company_context_plan: "公司资料植入规划", chapter_plan: "章节写作规划", section: "章节写作", assembly: "组装全文", configuration: "模型配置", preparation: "任务准备" } as Record<string, string>)[stage || ""] || stage || "生成"; }
type PlatformWebsite = { id: number; domain: string; industry: string; audience: string; brand_tone: string };
type PlatformTask = { id: number; website_id: number; domain: string; target_keyword: string; title: string; status: string; stage: string; progress: number; message: string };
type PlatformDashboard = { services: Record<string, string>; summary: { websites: number; tasks: number; waiting_for_sources: number; active_tasks: number } };
const platformApiUrl = "http://127.0.0.1:8010";

function LegacyAgentPlatformConsole() {
  const [dashboard, setDashboard] = useState<PlatformDashboard | null>(null);
  const [websites, setWebsites] = useState<PlatformWebsite[]>([]);
  const [tasks, setTasks] = useState<PlatformTask[]>([]);
  const [notice, setNotice] = useState("正在连接 Docker 平台服务…");
  const [domain, setDomain] = useState(""); const [industry, setIndustry] = useState(""); const [audience, setAudience] = useState(""); const [tone, setTone] = useState("");
  const [websiteId, setWebsiteId] = useState(""); const [keyword, setKeyword] = useState(""); const [title, setTitle] = useState("");
  const request = async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const response = await fetch(`${platformApiUrl}${path}`, { headers: { "Content-Type": "application/json" }, ...init });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "平台服务请求失败");
    return body as T;
  };
  const refresh = async () => {
    try {
      const [summary, sites, queuedTasks] = await Promise.all([request<PlatformDashboard>("/api/dashboard"), request<PlatformWebsite[]>("/api/websites"), request<PlatformTask[]>("/api/tasks")]);
      setDashboard(summary); setWebsites(sites); setTasks(queuedTasks); setWebsiteId((current) => current || (sites[0] ? String(sites[0].id) : "")); setNotice("平台已连接。任务会先进入 Celery，再等待资料来源后才能开始 SERP 与写作。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "无法连接平台服务，请确认 Docker 已启动。"); }
  };
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), 8000); return () => window.clearInterval(timer); }, []);
  const createWebsite = async () => {
    try { const site = await request<PlatformWebsite>("/api/websites", { method: "POST", body: JSON.stringify({ domain, industry, audience, brand_tone: tone }) }); setDomain(""); setIndustry(""); setAudience(""); setTone(""); setWebsiteId(String(site.id)); setNotice(`站点 ${site.domain} 已创建并隔离。`); await refresh(); } catch (error) { setNotice(error instanceof Error ? error.message : "创建站点失败"); }
  };
  const createTask = async () => {
    if (!websiteId) { setNotice("请先创建或选择一个站点。"); return; }
    try { await request<PlatformTask>("/api/tasks", { method: "POST", body: JSON.stringify({ website_id: Number(websiteId), target_keyword: keyword, title }) }); setKeyword(""); setTitle(""); setNotice("任务已进入 Celery。尚未接入 SERP/来源服务时，任务会诚实停在“待补来源”。"); await refresh(); } catch (error) { setNotice(error instanceof Error ? error.message : "创建任务失败"); }
  };
  const serviceLabel = (name: string) => ({ api: "FastAPI", database: "PostgreSQL", queue: "Redis", orchestrator: "编排器" } as Record<string, string>)[name] || name;
  return <section className="agent-console" id="agent-console"><div className="agent-console-hero"><div><p className="eyebrow">B2B SEO Agent Platform</p><h2>Agent 控制台</h2><p>多站点资料、异步任务与内容工作流的新版入口。现阶段由 Celery 编排；CrewAI 保留为后续可选编排器。</p></div><button onClick={() => void refresh()}>刷新状态</button></div><p className="agent-notice">{notice}</p><div className="agent-service-grid">{Object.entries(dashboard?.services || { api: "checking", database: "checking", queue: "checking", orchestrator: "celery workflow" }).map(([name, state]) => <article key={name}><span className={state === "ready" || name === "orchestrator" ? "is-ready" : ""}>● {state}</span><strong>{serviceLabel(name)}</strong></article>)}</div><div className="agent-metrics"><Metric label="隔离站点" value={dashboard?.summary.websites || 0} /><Metric label="内容任务" value={dashboard?.summary.tasks || 0} /><Metric label="待补来源" value={dashboard?.summary.waiting_for_sources || 0} /><Metric label="运行中" value={dashboard?.summary.active_tasks || 0} /></div><div className="agent-grid"><section className="panel"><PanelTitle eyebrow="01 Workspace" title="创建 B2B 站点" /><div className="agent-form"><Input label="域名" value={domain} onChange={setDomain} /><Input label="行业" value={industry} onChange={setIndustry} /><Input label="目标受众" value={audience} onChange={setAudience} /><Input label="品牌语调" value={tone} onChange={setTone} /></div><button className="primary" disabled={!domain.trim() || !industry.trim()} onClick={() => void createWebsite}>创建隔离站点</button><div className="agent-site-list">{websites.length ? websites.map((site) => <button className={String(site.id) === websiteId ? "is-selected" : ""} key={site.id} onClick={() => setWebsiteId(String(site.id))}><strong>{site.domain}</strong><span>{site.industry} · {site.audience || "未填受众"}</span></button>) : <p className="empty">还没有站点。先创建站点，再建立内容任务。</p>}</div></section><section className="panel"><PanelTitle eyebrow="02 Task" title="创建内容任务" /><div className="agent-form"><Select label="执行站点" value={websiteId} onChange={setWebsiteId} options={websites.map((site) => [String(site.id), `${site.domain} · ${site.industry}`])} /><Input label="目标关键词" value={keyword} onChange={setKeyword} /><Input label="拟定标题（可选）" value={title} onChange={setTitle} /></div><button className="primary" disabled={!keyword.trim() || !websiteId} onClick={() => void createTask}>提交到 Celery</button><p className="hint">`Best / Compare / Pricing` 等任务需先绑定来源包；未接资料服务前不会假装完成竞品研究。</p></section></div><section className="panel"><PanelTitle eyebrow="03 Pipeline" title="任务运行可视化" tag={`${tasks.length} 条`} /><ol className="agent-pipeline"><li><strong>任务初始化</strong><span>站点上下文与任务隔离</span></li><li><strong>来源 / SERP</strong><span>需接入来源包或 SERP 服务</span></li><li><strong>大纲确认</strong><span>人工可编辑后确认</span></li><li><strong>H2 独立深写</strong><span>每节独立生成后组装</span></li><li><strong>自然内链 / 图片</strong><span>后续 GSC 与图片适配器</span></li></ol><div className="agent-task-list">{tasks.length ? tasks.map((task) => <article key={task.id}><div><span className={`task-state task-${task.status}`}>{task.status}</span><strong>{task.target_keyword}</strong><small>{task.domain} · {task.title || "未指定标题"}</small></div><div className="task-progress"><span>{task.stage}</span><i><b style={{ width: `${task.progress}%` }} /></i><small>{task.progress}%</small></div><p>{task.message}</p></article>) : <p className="empty">还没有任务。创建后会在此显示实时状态。</p>}</div></section></section>;
}
function ProjectKnowledgeLibrary({ projectId, crawlOnly = false }: { projectId: number | null; crawlOnly?: boolean }) {
  const [documents, setDocuments] = useState<ProjectKnowledgeDocument[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [knowledgeType, setKnowledgeType] = useState("company");
  const [crawlLimit, setCrawlLimit] = useState("50");
  const [crawl, setCrawl] = useState<KnowledgeCrawlResult | null>(null);
  const [notice, setNotice] = useState("读取当前网站的公司知识库…");
  const [saving, setSaving] = useState(false);

  const load = async () => {
    if (!projectId) { setDocuments([]); return; }
    const items = await api.listProjectKnowledge(projectId) as ProjectKnowledgeDocument[];
    setDocuments(items);
  };
  useEffect(() => { void load().catch((error: unknown) => setNotice(error instanceof Error ? error.message : "读取知识库失败。")); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const uploadTextFile = async (file: File | undefined) => {
    if (!file) return;
    const text = await file.text();
    setTitle(file.name.replace(/\.[^.]+$/, ""));
    setContent(text);
    setNotice(`已读取“${file.name}”，确认后点击“加入知识库”。`);
  };
  const addDocument = async () => {
    if (!projectId || !title.trim() || !content.trim()) return;
    setSaving(true);
    try {
      await api.createProjectKnowledge(projectId, { title: title.trim(), content: content.trim(), source_type: "upload", knowledge_type: knowledgeType });
      setTitle(""); setContent(""); setNotice("DeepSeek 已整理资料类型、摘要和检索标签，并写入当前网站知识库。");
      await load();
    } catch (error) { setNotice(error instanceof Error ? error.message : "资料入库失败。"); }
    finally { setSaving(false); }
  };
  const runCrawl = async () => {
    if (!projectId) return;
    setSaving(true); setCrawl(null); setNotice("正在从当前网站采集产品、公司、案例、认证与 FAQ 页面…");
    try {
      const result = await api.crawlProjectKnowledge(projectId, Number(crawlLimit)) as KnowledgeCrawlResult;
      setCrawl(result);
      setNotice(`网站采集完成：入库 ${result.accepted_count} 页，跳过 ${result.skipped_count} 页，失败 ${result.failed_count} 页。`);
      await load();
    } catch (error) { setNotice(error instanceof Error ? error.message : "网站采集失败。"); }
    finally { setSaving(false); }
  };
  const remove = async (documentId: number) => {
    if (!projectId || !window.confirm("确定删除这份知识库资料吗？不会影响已生成文章。")) return;
    await api.deleteProjectKnowledge(projectId, documentId); await load();
  };
  if (!projectId) return <p className="empty">请先选择网站项目。</p>;
  return <section className="project-knowledge-library">
    <section className="project-knowledge-intro"><div><h2>{crawlOnly ? "网站产品与公司资料采集" : "公司知识库"}</h2><p>{crawlOnly ? "从当前项目的网站地址自动发现并提取可用的产品、公司、案例、认证与 FAQ 页面，采集完成后会按页面规则归类并写入公司知识库。" : "上传/补充资料会由 DeepSeek 整理类型、摘要与检索标签；资料只属于当前网站。内容生成时会按标题相关性选取最合适的产品、品牌或公司事实，不会每一段都强行植入。"}</p></div><span>{documents.length} 份资料</span></section>
    <section className="project-knowledge-crawl"><div><strong>采集当前网站资料</strong><p>自动发现并归类产品、公司介绍、公开联系方式、案例、认证和 FAQ 页面；博客、PDF、DOCX、图片、登录与下载页会跳过。</p></div><label>扫描上限<select value={crawlLimit} onChange={(event) => setCrawlLimit(event.target.value)}><option value="20">20 页</option><option value="50">50 页</option><option value="80">80 页</option></select></label><button className="primary" disabled={saving} onClick={() => void runCrawl()}>{saving ? "正在采集…" : "开始采集并归类"}</button></section>
    {crawl ? <section className="project-knowledge-crawl-result"><strong>本次采集结果</strong>{crawl.pages.map((page) => <article key={page.url}><span className={`knowledge-page-status ${page.status}`}>{page.status === "ready" ? "已入库" : page.status === "skipped" ? "已跳过" : "失败"}</span><div><a href={page.url} target="_blank" rel="noreferrer">{page.title || page.url}</a><small>{page.reason || page.knowledge_type}</small></div></article>)}</section> : null}
    {!crawlOnly && <section className="project-knowledge-upload"><div><h3>上传或补充资料</h3><p>支持 TXT、MD、HTML、CSV 文本文件，也可以直接粘贴公司、产品、规格、认证或案例资料。保存时由 DeepSeek 整理为可检索的长期知识记忆。</p></div><div className="project-knowledge-form"><input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="资料标题" /><select value={knowledgeType} onChange={(event) => setKnowledgeType(event.target.value)}><option value="company">公司资料</option><option value="product">产品资料</option><option value="certification">认证资料</option><option value="case_study">案例资料</option><option value="faq">FAQ</option><option value="other">其他</option></select><label className="project-knowledge-file">选择文本文件<input type="file" accept=".txt,.md,.html,.htm,.csv,text/plain,text/markdown,text/html,text/csv" onChange={(event) => void uploadTextFile(event.target.files?.[0])} /></label><textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder="粘贴资料内容，或先选择上方文本文件…" /><button className="primary" disabled={saving || !title.trim() || !content.trim()} onClick={() => void addDocument()}>{saving ? "DeepSeek 整理中…" : "DeepSeek 整理并入库"}</button></div></section>}
    <p className="project-knowledge-notice" role="status">{notice}</p>
    {!crawlOnly && <section className="project-knowledge-documents"><h3>已收录资料</h3>{documents.length ? documents.map((document) => <article key={document.id}><div><strong>{document.title}</strong><span>{document.source_type === "website_crawl" ? "网站采集" : "DeepSeek 整理"} · {document.knowledge_type || "其他"}{document.url ? ` · ${document.url}` : ""}</span>{document.summary ? <small>{document.summary}</small> : null}{document.tags?.length ? <div className="authority-tags">{document.tags.map((tag) => <i key={tag}>{tag}</i>)}</div> : null}</div><button className="link danger" onClick={() => void remove(document.id)}>删除</button></article>) : <p className="empty">暂未收录资料。可先采集当前网站，或上传文本资料。</p>}</section>}
  </section>;
}

function CompetitorLearningPlanner({ projectId }: { projectId: number | null }) {
  const [data, setData] = useState<CompetitorLearningDashboard | null>(null); const [topicsText, setTopicsText] = useState(""); const [interval, setInterval] = useState("14"); const [enabled, setEnabled] = useState(false); const [provider, setProvider] = useState<AiProvider>("deepseek"); const [model, setModel] = useState(""); const [notice, setNotice] = useState("保存计划后，系统会在后台按周期执行，不受页面跳转影响。"); const [saving, setSaving] = useState(false);
  const load = async () => { if (!projectId) return; try { const value = await api.getCompetitorLearning(projectId); setData(value); setTopicsText(value.schedule.topics.join("\n")); setInterval(String(value.schedule.interval_days)); setEnabled(Boolean(value.schedule.enabled)); setProvider(value.schedule.provider || "deepseek"); setModel(value.schedule.model || ""); } catch (error) { setNotice(error instanceof Error ? error.message : "读取定期竞品学习计划失败。"); } };
  useEffect(() => { void load(); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = async () => { if (!projectId) return; const topics = Array.from(new Set(topicsText.split(/\n|,/).map((item) => item.trim()).filter(Boolean))); setSaving(true); try { await api.saveCompetitorLearningSchedule(projectId, { topics, interval_days: Number(interval), enabled, provider, ...(model.trim() ? { model: model.trim() } : {}) }); setNotice(enabled ? "计划已启用：后台会按周期重新抓取可读的竞品文章，并更新写法卡片。" : "计划已保存但未启用；不会自动执行。"); await load(); } catch (error) { setNotice(error instanceof Error ? error.message : "保存学习计划失败。"); } finally { setSaving(false); } };
  const runNow = async () => { if (!projectId) return; setSaving(true); try { const result = await api.runCompetitorLearningNow(projectId); setNotice(`已进入后台队列（任务 #${result.id}）。可切换页面，完成后刷新此处查看写法卡片。`); await load(); } catch (error) { setNotice(error instanceof Error ? error.message : "启动竞品学习失败。"); } finally { setSaving(false); } };
  if (!projectId) return null;
  return <section className="competitor-learning-planner"><div className="competitor-learning-planner-head"><div><p className="eyebrow">Periodic Competitor Learning</p><h3>定期竞品学习与写法卡片</h3><p>系统仅抓取可读的内容页，提炼结构、决策顺序和信息格式；不保存或复用竞品正文。</p></div><span>{data?.cards.length || 0} 张写法卡片</span></div><div className="competitor-learning-form"><label className="competitor-learning-topics">学习主题（每行一个）<textarea value={topicsText} onChange={(event) => setTopicsText(event.target.value)} placeholder="例如：outdoor stair lighting installation&#10;例如：LED step light IP rating" /></label><Select label="学习周期" value={interval} onChange={setInterval} options={[["7", "每 7 天"], ["14", "每 14 天（推荐）"], ["30", "每 30 天"], ["60", "每 60 天"]]} /><Select label="分析模型" value={provider} onChange={(value) => setProvider(value as AiProvider)} options={[["openai", "ChatGPT"], ["deepseek", "DeepSeek"], ["gemini", "Gemini"]]} /><Input label="指定模型（可选）" value={model} onChange={setModel} /><label className="competitor-learning-enable"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /> 启用后台定期学习</label><div className="actions"><button className="primary" disabled={saving} onClick={() => void save()}>保存学习计划</button><button disabled={saving || !topicsText.trim()} onClick={() => void runNow()}>立即学习一次</button></div></div><p className="tag" role="status">{notice}</p>{data?.cards.length ? <div className="competitor-style-card-grid">{data.cards.map((card) => <article key={card.id}><div><span>写法卡片</span><b>质量 {Math.round(card.quality_score * 100)}</b></div><h4>{card.card_title}</h4><p>{card.summary}</p><small>{card.topic} · {card.evidence.source_count || 0} 个可读来源 · 更新于 {card.updated_at}</small><details><summary>查看来源范围</summary>{card.evidence.sources?.map((source, index) => source.url ? <a key={`${source.url}-${index}`} href={source.url} target="_blank" rel="noreferrer">{source.title || source.domain || source.url}</a> : null)}</details></article>)}</div> : <p className="empty">尚未生成写法卡片。先填写主题并执行一次学习；没有可读、相关竞品文章时会保留失败原因，不会编造学习结论。</p>}{data?.runs.length ? <details className="competitor-learning-runs"><summary>查看近期学习任务（{data.runs.length}）</summary>{data.runs.map((run) => <p key={run.id}><b>{run.status}</b> · {run.trigger_type === "scheduled" ? "定期" : "手动"} · {run.topic} · 来源 {run.source_count} · 卡片 {run.cards_created}{run.error_summary ? `：${run.error_summary}` : ""}</p>)}</details> : null}</section>;
}

function ContentMemoryLibrary({ projectId }: { projectId: number | null }) {
  const [items, setItems] = useState<ContentMemoryItem[]>([]); const [query, setQuery] = useState(""); const [notice, setNotice] = useState("读取当前网站的竞品内容学习记忆。");
  const load = async () => { if (!projectId) return; try { const value = await api.listContentMemory(projectId, query); setItems(value); setNotice(`已读取 ${value.length} 篇当前网站竞品资料。`); } catch (error) { setNotice(error instanceof Error ? error.message : "读取内容记忆失败。"); } };
  useEffect(() => { void load(); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const remove = async (id: number) => { if (!projectId || !window.confirm("删除这篇竞品学习资料吗？它不会删除已生成文章。")) return; await api.deleteContentMemory(id, { project_id: projectId }); await load(); };
  return <section className="panel content-memory-panel" id="content-memory"><PanelTitle eyebrow="Website Learning Memory" title="竞品内容学习库" tag={`${items.length} 篇`} /><p className="hint">仅保存当前网站采集到的竞品正文与结构资料。AI 会学习覆盖范围和写法，不会复制原文。</p><div className="actions"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、域名或正文" /><button onClick={() => void load()}>搜索</button></div><p className="tag">{notice}</p><div className="content-memory-grid">{items.map((item) => <article key={item.id}><span>{item.domain}</span><h3>{item.page_title}</h3><a href={item.url} target="_blank" rel="noreferrer">打开来源</a><small>最近采集：{item.last_captured_at}</small><button className="link danger" onClick={() => void remove(item.id)}>删除记忆</button></article>)}</div>{items.length ? null : <p className="empty">尚未采集竞品资料。执行一键生成后会自动建立记忆。</p>}</section>;
}

function LearningMemoryManager({ projectId }: { projectId: number | null }) {
  const [items, setItems] = useState<ContentLearningMemory[]>([]);
  const [selected, setSelected] = useState<ContentLearningMemoryDetail | null>(null);
  const [notice, setNotice] = useState("正在读取当前网站可复用的学习记忆…");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [topic, setTopic] = useState(""); const [summary, setSummary] = useState(""); const [priority, setPriority] = useState("0");
  const [feedbackNote, setFeedbackNote] = useState("");
  const [creating, setCreating] = useState(false); const [newType, setNewType] = useState<ContentLearningMemory["memory_type"]>("editorial"); const [newTopic, setNewTopic] = useState(""); const [newSummary, setNewSummary] = useState("");
  const load = async () => {
    if (!projectId) return;
    try { const result = await api.listContentLearningMemories(projectId); setItems(result); setNotice(`当前网站共有 ${result.length} 条记忆；仅“启用”的相关记忆会进入后续内容任务。`); }
    catch (error) { setNotice(error instanceof Error ? error.message : "读取学习记忆失败。"); }
  };
  const loadDetail = async (memory: ContentLearningMemory) => {
    if (!projectId) return;
    try { const detail = await api.getContentLearningMemory(memory.id, projectId); setSelected(detail); }
    catch (error) { setNotice(error instanceof Error ? error.message : "读取记忆详情失败。"); }
  };
  useEffect(() => { void load(); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const mutate = async (id: number, action: () => Promise<unknown>, message: string) => {
    setBusyId(id);
    try { await action(); await load(); const current = selected?.id === id ? await api.getContentLearningMemory(id, projectId!) : null; if (current) setSelected(current); setNotice(message); }
    catch (error) { setNotice(error instanceof Error ? error.message : "操作失败。"); }
    finally { setBusyId(null); }
  };
  const startEdit = (memory: ContentLearningMemory) => { setEditing(memory.id); setTopic(memory.topic); setSummary(memory.summary); setPriority(String(memory.manual_priority ?? 0)); };
  const saveEdit = (memory: ContentLearningMemory) => void mutate(memory.id, () => api.updateContentLearningMemory(memory.id, { project_id: projectId!, topic, summary, manual_priority: Number(priority) }), "策略说明已保存；来源与 GSC 原始证据保持不变。").then(() => setEditing(null));
  const addFeedback = (memory: ContentLearningMemory, decision: "useful" | "not_useful") => void mutate(memory.id, () => api.addContentLearningMemoryFeedback(memory.id, { project_id: projectId!, decision, note: feedbackNote.trim() || undefined }), decision === "useful" ? "已记录为有用，后续相关内容会提高它的优先级。" : "已记录为不适用，后续相关内容会降低它的优先级。 ").then(() => setFeedbackNote(""));
  const create = async () => {
    if (!projectId || !newTopic.trim() || !newSummary.trim()) return;
    setBusyId(-1);
    try { await api.createContentLearningMemory({ project_id: projectId, memory_type: newType, topic: newTopic.trim(), summary: newSummary.trim(), quality_score: 0.7, evidence: { source: "manual_editorial_feedback", note: "Created by a user in Learning Memory Manager." } }); setNewTopic(""); setNewSummary(""); setCreating(false); await load(); setNotice("人工学习偏好已加入；它仍需与文章主题相关才会被使用。"); }
    catch (error) { setNotice(error instanceof Error ? error.message : "创建人工学习偏好失败。"); }
    finally { setBusyId(null); }
  };
  if (!projectId) return <section className="panel"><p className="empty">请先选择网站项目，再管理该网站的学习记忆。</p></section>;
  const typeLabel: Record<ContentLearningMemory["memory_type"], string> = { style: "写作方式", brand: "品牌事实", fact: "可信事实", performance: "GSC 效果", editorial: "编辑偏好" };
  return <section className="panel learning-memory-panel" id="learning-memories">
    <PanelTitle eyebrow="Learning Memory & Human Feedback" title="学习记忆与人工反馈" tag={`${items.filter((item) => item.status === "active").length} 条启用`} />
    <p className="hint">这里管理的是 AI 可复用的策略卡，而不是竞品原文。置顶、优先级和“有用 / 不适用”都会影响后续检索；即使置顶，也必须与当前标题相关才会注入正文。</p>
    <div className="learning-memory-toolbar"><button onClick={() => void load()} disabled={busyId !== null}>刷新记忆</button><button className="primary" onClick={() => setCreating((value) => !value)}>{creating ? "收起人工偏好" : "新增人工学习偏好"}</button></div>
    {creating ? <section className="learning-memory-create"><Select label="记忆类型" value={newType} onChange={(value) => setNewType(value as ContentLearningMemory["memory_type"])} options={[["editorial", "编辑偏好"], ["style", "写作方式"], ["brand", "品牌事实"], ["fact", "可信事实"]]} /><Input label="适用主题" value={newTopic} onChange={setNewTopic} /><label>策略说明<textarea value={newSummary} onChange={(event) => setNewSummary(event.target.value)} placeholder="例如：面向安装指南，先给出选择条件，再用简短表格比较方案；避免夸大承诺。" /></label><div className="actions"><button className="primary" disabled={busyId !== null || !newTopic.trim() || !newSummary.trim()} onClick={() => void create()}>保存人工偏好</button><button onClick={() => setCreating(false)}>取消</button></div></section> : null}
    <p className="tag" role="status">{notice}</p>
    <div className="learning-memory-layout"><div className="learning-memory-list">{items.length ? items.map((memory) => <article className={`${selected?.id === memory.id ? "is-selected" : ""} ${memory.status !== "active" ? "is-disabled" : ""}`} key={memory.id}><button className="learning-memory-select" onClick={() => void loadDetail(memory)}><span>{typeLabel[memory.memory_type]}</span><strong>{memory.topic}</strong><small>质量 {Math.round(memory.quality_score * 100)} · 优先级 {memory.manual_priority ?? 0} · 有用 {memory.positive_feedback_count ?? 0} / 不适用 {memory.negative_feedback_count ?? 0}</small></button><div className="learning-memory-quick-actions"><button disabled={busyId === memory.id} onClick={() => void mutate(memory.id, () => api.setContentLearningMemoryPin(memory.id, projectId, !Boolean(memory.pinned)), memory.pinned ? "已取消置顶。" : "已置顶；仅在主题相关时优先使用。")}>{memory.pinned ? "取消置顶" : "置顶"}</button><button disabled={busyId === memory.id} onClick={() => void mutate(memory.id, () => api.setContentLearningMemoryStatus(memory.id, projectId, memory.status !== "active"), memory.status === "active" ? "记忆已停用，不再参与后续生成。" : "记忆已启用。")}>{memory.status === "active" ? "停用" : "启用"}</button></div></article>) : <p className="empty">暂时没有可管理的学习记忆。完成竞品研究、知识库归纳或 GSC 效果学习后，记忆会出现在这里；也可先新增人工偏好。</p>}</div>
      <aside className="learning-memory-detail">{selected ? <><div className="learning-memory-detail-head"><div><span>{typeLabel[selected.memory_type]}</span><h3>{selected.topic}</h3></div><b>{selected.status === "active" ? "启用中" : "已停用"}</b></div>{editing === selected.id ? <div className="learning-memory-edit"><Input label="适用主题" value={topic} onChange={setTopic} /><label>策略说明<textarea value={summary} onChange={(event) => setSummary(event.target.value)} /></label><Input label="人工优先级（-10 至 10）" value={priority} onChange={setPriority} type="number" /><div className="actions"><button className="primary" disabled={busyId === selected.id} onClick={() => saveEdit(selected)}>保存策略</button><button onClick={() => setEditing(null)}>取消</button></div></div> : <><p className="learning-memory-summary">{selected.summary}</p><dl className="learning-memory-stats"><div><dt>质量分</dt><dd>{Math.round(selected.quality_score * 100)}</dd></div><div><dt>人工优先级</dt><dd>{selected.manual_priority ?? 0}</dd></div><div><dt>已使用内容</dt><dd>{selected.content_links.length}</dd></div><div><dt>反馈</dt><dd>+{selected.positive_feedback_count ?? 0} / −{selected.negative_feedback_count ?? 0}</dd></div></dl><div className="actions"><button onClick={() => startEdit(selected)}>编辑策略</button><button className={selected.pinned ? "primary" : ""} onClick={() => void mutate(selected.id, () => api.setContentLearningMemoryPin(selected.id, projectId, !Boolean(selected.pinned)), selected.pinned ? "已取消置顶。" : "已置顶。")}>{selected.pinned ? "已置顶" : "置顶此记忆"}</button></div></>}
        <section className="learning-memory-feedback"><h4>人工反馈</h4><p>“不适用”只会降低后续权重，不会删除证据或历史记录。</p><textarea value={feedbackNote} onChange={(event) => setFeedbackNote(event.target.value)} placeholder="可选：说明为什么有用或不适用，帮助以后人工复核。" /><div className="actions"><button className="primary" disabled={busyId === selected.id} onClick={() => addFeedback(selected, "useful")}>这条有用</button><button className="danger" disabled={busyId === selected.id} onClick={() => addFeedback(selected, "not_useful")}>这条不适用</button></div>{selected.feedback.length ? <details><summary>查看反馈记录（{selected.feedback.length}）</summary>{selected.feedback.map((item) => <p key={item.id}><b>{item.decision === "useful" ? "有用" : "不适用"}</b> · {item.created_at}{item.note ? `：${item.note}` : ""}</p>)}</details> : null}</section>
        <details className="learning-memory-evidence"><summary>查看只读来源与证据</summary>{selected.source_url ? <a href={selected.source_url} target="_blank" rel="noreferrer">打开来源页面</a> : <p>这是一条人工或系统内部策略记忆，没有外部 URL。</p>}<pre>{JSON.stringify(selected.evidence, null, 2)}</pre></details>{selected.content_links.length ? <details><summary>查看已使用的内容（{selected.content_links.length}）</summary>{selected.content_links.map((link) => <p key={link.id}>{link.title_snapshot || `内容 #${link.content_asset_id}`} · 相关度 {Math.round(link.relevance_score * 100)}%</p>)}</details> : null}</> : <div className="learning-memory-detail-empty"><strong>选择一条记忆</strong><p>在左侧查看来源、使用记录和人工反馈；策略说明可编辑，原始证据保持只读。</p></div>}</aside></div>
  </section>;
}

function ContentWorkspace({ titles, assets, projectId, onCreate, onRefresh, onDelete, contentModels }: { titles: TitleCandidate[]; assets: ContentAsset[]; projectId: number | null; onCreate: (title: TitleCandidate) => void; onRefresh: () => Promise<void>; onDelete: (assetIds: number[]) => Promise<void>; contentModels: Record<AiProvider, string> }) {
  const available = titles.filter((title) => title.status === "selected");
  const createdByTitle = new Map(assets.map((asset) => [asset.selected_title_candidate_id, asset]));
  const [selectedAssetId, setSelectedAssetId] = useState<number | null>(assets[0]?.id ?? null);
  const [selectedAssetIds, setSelectedAssetIds] = useState<number[]>([]);
  const [detail, setDetail] = useState<ContentAssetDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("选择一个内容资产，按 Brief → 大纲 → 正文的顺序推进；每个 H2 独立生成后再组装成长文。");
  const [audience, setAudience] = useState("US small business owners");
  const [goal, setGoal] = useState("commercial");
  const [sourcesText, setSourcesText] = useState("");
  const [companyKnowledgeSources, setCompanyKnowledgeSources] = useState<Array<Record<string, string>>>([]);
  const [gscAnchorSources, setGscAnchorSources] = useState<Array<Record<string, string>>>([]);
  const [selectedDraftVersion, setSelectedDraftVersion] = useState<number | null>(null);
  const [draftView, setDraftView] = useState<"markdown" | "html">("html");
  const [contentProvider, setContentProvider] = useState<AiProvider | "auto_collaborate">("openai");
  const [contentModel, setContentModel] = useState(contentModels.openai || "");
  const [reviewerProvider, setReviewerProvider] = useState<"follow_writer" | AiProvider>("follow_writer");
  const [reviewerModel, setReviewerModel] = useState(contentModels.openai || "");
  const [outlineRows, setOutlineRows] = useState([{ heading: "Introduction: what the reader will decide", purpose: "Answer the search need and set clear decision context", }, { heading: "How to evaluate the options", purpose: "Give practical criteria and explain trade-offs", }, { heading: "Recommended next steps", purpose: "Turn the comparison into an informed action", }]);

  useEffect(() => {
    if (!projectId) { setCompanyKnowledgeSources([]); return; }
    api.listProjectKnowledge(projectId)
      .then((documents) => {
        setCompanyKnowledgeSources(documents
          .filter((item) => item?.status === "ready" && typeof item.content === "string" && item.content.trim())
          .map((item) => ({
            source_id: `company-knowledge-${item.id}`,
            source_type: "company_knowledge",
            availability: "available",
            title: String(item.title || "Current website knowledge"),
            url: String(item.url || ""),
            publisher: "Current website company knowledge base",
            knowledge_type: String(item.knowledge_type || "other"),
            content: `[Company knowledge · ${item.knowledge_type || "other"} · ${item.title || "Current website knowledge"}]\n${item.content.slice(0, 4000)}`,
          })));
      })
      .catch(() => setCompanyKnowledgeSources([]));
  }, [projectId]);

  useEffect(() => {
    setContentModel(contentModels[contentProvider === "auto_collaborate" ? "openai" : contentProvider] || "");
  }, [contentProvider, contentModels.openai, contentModels.gemini, contentModels.deepseek]);

  useEffect(() => {
    if (reviewerProvider === "follow_writer") setReviewerModel(contentModel);
    else setReviewerModel(contentModels[reviewerProvider] || "");
  }, [reviewerProvider, contentModel, contentModels.openai, contentModels.gemini, contentModels.deepseek]);
  useEffect(() => {
    if (!projectId) { setGscAnchorSources([]); return; }
    api.listProjectGscAnchors(projectId)
      .then(({ anchors }) => setGscAnchorSources(anchors.slice(0, 40).map((item, index) => ({
        source_id: `gsc-anchor-${index + 1}`,
        source_type: "gsc_anchor",
        availability: "available",
        title: `GSC internal-link anchor: ${item.query}`,
        url: item.page_url,
        publisher: "Google Search Console (current website)",
        content: `[Google Search Console internal-link evidence]\nSuggested anchor text: ${item.query}\nTarget URL: ${item.page_url}\nClicks: ${item.clicks}; impressions: ${item.impressions}; average position: ${item.position.toFixed(1)}\nUse only when this page is genuinely relevant. Keep the anchor natural and do not claim rankings as product facts.`,
      }))))
      .catch(() => setGscAnchorSources([]));
  }, [projectId]);

  const loadDetail = async (assetId: number) => {
    if (!projectId) return;
    setSelectedAssetId(assetId); setLoadingDetail(true);
    try {
      const next = await api.getContentAsset(assetId, projectId);
      setDetail(next);
      if (next.brief) {
        setAudience(next.brief.target_audience || ""); setGoal(next.brief.business_goal || "commercial");
        setSourcesText((next.brief.sources || [])
          .filter((source) => typeof source === "string" || !(source && typeof source === "object" && (source as { source_type?: unknown }).source_type === "company_knowledge"))
          .map((source) => {
            if (typeof source === "string") return source;
            const item = source as { content?: unknown };
            return typeof item.content === "string" ? item.content : JSON.stringify(source);
          })
          .join("\n\n---\n\n"));
      }
      if (next.outline?.sections?.length) setOutlineRows(next.outline.sections.map(({ heading, purpose }) => ({ heading, purpose })));
    } catch (error) { setNotice(error instanceof Error ? error.message : "读取内容详情失败。"); }
    finally { setLoadingDetail(false); }
  };

  useEffect(() => {
    if (!assets.length) { setSelectedAssetId(null); setDetail(null); return; }
    if (!selectedAssetId || !assets.some((asset) => asset.id === selectedAssetId)) void loadDetail(assets[0].id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assets, projectId]);

  useEffect(() => setSelectedAssetIds((current) => current.filter((assetId) => assets.some((asset) => asset.id === assetId))), [assets]);

  const sourcePack = () => {
    const topicTerms = new Set(`${detail?.title_snapshot || ""} ${detail?.keyword || ""}`.toLowerCase().match(/[a-z0-9]{3,}/g) || []);
    const rankedCompanyKnowledge = companyKnowledgeSources
      .map((source) => ({ source, score: (source.title + " " + source.content.slice(0, 1600)).toLowerCase().match(/[a-z0-9]{3,}/g)?.filter((term) => topicTerms.has(term)).length || 0 }))
      .sort((left, right) => right.score - left.score)
      .slice(0, 12)
      .map(({ source }) => source);
    const rankedGscAnchors = gscAnchorSources
      .map((source) => ({ source, score: (source.title + " " + source.content).toLowerCase().match(/[a-z0-9]{3,}/g)?.filter((term) => topicTerms.has(term)).length || 0 }))
      .sort((left, right) => right.score - left.score)
      .slice(0, 6)
      .map(({ source }) => source);
    return [
    ...rankedCompanyKnowledge,
    ...rankedGscAnchors,
    ...sourcesText.split(/\n\s*---+\s*\n/).map((item) => item.trim()).filter(Boolean).map((content, index) => {
      const url = content.match(/https?:\/\/[^\s]+/)?.[0] || "";
      return { source_id: `source-${index + 1}`, source_type: url ? "url" : "note", url, publisher: "", published_at: "", content, availability: "available" };
    }),
    ];
  };
  const toggleAssetSelection = (assetId: number) => setSelectedAssetIds((current) => current.includes(assetId) ? current.filter((id) => id !== assetId) : [...current, assetId]);
  const deleteSelectedAssets = async () => { if (!selectedAssetIds.length || !window.confirm(`确认删除 ${selectedAssetIds.length} 篇内容资产及所有版本吗？`)) return; await onDelete(selectedAssetIds); setSelectedAssetIds([]); };

  const saveBrief = async () => {
    if (!detail || !projectId) return;
    setSaving(true);
    try {
      const brief = await api.createContentBrief(detail.id, { project_id: projectId, target_audience: audience.trim(), business_goal: goal, sources: sourcePack() });
      setDetail((current) => current ? { ...current, brief, current_brief_id: brief.id } : current);
      setNotice("Brief 已保存。下一步可编辑并保存文章大纲。");
      await onRefresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : "保存 Brief 失败。"); }
    finally { setSaving(false); }
  };

  const saveOutline = async () => {
    if (!detail || !projectId) return;
    const sections = outlineRows.filter((row) => row.heading.trim()).map((row) => ({ heading: row.heading, purpose: row.purpose }));
    if (!sections.length) { setNotice("至少保留一个大纲章节。"); return; }
    setSaving(true);
    try {
      const outline = await api.createContentOutline(detail.id, { project_id: projectId, sections });
      setDetail((current) => current ? { ...current, outline, current_outline_id: outline.id } : current);
      setNotice("文章大纲已保存。正文生成和质量检查会基于此大纲执行。");
      await onRefresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : "保存大纲失败。"); }
    finally { setSaving(false); }
  };

  const generationPayload = (withCompetitorResearch = false) => ({
    project_id: projectId,
    ...(contentProvider === "auto_collaborate" ? { routing_mode: "auto_collaborate" } : { provider: contentProvider }),
    model: contentModel.trim() || undefined,
    reviewer_provider: reviewerProvider === "follow_writer" ? undefined : reviewerProvider,
    reviewer_model: (reviewerProvider === "follow_writer" ? contentModel : reviewerModel).trim() || undefined,
    target_audience: audience.trim(),
    business_goal: goal,
    sources: sourcePack(),
    competitor_research: withCompetitorResearch,
  });
  const applyGenerated = async (result: ContentGenerationResult) => {
    if (!detail) return;
    setDetail((current) => current ? { ...current, ...(result.asset || {}), brief: result.brief ?? current.brief, outline: result.outline ?? current.outline, competitor_research: result.competitor_research ?? current.competitor_research, current_draft: result.current_draft ?? result.draft ?? current.current_draft, drafts: result.draft ? [result.draft, ...(current.drafts || []).filter((draft) => draft.id !== result.draft?.id)] : current.drafts, generation_runs: result.generation_runs ?? result.runs ?? current.generation_runs, generation_jobs: result.generation_job ? [...(current.generation_jobs || []), result.generation_job] : current.generation_jobs } : current);
    await loadDetail(detail.id);
    await onRefresh();
  };
  const executionLabel = contentProvider === "auto_collaborate" ? "自动协作：DeepSeek 分析 / ChatGPT 写作" : `${contentProviderLabel(contentProvider)}（${contentModel || "默认模型"}）`;
  const generateStage = async (stageName: "brief" | "outline" | "draft" | "all") => {
    if (!detail || !projectId) return;
    setSaving(true); setNotice(`${executionLabel} 正在严格执行${stageName === "all" ? (detail.brief ? "大纲与全文" : "Brief、大纲与全文") : `内容${stageName === "brief" ? " Brief" : stageName === "outline" ? "大纲" : "正文"}`}；每个 H2 独立写作后再组装，失败不会静默切换其他模型…`);
    try {
      const action = stageName === "brief" ? api.generateContentBrief : stageName === "outline" ? api.generateContentOutline : stageName === "draft" ? api.generateContentDraft : api.generateContent;
      const result = await action(detail.id, generationPayload(stageName === "all"));
      await applyGenerated(result);
      setNotice(`${executionLabel} 已完成本次任务并保存；可在执行日志查看每个阶段的实际模型。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : `${executionLabel} 生成失败；未切换其他模型。`); await loadDetail(detail.id); await onRefresh(); }
    finally { setSaving(false); }
  };
  const reviewQuality = async () => {
    if (!detail || !projectId || !detail.current_draft) return;
    setSaving(true); setNotice(`${contentProviderLabel(reviewerProvider === "follow_writer" ? contentProvider : reviewerProvider)} 正在审核当前正文；不会覆盖 v${detail.current_draft.version}…`);
    try {
      const result = await api.reviewContentQuality(detail.id, generationPayload(false));
      await applyGenerated(result);
      setNotice(`质量审核已保存：${result.draft?.qa_status || "已完成"}。如有问题，下方会显示最多两条定向重写建议。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "内容质量审核失败；正文版本未被覆盖。"); }
    finally { setSaving(false); }
  };
  const rewriteTargeted = async () => {
    if (!detail || !projectId || !detail.current_draft?.qa?.targeted_rewrite?.length) return;
    setSaving(true); setNotice("正在按最多两条审核建议生成修订版；当前版本会保留，不会被覆盖…");
    try {
      const result = await api.rewriteContentTargeted(detail.id, generationPayload(false));
      await applyGenerated(result);
      setNotice(`已生成修订版 v${result.draft?.version || ""}；建议再次运行质量审核。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "定向重写失败；旧正文仍可使用。"); }
    finally { setSaving(false); }
  };

  const displayedDraft = detail?.drafts?.find((draft) => draft.version === selectedDraftVersion) || detail?.current_draft;
  const stage = detail?.current_draft ? 3 : detail?.outline ? 2 : detail?.brief ? 1 : 1;
  const stages = ["内容 Brief", "文章大纲", "正文版本"];
  const robotsBlockedItems = (detail?.competitor_research?.items || []).filter((item) => /robots/i.test(item.error_summary || ""));
  return <div className="content-workspace" id="content-workspace">
    <div className="design-content-tabs" role="tablist" aria-label="内容生产页面导航"><button className="is-active" role="tab" aria-selected="true">内容任务</button><NavLink role="tab" to="/content-library">内容库</NavLink><NavLink role="tab" to="/content-memory">竞品学习记忆</NavLink><NavLink role="tab" to="/learning-memories">学习记忆与反馈</NavLink></div>
    {detail && <section className="design-content-dashboard" aria-label="当前文章任务">
      <div className="design-workflow-card"><div className="design-workflow-heading"><div><h2>{detail.title_snapshot}</h2><small>本次路由：{executionLabel}</small></div><span className={saving ? "design-status is-running" : "design-status"}>{saving ? "正在生成" : detail.current_draft ? "正文已生成" : detail.outline ? "大纲已生成" : "待生成"}</span></div><div className="design-workflow-steps"><div className={detail.competitor_research?.status === "completed" ? "is-done" : ""}><i>{detail.competitor_research?.status === "completed" ? "✓" : "1"}</i><b>竞品采集</b></div><div className={detail.competitor_research?.analysis ? "is-done" : ""}><i>{detail.competitor_research?.analysis ? "✓" : "2"}</i><b>竞品分析</b></div><div className={detail.outline ? "is-done" : "is-current"}><i>{detail.outline ? "✓" : "3"}</i><b>文章大纲</b></div><div className={detail.current_draft ? "is-done" : ""}><i>{detail.current_draft ? "✓" : "4"}</i><b>H2 写作</b></div><div className={detail.current_draft ? "is-done" : ""}><i>{detail.current_draft ? "✓" : "5"}</i><b>全文组装</b></div></div><div className="design-workflow-actions"><button className="primary" disabled={saving} onClick={() => void generateStage("all")}>{saving ? "正在生成…" : "一键生成文章"}</button><button disabled={saving} onClick={() => document.getElementById("content-editor-area")?.scrollIntoView({ behavior: "smooth" })}>编辑大纲与资料</button></div></div>
      <div className="design-content-grid"><section className="design-content-panel"><div className="design-panel-heading"><h2>已采集同行文章</h2><small>{detail.competitor_research?.usable_count || 0} 篇来源</small></div>{detail.competitor_research?.items?.filter((item) => item.status === "selected").length ? <ol className="design-source-list">{detail.competitor_research.items.filter((item) => item.status === "selected").slice(0, 5).map((item) => <li key={item.id}><span>#{item.rank}</span><a href={item.url} target="_blank" rel="noreferrer">{item.page_title || item.search_title}</a><small>{item.domain}</small></li>)}</ol> : <p className="design-empty">暂无可用同行文章。启动生成后会用 Serper 搜索并采集真实正文。</p>}</section><section className="design-content-panel"><div className="design-panel-heading"><h2>动态大纲</h2><small>{detail.outline?.sections.length || outlineRows.length} 个 H2</small></div><ol className="design-outline-list">{(detail.outline?.sections || outlineRows).slice(0, 7).map((row, index) => <li key={`${row.heading}-${index}`}><span>{index + 1}</span><b>{row.heading}</b></li>)}</ol></section></div>
    </section>}
    <div className="content-workspace-header"><div><p className="eyebrow">Production Workspace</p><h3>内容工作流</h3><p>保留每一步输入与版本；没有来源支撑的事实在生成时会标记为 <code>[VERIFY]</code>。</p></div><div className="content-workspace-actions"><Select label="执行模式" value={contentProvider} onChange={(value) => setContentProvider(value as AiProvider | "auto_collaborate")} options={[["auto_collaborate", "自动协作（推荐）"], ["openai", "仅 ChatGPT"], ["deepseek", "仅 DeepSeek"], ["gemini", "仅 Gemini"]]} /><Input label={contentProvider === "auto_collaborate" ? "ChatGPT 写作模型" : "写作模型"} value={contentModel} onChange={setContentModel} /><Select label="审核 AI" value={reviewerProvider} onChange={(value) => setReviewerProvider(value as "follow_writer" | AiProvider)} options={[["follow_writer", contentProvider === "auto_collaborate" ? "自动使用 DeepSeek 审核" : "跟随写作模型"], ["openai", "ChatGPT"], ["gemini", "Gemini"], ["deepseek", "DeepSeek"]]} /><Input label="审核模型" value={reviewerModel} onChange={setReviewerModel} /><span className="content-provider-lock">本次任务：<strong>{contentProviderLabel(contentProvider)}</strong><small>{contentProvider === "auto_collaborate" ? "DeepSeek 分析/审核 · ChatGPT 写作" : contentModel || "未配置模型"}</small></span><button onClick={() => void onRefresh()}>刷新资产</button><NavLink className="link" to="/title-library">管理标题库</NavLink></div></div>
    <p className="content-notice">{notice}</p>
    <div className="content-memory-link"><NavLink to="/content-memory">打开竞品内容学习库 →</NavLink><NavLink to="/competitor-learning">设置定期竞品学习 →</NavLink><NavLink to="/learning-memories">管理学习策略与人工反馈 →</NavLink></div>
    {assets.length ? <section className="content-workspace-bulk"><div><strong>资产批量操作</strong><span>已选 {selectedAssetIds.length} / {assets.length}</span></div><div className="content-workspace-selection">{assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={selectedAssetIds.includes(asset.id)} onChange={() => toggleAssetSelection(asset.id)} /> <span>{asset.current_draft_id ? "正文完成" : asset.current_outline_id ? "大纲完成" : asset.current_brief_id ? "Brief 完成" : "待开始"}</span>{asset.title_snapshot}</label>)}</div><div className="actions"><button onClick={() => setSelectedAssetIds(selectedAssetIds.length === assets.length ? [] : assets.map((asset) => asset.id))}>{selectedAssetIds.length === assets.length ? "取消全选" : "全选资产"}</button><button className="danger" disabled={!selectedAssetIds.length} onClick={() => void deleteSelectedAssets}>删除已选内容</button></div></section> : null}
    <div className="content-layout">
      <aside className="content-asset-list" id="content-asset-list"><div className="content-list-heading"><strong>内容资产</strong><span>{assets.length}</span></div>{assets.length ? assets.map((asset) => <article className={`content-asset-card ${asset.id === selectedAssetId ? "is-active" : ""} ${asset.current_draft_id ? "is-complete" : asset.current_outline_id ? "is-outline-ready" : asset.current_brief_id ? "is-brief-ready" : "is-pending"}`} key={asset.id}><button type="button" className="content-asset-open" onClick={() => void loadDetail(asset.id)}><span className="content-asset-status">{asset.content_status_label || (asset.current_draft_id ? "内容完成" : asset.current_outline_id ? "大纲完成" : asset.current_brief_id ? "Brief 完成" : "待生成")}</span><strong>{asset.title_snapshot}</strong><small>{asset.keyword || "—"} · {asset.locale}</small></button><button type="button" className="content-asset-delete" aria-label={`删除 ${asset.title_snapshot}`} title="删除内容资产" onClick={(event) => { event.stopPropagation(); void onDelete([asset.id]); }}>×</button></article>) : <p className="empty">尚未创建内容资产。</p>}<div className="content-list-divider" /><strong className="content-list-label">已选标题</strong>{available.length ? available.map((title) => { const asset = createdByTitle.get(title.id); return <article className="content-title-entry" key={title.id}><ProviderBadge reason={title.reason} /><strong>{title.title}</strong>{asset ? <button onClick={() => void loadDetail(asset.id)}>打开</button> : <button className="primary" onClick={() => onCreate(title)}>创建</button>}</article>; }) : <p className="empty">请先在标题库选定标题。</p>}</aside>
      <section className="content-editor-area">{loadingDetail ? <p className="empty">正在读取内容详情…</p> : !detail ? <div className="content-empty-state"><strong>从标题库选择一个标题开始</strong><p>创建内容资产后，所有 Brief、大纲、正文和审核结果都会在这里集中保存。</p></div> : <>
        <div className="content-hero"><div><span className="content-kicker">{detail.content_type} · {detail.locale}</span><h3>{detail.title_snapshot}</h3><p>核心关键词：<strong>{detail.keyword || "—"}</strong></p></div><span className="tag">{detail.status === "planned" ? "规划中" : detail.status}</span></div>
        <ol className="content-stage-rail">{stages.map((item, index) => <li className={`content-stage-step ${index + 1 < stage ? "is-complete" : index + 1 === stage ? "is-current" : ""}`} key={item}><span>{index + 1}</span><strong>{item}</strong><small>{index + 1 < stage ? "已保存" : index + 1 === stage ? "当前步骤" : "等待前序"}</small></li>)}</ol>
        {detail.learning_memories?.length ? <section className="content-editor-card" aria-label="本次内容使用的学习记忆"><div className="content-card-heading"><div><span>记忆</span><div><h4>本次已选学习记忆</h4><p>只注入与当前标题相关的写法、品牌事实或表现策略；竞品原文不会进入正文。</p></div></div><span className="stage-state done">{detail.learning_memories.length} 条</span></div><div className="content-asset-tags">{detail.learning_memories.map((memory) => <span key={memory.id} title={memory.summary}>{memory.role || memory.memory_type} · {memory.topic} · 相关度 {Math.round((memory.relevance_score || 0) * 100)}%</span>)}</div></section> : null}
        <section className="content-editor-card competitor-research-card" id="competitor-research"><div className="content-card-heading"><div><span>00</span><div><h4>竞品学习与动态大纲</h4><p>Serper.dev · Google 搜索 API 先返回完整标题的前 30 条自然结果；系统排除产品、商城与社媒页后，按排名用 Python 爬虫最多抓 5 篇文章正文，再按实际采集到的 1–5 篇交给 AI 分析。</p></div></div><span className={detail.competitor_research?.status === "completed" ? "stage-state done" : detail.competitor_research?.status === "insufficient" ? "stage-state warning" : "stage-state"}>{detail.competitor_research?.status === "completed" ? `已采集 ${detail.competitor_research.usable_count} 篇` : detail.competitor_research?.status === "insufficient" ? "无可用来源" : "待采集"}</span></div>{detail.competitor_research ? <div className="competitor-research-summary"><p><strong>检索词：</strong>{detail.competitor_research.query}　<strong>模型：</strong>{contentProviderLabel(detail.competitor_research.provider || contentProvider)} · {detail.competitor_research.model || contentModel}</p>{detail.competitor_research.status === "insufficient" ? <p className="verify-warning">没有获得可抓取且相关的同行文章；本次不会编造竞品依据。可重试、改标题，或在 Brief 中补充人工资料。</p> : null}{detail.competitor_research.analysis.missing_gaps?.length ? <p><strong>竞品遗漏点：</strong>{detail.competitor_research.analysis.missing_gaps.join(" · ")}</p> : null}{robotsBlockedItems.length ? <details className="competitor-robots-list" open><summary>robots.txt 标记的候选（{robotsBlockedItems.length}）· 未入库</summary>{robotsBlockedItems.map((item) => <a key={item.id} href={item.url} target="_blank" rel="noreferrer"><strong>#{item.rank} {item.search_title}</strong><small>{item.domain} · {item.error_summary}</small></a>)}</details> : null}<div className="competitor-source-list">{detail.competitor_research.items.filter((item) => item.status === "selected").map((item) => <article key={item.id}><span>#{item.rank}</span><div><a className="competitor-source-link" href={item.url} target="_blank" rel="noreferrer" title="在新标签页打开来源原文">{item.page_title || item.search_title}</a><small>{item.domain} · 本次 AI 分析来源 · 点击标题查看原文</small></div></article>)}</div></div> : <p className="empty">尚未采集竞品。点击下方“一键生成”后自动执行；成功抓到 1 篇或更多可用文章就会继续分析和写作。</p>}</section>
        <section className="content-editor-card" id="content-brief"><div className="content-card-heading"><div><span>01</span><div><h4>内容 Brief</h4><p>定义受众、业务目标和可验证资料；它是后续 AI 写作的事实边界。</p></div></div><span className={detail.brief ? "stage-state done" : "stage-state"}>{detail.brief ? "已保存" : "待填写"}</span></div><div className="content-brief-grid"><Input label="目标读者" value={audience} onChange={setAudience} /><Select label="业务目标" value={goal} onChange={setGoal} options={[["informational", "信息型内容"], ["commercial", "商业调研 / 对比"], ["lead-generation", "获客 / 线索"]]} /><label className="content-source-field">多篇文章 / URL / 研究资料（每篇文章之间用 --- 分隔，可粘贴全文）<textarea value={sourcesText} onChange={(event) => setSourcesText(event.target.value)} placeholder="【来源 1：官方文章】&#10;粘贴 URL 或全文…&#10;&#10;【来源 2：同行文章/笔记】&#10;粘贴 URL 或全文…" /></label></div><div className="content-card-footer"><span>{companyKnowledgeSources.length ? `已自动绑定当前网站 ${companyKnowledgeSources.length} 份公司/产品资料：每篇文章只选择 1–2 个相关 H2 自然加入产品或品牌事实；有对应产品页时可加入自然内链。` : sourcesText.trim() ? "将把资料作为可引用事实来源。" : "未添加资料：正文中的具体事实将标记为 [VERIFY]。"}</span><div className="actions"><button disabled={saving} onClick={() => void saveBrief}>人工保存</button><button className="primary" disabled={saving} onClick={() => void generateStage("brief")}>AI 生成 Brief</button></div></div></section>
        <section className={`content-editor-card ${!detail.brief ? "is-locked" : ""}`} id="content-outline"><div className="content-card-heading"><div><span>02</span><div><h4>文章大纲</h4><p>每个 H2 只解决一个决策问题；比较类内容在正文阶段需要输出带来源列的表格。</p></div></div><span className={detail.outline ? "stage-state done" : "stage-state"}>{detail.outline ? "已保存" : "待编辑"}</span></div><div className="outline-table"><div className="outline-row outline-head"><span>章节标题</span><span>章节任务</span><span /></div>{outlineRows.map((row, index) => <div className="outline-row" key={`${row.heading}-${index}`}><input aria-label={`章节标题 ${index + 1}`} value={row.heading} onChange={(event) => setOutlineRows((current) => current.map((item, rowIndex) => rowIndex === index ? { ...item, heading: event.target.value } : item))} disabled={!detail.brief} /><input aria-label={`章节任务 ${index + 1}`} value={row.purpose} onChange={(event) => setOutlineRows((current) => current.map((item, rowIndex) => rowIndex === index ? { ...item, purpose: event.target.value } : item))} disabled={!detail.brief} /><button className="link danger" disabled={!detail.brief || outlineRows.length <= 1} onClick={() => setOutlineRows((current) => current.filter((_, rowIndex) => rowIndex !== index))}>移除</button></div>)}</div><div className="content-card-footer"><button disabled={!detail.brief} onClick={() => setOutlineRows((current) => [...current, { heading: "New section", purpose: "Add a distinct reader decision" }])}>添加章节</button><div className="actions"><button disabled={!detail.brief || saving} onClick={() => void saveOutline}>人工保存</button><button className="primary" disabled={!detail.brief || saving} onClick={() => void generateStage("outline")}>AI 生成大纲</button></div></div></section>
        <section className={`content-editor-card content-future-card ${!detail.outline ? "is-locked" : ""}`} id="content-draft"><div className="content-card-heading"><div><span>03</span><div><h4>正文版本</h4><p>每个 H2 独立写成一篇完整章节，再按大纲组装为一篇长文，并保留可回滚的版本历史。</p></div></div><span className="stage-state">{detail.current_draft ? `版本 v${displayedDraft!.version}` : detail.outline ? "准备生成" : "需先保存大纲"}</span></div>{displayedDraft ? <><div className="draft-switcher"><Select label="版本" value={String(selectedDraftVersion || displayedDraft!.version)} onChange={(value) => setSelectedDraftVersion(Number(value))} options={(detail.drafts || []).slice().reverse().map((draft) => [String(draft.version), `v${draft.version} · ${draft.provider || "AI"}`])} /><div className="actions"><button className={draftView === "markdown" ? "primary" : ""} onClick={() => setDraftView("markdown")}>Markdown 源码</button><button className={draftView === "html" ? "primary" : ""} onClick={() => setDraftView("html")}>HTML 预览</button></div></div><div className="content-draft-preview"><div><strong>{displayedDraft!.title}</strong><p>{displayedDraft!.meta_description}</p></div>{draftView === "markdown" ? <pre>{displayedDraft!.markdown}</pre> : <article className="markdown-preview" dangerouslySetInnerHTML={{ __html: renderMarkdownPreview(displayedDraft!.markdown) }} />}<div className="draft-meta"><span>版本 v{displayedDraft!.version}</span><span>{displayedDraft!.provider || contentProvider}</span><span>{displayedDraft!.model || "已保存"}</span><span>审核：{displayedDraft!.qa_status}</span></div></div>{displayedDraft!.qa?.targeted_rewrite?.length ? <div className="verify-warning"><strong>定向重写建议（最多 2 条）</strong>{displayedDraft!.qa.targeted_rewrite.map((item, index) => <p key={`${item.target}-${index}`}>[{item.target}] {item.issue}：{item.instruction}</p>)}</div> : null}</> : <div className="future-grid"><div><strong>H2 分段生成</strong><p>每个 H2 仅发送本章 Brief 与相关资料，独立完成后再组装，避免重复与无来源编造。</p></div><div><strong>多模型任务</strong><p>内容模型将读取 AI 配置；每次生成都会记录模型、时间和版本。</p></div></div>}<div className="content-card-footer"><span>{detail.brief ? "一键生成将自动完成大纲、每个 H2 的独立章节和全文组装，并保留当前 Brief。" : "一键生成会先创建 Brief，再自动完成大纲、每个 H2 章节和全文组装。"}</span><div className="actions"><button disabled={saving} onClick={() => void generateStage("all")}>一键生成大纲和全文</button><button disabled={!detail.current_draft || saving} onClick={() => void reviewQuality()}>AI 质量审核</button><button disabled={!detail.current_draft?.qa?.targeted_rewrite?.length || saving} onClick={() => void rewriteTargeted()}>按建议生成修订版</button><button className="primary" disabled={!detail.outline || saving} onClick={() => void generateStage("draft")}>{detail.current_draft ? "生成新版本" : "AI 生成正文"}</button></div></div></section>
        <section className="content-generation-log" aria-label="模型执行日志"><div><p className="eyebrow">Model Audit</p><h4>模型执行日志</h4></div>{(detail.generation_jobs || []).slice().reverse().slice(0, 5).map((job) => <article className={`generation-job ${job.status}`} key={job.id}><div><strong>{job.routing_mode === "auto_collaborate" ? "自动协作 · " : ""}{contentProviderLabel(job.provider)} · {job.model || "未记录模型"}</strong><span>{job.status === "completed" ? "已完成" : job.status === "failed" ? `${contentStageLabel(job.failed_stage)}失败` : "执行中"}</span></div><small>{(detail.generation_runs || []).filter((run) => run.generation_job_id === job.id).map((run) => `${contentStageLabel(run.stage)} · ${contentProviderLabel(run.provider)} ${run.status === "completed" ? "✓" : "×"}`).join(" · ") || "任务尚未写入阶段日志"}</small>{job.routing_summary ? <p>{job.routing_summary}</p> : null}{job.error_summary ? <p>{job.error_summary}</p> : null}</article>)}</section>
      </>}</section>
    </div>
  </div>;
}
const aiProviderPresets: Record<AiProvider, { baseUrl: string; model: string }> = {
  openai: { baseUrl: "https://api.openai.com/v1", model: "gpt-5.6-sol" },
  gemini: { baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-2.5-flash" },
  deepseek: { baseUrl: "https://api.deepseek.com", model: "deepseek-v4-pro" },
};

function initialAiProfiles(): Record<AiProvider, AiProfile> {
  return Object.fromEntries(Object.entries(aiProviderPresets).map(([provider, preset]) => [provider, { baseUrl: preset.baseUrl, model: preset.model, apiKey: "" }])) as Record<AiProvider, AiProfile>;
}

function demandEstimate(keyword: string) {
  const words = Math.max(1, keyword.trim().split(/\s+/).length);
  let score = words === 1 ? 75 : words === 2 ? 62 : words === 3 ? 50 : 38;
  if (/\b(best|vs|review|price|buy|service)\b|推荐|评测|价格|购买|报价/.test(keyword.toLowerCase())) score += 8;
  if (/\b(how|what|why)\b|怎么|如何|是什么/.test(keyword.toLowerCase())) score -= 8;
  return Math.max(10, Math.min(90, score));
}

function categoryOf(keyword: string, review: Review) {
  const value = keyword.toLowerCase();
  if (/\b(best|vs|review|compare|alternative)\b|推荐|评测|对比|哪个好/.test(value)) return "对比评测";
  if (review.search_intent === "transactional" || /\b(buy|price|pricing|service|quote)\b|购买|价格|报价|服务/.test(value)) return "购买服务";
  if (/\?|\b(how|what|why|guide)\b|怎么|如何|是什么|教程/.test(value)) return "教程问答";
  return review.search_intent === "commercial" ? "商业调研" : "主题内容";
}

function readableIntent(intent?: string) {
  return ({ informational: "信息型", commercial: "商业调研", transactional: "交易型", navigational: "导航型", local: "本地型" } as Record<string, string>)[intent || ""] || "—";
}

type ProjectSummary = { id: number; name: string; site_url?: string | null; industry: string; default_country: string; default_language: string; keyword_count: number; selected_title_count: number; content_count: number; knowledge_count: number; latest_content_status?: string | null };
function ProjectDirectory() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [form, setForm] = useState({ name: "", industry: "", site_url: "" });
  const [notice, setNotice] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const load = async () => { try { setProjects(await api.listProjectSummaries()); } catch (error) { setNotice(error instanceof Error ? error.message : "读取网站项目失败。"); } };
  useEffect(() => { void load(); }, []);
  const create = async (event: FormEvent) => { event.preventDefault(); if (!form.name.trim()) return; try { const created = await api.createProject({ name: form.name.trim(), industry: form.industry.trim(), site_url: form.site_url.trim(), country_code: "US", language_code: "en" }); navigate(`/research?project_id=${created.id}`); } catch (error) { setNotice(error instanceof Error ? error.message : "创建网站失败。"); } };
  const openProject = (projectId: number, destination = "/research") => navigate(`${destination}?project_id=${projectId}`);
  return <section className="project-directory">
    <div className="project-directory-topbar"><div><p>网站项目 / 全部网站项目</p><h1>网站项目</h1><span>每个网站都有独立的关键词、标题、内容、知识库和发布记录。</span></div><button className="primary" onClick={() => setShowCreate(true)}>＋ 新建网站</button></div>
    <section className="project-directory-create-banner"><b>＋</b><div><h2>创建网站项目</h2><p>填写域名、行业、国家与语言后，建立独立的数据空间。</p></div><button className="primary" onClick={() => setShowCreate(true)}>新建网站</button></section>
    {showCreate && <form id="new-site-form" className="project-directory-create" onSubmit={create}><h2>新建独立网站</h2><input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="域名，例如 example.com" /><input value={form.industry} onChange={(event) => setForm({ ...form, industry: event.target.value })} placeholder="行业，例如 Industrial LED" /><input value={form.site_url} onChange={(event) => setForm({ ...form, site_url: event.target.value })} placeholder="网站地址（可选）" /><button className="primary">创建网站项目</button></form>}
    {notice ? <p className="project-directory-notice">{notice}</p> : null}
    <div className="project-directory-grid">{projects.map((project) => <article key={project.id}><div className="project-directory-card-head"><div><button type="button" className="project-directory-project-link" onClick={() => openProject(project.id)}>{project.site_url || project.name}</button><span>行业：{project.industry || "未设置"}　|　{project.default_country} / {project.default_language}</span></div><em className={project.latest_content_status === "failed" ? "is-warning" : ""}>● {project.latest_content_status === "failed" ? "需处理" : "正常"}</em></div><dl><div><dt>关键词</dt><dd><button type="button" onClick={() => openProject(project.id, "/keywords")} aria-label={`打开 ${project.name} 的关键词库`}>{project.keyword_count}</button></dd></div><div><dt>已选标题</dt><dd><button type="button" onClick={() => openProject(project.id, "/title-library")} aria-label={`打开 ${project.name} 的标题库`}>{project.selected_title_count}</button></dd></div><div><dt>内容文章</dt><dd><button type="button" onClick={() => openProject(project.id, "/content-library")} aria-label={`打开 ${project.name} 的内容库`}>{project.content_count}</button></dd></div><div><dt>知识库</dt><dd><button type="button" onClick={() => openProject(project.id, "/knowledge")} aria-label={`进入 ${project.name} 项目的知识库工作区`}>{project.knowledge_count}</button></dd></div></dl><footer><span>最近任务：{project.latest_content_status || "暂无任务"}</span><button className="primary" onClick={() => openProject(project.id)}>进入项目</button></footer></article>)}</div>
  </section>;
}

export default function App() {
  return <BrowserRouter><Workspace /></BrowserRouter>;
}

/**
 * The original production workflow is also used by the new project console.
 * Keeping this surface component-based (rather than sending users back to the
 * former routes) means there is one implementation for every live operation.
 */
export function Workspace({ embedded = false, forcedProjectId, legacyPath }: { embedded?: boolean; forcedProjectId?: number | null; legacyPath?: string } = {}) {
  const navigate = useNavigate();
  const location = useLocation();
  const activePath = legacyPath || location.pathname;
  const routeLocation = legacyPath ? { ...location, pathname: legacyPath } : location;
  const currentSiteId = new URLSearchParams(location.search).get("site_id");
  const isProjectContext = embedded || Boolean(currentSiteId);
  const [seedsText, setSeedsText] = useState("seo tools");
  const [language, setLanguage] = useState("en");
  const [country, setCountry] = useState("US");
  const [maxRequests, setMaxRequests] = useState("20");
  const [maxDepth, setMaxDepth] = useState("3");
  const [run, setRun] = useState<RunState>(null);
  const [logs, setLogs] = useState<string[]>(["等待关键词任务。"]);
  const [busy, setBusy] = useState(false);
  const [reviews, setReviews] = useState<Record<string, Review>>({});
  const [reviewStatus, setReviewStatus] = useState("等待扩词结果");
  const [reviewMode, setReviewMode] = useState<"fast" | "hybrid">("hybrid");
  const [projectId, setProjectId] = useState<number | null>(() => forcedProjectId || Number(new URLSearchParams(window.location.search).get("project_id")) || Number(localStorage.getItem(projectKey)) || null);
  const [currentProjectName, setCurrentProjectName] = useState("");
  const [library, setLibrary] = useState<LibraryKeyword[]>([]);
  const [libraryStatus, setLibraryStatus] = useState("审核通过的扩展词可加入关键词库。");
  const [scoreInputs, setScoreInputs] = useState<ScoreInputs>({ keyword: "", volume: "", authority: "", domains: "", titleMatch: "", authoritySites: "", intent: "3", relevance: "80", businessValue: "80" });
  const [score, setScore] = useState<Score | null>(null);
  const [titleKeyword, setTitleKeyword] = useState<LibraryKeyword | null>(null);
  const [titleCandidates, setTitleCandidates] = useState<TitleCandidate[]>([]);
  const [titleLibrary, setTitleLibrary] = useState<TitleCandidate[]>([]);
  const [contentAssets, setContentAssets] = useState<ContentAsset[]>([]);
  const [contentLibraryAssets, setContentLibraryAssets] = useState<ContentAsset[]>([]);
  const [contentStatus, setContentStatus] = useState("从标题库选择已选定标题，创建内容 Brief 和大纲。");
  const [titleStatus, setTitleStatus] = useState("从关键词库选择一个已审核关键词，生成美国市场 SEO 标题。");
  const [titleType, setTitleType] = useState("auto");
  const [titleCount, setTitleCount] = useState("8");
  const [manualTitle, setManualTitle] = useState("");
  const [serpTitles, setSerpTitles] = useState<SerpTitle[]>([]);
  const [serpStatus, setSerpStatus] = useState("尚未抓取 Google 排名标题。");
  const [verificationImage, setVerificationImage] = useState<string | null>(null);
  const [aiProfiles, setAiProfiles] = useState<Record<AiProvider, AiProfile>>(initialAiProfiles);
  const [aiConfigured, setAiConfigured] = useState<Record<AiProvider, boolean>>({ openai: false, gemini: false, deepseek: false });
  const [aiAssignments, setAiAssignments] = useState<AiAssignments>({ keyword_review: "openai", title_generation: "openai" });
  const [aiStatus, setAiStatus] = useState("正在读取 AI 配置…");
  const [serperKey, setSerperKey] = useState("");
  const [serperConfigured, setSerperConfigured] = useState(false);
  const [serperStatus, setSerperStatus] = useState("正在读取 Serper 搜索配置…");
  const [sidebarGroup, setSidebarGroup] = useState<"project" | "tasks" | "ai">("project");
  const [projectNavigationSection, setProjectNavigationSection] = useState<"knowledge" | "keywords" | "titles" | "content" | "site" | null>("keywords");
  const reviewedCount = useMemo(() => Object.keys(reviews).length, [reviews]);
  const isIntegrationPage = activePath === "/integrations";
  const isProjectHome = activePath === "/projects" || /^\/projects\/\d+$/.test(activePath) || activePath === "/agent-platform" || /^\/agent-platform\/site\/\d+$/.test(activePath);
  const showKeywordMetrics = ["/research", "/keywords", "/scoring"].includes(activePath);
  const projectQuery = projectId ? `?project_id=${projectId}` : "";
  useEffect(() => {
    if (activePath === "/system-tasks") setSidebarGroup("tasks");
    else if (["/integrations", "/settings"].includes(activePath)) setSidebarGroup("ai");
    else {
      setSidebarGroup("project");
      if (["/knowledge", "/website-crawl"].includes(activePath)) setProjectNavigationSection("knowledge");
      else if (["/research", "/keywords", "/scoring"].includes(activePath)) setProjectNavigationSection("keywords");
      else if (["/titles", "/title-library"].includes(activePath)) setProjectNavigationSection("titles");
      else if (["/content", "/content-library", "/content-memory", "/competitor-learning", "/learning-memories"].includes(activePath) || activePath.startsWith("/content-library/")) setProjectNavigationSection("content");
      else if (["/authority-sources", "/knowledge", "/website-crawl", "/gsc", "/content-publish"].includes(activePath)) setProjectNavigationSection("site");
    }
  }, [activePath]);
  const headerMeta = (() => {
    if (isIntegrationPage) return { eyebrow: "系统配置", title: "系统连接中心", description: "配置全局 AI 模型与搜索服务；网站发布账号仍由每个项目独立管理。" };
    if (activePath === "/research") return { eyebrow: "关键词研究", title: "关键词挖掘", description: "从种子词发现、审核并沉淀当前网站的内容机会。" };
    if (activePath === "/keywords") return { eyebrow: "Keyword library", title: "关键词库", description: "管理当前网站已审核的关键词、意图和内容机会。" };
    if (activePath === "/titles" || activePath === "/title-library") return { eyebrow: "内容策划", title: "标题与选题", description: "从当前网站关键词选择、生成和锁定可进入内容生产的 SEO 标题。" };
    if (activePath === "/content" || activePath.startsWith("/content-library")) return { eyebrow: "内容生产", title: "内容生产", description: "让标题、公司知识库、竞品研究和模型运行记录在一个可追溯的工作区协同。" };
    if (activePath === "/authority-sources") return { eyebrow: "证据资料库", title: "权威来源库", description: "沉淀当前网站可复用的可信来源与可支持的内容主题。" };
    if (activePath === "/knowledge") return { eyebrow: "网站资料", title: "公司知识库", description: "采集和上传当前网站的产品、品牌与公司资料，为内容生成提供可追溯的一方事实。" };
    if (activePath === "/website-crawl") return { eyebrow: "网站资料", title: "网站采集", description: "从当前网站的 About Us、产品、工厂、认证、案例、FAQ 与公开联系页面提取可用的一方资料。" };
    if (activePath === "/content-memory") return { eyebrow: "Competitor memory", title: "竞品学习记忆", description: "查看并管理当前网站的竞品研究资料，后续写作会按相关性检索。" };
    if (activePath === "/competitor-learning") return { eyebrow: "Periodic Competitor Learning", title: "定期竞品学习与写法卡片", description: "按项目主题定期重新研究可读竞品文章，并沉淀仅供结构与写法学习的策略卡。" };
    if (activePath === "/learning-memories") return { eyebrow: "Learning Memory & Feedback", title: "学习记忆与人工反馈", description: "用人工优先级、置顶和反馈校准 AI 后续写作；来源与 GSC 原始证据始终保留只读。" };
    if (activePath === "/gsc") return { eyebrow: "Google Search Console", title: "GSC 网站绑定与锚文本", description: "读取当前网站的真实排名查询和页面数据，为内容内链提供可追溯的锚文本建议。" };
    if (activePath === "/content-publish") return { eyebrow: "Website publishing", title: "内容发布", description: "只配置当前网站的 WordPress 后台，并默认将文章创建为草稿。" };
    if (activePath === "/settings") return { eyebrow: "AI configuration", title: "AI 模型配置", description: "保存提供商、模型和功能分配；内容任务将严格使用创建时锁定的模型。" };
    if (activePath === "/scoring") return { eyebrow: "SEO opportunity", title: "机会评分", description: "结合真实可用数据判断关键词竞争难度与投入优先级。" };
    if (activePath === "/system-tasks") return { eyebrow: "System activity", title: "系统任务", description: "集中查看采集、AI 生成和发布任务的真实运行状态。" };
    return { eyebrow: "SEO 中控系统", title: "关键词工作台", description: "挖掘、审核、分类、入库和机会评估。" };
  })();

  const loadLibrary = async (id = projectId) => {
    if (!id) return;
    const keywords = await api.listKeywords(id);
    setLibrary(keywords);
  };

  useEffect(() => {
    const restoreProjectAndAssets = async () => {
      let activeProjectId = projectId;
      if (!activeProjectId) {
        const projects = await api.listProjects();
        activeProjectId = projects[0]?.id ?? null;
        if (activeProjectId) {
          localStorage.setItem(projectKey, String(activeProjectId));
          setProjectId(activeProjectId);
        }
      }
      try {
        const [settings, serper] = await Promise.all([api.getAiSettings(), api.getSerperSettings()]);
        setAiProfiles((current) => (Object.keys(aiProviderPresets) as AiProvider[]).reduce((profiles, provider) => {
          const saved = settings.providers?.[provider];
          profiles[provider] = { baseUrl: saved?.base_url || current[provider].baseUrl, model: saved?.model || current[provider].model, apiKey: "" };
          return profiles;
        }, {} as Record<AiProvider, AiProfile>));
        setAiConfigured((Object.keys(aiProviderPresets) as AiProvider[]).reduce((profiles, provider) => ({ ...profiles, [provider]: Boolean(settings.providers?.[provider]?.configured) }), {} as Record<AiProvider, boolean>));
        if (settings.assignments?.keyword_review && settings.assignments?.title_generation) setAiAssignments(settings.assignments as AiAssignments);
        setAiStatus(settings.configured ? `已配置：${settings.provider || "兼容接口"}` : "尚未配置 AI 接口。");
        setSerperConfigured(serper.configured);
        setSerperStatus(serper.configured ? "已保存；权威来源搜索优先使用 Serper。" : "未配置；将使用本机浏览器 Google 搜索。");
      } catch (error) {
        setAiStatus(error instanceof Error ? `AI 配置读取失败：${error.message}` : "AI 配置读取失败。");
      }
      if (activeProjectId) await Promise.allSettled([loadLibrary(activeProjectId), loadTitleLibrary(activeProjectId), loadContentAssets(activeProjectId), loadContentLibrary(activeProjectId)]);
    };
    restoreProjectAndAssets().catch(() => setAiStatus("项目初始化失败；请刷新后重试。"));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const selected = Number(new URLSearchParams(location.search).get("project_id")) || null;
    if (selected && selected !== projectId) { localStorage.setItem(projectKey, String(selected)); setProjectId(selected); void Promise.allSettled([loadLibrary(selected), loadTitleLibrary(selected), loadContentAssets(selected), loadContentLibrary(selected)]); }
  }, [location.search, projectId]);
  useEffect(() => {
    if (!projectId) { setCurrentProjectName(""); return; }
    api.listProjects()
      .then((projects) => {
        const project = projects.find((item) => item.id === projectId);
        setCurrentProjectName(project?.site_url || project?.name || "");
      })
      .catch(() => setCurrentProjectName(""));
  }, [projectId]);

  const ensureProject = async () => {
    if (projectId) return projectId;
    const project = await api.createProject({ name: "默认关键词项目", country_code: country, language_code: language });
    localStorage.setItem(projectKey, String(project.id));
    setProjectId(project.id);
    return project.id;
  };

  const loadTitleCandidates = async (keyword = titleKeyword, id = projectId) => {
    if (!keyword || !id) return;
    const result = await api.listTitleCandidates(id, keyword.id);
    setTitleCandidates(result.candidates);
  };

  const loadTitleLibrary = async (id = projectId) => {
    if (!id) return;
    setTitleLibrary(await api.listTitleLibrary(id));
  };
  const loadContentAssets = async (id = projectId) => { if (id) setContentAssets(await api.listContentAssets(id)); };
  const loadContentLibrary = async (id = projectId) => { if (id) setContentLibraryAssets(await api.listContentLibrary(id)); };
  const refreshContentData = async (id = projectId) => { await Promise.all([loadContentAssets(id), loadContentLibrary(id)]); };
  const deleteContentAssets = async (assetIds: number[]) => {
    if (!projectId || !assetIds.length) return;
    try {
      const result = await api.deleteContentAssets({ project_id: projectId, content_asset_ids: assetIds });
      setContentStatus(`已删除 ${result.deleted} 篇内容资产及其关联版本。`);
      await refreshContentData();
    } catch (error) { setContentStatus(error instanceof Error ? error.message : "删除内容失败。"); }
  };
  const createContentFromTitle = async (title: TitleCandidate) => {
    if (!projectId) return;
    try {
      const asset = await api.createContentAsset({ project_id: projectId, selected_title_candidate_id: title.id, content_type: "guide" });
      setContentStatus(`已创建内容：${asset.title_snapshot}`);
      await refreshContentData();
      navigate("/content");
    } catch (error) { setContentStatus(error instanceof Error ? error.message : "创建内容失败。"); navigate("/content"); }
  };
  const selectLibraryTitle = async (title: TitleCandidate) => {
    if (!projectId) return;
    try { await api.selectTitleCandidate(title.id, { project_id: projectId }); }
    catch (error) {
      const message = error instanceof Error ? error.message : "选定标题失败。";
      if (!message.toLowerCase().includes("replace") || !window.confirm("该关键词已有选定标题，确认替换为当前标题吗？")) { setTitleStatus(message); return; }
      try { await api.selectTitleCandidate(title.id, { project_id: projectId, confirm_replace: true }); }
      catch (retryError) { setTitleStatus(retryError instanceof Error ? retryError.message : "替换标题失败。"); return; }
    }
    await Promise.all([loadTitleLibrary(), loadLibrary()]);
    setTitleStatus("标题已选定，现在可以加入内容生成。");
  };

  const openTitleWorkspace = async (keyword: LibraryKeyword) => {
    if (!projectId) return;
    setTitleKeyword(keyword);
    setTitleCandidates([]);
    setSerpTitles([]);
    setVerificationImage(null);
    setSerpStatus("正在读取本项目已保存的标题学习样本…");
    setTitleStatus(keyword.is_seo_content_fit === 1 ? `已选择“${keyword.keyword}”，默认按美国市场 en-US 生成。` : "该关键词尚未通过 SEO 审核，不能生成标题。");
    if (keyword.is_seo_content_fit !== 1) return;
    try {
      const [memory] = await Promise.all([api.listSerpTitleMemory(projectId, keyword.id), loadTitleCandidates(keyword)]);
      setSerpTitles(memory.titles);
      setSerpStatus(memory.titles.length ? `已读取 ${memory.titles.length} 条本项目标题学习样本；生成时会参考写法但不会复制。` : "尚未抓取 Google 排名标题。");
      // Keep the project explicit in the destination URL.  Relying solely on
      // localStorage made a direct keyword-to-title jump lose its project
      // context after refreshes or when more than one project was open.
      const params = new URLSearchParams({ project_id: String(projectId) });
      if (currentSiteId) params.set("site_id", currentSiteId);
      navigate(`/titles?${params.toString()}`);
    } catch (error) {
      setTitleStatus(error instanceof Error ? error.message : "读取标题候选失败。");
    }
  };

  const generateTitleCandidates = async () => {
    if (!titleKeyword || !projectId) return;
    setBusy(true);
    setTitleStatus("正在按美国本地搜索习惯生成 SEO 标题候选…");
    try {
      const referenceTitles = serpTitles.map((item) => item.title).slice(0, 20);
      const job = await api.createTitleJob({ project_id: projectId, keyword_id: titleKeyword.id, locale: "en-US", count: Number(titleCount), title_type: titleType === "auto" ? undefined : titleType, competitor_titles: referenceTitles });
      await loadTitleCandidates();
      await loadLibrary();
      await loadTitleLibrary();
      setTitleStatus(`生成完成：已保存 ${job.generated_count} 个候选。每个关键词只能选定一个标题。`);
    } catch (error) {
      setTitleStatus(error instanceof Error ? error.message : "标题生成失败。");
    } finally {
      setBusy(false);
    }
  };

  const generateMultiProviderTitles = async () => {
    if (!titleKeyword || !projectId) return;
    setBusy(true);
    setTitleStatus("ChatGPT、Gemini、DeepSeek 正在各自学习 Google 标题结构并各生成 3 个候选…");
    try {
      const job = await api.generateMultiProviderTitles({ project_id: projectId, keyword_id: titleKeyword.id, locale: "en-US", title_type: titleType === "auto" ? undefined : titleType, competitor_titles: serpTitles.map((item) => item.title).slice(0, 20) });
      await loadTitleCandidates(); await loadLibrary(); await loadTitleLibrary();
      setTitleStatus(`三模型生成完成：已保存 ${job.generated_count} 个候选。${job.failures.length ? job.failures.join("；") : ""}`);
    } catch (error) { setTitleStatus(error instanceof Error ? error.message : "三模型标题生成失败。"); }
    finally { setBusy(false); }
  };

  const researchSerpTitles = async () => {
    if (!titleKeyword || !projectId) return;
    setBusy(true);
    setSerpStatus("AI 正在抓取 Google 前 20 排名标题…");
    try {
      const result = await api.researchSerpTitles({ project_id: projectId, keyword_id: titleKeyword.id, locale: "en-US" });
      setSerpTitles(result.titles);
      setSerpStatus(result.warning || `已提取并保存 ${result.titles.length} 条排名标题，已加入本项目的标题学习样本。`);
    } catch (error) {
      setSerpStatus(error instanceof Error ? error.message : "AI SERP 标题抓取失败。");
    } finally {
      setBusy(false);
    }
  };

  const researchBrowserSerpTitles = async () => {
    if (!titleKeyword || !projectId) return;
    setBusy(true);
    setSerpStatus("浏览器正在抓取 Google 美国自然排名标题…");
    try {
      const result = await api.researchBrowserSerpTitles({ project_id: projectId, keyword_id: titleKeyword.id, locale: "en-US" });
      setSerpTitles(result.titles);
      setVerificationImage(result.verification_image || null);
      if (result.verification_required) {
        setSerpStatus("Google 要求浏览器验证：请在验证码图片对应的 Chrome 窗口完成验证后，再点击一次抓取。");
        return;
      }
      setSerpStatus(`浏览器已抓取并保存 ${result.titles.length} 条 Google 自然排名标题；AI 将学习搜索意图与写法，不会复制原题。`);
    } catch (error) {
      setSerpStatus(error instanceof Error ? error.message : "浏览器 Google 标题抓取失败。");
    } finally {
      setBusy(false);
    }
  };

  const saveAiProvider = async (provider: AiProvider) => {
    try {
      const profile = aiProfiles[provider];
      const result = await api.saveAiSettings({ providers: { [provider]: { api_key: profile.apiKey, base_url: profile.baseUrl, model: profile.model } }, assignments: aiAssignments });
      setAiProfiles((current) => ({ ...current, [provider]: { ...current[provider], apiKey: "" } }));
      setAiConfigured((Object.keys(aiProviderPresets) as AiProvider[]).reduce((profiles, provider) => ({ ...profiles, [provider]: Boolean(result.providers?.[provider]?.configured) }), {} as Record<AiProvider, boolean>));
      setAiStatus(`已保存 ${provider} 配置。`);
    } catch (error) {
      setAiStatus(error instanceof Error ? error.message : "保存 AI 配置失败。");
    }
  };

  const saveAiAssignments = async () => {
    try {
      await api.saveAiSettings({ providers: {}, assignments: aiAssignments });
      setAiStatus(`已保存功能分配：审核使用 ${aiAssignments.keyword_review}，标题使用 ${aiAssignments.title_generation}。`);
    } catch (error) {
      setAiStatus(error instanceof Error ? error.message : "保存功能分配失败。");
    }
  };

  const testAiSettings = async (provider: AiProvider) => {
    setAiStatus(`正在测试 ${provider} 连接…`);
    try {
      const profile = aiProfiles[provider];
      const result = await api.testAiSettings(profile.apiKey ? { provider, config: profile } : { provider });
      setAiStatus(`连接成功：${result.provider} / ${result.model}`);
    } catch (error) {
      setAiStatus(error instanceof Error ? error.message : "AI 连接测试失败。");
    }
  };

  const updateAiProfile = (provider: AiProvider, field: keyof AiProfile, value: string) => {
    setAiProfiles((current) => ({ ...current, [provider]: { ...current[provider], [field]: value } }));
  };
  const saveSerper = async () => {
    if (!serperKey.trim()) { setSerperStatus("请输入 Serper API Key 后再保存。"); return; }
    try { const result = await api.saveSerperSettings({ api_key: serperKey }); setSerperConfigured(result.configured); setSerperKey(""); setSerperStatus("已保存。权威来源搜索将优先使用 Serper。"); }
    catch (error) { setSerperStatus(error instanceof Error ? error.message : "Serper 配置保存失败。"); }
  };
  const testSerper = async () => {
    setSerperStatus("正在连接 Serper.dev…");
    try { const result = await api.testSerperSettings(serperKey.trim() ? { api_key: serperKey } : {}); setSerperStatus(`连接成功：${result.sample_title}`); }
    catch (error) { setSerperStatus(error instanceof Error ? error.message : "Serper 连接测试失败。"); }
  };

  const selectTitleCandidate = async (candidate: TitleCandidate) => {
    if (!projectId || !titleKeyword) return;
    try {
      await api.selectTitleCandidate(candidate.id, { project_id: projectId });
    } catch (error) {
      const message = error instanceof Error ? error.message : "选定标题失败。";
      if (!message.toLowerCase().includes("replace") || !window.confirm("该关键词已有选定标题。确认替换为当前标题吗？")) {
        setTitleStatus(message);
        return;
      }
      try {
        await api.selectTitleCandidate(candidate.id, { project_id: projectId, confirm_replace: true });
      } catch (retryError) {
        setTitleStatus(retryError instanceof Error ? retryError.message : "替换标题失败。");
        return;
      }
    }
    await loadTitleCandidates();
    await loadLibrary();
    await loadTitleLibrary();
    setTitleStatus("标题已选定。后续大纲和正文模块将只使用这个标题。");
  };

  const addManualTitle = async () => {
    if (!projectId || !titleKeyword || !manualTitle.trim()) return;
    try {
      await api.createTitleCandidate({ project_id: projectId, keyword_id: titleKeyword.id, title: manualTitle, title_type: titleType === "auto" ? "manual" : titleType, search_intent: titleKeyword.search_intent || undefined });
      setManualTitle("");
      await loadTitleCandidates();
      setTitleStatus("人工标题已保存为候选，可再选定。 ");
    } catch (error) {
      setTitleStatus(error instanceof Error ? error.message : "保存人工标题失败。");
    }
  };

  const deleteTitleCandidate = async (candidate: TitleCandidate) => {
    if (!projectId || !window.confirm("确认删除此标题候选吗？")) return;
    try {
      await api.deleteTitleCandidate(candidate.id, { project_id: projectId });
      await loadTitleCandidates();
      setTitleStatus("已删除标题候选。");
    } catch (error) {
      setTitleStatus(error instanceof Error ? error.message : "删除标题候选失败。");
    }
  };
  const deleteTitleLibraryCandidates = async (candidateIds: number[]) => {
    if (!projectId || !candidateIds.length) return;
    try {
      await api.deleteTitleCandidates({ project_id: projectId, candidate_ids: candidateIds });
      await Promise.all([loadTitleLibrary(), loadLibrary()]);
      setTitleStatus(`已删除 ${candidateIds.length} 条标题候选。`);
    } catch (error) { setTitleStatus(error instanceof Error ? error.message : "删除标题失败。"); }
  };

  const expand = async () => {
    const seeds = seedsText.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    if (!seeds.length) return setLogs(["请至少输入一个种子关键词。"]);
    setBusy(true);
    setReviews({});
    setReviewStatus("等待扩词完成");
    setLogs([`开始扩展：${seeds.length} 个种子词，最多 ${maxRequests} 次请求、${maxDepth} 层。`]);
    try {
      const result = await api.expand({ seed_keywords: seeds, hl: language, gl: country, max_keywords: 1000, max_requests: Number(maxRequests), max_depth: Number(maxDepth) });
      setRun({ seeds, language, country, result });
      setScoreInputs((current) => ({ ...current, keyword: result.keywords[0] || "" }));
      setLogs((current) => [...current, ...result.debug_logs.map((entry) => `${entry.event}：${entry.message}（${entry.code}）`), `完成：${result.keywords.length} 个词，${result.requests_made} 次请求，${result.stop_reason}。`]);
    } catch (error) {
      setLogs((current) => [...current, error instanceof Error ? error.message : "扩展失败。"]);
    } finally {
      setBusy(false);
    }
  };

  const persistReviewedKeywords = async (runState: NonNullable<RunState>, reviewed: Record<string, Review>) => {
    const keywords = runState.result.keywords.map((keyword) => ({ keyword, review: reviewed[keyword] }))
      .filter(({ review }) => review.is_seo_content_fit && review.same_topic_as_seed)
      .map(({ keyword, review }) => ({ keyword, category: categoryOf(keyword, review), search_intent: review.search_intent, is_seo_content_fit: review.is_seo_content_fit, same_topic_as_seed: review.same_topic_as_seed, recommended_action: review.recommended_action, review_reason: review.reason, review_confidence: review.confidence, demand_estimate: demandEstimate(keyword) }));
    if (!keywords.length) {
      setLibraryStatus("本次没有通过审核的关键词，未写入关键词库。");
      return { inserted: 0, existing: 0 };
    }
    const id = await ensureProject();
    const result = await api.saveExpanded({ project_id: id, country_code: runState.country, language_code: runState.language, seed_keyword: runState.seeds[0], keywords });
    setLibraryStatus(`自动入库完成：新增 ${result.inserted} 个，已存在 ${result.existing} 个。`);
    await loadLibrary(id);
    return result;
  };

  const reviewKeywords = async () => {
    if (!run) return;
    setBusy(true);
    const next: Record<string, Review> = {};
    try {
      let cursor = 0;
      const worker = async () => {
        while (cursor < run.result.keywords.length) {
          const index = cursor++;
          const keyword = run.result.keywords[index];
          const modeDescription = reviewMode === "fast" ? "本地规则，零 AI 请求" : "混合模式，规则预筛 + AI 精审";
          setReviewStatus(`正在审核 ${index + 1}/${run.result.keywords.length}（${modeDescription}，最多 3 个并发）`);
          next[keyword] = (await api.review({ seed_keyword: run.seeds[0], keyword, language: run.language, mode: reviewMode })).review;
        }
      };
      await Promise.all(Array.from({ length: Math.min(3, run.result.keywords.length) }, worker));
      setReviews(next);
      const approved = Object.values(next).filter((review) => review.is_seo_content_fit && review.same_topic_as_seed).length;
      try {
        const saved = await persistReviewedKeywords(run, next);
        setReviewStatus(`审核完成并已入库：${approved}/${run.result.keywords.length} 个词通过，新增 ${saved.inserted} 个，已存在 ${saved.existing} 个。`);
      } catch (error) {
        const message = error instanceof Error ? error.message : "自动入库失败。";
        setLibraryStatus(message);
        setReviewStatus(`审核完成：${approved}/${run.result.keywords.length} 个词通过；自动入库失败，请稍后重新审核。`);
      }
    } catch (error) {
      setReviewStatus(error instanceof Error ? error.message : "审核失败。");
    } finally {
      setBusy(false);
    }
  };

  const removeKeywords = async (ids: number[], clearAll = false) => {
    if (!projectId || !window.confirm(clearAll ? "确认清空当前项目的关键词库吗？该操作可恢复。" : "确认删除该关键词吗？该操作可恢复。")) return;
    try {
      const result = await api.deleteKeywords(clearAll ? { project_id: projectId, clear_all: true, confirm_project_id: projectId } : { project_id: projectId, keyword_ids: ids });
      setLibraryStatus(`已移除 ${result.deleted} 个关键词。`);
      await loadLibrary();
    } catch (error) {
      setLibraryStatus(error instanceof Error ? error.message : "删除失败。");
    }
  };

  const exportKeywordLibrary = () => {
    if (!library.length) {
      setLibraryStatus("关键词库没有数据可导出。");
      return;
    }
    const escapeCsv = (value: string | number | null) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const rows = [
      ["关键词", "分类", "搜索意图", "需求预估指数", "真实 VOL", "SEO 审核"],
      ...library.map((keyword) => [keyword.keyword, keyword.category, readableIntent(keyword.search_intent || undefined), keyword.demand_estimate, keyword.search_volume, keyword.is_seo_content_fit === 1 ? "适合" : "待审核"]),
    ];
    const csv = `\uFEFF${rows.map((row) => row.map(escapeCsv).join(",")).join("\r\n")}`;
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `关键词库-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
    setLibraryStatus(`已导出 ${library.length} 个关键词。`);
  };

  const calculateScore = async () => {
    try {
      const number = (key: keyof ScoreInputs) => Number(scoreInputs[key]);
      const result = await api.score({ items: [{ keyword: scoreInputs.keyword, monthly_search_volume: number("volume"), average_domain_authority: number("authority"), average_referring_domains: number("domains"), exact_title_match_rate: number("titleMatch") / 100, authority_site_ratio: number("authoritySites") / 100, intent_competition: number("intent"), relevance_score: number("relevance") / 100, business_value_score: number("businessValue") / 100 }] });
      setScore(result.scores[0]);
    } catch (error) {
      setScore(null);
      setLogs((current) => [...current, error instanceof Error ? error.message : "评分失败。"]);
    }
  };

  const setScoreField = (key: keyof ScoreInputs, value: string) => setScoreInputs((current) => ({ ...current, [key]: value }));

  return <div className={embedded ? "cc-legacy-operations" : "app-shell"}><a className="skip-link" href="#main-content">跳至主要内容</a>
    {!embedded && <aside className="sidebar control-sidebar">
      <div className="brand"><span>SEO</span><div><b>SEO 中控系统</b><small>多网站内容运营平台</small></div></div>
      <nav aria-label="主导航">
        <button type="button" className={sidebarGroup === "project" ? "nav-group-toggle active" : "nav-group-toggle"} aria-expanded={sidebarGroup === "project"} onClick={() => setSidebarGroup("project")}>网站项目 <i>{sidebarGroup === "project" ? "⌃" : "⌄"}</i></button>
        {sidebarGroup === "project" && <div className="nav-children">
          <NavLink to="/projects">全部网站项目</NavLink>
          <NavLink to="/projects">新建网站</NavLink>
          {projectId ? <><p className="nav-context">{currentProjectName || `当前项目 #${projectId}`}</p>
            <div className="project-nav-tree">
              <button type="button" className={projectNavigationSection === "knowledge" ? "project-nav-section is-active" : "project-nav-section"} aria-expanded={projectNavigationSection === "knowledge"} aria-controls="project-nav-knowledge" onClick={() => setProjectNavigationSection((current) => current === "knowledge" ? null : "knowledge")}>公司知识库 <i>{projectNavigationSection === "knowledge" ? "⌃" : "⌄"}</i></button>
              {projectNavigationSection === "knowledge" && <div className="project-nav-pages" id="project-nav-knowledge"><NavLink to={`/knowledge${projectQuery}`}>知识库管理</NavLink><NavLink to={`/website-crawl${projectQuery}`}>网站采集</NavLink></div>}

              <button type="button" className={projectNavigationSection === "keywords" ? "project-nav-section is-active" : "project-nav-section"} aria-expanded={projectNavigationSection === "keywords"} aria-controls="project-nav-keywords" onClick={() => setProjectNavigationSection((current) => current === "keywords" ? null : "keywords")}>关键词研究 <i>{projectNavigationSection === "keywords" ? "⌃" : "⌄"}</i></button>
              {projectNavigationSection === "keywords" && <div className="project-nav-pages" id="project-nav-keywords"><NavLink to={`/research${projectQuery}`}>关键词挖掘</NavLink><NavLink to={`/keywords${projectQuery}`}>关键词库</NavLink><NavLink to={`/scoring${projectQuery}`}>机会评分</NavLink></div>}

              <button type="button" className={projectNavigationSection === "titles" ? "project-nav-section is-active" : "project-nav-section"} aria-expanded={projectNavigationSection === "titles"} aria-controls="project-nav-titles" onClick={() => setProjectNavigationSection((current) => current === "titles" ? null : "titles")}>标题与选题 <i>{projectNavigationSection === "titles" ? "⌃" : "⌄"}</i></button>
              {projectNavigationSection === "titles" && <div className="project-nav-pages" id="project-nav-titles"><NavLink to={`/titles${projectQuery}`}>生成标题</NavLink><NavLink to={`/title-library${projectQuery}`}>标题库</NavLink></div>}

              <button type="button" className={projectNavigationSection === "content" ? "project-nav-section is-active" : "project-nav-section"} aria-expanded={projectNavigationSection === "content"} aria-controls="project-nav-content" onClick={() => setProjectNavigationSection((current) => current === "content" ? null : "content")}>内容生产 <i>{projectNavigationSection === "content" ? "⌃" : "⌄"}</i></button>
              {projectNavigationSection === "content" && <div className="project-nav-pages" id="project-nav-content"><NavLink to={`/content${projectQuery}`}>内容任务</NavLink><NavLink to={`/content-library${projectQuery}`}>内容库</NavLink><NavLink to={`/content-memory${projectQuery}`}>竞品学习记忆</NavLink><NavLink to={`/competitor-learning${projectQuery}`}>定期竞品学习</NavLink><NavLink to={`/learning-memories${projectQuery}`}>学习记忆与反馈</NavLink></div>}

              <button type="button" className={projectNavigationSection === "site" ? "project-nav-section is-active" : "project-nav-section"} aria-expanded={projectNavigationSection === "site"} aria-controls="project-nav-site" onClick={() => setProjectNavigationSection((current) => current === "site" ? null : "site")}>网站资料与发布 <i>{projectNavigationSection === "site" ? "⌃" : "⌄"}</i></button>
              {projectNavigationSection === "site" && <div className="project-nav-pages" id="project-nav-site"><NavLink to={`/authority-sources${projectQuery}`}>权威来源库</NavLink><NavLink to={`/gsc${projectQuery}`}>GSC 绑定与锚文本</NavLink><NavLink to={`/content-publish${projectQuery}`}>内容发布</NavLink></div>}
            </div>
          </> : <p className="nav-empty">请选择网站项目后进入工作流</p>}
        </div>}
        <button type="button" className={sidebarGroup === "tasks" ? "nav-group-toggle active" : "nav-group-toggle"} aria-expanded={sidebarGroup === "tasks"} onClick={() => setSidebarGroup("tasks")}>系统任务 <i>{sidebarGroup === "tasks" ? "⌃" : "⌄"}</i></button>
        {sidebarGroup === "tasks" && <div className="nav-children"><NavLink to="/system-tasks">任务总览</NavLink><span>关键词与标题</span><span>内容生成</span><span>网站采集</span><span>发布记录</span></div>}
        <button type="button" className={sidebarGroup === "ai" ? "nav-group-toggle active" : "nav-group-toggle"} aria-expanded={sidebarGroup === "ai"} onClick={() => setSidebarGroup("ai")}>AI 与集成 <i>{sidebarGroup === "ai" ? "⌃" : "⌄"}</i></button>
        {sidebarGroup === "ai" && <div className="nav-children"><NavLink to="/integrations">AI 服务商</NavLink><NavLink to="/settings">模型分配与图片</NavLink><NavLink to="/integrations">Serper 搜索服务</NavLink></div>}
      </nav>
      <p className="connection">● 服务已连接<br /><small>当前数据按网站独立隔离</small></p>
    </aside>}
    <main className={`workspace ${isProjectHome ? "workspace-project-home" : ""}`} id="main-content">
      {!embedded && !isProjectHome && <header><div><p className="eyebrow">{headerMeta.eyebrow}</p><h1>{headerMeta.title}</h1><p>{headerMeta.description}</p></div><span className="project-chip">{isIntegrationPage ? "全局配置" : `项目：${projectId ? `#${projectId}` : "未创建"}`}</span></header>}

      {showKeywordMetrics && <section className="metrics"><Metric label="本次扩展关键词" value={run?.result.keywords.length ?? 0} /><Metric label="下拉词请求数" value={run?.result.requests_made ?? 0} /><Metric label="已审核关键词" value={reviewedCount} /><Metric label="已入库关键词" value={library.length} /></section>}

      <Routes location={routeLocation}>
      <Route path="/agent-platform" element={<Navigate to="/projects" replace />} />
      <Route path="/agent-platform/site/:siteId" element={<AgentPlatformConsole />} />
      <Route path="/projects" element={<ProjectDirectory />} />
      <Route path="/projects/:siteId" element={<ProjectDirectory />} />
      <Route path="/system-tasks" element={<section className="panel"><PanelTitle eyebrow="System Tasks" title="系统任务" /><p className="empty">采集、AI 归纳和发布任务将在这里显示真实执行状态。</p></section>} />
      <Route path="/integrations" element={<IntegrationHub aiConfigured={aiConfigured} aiProfiles={aiProfiles} aiStatus={aiStatus} serperKey={serperKey} serperConfigured={serperConfigured} serperStatus={serperStatus} onSerperChange={setSerperKey} onSaveSerper={saveSerper} onTestSerper={testSerper} />} />
      <Route path="/research" element={<>
      <section className="panel research-panel" id="research"><PanelTitle eyebrow="Google Suggest" title="递归关键词扩展" tag={busy ? "执行中" : "就绪"} /><textarea id="seed-keywords" value={seedsText} onChange={(event) => setSeedsText(event.target.value)} aria-label="输入种子关键词" placeholder="每行一个种子关键词" />
        <div className="controls"><Select id="suggest-language" label="建议语言" value={language} onChange={setLanguage} options={[["en", "English"], ["zh-CN", "简体中文"], ["zh-TW", "繁體中文"]]} /><Select id="suggest-country" label="目标国家/地区" value={country} onChange={setCountry} options={[["US", "美国"], ["CN", "中国"], ["GB", "英国"], ["SG", "新加坡"]]} /><Select label="最大请求数" value={maxRequests} onChange={setMaxRequests} options={[["20", "20（快速）"], ["50", "50（推荐）"], ["200", "200（深度）"]]} /><Select label="递归层数" value={maxDepth} onChange={setMaxDepth} options={[["2", "2 层"], ["3", "3 层"], ["5", "5 层"]]} /><button id="start-suggest-expansion" className="primary" disabled={busy} onClick={expand}>开始扩展关键词</button></div>
        <p className="hint">Google 下拉用于发现词，不代表真实 VOL。遇到单个子词网络失败时保留已有结果并写入日志。</p></section>

      <section className="two-columns"><section className="panel" id="review"><PanelTitle eyebrow="内容适配" title="SEO 关键词审核" /><p id="ai-review-status" className="tag">{reviewStatus}</p><div className="controls"><Select id="keyword-review-mode" label="审核模式" value={reviewMode} onChange={(value) => setReviewMode(value as "fast" | "hybrid")} options={[["fast", "快速：仅本地规则（即时）"], ["hybrid", "混合：规则预筛 + AI 精审（推荐）"]]} /></div><p className="hint">快速模式不调用 AI；混合模式先过滤明显无关词，再对相关词进行 AI 精审。审核通过的关键词会自动加入关键词库。</p><div className="actions"><button disabled={busy || !run} onClick={reviewKeywords} id="review-expanded-keywords">审核并自动入库</button><NavLink className="keyword-library-link" to={`/keywords${projectQuery}`}>查看已入库关键词</NavLink></div></section><section className="panel"><PanelTitle eyebrow="任务诊断" title="扩展日志" /><ol id="suggest-debug-log" className="logs">{logs.map((log, index) => <li key={`${log}-${index}`}>{log}</li>)}</ol></section></section>

      <section className="panel results"><PanelTitle eyebrow="扩展结果" title="Google 下拉关键词" tag={run?.result.stop_reason || "等待任务"} /><Results run={run} reviews={reviews} /></section>
      </>} />

      <Route path="/keywords" element={<>
      <section className="panel" id="library"><PanelTitle eyebrow="关键词资产" title="关键词库" tag={libraryStatus} /><div className="actions"><button onClick={() => loadLibrary().catch((error) => setLibraryStatus(error.message))}>刷新</button><button id="export-keyword-library" disabled={!library.length} onClick={exportKeywordLibrary}>导出 CSV</button><NavLink className="primary keyword-title-action" to={`/titles${projectQuery}`}>生成标题</NavLink><button className="danger" disabled={!projectId} onClick={() => removeKeywords([], true)}>清空当前项目</button></div><p className="keyword-title-hint">请在下方对应关键词行点击“根据此关键词生成标题”，系统会带入该关键词进入标题生成。</p><Library keywords={library} onDelete={(id) => removeKeywords([id])} onOpenTitles={openTitleWorkspace} /></section>
      </>} />

      <Route path="/titles" element={<>
      <section className="panel" id="title-generation"><PanelTitle eyebrow="Content Planning · en-US" title="SEO 标题生成" tag={titleKeyword ? "已选择关键词" : "等待选择"} />
        {!titleKeyword ? <><div className="title-keyword-entry"><strong>先选择已入库关键词</strong><span>标题必须绑定当前网站的关键词；选择后可研究 SERP 标题并生成候选。</span><NavLink className="primary" to={`/keywords${projectQuery}`}>打开已入库关键词</NavLink></div><RecentTitleLibrary titles={titleLibrary} /></> : <><div className="title-context"><strong>{titleKeyword.keyword}</strong><span>意图：{readableIntent(titleKeyword.search_intent || undefined)} · 市场：美国（en-US）</span></div><p className="hint">按美国本地英语搜索习惯生成，不使用中文营销腔；标题候选可多条，但一个关键词只能选定一个。</p>
          <div className="controls"><Select label="标题类型" value={titleType} onChange={setTitleType} options={[["auto", "按搜索意图自动选择"], ["tutorial", "教程指南"], ["comparison", "对比评测"], ["transactional", "购买服务"]]} /><Select label="生成数量" value={titleCount} onChange={setTitleCount} options={[["5", "5 个候选"], ["8", "8 个候选（推荐）"], ["12", "12 个候选"]]} /><button id="generate-title-candidates" className="primary" disabled={busy || titleKeyword.is_seo_content_fit !== 1} onClick={generateTitleCandidates}>生成标题候选</button><button id="generate-multi-provider-titles" disabled={busy || titleKeyword.is_seo_content_fit !== 1 || !serpTitles.length} onClick={generateMultiProviderTitles}>三模型各生成 3 个标题</button></div>
          <div className="competitor-research"><div><strong>浏览器抓取 Google 前 20 自然标题</strong><p>系统浏览器自动搜索关键词“{titleKeyword.keyword}”，跳过广告与 AI Overview，翻页提取自然结果标题；抓取结果会直接作为标题生成参考。</p></div><button id="research-browser-serp-titles" className="primary" disabled={busy} onClick={researchBrowserSerpTitles}>浏览器抓取前 20 标题</button><p className="tag">{serpStatus}</p>{verificationImage ? <img className="captcha-image" src={`data:image/png;base64,${verificationImage}`} alt="Google 浏览器验证码" /> : null}{serpTitles.length ? <div className="table-wrap"><table><thead><tr><th>排名</th><th>Google 标题</th><th>来源</th></tr></thead><tbody>{serpTitles.map((item) => <tr key={`${item.rank}-${item.title}`}><td>{item.rank}</td><td>{item.title}</td><td>{item.source || "—"}</td></tr>)}</tbody></table></div> : null}</div>
          <p className="tag">{titleStatus}</p><div id="selected-title" className="selected-title">{titleCandidates.find((candidate) => candidate.status === "selected") ? <>当前选定标题：<strong>{titleCandidates.find((candidate) => candidate.status === "selected")?.title}</strong></> : "当前尚未选定标题"}</div>
          <div id="title-candidate-list" className="title-candidate-list">{titleCandidates.length ? titleCandidates.map((candidate) => <article className={`title-candidate ${candidate.status === "selected" ? "is-selected" : ""}`} key={candidate.id}><div><p className="eyebrow">{candidate.source_type === "ai" ? <ProviderBadge reason={candidate.reason} /> : <span className="provider-badge provider-manual">人工候选</span>} · {candidate.title_type || "通用"} · 质量分 {candidate.quality_score}</p><h3>{candidate.title}</h3><p>{candidate.reason || "—"}</p></div><div className="actions">{candidate.status === "selected" ? <span className="tag">已选定</span> : <button className="primary" onClick={() => selectTitleCandidate(candidate)}>选定此标题</button>}<button className="link danger" disabled={candidate.status === "selected"} onClick={() => deleteTitleCandidate(candidate)}>删除</button></div></article>) : <p className="empty">暂时没有标题候选。生成后会显示在这里。</p>}</div>
          <div className="manual-title"><Input label="人工补充标题" value={manualTitle} onChange={setManualTitle} /><button onClick={addManualTitle}>加入候选</button></div></>}</section>
      </>} />

      <Route path="/content-library/:assetId" element={<section className="panel content-reader-panel"><ContentReader projectId={projectId} /><ContentImageStudio projectId={projectId} /></section>} />
      <Route path="/knowledge" element={<section className="panel"><ProjectKnowledgeLibrary projectId={projectId} /></section>} />
      <Route path="/website-crawl" element={<section className="panel"><ProjectKnowledgeLibrary projectId={projectId} crawlOnly /></section>} />
      <Route path="/gsc" element={<section className="panel"><GscIntegrationCard projectId={projectId} /></section>} />
      <Route path="/content-publish" element={<section className="panel"><PanelTitle eyebrow="Website Publishing" title="内容发布" /><p className="hint">仅配置当前网站的 WordPress 后台登录信息。Python 会模拟后台登录并保存文章草稿，不使用 REST API。</p><WordPressIntegrationCard projectId={projectId} /></section>} />
      <Route path="/content-memory" element={<ContentMemoryLibrary projectId={projectId} />} />
      <Route path="/competitor-learning" element={<section className="panel"><CompetitorLearningPlanner projectId={projectId} /></section>} />
      <Route path="/learning-memories" element={<LearningMemoryManager projectId={projectId} />} />
      <Route path="/content-library" element={<section className="panel" id="content-library"><PanelTitle eyebrow="Content Library" title="所有内容" tag={`${contentLibraryAssets.length} 篇已完成`} /><p className="hint">这里只显示后端确认已生成正文的内容；点击阅读全文查看版本化保存的完整文章。</p><ContentLibrary assets={contentLibraryAssets} onDelete={deleteContentAssets} /></section>} />

      <Route path="/content" element={<section className="panel content-system-panel" id="content-system"><PanelTitle eyebrow="Content System" title="内容系统" tag={`${contentAssets.length} 篇内容`} /><p className="hint">从已选标题建立内容资产，以 Brief → 大纲 → 每个 H2 独立生成 → 长文组装的工作流生成可追溯的 SEO 内容。</p><p className="tag">{contentStatus}</p><ContentWorkspace titles={titleLibrary} assets={contentAssets} projectId={projectId} onCreate={createContentFromTitle} onRefresh={refreshContentData} onDelete={deleteContentAssets} contentModels={{ openai: aiProfiles.openai.model, gemini: aiProfiles.gemini.model, deepseek: aiProfiles.deepseek.model }} /></section>} />

      <Route path="/title-library" element={<section className="panel" id="title-library"><PanelTitle eyebrow="Content Assets" title="标题库" tag={`${titleLibrary.length} 条标题`} /><p className="hint">每次 AI 或人工生成的标题都会自动保存到这里；“已选定”代表该关键词当前唯一可进入后续内容流程的标题。</p><div className="actions"><button onClick={() => loadTitleLibrary().catch((error) => setTitleStatus(error.message))}>刷新标题库</button></div><TitleLibrary titles={titleLibrary} onCreateContent={createContentFromTitle} onSelectTitle={selectLibraryTitle} onDelete={deleteTitleLibraryCandidates} /></section>} />

      <Route path="/settings" element={<section className="panel" id="ai-settings"><PanelTitle eyebrow="Multi Provider" title="AI 配置" tag={aiStatus} /><p className="hint">三套配置独立保存、独立测试。下方可为关键词审核和标题生成分别指定提供商；后续模块会继续复用这些配置。</p><div className="provider-grid"><ProviderCard provider="openai" title="ChatGPT（OpenAI）" profile={aiProfiles.openai} configured={aiConfigured.openai} onChange={updateAiProfile} onTest={testAiSettings} onSave={saveAiProvider} /><ProviderCard provider="gemini" title="Gemini（Google）" profile={aiProfiles.gemini} configured={aiConfigured.gemini} onChange={updateAiProfile} onTest={testAiSettings} onSave={saveAiProvider} /><ProviderCard provider="deepseek" title="DeepSeek" profile={aiProfiles.deepseek} configured={aiConfigured.deepseek} onChange={updateAiProfile} onTest={testAiSettings} onSave={saveAiProvider} /></div><ImageGenerationIntegrationCard /><section className="assignment-panel"><div><strong>功能使用分配</strong><p>每个功能只使用此处选定的 AI；不会因为配置其他服务而自动切换。</p></div><div className="assignment-controls"><Select label="关键词审核" value={aiAssignments.keyword_review} onChange={(value) => setAiAssignments((current) => ({ ...current, keyword_review: value as AiProvider }))} options={[["openai", "ChatGPT"], ["gemini", "Gemini"], ["deepseek", "DeepSeek"]]} /><Select label="标题生成" value={aiAssignments.title_generation} onChange={(value) => setAiAssignments((current) => ({ ...current, title_generation: value as AiProvider }))} options={[["openai", "ChatGPT"], ["gemini", "Gemini"], ["deepseek", "DeepSeek"]]} /></div><div className="actions"><button onClick={saveAiAssignments}>保存功能分配</button></div></section></section>} />

      <Route path="/scoring" element={<>
      <section className="panel" id="score"><PanelTitle eyebrow="SEO 机会评分" title="VOL · KD · 机会分" /><p className="hint">VOL 请使用 Google Ads CSV/API 数据；其余指标来自 SERP 前 10 名。缺数据时不要填猜测值。</p><div className="score-grid"><Input label="关键词" value={scoreInputs.keyword} onChange={(value) => setScoreField("keyword", value)} /><Input label="月搜索量 VOL" type="number" value={scoreInputs.volume} onChange={(value) => setScoreField("volume", value)} /><Input label="平均 DA (0-100)" type="number" value={scoreInputs.authority} onChange={(value) => setScoreField("authority", value)} /><Input label="平均引用域" type="number" value={scoreInputs.domains} onChange={(value) => setScoreField("domains", value)} /><Input label="标题完全匹配率 %" type="number" value={scoreInputs.titleMatch} onChange={(value) => setScoreField("titleMatch", value)} /><Input label="大站占比 %" type="number" value={scoreInputs.authoritySites} onChange={(value) => setScoreField("authoritySites", value)} /><Select label="意图竞争" value={scoreInputs.intent} onChange={(value) => setScoreField("intent", value)} options={[["1", "1 - 很低"], ["2", "2 - 较低"], ["3", "3 - 中等"], ["4", "4 - 较高"], ["5", "5 - 很高"]]} /><Input label="相关性 %" type="number" value={scoreInputs.relevance} onChange={(value) => setScoreField("relevance", value)} /><Input label="商业价值 %" type="number" value={scoreInputs.businessValue} onChange={(value) => setScoreField("businessValue", value)} /></div><div className="actions"><button id="calculate-keyword-score" className="primary" onClick={calculateScore}>计算 KD 与机会分</button><strong id="keyword-score-result" className="score-result">{score ? `KD ${score.keyword_difficulty}（${readableLevel(score.difficulty_level)}） · 机会分 ${score.opportunity_score}/100` : "等待 VOL 与 SERP 数据"}</strong></div></section>
      </>} />
      <Route path="*" element={<Navigate to="/research" replace />} />
      <Route path="/authority-sources" element={<section className="panel"><AuthoritySourceLibrary projectId={projectId} /></section>} />
      </Routes>
    </main>
    {!embedded && <aside className="workspace-right-info"><h2>辅助信息</h2><p>状态与操作记录，不作为导航。</p><section><b>{isProjectHome ? "项目数量" : "当前项目"}</b><span>{isProjectHome ? "独立网站项目" : projectId ? `项目 #${projectId}` : "未选择网站"}</span></section><section><b>数据空间</b><span>项目间不共享业务数据</span></section><section><b>最近活动</b><span>{isIntegrationPage ? "AI 与搜索配置" : "内容任务与来源记录"}</span></section></aside>}
  </div>;
}

function Metric({ label, value }: { label: string; value: number }) { return <article className="metric"><span>{label}</span><strong>{value}</strong></article>; }
function PanelTitle({ eyebrow, title, tag }: { eyebrow: string; title: string; tag?: string }) { return <div className="panel-title"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div>{tag && <span className="tag">{tag}</span>}</div>; }
function Select({ id, label, value, onChange, options }: { id?: string; label: string; value: string; onChange: (value: string) => void; options: string[][] }) { return <label>{label}<select id={id} value={value} onChange={(event) => onChange(event.target.value)}>{options.map(([key, text]) => <option value={key} key={key}>{text}</option>)}</select></label>; }
function Input({ label, value, onChange, type = "text" }: { label: string; value: string; onChange: (value: string) => void; type?: string }) { return <label>{label}<input type={type} value={value} onChange={(event) => onChange(event.target.value)} /></label>; }
function ProviderCard({ provider, title, profile, configured, onChange, onTest, onSave }: { provider: AiProvider; title: string; profile: AiProfile; configured: boolean; onChange: (provider: AiProvider, field: keyof AiProfile, value: string) => void; onTest: (provider: AiProvider) => void; onSave: (provider: AiProvider) => void }) { return <article className="provider-card"><div className="provider-card-title"><div><p className="eyebrow">独立 AI 配置</p><h3>{title}</h3></div><span className="tag">{profile.apiKey ? "待保存新 Key" : configured ? "已保存" : "待配置"}</span></div><Input label="兼容接口地址" value={profile.baseUrl} onChange={(value) => onChange(provider, "baseUrl", value)} /><Input label="模型名称" value={profile.model} onChange={(value) => onChange(provider, "model", value)} /><label>API Key<input type="password" value={profile.apiKey} onChange={(event) => onChange(provider, "apiKey", event.target.value)} placeholder="留空则保留已保存的 Key" autoComplete="off" /></label><div className="actions"><button className="primary" onClick={() => onSave(provider)}>保存 {title}</button><button onClick={() => onTest(provider)}>测试 {title}</button></div></article>; }
function SerperIntegrationCard({ apiKey, configured, status, onChange, onSave, onTest }: { apiKey: string; configured: boolean; status: string; onChange: (value: string) => void; onSave: () => void; onTest: () => void }) { return <section className="serper-integration-card"><div><p className="eyebrow">Search Integration</p><h3>Serper.dev · Google 搜索 API</h3><p>用于权威来源研究，避免本机 Chrome 被 Google 验证拦截。搜索结果仍会经过域名白名单、PDF 排除、robots 和网页正文检查。</p><a href="https://serper.dev/" target="_blank" rel="noreferrer">在 serper.dev 注册 / 管理 Key ↗</a></div><label>Serper API Key<input type="password" value={apiKey} onChange={(event) => onChange(event.target.value)} placeholder={configured ? "留空则保留已保存的 Key" : "粘贴 X-API-KEY"} autoComplete="off" /></label><div className="actions"><button className="primary" onClick={onSave}>保存 Serper Key</button><button onClick={onTest}>测试 Serper</button></div><p className="tag">{status}</p></section>; }
function IntegrationHub({ aiConfigured, aiProfiles, aiStatus, serperKey, serperConfigured, serperStatus, onSerperChange, onSaveSerper, onTestSerper }: { aiConfigured: Record<AiProvider, boolean>; aiProfiles: Record<AiProvider, AiProfile>; aiStatus: string; serperKey: string; serperConfigured: boolean; serperStatus: string; onSerperChange: (value: string) => void; onSaveSerper: () => void; onTestSerper: () => void }) {
  const providers: Array<{ provider: AiProvider; label: string; role: string }> = [
    { provider: "openai", label: "ChatGPT", role: "内容生成与图像" },
    { provider: "gemini", label: "Gemini", role: "权威来源筛选" },
    { provider: "deepseek", label: "DeepSeek", role: "可选内容模型" },
  ];
  const configuredCount = providers.filter(({ provider }) => aiConfigured[provider]).length;
  return <div className="integration-hub">
    <section className="integration-hero">
      <div>
        <p className="eyebrow">System configuration</p>
        <h2>AI 与搜索集成</h2>
        <p>统一管理内容模型与搜索能力。内容发布账号不在这里配置，而是保存在每个网站项目自己的“内容发布”页面。</p>
        <div className="integration-hero-actions"><NavLink className="primary" to="/settings">管理 AI 模型</NavLink><a href="#serper-config">配置搜索能力</a></div>
      </div>
      <div className="integration-readiness" aria-label="集成就绪状态">
        <span>系统就绪度</span><strong>{configuredCount + (serperConfigured ? 1 : 0)}<small>/4</small></strong>
        <p>{configuredCount === 3 && serperConfigured ? "所有全局能力已连接" : "完成模型与搜索配置后可启动完整工作流"}</p>
      </div>
    </section>

    <section className="integration-status-grid" aria-label="全局集成状态">
      {providers.map(({ provider, label, role }) => <article key={provider} className={aiConfigured[provider] ? "is-ready" : "is-pending"}>
        <div className="integration-status-top"><span className="integration-mark">{label.slice(0, 1)}</span><span className="integration-state">{aiConfigured[provider] ? "已连接" : "待配置"}</span></div>
        <strong>{label}</strong><p>{role}</p><small>{aiConfigured[provider] ? aiProfiles[provider].model || "已保存模型" : "尚未保存 API 配置"}</small>
      </article>)}
      <article className={serperConfigured ? "is-ready" : "is-pending"}>
        <div className="integration-status-top"><span className="integration-mark search">S</span><span className="integration-state">{serperConfigured ? "已连接" : "待配置"}</span></div>
        <strong>Serper.dev</strong><p>Google 搜索结果</p><small>{serperConfigured ? "用于竞品与权威来源研究" : "需要 X-API-KEY"}</small>
      </article>
    </section>

    <section className="integration-main-grid">
      <div className="integration-card integration-ai-card">
        <div className="integration-card-heading"><div><p className="eyebrow">01 · AI providers</p><h3>模型配置与任务边界</h3></div><span className={configuredCount ? "integration-pill ready" : "integration-pill"}>{configuredCount}/3 已连接</span></div>
        <p>每个内容任务使用创建时选定的模型；一个任务内不会自动切换提供商。模型名称、错误和阶段日志会被保留，便于追溯。</p>
        <dl className="integration-facts"><div><dt>内容生成</dt><dd>由文章任务中选定的模型执行</dd></div><div><dt>关键词与标题</dt><dd>在 AI 配置页指定各自提供商</dd></div><div><dt>图片生成</dt><dd>复用 ChatGPT 兼容中转配置</dd></div></dl>
        <div className="integration-card-footer"><span>{aiStatus}</span><NavLink className="primary" to="/settings">打开 AI 配置</NavLink></div>
      </div>
      <div className="integration-card integration-flow-card">
        <div className="integration-card-heading"><div><p className="eyebrow">How it connects</p><h3>配置只出现一次，按项目使用</h3></div></div>
        <ol className="integration-flow"><li><span>1</span><div><strong>连接模型</strong><p>保存可用的 AI 提供商与模型。</p></div></li><li><span>2</span><div><strong>连接搜索</strong><p>用 Serper 获取 Google 结果，避免浏览器搜索不稳定。</p></div></li><li><span>3</span><div><strong>在网站项目中工作</strong><p>关键词、知识库、内容和发布配置都严格归属当前网站。</p></div></li></ol>
        <p className="integration-note">WordPress 后台登录信息仅保存在对应网站项目中，不会在全局集成中心出现。</p>
      </div>
    </section>

    <section className="integration-search-section" id="serper-config"><div className="integration-section-heading"><div><p className="eyebrow">02 · Search provider</p><h3>搜索与研究能力</h3><p>竞品研究与权威来源研究优先通过 Serper.dev 获取 Google 自然搜索候选；后端仍会排除 PDF、下载文件、无关页和不可用页面。</p></div><span className={serperConfigured ? "integration-pill ready" : "integration-pill"}>{serperConfigured ? "搜索已连接" : "需要 API Key"}</span></div><SerperIntegrationCard apiKey={serperKey} configured={serperConfigured} status={serperStatus} onChange={onSerperChange} onSave={onSaveSerper} onTest={onTestSerper} /></section>
  </div>;
}
function ImageGenerationIntegrationCard() { const [provider, setProvider] = useState<"openai" | "siliconflow">("openai"); const [baseUrl, setBaseUrl] = useState(""); const [model, setModel] = useState("gpt-image-2"); const [apiKey, setApiKey] = useState(""); const [keySaved, setKeySaved] = useState(false); const [status, setStatus] = useState("正在读取图片生成配置…"); const load = async () => { try { const item = await api.getImageGenerationSettings(); setProvider(item.provider); setBaseUrl(item.base_url || ""); setModel(item.model); setKeySaved(item.api_key_saved); setStatus(item.configured ? `当前使用：${item.provider_label} · ${item.model}` : `请完成 ${item.provider_label} 配置。`); } catch (error) { setStatus(error instanceof Error ? error.message : "读取图片配置失败。"); } }; useEffect(() => { void load(); }, []); const switchProvider = (next: "openai" | "siliconflow") => { setProvider(next); setApiKey(""); if (next === "siliconflow") { setBaseUrl("https://api.siliconflow.cn/v1"); setModel("Kwai-Kolors/Kolors"); setStatus("硅基流动 Kolors 可使用账户免费额度；请保存 API Key 后启用。"); } else { setBaseUrl(""); setModel("gpt-image-2"); setStatus("ChatGPT 中转会复用上方 OpenAI 配置的地址与 Key。"); } }; const save = async () => { try { const item = await api.saveImageGenerationSettings({ provider, model, ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}) }); setApiKey(""); setKeySaved(item.api_key_saved); setBaseUrl(item.base_url || ""); setStatus(`已保存：${item.provider_label} · ${item.model}`); } catch (error) { setStatus(error instanceof Error ? error.message : "保存图片配置失败。"); } }; const test = async () => { try { const item = await api.testImageGenerationSettings(); setStatus(`配置已就绪：${item.base_url} · ${item.model}`); } catch (error) { setStatus(error instanceof Error ? error.message : "图片配置测试失败。"); } }; const isSiliconflow = provider === "siliconflow"; return <section className="serper-integration-card image-generation-card"><div><p className="eyebrow">Image Generation</p><h3>图片生成来源</h3><p>内容页的 H2 配图会使用当前选中的来源，自动保存 SEO 文件名、alt 和本地图片。来源可随时切换，已生成图片不会被覆盖。</p></div><div className="image-provider-switch" role="group" aria-label="选择图片生成来源"><button type="button" className={provider === "openai" ? "primary" : ""} aria-pressed={provider === "openai"} onClick={() => switchProvider("openai")}>ChatGPT 中转</button><button type="button" className={isSiliconflow ? "primary" : ""} aria-pressed={isSiliconflow} onClick={() => switchProvider("siliconflow")}>硅基流动 Kolors <span>免费额度</span></button></div>{isSiliconflow ? <><p className="image-provider-note">固定使用硅基流动官方接口 https://api.siliconflow.cn/v1；免费额度以账户实际可用配额为准。</p><label>硅基流动 API Key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={keySaved ? "留空则保留已保存的 Key" : "粘贴硅基流动 API Key"} autoComplete="off" /></label></> : <><Input label="复用的 ChatGPT 中转地址" value={baseUrl} onChange={setBaseUrl} /><p className="image-provider-note">地址和 Key 复用 ChatGPT（OpenAI）配置；无需重复填写。</p></>}<Input label="图片模型" value={model} onChange={setModel} /><div className="actions"><button className="primary" onClick={() => void save()}>保存并切换来源</button><button onClick={() => void test()}>测试图片配置</button></div><p className="tag">{status}</p></section>; }
function WordPressIntegrationCard({ projectId }: { projectId: number | null }) { const [siteUrl, setSiteUrl] = useState(""); const [username, setUsername] = useState(""); const [password, setPassword] = useState(""); const [status, setStatus] = useState("选择当前网站项目后可配置后台发布。"); useEffect(() => { if (!projectId) return; api.getWordPressConfig(projectId).then((item) => { setSiteUrl(item.site_url || ""); setUsername(item.username || ""); setStatus(item.configured ? "已保存；后台密码已用当前 Windows 用户加密，页面不会回显。" : "未配置。请填写 WordPress 后台账号密码。"); }).catch((error: unknown) => setStatus(error instanceof Error ? error.message : "WordPress 配置读取失败。")); }, [projectId]); const save = async () => { if (!projectId || !siteUrl || !username || !password) { setStatus("请填写站点 URL、后台用户名和密码。"); return; } try { await api.saveWordPressConfig(projectId, { site_url: siteUrl, username, password }); setPassword(""); setStatus("已保存。Python 将模拟后台登录，发布默认创建草稿。"); } catch (error) { setStatus(error instanceof Error ? error.message : "保存失败。"); } }; const test = async () => { if (!projectId) return; try { const result = await api.testWordPressConfig(projectId); setStatus(`后台登录成功：${result.username}`); } catch (error) { setStatus(error instanceof Error ? error.message : "后台登录失败。"); } }; return <section className="serper-integration-card"><div><p className="eyebrow">Website Publishing</p><h3>WordPress 后台模拟发布</h3><p>配置仅属于当前网站。Python 使用后台表单和 nonce 创建文章草稿，不使用 REST API、Application Password 或 wp-json。</p></div><Input label="WordPress 网站地址（可填首页或 /wp-admin）" value={siteUrl} onChange={setSiteUrl} /><Input label="WordPress 后台用户名" value={username} onChange={setUsername} /><label>WordPress 后台密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="保存后仅当前 Windows 用户可解密" autoComplete="off" /></label><div><p className="hint">系统会自动识别并保存网站根地址，填写 wp-admin 也不会造成路径重复。</p><div className="actions"><button className="primary" onClick={() => void save()}>保存当前网站发布配置</button><button onClick={() => void test()}>测试后台登录</button></div></div><p className="tag">{status}</p></section>; }
function Results({ run, reviews }: { run: RunState; reviews: Record<string, Review> }) { if (!run) return <p className="empty">输入种子词并开始扩展后，结果会显示在这里。</p>; return <div className="table-wrap"><table><thead><tr><th>关键词</th><th>意图</th><th>分类</th><th>需求预估</th><th>SEO 审核</th></tr></thead><tbody id="suggest-keyword-table-body">{run.result.keywords.map((keyword) => { const review = reviews[keyword]; return <tr key={keyword}><td><strong>{keyword}</strong></td><td>{readableIntent(review?.search_intent)}</td><td>{review ? categoryOf(keyword, review) : "待审核"}</td><td>{demandEstimate(keyword)}/100</td><td>{review ? (review.is_seo_content_fit && review.same_topic_as_seed ? "适合" : "需人工确认") : "待审核"}</td></tr>; })}</tbody></table></div>; }
function Library({ keywords, onDelete, onOpenTitles }: { keywords: LibraryKeyword[]; onDelete: (id: number) => void; onOpenTitles: (keyword: LibraryKeyword) => void }) { if (!keywords.length) return <p className="empty">当前项目还没有保存关键词。</p>; return <div className="table-wrap"><table><thead><tr><th>关键词</th><th>分类</th><th>意图</th><th>需求预估</th><th>真实 VOL</th><th>标题</th><th>操作</th></tr></thead><tbody>{keywords.map((keyword) => <tr key={keyword.id}><td><button className="keyword-title-link" title="点击根据此关键词生成标题" disabled={keyword.is_seo_content_fit !== 1} onClick={() => onOpenTitles(keyword)}>{keyword.keyword}</button></td><td>{keyword.category || "未分类"}</td><td>{readableIntent(keyword.search_intent || undefined)}</td><td>{keyword.demand_estimate ?? "—"}/100</td><td>{keyword.search_volume ?? "待 Ads"}</td><td>{keyword.selected_title || (keyword.title_candidate_count ? `待选择（${keyword.title_candidate_count}）` : "未生成")}</td><td><div className="actions"><button id="open-title-workspace" className="link" disabled={keyword.is_seo_content_fit !== 1} onClick={() => onOpenTitles(keyword)}>生成标题</button><button className="link danger" onClick={() => onDelete(keyword.id)}>删除</button></div></td></tr>)}</tbody></table></div>; }
function TitleLibrary({ titles, onCreateContent, onSelectTitle, onDelete }: { titles: TitleCandidate[]; onCreateContent: (title: TitleCandidate) => void; onSelectTitle: (title: TitleCandidate) => void; onDelete: (candidateIds: number[]) => Promise<void> }) {
  const removableTitles = titles.filter((title) => title.status !== "selected");
  const [selectedTitleIds, setSelectedTitleIds] = useState<number[]>([]);
  useEffect(() => setSelectedTitleIds((current) => current.filter((titleId) => removableTitles.some((title) => title.id === titleId))), [titles]);
  const toggleTitle = (titleId: number) => setSelectedTitleIds((current) => current.includes(titleId) ? current.filter((id) => id !== titleId) : [...current, titleId]);
  const deleteSelected = async () => { if (!selectedTitleIds.length || !window.confirm(`确认删除 ${selectedTitleIds.length} 条未选定标题吗？`)) return; await onDelete(selectedTitleIds); setSelectedTitleIds([]); };
  if (!titles.length) return <p className="empty">还没有已保存标题。请先从关键词库生成标题。</p>;
  return <><div className="title-library-bulk"><label><input type="checkbox" checked={removableTitles.length > 0 && selectedTitleIds.length === removableTitles.length} onChange={(event) => setSelectedTitleIds(event.target.checked ? removableTitles.map((title) => title.id) : [])} /> 全选可删除标题</label><button className="danger" disabled={!selectedTitleIds.length} onClick={() => void deleteSelected}>批量删除标题（{selectedTitleIds.length}）</button><span>已选定标题不可删除</span></div><div className="table-wrap"><table><thead><tr><th>内容操作</th><th>选择</th><th>标题</th><th>关联关键词</th><th>来源</th><th>质量分</th><th>状态</th><th>管理</th></tr></thead><tbody>{titles.map((title) => <tr key={title.id}><td className="title-library-create">{title.status === "selected" ? <button className="primary" onClick={() => onCreateContent(title)}>加入内容生成</button> : <span>—</span>}</td><td>{title.status === "selected" ? <span className="locked-title" title="已选定标题不可删除">锁定</span> : <input type="checkbox" checked={selectedTitleIds.includes(title.id)} onChange={() => toggleTitle(title.id)} aria-label={`选择 ${title.title}`} />}</td><td><strong>{title.title}</strong></td><td>{title.keyword || "—"}</td><td>{title.source_type === "ai" ? <ProviderBadge reason={title.reason} /> : <span className="provider-badge provider-manual">人工录入</span>}</td><td>{title.quality_score}/100</td><td>{title.status === "selected" ? "已选定" : title.status === "candidate" ? "待选择" : "未选定"}</td><td><div className="actions">{title.status === "selected" ? <span className="tag">可生成内容</span> : <><button onClick={() => onSelectTitle(title)}>选定标题</button><button className="link danger" onClick={() => void onDelete([title.id])}>删除</button></>}</div></td></tr>)}</tbody></table></div></>;
}
function ProviderBadge({ reason }: { reason: string | null }) { return <span className={`provider-badge provider-${providerKey(reason)}`}>{providerLabel(reason)}</span>; }
function providerKey(reason: string | null) { const provider = reason?.match(/^\[(ChatGPT|Gemini|DeepSeek)\]/)?.[1]; return ({ ChatGPT: "chatgpt", Gemini: "gemini", DeepSeek: "deepseek" } as Record<string, string>)[provider || ""] || "ai"; }
function providerLabel(reason: string | null) { const provider = reason?.match(/^\[(ChatGPT|Gemini|DeepSeek)\]/)?.[1]; return provider ? `${provider} 生成` : "AI 生成"; }
function readableLevel(level: string) { return ({ low: "低竞争", medium: "中等竞争", high: "高竞争", very_high: "很高竞争" } as Record<string, string>)[level] || level; }
