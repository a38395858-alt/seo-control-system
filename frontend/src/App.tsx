import { useEffect, useMemo, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api } from "./api";
import type { ContentAsset, ContentAssetDetail, ContentBrief, ContentDraft, ContentGenerationResult, ContentOutline, ExpansionResult, LibraryKeyword, Review, Score, SerpTitle, TitleCandidate } from "./types";

type RunState = { seeds: string[]; language: string; country: string; result: ExpansionResult } | null;
type ScoreInputs = Record<"keyword" | "volume" | "authority" | "domains" | "titleMatch" | "authoritySites" | "intent" | "relevance" | "businessValue", string>;

const projectKey = "seo-keyword-project-id";
type AiProvider = "openai" | "gemini" | "deepseek";
type AiProfile = { baseUrl: string; model: string; apiKey: string };
type AiAssignments = { keyword_review: AiProvider; title_generation: AiProvider };
function RecentTitleLibrary({ titles }: { titles: TitleCandidate[] }) { return <section className="recent-title-library"><div><strong>最近入库标题</strong><p>每次生成完成后会自动写入标题库，不会因为刷新或切换页面而丢失。</p></div><NavLink className="primary" to="/title-library">打开标题库（{titles.length} 条）</NavLink>{titles.length ? <div className="recent-title-list">{titles.slice(0, 5).map((title) => <article key={title.id}><ProviderBadge reason={title.reason} /><strong>{title.title}</strong><span>{title.keyword || "—"}</span></article>)}</div> : <p className="empty">暂时还没有标题。生成后会自动写入标题库。</p>}</section>; }
function ContentLibrary({ assets, onDelete }: { assets: ContentAsset[]; onDelete: (assetIds: number[]) => Promise<void> }) {
  const completedAssets = assets.filter((asset) => Boolean(asset.current_draft_id));
  const [selectedAssetIds, setSelectedAssetIds] = useState<number[]>([]);
  useEffect(() => setSelectedAssetIds((current) => current.filter((assetId) => completedAssets.some((asset) => asset.id === assetId))), [assets]);
  const toggleAsset = (assetId: number) => setSelectedAssetIds((current) => current.includes(assetId) ? current.filter((id) => id !== assetId) : [...current, assetId]);
  const deleteSelected = async () => { if (!selectedAssetIds.length || !window.confirm(`确认删除 ${selectedAssetIds.length} 篇已完成内容及其版本吗？`)) return; await onDelete(selectedAssetIds); setSelectedAssetIds([]); };
  if (!completedAssets.length) return <div className="content-library-empty"><strong>暂无已完成正文</strong><p>内容资产完成 AI 正文生成后才会显示在这里。可前往“内容系统”继续完成 Brief、大纲和正文。</p><NavLink className="primary" to="/content">前往内容系统</NavLink></div>;
  return <><div className="content-library-bulk"><label><input type="checkbox" checked={selectedAssetIds.length === completedAssets.length} onChange={(event) => setSelectedAssetIds(event.target.checked ? completedAssets.map((asset) => asset.id) : [])} /> 全选已完成内容</label><button className="danger" disabled={!selectedAssetIds.length} onClick={() => void deleteSelected}>删除已选内容（{selectedAssetIds.length}）</button></div><div className="content-library-grid">{completedAssets.map((asset) => <article className={`content-library-card ${selectedAssetIds.includes(asset.id) ? "is-selected" : ""}`} key={asset.id}><div className="content-library-card-top"><label className="asset-checkbox"><input type="checkbox" checked={selectedAssetIds.includes(asset.id)} onChange={() => toggleAsset(asset.id)} aria-label={`选择 ${asset.title_snapshot}`} /></label><span className="content-completion-chip">✓ {asset.content_status_label || "内容完成"}</span><span className="content-outline-chip">✓ {asset.outline_status_label || "大纲完成"}</span></div><h3>{asset.title_snapshot}</h3><p>{asset.keyword || "—"} · {asset.locale}</p><div className="content-library-card-footer"><span>{asset.status === "completed" ? "已完成" : "已生成"}</span><div className="actions"><button className="link danger" onClick={() => void onDelete([asset.id])}>删除</button><NavLink className="primary" to={`/content-library/${asset.id}`}>阅读全文</NavLink></div></div></article>)}</div></>;
}
function ContentReader({ projectId }: { projectId: number | null }) {
  const { assetId } = useParams();
  const [detail, setDetail] = useState<ContentAssetDetail | null>(null);
  const [readerError, setReaderError] = useState("");
  useEffect(() => { if (projectId && assetId) { setReaderError(""); api.getContentAsset(Number(assetId), projectId).then(setDetail).catch((error: unknown) => setReaderError(error instanceof Error ? error.message : "文章读取失败。")); } }, [assetId, projectId]);
  if (readerError) return <p className="empty">{readerError}</p>;
  if (!detail?.current_draft) return <p className="empty">正在加载文章，或该内容尚未生成正文。</p>;
  const draft = detail.current_draft;
  return <article className="content-reader"><header className="content-reader-header"><NavLink to="/content-library">← 返回所有内容</NavLink><p className="eyebrow">{draft.provider || "AI"} · v{draft.version} · {draft.qa_status}</p><h1>{draft.title}</h1><p className="content-reader-description">{draft.meta_description}</p></header><article className="markdown-preview" dangerouslySetInnerHTML={{ __html: renderMarkdownPreview(draft.markdown) }} />{draft.unresolved_verify?.length ? <p className="verify-warning">待验证：{draft.unresolved_verify.join("；")}</p> : null}</article>;
}
function escapeHtml(value: string) { return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;").replace(/'/g, "&#39;"); }
function renderInlineMarkdown(value: string) { return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/__(.+?)__/g, "<strong>$1</strong>"); }
function tableCells(line: string) { return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim()); }
function isTableDivider(line: string) { const cells = tableCells(line); return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell)); }
function renderMarkdownPreview(markdown: string) {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const output: string[] = [];
  const isList = (line: string) => /^[-*+]\s+/.test(line) || /^\d+[.)]\s+/.test(line);
  const isBlockStart = (index: number) => /^#{1,3}\s+/.test(lines[index]) || isList(lines[index]) || (lines[index].includes("|") && isTableDivider(lines[index + 1] || ""));
  for (let index = 0; index < lines.length;) {
    const line = lines[index].trim();
    if (!line) { index += 1; continue; }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) { const level = heading[1].length === 1 ? 2 : heading[1].length; output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`); index += 1; continue; }
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
function contentProviderLabel(provider: AiProvider | string | undefined) { return ({ openai: "ChatGPT", gemini: "Gemini", deepseek: "DeepSeek" } as Record<string, string>)[provider || ""] || provider || "AI"; }
function contentStageLabel(stage: string | null | undefined) { return ({ semantic: "语义分析", title: "标题与元信息", outline: "文章大纲", section: "章节写作", assembly: "组装全文", configuration: "模型配置", preparation: "任务准备" } as Record<string, string>)[stage || ""] || stage || "生成"; }
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
  const [selectedDraftVersion, setSelectedDraftVersion] = useState<number | null>(null);
  const [draftView, setDraftView] = useState<"markdown" | "html">("html");
  const [contentProvider, setContentProvider] = useState<AiProvider>("openai");
  const contentModel = contentModels[contentProvider];
  const [outlineRows, setOutlineRows] = useState([{ heading: "Introduction: what the reader will decide", purpose: "Answer the search need and set clear decision context", }, { heading: "How to evaluate the options", purpose: "Give practical criteria and explain trade-offs", }, { heading: "Recommended next steps", purpose: "Turn the comparison into an informed action", }]);

  const loadDetail = async (assetId: number) => {
    if (!projectId) return;
    setSelectedAssetId(assetId); setLoadingDetail(true);
    try {
      const next = await api.getContentAsset(assetId, projectId);
      setDetail(next);
      if (next.brief) {
        setAudience(next.brief.target_audience || ""); setGoal(next.brief.business_goal || "commercial");
        setSourcesText((next.brief.sources || []).map((source) => typeof source === "string" ? source : JSON.stringify(source)).join("\n\n---\n\n"));
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

  const sourcePack = () => sourcesText.split(/\n\s*---+\s*\n/).map((item) => item.trim()).filter(Boolean).map((content, index) => { const url = content.match(/https?:\/\/[^\s]+/)?.[0] || ""; return { source_id: `source-${index + 1}`, source_type: url ? "url" : "note", url, publisher: "", published_at: "", content, availability: "provided" }; });
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

  const generationPayload = () => ({ project_id: projectId, provider: contentProvider, target_audience: audience.trim(), business_goal: goal, sources: sourcePack() });
  const applyGenerated = async (result: ContentGenerationResult) => {
    if (!detail) return;
    setDetail((current) => current ? { ...current, ...(result.asset || {}), brief: result.brief ?? current.brief, outline: result.outline ?? current.outline, current_draft: result.current_draft ?? result.draft ?? current.current_draft, drafts: result.draft ? [result.draft, ...(current.drafts || []).filter((draft) => draft.id !== result.draft?.id)] : current.drafts, generation_runs: result.generation_runs ?? result.runs ?? current.generation_runs, generation_jobs: result.generation_job ? [...(current.generation_jobs || []), result.generation_job] : current.generation_jobs } : current);
    await loadDetail(detail.id);
    await onRefresh();
  };
  const generateStage = async (stageName: "brief" | "outline" | "draft" | "all") => {
    if (!detail || !projectId) return;
    setSaving(true); setNotice(`${contentProviderLabel(contentProvider)}（${contentModel}）正在严格执行${stageName === "all" ? (detail.brief ? "大纲与全文" : "Brief、大纲与全文") : `内容${stageName === "brief" ? " Brief" : stageName === "outline" ? "大纲" : "正文"}`}；每个 H2 独立写作后再组装，失败不会切换其他模型…`);
    try {
      const action = stageName === "brief" ? api.generateContentBrief : stageName === "outline" ? api.generateContentOutline : stageName === "draft" ? api.generateContentDraft : api.generateContent;
      const result = await action(detail.id, generationPayload());
      await applyGenerated(result);
      setNotice(`${contentProviderLabel(contentProvider)} 已完成本次任务并保存；可在执行日志查看每个阶段。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : `${contentProviderLabel(contentProvider)} 生成失败；未切换其他模型。`); await loadDetail(detail.id); await onRefresh(); }
    finally { setSaving(false); }
  };

  const displayedDraft = detail?.drafts?.find((draft) => draft.version === selectedDraftVersion) || detail?.current_draft;
  const stage = detail?.current_draft ? 3 : detail?.outline ? 2 : detail?.brief ? 1 : 1;
  const stages = ["内容 Brief", "文章大纲", "正文版本"];
  return <div className="content-workspace" id="content-workspace">
    <div className="content-workspace-header"><div><p className="eyebrow">Production Workspace</p><h3>内容工作流</h3><p>保留每一步输入与版本；没有来源支撑的事实在生成时会标记为 <code>[VERIFY]</code>。</p></div><div className="content-workspace-actions"><Select label="内容模型" value={contentProvider} onChange={(value) => setContentProvider(value as AiProvider)} options={[["openai", "ChatGPT"], ["gemini", "Gemini"], ["deepseek", "DeepSeek"]]} /><span className="content-provider-lock">本次严格使用：<strong>{contentProviderLabel(contentProvider)}</strong><small>{contentModel}</small></span><button onClick={() => void onRefresh()}>刷新资产</button><NavLink className="link" to="/title-library">管理标题库</NavLink></div></div>
    <p className="content-notice">{notice}</p>
    {assets.length ? <section className="content-workspace-bulk"><div><strong>资产批量操作</strong><span>已选 {selectedAssetIds.length} / {assets.length}</span></div><div className="content-workspace-selection">{assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={selectedAssetIds.includes(asset.id)} onChange={() => toggleAssetSelection(asset.id)} /> <span>{asset.current_draft_id ? "正文完成" : asset.current_outline_id ? "大纲完成" : asset.current_brief_id ? "Brief 完成" : "待开始"}</span>{asset.title_snapshot}</label>)}</div><div className="actions"><button onClick={() => setSelectedAssetIds(selectedAssetIds.length === assets.length ? [] : assets.map((asset) => asset.id))}>{selectedAssetIds.length === assets.length ? "取消全选" : "全选资产"}</button><button className="danger" disabled={!selectedAssetIds.length} onClick={() => void deleteSelectedAssets}>删除已选内容</button></div></section> : null}
    <div className="content-layout">
      <aside className="content-asset-list" id="content-asset-list"><div className="content-list-heading"><strong>内容资产</strong><span>{assets.length}</span></div>{assets.length ? assets.map((asset) => <article className={`content-asset-card ${asset.id === selectedAssetId ? "is-active" : ""} ${asset.current_draft_id ? "is-complete" : asset.current_outline_id ? "is-outline-ready" : asset.current_brief_id ? "is-brief-ready" : "is-pending"}`} key={asset.id}><button type="button" className="content-asset-open" onClick={() => void loadDetail(asset.id)}><span className="content-asset-status">{asset.content_status_label || (asset.current_draft_id ? "内容完成" : asset.current_outline_id ? "大纲完成" : asset.current_brief_id ? "Brief 完成" : "待生成")}</span><strong>{asset.title_snapshot}</strong><small>{asset.keyword || "—"} · {asset.locale}</small></button><button type="button" className="content-asset-delete" aria-label={`删除 ${asset.title_snapshot}`} title="删除内容资产" onClick={(event) => { event.stopPropagation(); void onDelete([asset.id]); }}>×</button></article>) : <p className="empty">尚未创建内容资产。</p>}<div className="content-list-divider" /><strong className="content-list-label">已选标题</strong>{available.length ? available.map((title) => { const asset = createdByTitle.get(title.id); return <article className="content-title-entry" key={title.id}><ProviderBadge reason={title.reason} /><strong>{title.title}</strong>{asset ? <button onClick={() => void loadDetail(asset.id)}>打开</button> : <button className="primary" onClick={() => onCreate(title)}>创建</button>}</article>; }) : <p className="empty">请先在标题库选定标题。</p>}</aside>
      <section className="content-editor-area">{loadingDetail ? <p className="empty">正在读取内容详情…</p> : !detail ? <div className="content-empty-state"><strong>从标题库选择一个标题开始</strong><p>创建内容资产后，所有 Brief、大纲、正文和审核结果都会在这里集中保存。</p></div> : <>
        <div className="content-hero"><div><span className="content-kicker">{detail.content_type} · {detail.locale}</span><h3>{detail.title_snapshot}</h3><p>核心关键词：<strong>{detail.keyword || "—"}</strong></p></div><span className="tag">{detail.status === "planned" ? "规划中" : detail.status}</span></div>
        <ol className="content-stage-rail">{stages.map((item, index) => <li className={`content-stage-step ${index + 1 < stage ? "is-complete" : index + 1 === stage ? "is-current" : ""}`} key={item}><span>{index + 1}</span><strong>{item}</strong><small>{index + 1 < stage ? "已保存" : index + 1 === stage ? "当前步骤" : "等待前序"}</small></li>)}</ol>
        <section className="content-editor-card" id="content-brief"><div className="content-card-heading"><div><span>01</span><div><h4>内容 Brief</h4><p>定义受众、业务目标和可验证资料；它是后续 AI 写作的事实边界。</p></div></div><span className={detail.brief ? "stage-state done" : "stage-state"}>{detail.brief ? "已保存" : "待填写"}</span></div><div className="content-brief-grid"><Input label="目标读者" value={audience} onChange={setAudience} /><Select label="业务目标" value={goal} onChange={setGoal} options={[["informational", "信息型内容"], ["commercial", "商业调研 / 对比"], ["lead-generation", "获客 / 线索"]]} /><label className="content-source-field">多篇文章 / URL / 研究资料（每篇文章之间用 --- 分隔，可粘贴全文）<textarea value={sourcesText} onChange={(event) => setSourcesText(event.target.value)} placeholder="【来源 1：官方文章】&#10;粘贴 URL 或全文…&#10;&#10;【来源 2：同行文章/笔记】&#10;粘贴 URL 或全文…" /></label></div><div className="content-card-footer"><span>{sourcesText.trim() ? "将把资料作为可引用事实来源。" : "未添加资料：正文中的具体事实将标记为 [VERIFY]。"}</span><div className="actions"><button disabled={saving} onClick={() => void saveBrief}>人工保存</button><button className="primary" disabled={saving} onClick={() => void generateStage("brief")}>AI 生成 Brief</button></div></div></section>
        <section className={`content-editor-card ${!detail.brief ? "is-locked" : ""}`} id="content-outline"><div className="content-card-heading"><div><span>02</span><div><h4>文章大纲</h4><p>每个 H2 只解决一个决策问题；比较类内容在正文阶段需要输出带来源列的表格。</p></div></div><span className={detail.outline ? "stage-state done" : "stage-state"}>{detail.outline ? "已保存" : "待编辑"}</span></div><div className="outline-table"><div className="outline-row outline-head"><span>章节标题</span><span>章节任务</span><span /></div>{outlineRows.map((row, index) => <div className="outline-row" key={`${row.heading}-${index}`}><input aria-label={`章节标题 ${index + 1}`} value={row.heading} onChange={(event) => setOutlineRows((current) => current.map((item, rowIndex) => rowIndex === index ? { ...item, heading: event.target.value } : item))} disabled={!detail.brief} /><input aria-label={`章节任务 ${index + 1}`} value={row.purpose} onChange={(event) => setOutlineRows((current) => current.map((item, rowIndex) => rowIndex === index ? { ...item, purpose: event.target.value } : item))} disabled={!detail.brief} /><button className="link danger" disabled={!detail.brief || outlineRows.length <= 1} onClick={() => setOutlineRows((current) => current.filter((_, rowIndex) => rowIndex !== index))}>移除</button></div>)}</div><div className="content-card-footer"><button disabled={!detail.brief} onClick={() => setOutlineRows((current) => [...current, { heading: "New section", purpose: "Add a distinct reader decision" }])}>添加章节</button><div className="actions"><button disabled={!detail.brief || saving} onClick={() => void saveOutline}>人工保存</button><button className="primary" disabled={!detail.brief || saving} onClick={() => void generateStage("outline")}>AI 生成大纲</button></div></div></section>
        <section className={`content-editor-card content-future-card ${!detail.outline ? "is-locked" : ""}`} id="content-draft"><div className="content-card-heading"><div><span>03</span><div><h4>正文版本</h4><p>每个 H2 独立写成一篇完整章节，再按大纲组装为一篇长文，并保留可回滚的版本历史。</p></div></div><span className="stage-state">{detail.current_draft ? `版本 v${displayedDraft!.version}` : detail.outline ? "准备生成" : "需先保存大纲"}</span></div>{displayedDraft ? <><div className="draft-switcher"><Select label="版本" value={String(selectedDraftVersion || displayedDraft!.version)} onChange={(value) => setSelectedDraftVersion(Number(value))} options={(detail.drafts || []).slice().reverse().map((draft) => [String(draft.version), `v${draft.version} · ${draft.provider || "AI"}`])} /><div className="actions"><button className={draftView === "markdown" ? "primary" : ""} onClick={() => setDraftView("markdown")}>Markdown 源码</button><button className={draftView === "html" ? "primary" : ""} onClick={() => setDraftView("html")}>HTML 预览</button></div></div><div className="content-draft-preview"><div><strong>{displayedDraft!.title}</strong><p>{displayedDraft!.meta_description}</p></div>{draftView === "markdown" ? <pre>{displayedDraft!.markdown}</pre> : <article className="markdown-preview" dangerouslySetInnerHTML={{ __html: renderMarkdownPreview(displayedDraft!.markdown) }} />}<div className="draft-meta"><span>版本 v{displayedDraft!.version}</span><span>{displayedDraft!.provider || contentProvider}</span><span>{displayedDraft!.model || "已保存"}</span></div></div></> : <div className="future-grid"><div><strong>H2 分段生成</strong><p>每个 H2 仅发送本章 Brief 与相关资料，独立完成后再组装，避免重复与无来源编造。</p></div><div><strong>多模型任务</strong><p>内容模型将读取 AI 配置；每次生成都会记录模型、时间和版本。</p></div></div>}<div className="content-card-footer"><span>{detail.brief ? "一键生成将自动完成大纲、每个 H2 的独立章节和全文组装，并保留当前 Brief。" : "一键生成会先创建 Brief，再自动完成大纲、每个 H2 章节和全文组装。"}</span><div className="actions"><button disabled={saving} onClick={() => void generateStage("all")}>一键生成大纲和全文</button><button className="primary" disabled={!detail.outline || saving} onClick={() => void generateStage("draft")}>{detail.current_draft ? "生成新版本" : "AI 生成正文"}</button></div></div></section>
        <section className="content-generation-log" aria-label="模型执行日志"><div><p className="eyebrow">Model Audit</p><h4>模型执行日志</h4></div>{(detail.generation_jobs || []).slice().reverse().slice(0, 5).map((job) => <article className={`generation-job ${job.status}`} key={job.id}><div><strong>{contentProviderLabel(job.provider)} · {job.model || "未记录模型"}</strong><span>{job.status === "completed" ? "已完成" : job.status === "failed" ? `${contentStageLabel(job.failed_stage)}失败` : "执行中"}</span></div><small>{(detail.generation_runs || []).filter((run) => run.generation_job_id === job.id).map((run) => `${contentStageLabel(run.stage)} ${run.status === "completed" ? "✓" : "×"}`).join(" · ") || "任务尚未写入阶段日志"}</small>{job.error_summary ? <p>{job.error_summary}</p> : null}</article>)}</section>
      </>}</section>
    </div>
  </div>;
}
const aiProviderPresets: Record<AiProvider, { baseUrl: string; model: string }> = {
  openai: { baseUrl: "https://api.openai.com/v1", model: "gpt-5.5" },
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

export default function App() {
  return <BrowserRouter><Workspace /></BrowserRouter>;
}

function Workspace() {
  const navigate = useNavigate();
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
  const [projectId, setProjectId] = useState<number | null>(() => Number(localStorage.getItem(projectKey)) || null);
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
  const reviewedCount = useMemo(() => Object.keys(reviews).length, [reviews]);

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
        const settings = await api.getAiSettings();
        setAiProfiles((current) => (Object.keys(aiProviderPresets) as AiProvider[]).reduce((profiles, provider) => {
          const saved = settings.providers?.[provider];
          profiles[provider] = { baseUrl: saved?.base_url || current[provider].baseUrl, model: saved?.model || current[provider].model, apiKey: "" };
          return profiles;
        }, {} as Record<AiProvider, AiProfile>));
        setAiConfigured((Object.keys(aiProviderPresets) as AiProvider[]).reduce((profiles, provider) => ({ ...profiles, [provider]: Boolean(settings.providers?.[provider]?.configured) }), {} as Record<AiProvider, boolean>));
        if (settings.assignments?.keyword_review && settings.assignments?.title_generation) setAiAssignments(settings.assignments as AiAssignments);
        setAiStatus(settings.configured ? `已配置：${settings.provider || "兼容接口"}` : "尚未配置 AI 接口。");
      } catch (error) {
        setAiStatus(error instanceof Error ? `AI 配置读取失败：${error.message}` : "AI 配置读取失败。");
      }
      if (activeProjectId) await Promise.allSettled([loadLibrary(activeProjectId), loadTitleLibrary(activeProjectId), loadContentAssets(activeProjectId), loadContentLibrary(activeProjectId)]);
    };
    restoreProjectAndAssets().catch(() => setAiStatus("项目初始化失败；请刷新后重试。"));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    setSerpStatus("尚未抓取 Google 排名标题。");
    setTitleStatus(keyword.is_seo_content_fit === 1 ? `已选择“${keyword.keyword}”，默认按美国市场 en-US 生成。` : "该关键词尚未通过 SEO 审核，不能生成标题。");
    if (keyword.is_seo_content_fit !== 1) return;
    try {
      await loadTitleCandidates(keyword);
      navigate("/titles");
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
      setSerpStatus(result.warning || `已提取 ${result.titles.length} 条排名标题，可直接用于生成 SEO 标题。`);
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
      setSerpStatus(`浏览器已抓取 ${result.titles.length} 条 Google 自然排名标题，可直接用于生成 SEO 标题。`);
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

  return <main className="app-shell">
    <aside className="sidebar"><div className="brand"><span>SEO</span><small>Keyword Intelligence</small></div><nav><NavLink to="/research" className={({ isActive }) => isActive ? "active" : ""}>关键词挖掘</NavLink><NavLink to="/keywords" className={({ isActive }) => isActive ? "active" : ""}>关键词库</NavLink><NavLink to="/titles" className={({ isActive }) => isActive ? "active" : ""}>SEO 标题</NavLink><NavLink to="/title-library" className={({ isActive }) => isActive ? "active" : ""}>标题库</NavLink><NavLink to="/content" className={({ isActive }) => isActive ? "active" : ""}>内容系统</NavLink><NavLink to="/content-library" className={({ isActive }) => isActive ? "active" : ""}>所有内容</NavLink><NavLink to="/scoring" className={({ isActive }) => isActive ? "active" : ""}>SEO 评分</NavLink><NavLink to="/settings" className={({ isActive }) => isActive ? "active" : ""}>AI 配置</NavLink></nav><p className="connection">● 本地工作台已连接</p></aside>
    <section className="workspace">
      <header><div><p className="eyebrow">SEO 中控系统</p><h1>关键词工作台</h1><p>挖掘、审核、分类、入库和机会评估。</p></div><span className="project-chip">项目：{projectId ? `#${projectId}` : "未创建"}</span></header>

      <section className="metrics"><Metric label="本次扩展关键词" value={run?.result.keywords.length ?? 0} /><Metric label="下拉词请求数" value={run?.result.requests_made ?? 0} /><Metric label="已审核关键词" value={reviewedCount} /><Metric label="已入库关键词" value={library.length} /></section>

      <Routes>
      <Route path="/research" element={<>
      <section className="panel research-panel" id="research"><PanelTitle eyebrow="Google Suggest" title="递归关键词扩展" tag={busy ? "执行中" : "就绪"} /><textarea id="seed-keywords" value={seedsText} onChange={(event) => setSeedsText(event.target.value)} aria-label="输入种子关键词" placeholder="每行一个种子关键词" />
        <div className="controls"><Select id="suggest-language" label="建议语言" value={language} onChange={setLanguage} options={[["en", "English"], ["zh-CN", "简体中文"], ["zh-TW", "繁體中文"]]} /><Select id="suggest-country" label="目标国家/地区" value={country} onChange={setCountry} options={[["US", "美国"], ["CN", "中国"], ["GB", "英国"], ["SG", "新加坡"]]} /><Select label="最大请求数" value={maxRequests} onChange={setMaxRequests} options={[["20", "20（快速）"], ["50", "50（推荐）"], ["200", "200（深度）"]]} /><Select label="递归层数" value={maxDepth} onChange={setMaxDepth} options={[["2", "2 层"], ["3", "3 层"], ["5", "5 层"]]} /><button id="start-suggest-expansion" className="primary" disabled={busy} onClick={expand}>开始扩展关键词</button></div>
        <p className="hint">Google 下拉用于发现词，不代表真实 VOL。遇到单个子词网络失败时保留已有结果并写入日志。</p></section>

      <section className="two-columns"><section className="panel" id="review"><PanelTitle eyebrow="内容适配" title="SEO 关键词审核" /><p id="ai-review-status" className="tag">{reviewStatus}</p><div className="controls"><Select id="keyword-review-mode" label="审核模式" value={reviewMode} onChange={(value) => setReviewMode(value as "fast" | "hybrid")} options={[["fast", "快速：仅本地规则（即时）"], ["hybrid", "混合：规则预筛 + AI 精审（推荐）"]]} /></div><p className="hint">快速模式不调用 AI；混合模式先过滤明显无关词，再对相关词进行 AI 精审。审核通过的关键词会自动加入关键词库。</p><div className="actions"><button disabled={busy || !run} onClick={reviewKeywords} id="review-expanded-keywords">审核并自动入库</button></div></section><section className="panel"><PanelTitle eyebrow="任务诊断" title="扩展日志" /><ol id="suggest-debug-log" className="logs">{logs.map((log, index) => <li key={`${log}-${index}`}>{log}</li>)}</ol></section></section>

      <section className="panel results"><PanelTitle eyebrow="扩展结果" title="Google 下拉关键词" tag={run?.result.stop_reason || "等待任务"} /><Results run={run} reviews={reviews} /></section>
      </>} />

      <Route path="/keywords" element={<>
      <section className="panel" id="library"><PanelTitle eyebrow="关键词资产" title="关键词库" tag={libraryStatus} /><div className="actions"><button onClick={() => loadLibrary().catch((error) => setLibraryStatus(error.message))}>刷新</button><button id="export-keyword-library" disabled={!library.length} onClick={exportKeywordLibrary}>导出 CSV</button><button className="danger" disabled={!projectId} onClick={() => removeKeywords([], true)}>清空当前项目</button></div><Library keywords={library} onDelete={(id) => removeKeywords([id])} onOpenTitles={openTitleWorkspace} /></section>
      </>} />

      <Route path="/titles" element={<>
      <section className="panel" id="title-generation"><PanelTitle eyebrow="Content Planning · en-US" title="SEO 标题生成" tag={titleKeyword ? "已选择关键词" : "等待选择"} />
        {!titleKeyword ? <RecentTitleLibrary titles={titleLibrary} /> : <><div className="title-context"><strong>{titleKeyword.keyword}</strong><span>意图：{readableIntent(titleKeyword.search_intent || undefined)} · 市场：美国（en-US）</span></div><p className="hint">按美国本地英语搜索习惯生成，不使用中文营销腔；标题候选可多条，但一个关键词只能选定一个。</p>
          <div className="controls"><Select label="标题类型" value={titleType} onChange={setTitleType} options={[["auto", "按搜索意图自动选择"], ["tutorial", "教程指南"], ["comparison", "对比评测"], ["transactional", "购买服务"]]} /><Select label="生成数量" value={titleCount} onChange={setTitleCount} options={[["5", "5 个候选"], ["8", "8 个候选（推荐）"], ["12", "12 个候选"]]} /><button id="generate-title-candidates" className="primary" disabled={busy || titleKeyword.is_seo_content_fit !== 1} onClick={generateTitleCandidates}>生成标题候选</button><button id="generate-multi-provider-titles" disabled={busy || titleKeyword.is_seo_content_fit !== 1 || !serpTitles.length} onClick={generateMultiProviderTitles}>三模型各生成 3 个标题</button></div>
          <div className="competitor-research"><div><strong>浏览器抓取 Google 前 20 自然标题</strong><p>系统浏览器自动搜索关键词“{titleKeyword.keyword}”，跳过广告与 AI Overview，翻页提取自然结果标题；抓取结果会直接作为标题生成参考。</p></div><button id="research-browser-serp-titles" className="primary" disabled={busy} onClick={researchBrowserSerpTitles}>浏览器抓取前 20 标题</button><p className="tag">{serpStatus}</p>{verificationImage ? <img className="captcha-image" src={`data:image/png;base64,${verificationImage}`} alt="Google 浏览器验证码" /> : null}{serpTitles.length ? <div className="table-wrap"><table><thead><tr><th>排名</th><th>Google 标题</th><th>来源</th></tr></thead><tbody>{serpTitles.map((item) => <tr key={`${item.rank}-${item.title}`}><td>{item.rank}</td><td>{item.title}</td><td>{item.source || "—"}</td></tr>)}</tbody></table></div> : null}</div>
          <p className="tag">{titleStatus}</p><div id="selected-title" className="selected-title">{titleCandidates.find((candidate) => candidate.status === "selected") ? <>当前选定标题：<strong>{titleCandidates.find((candidate) => candidate.status === "selected")?.title}</strong></> : "当前尚未选定标题"}</div>
          <div id="title-candidate-list" className="title-candidate-list">{titleCandidates.length ? titleCandidates.map((candidate) => <article className={`title-candidate ${candidate.status === "selected" ? "is-selected" : ""}`} key={candidate.id}><div><p className="eyebrow">{candidate.source_type === "ai" ? <ProviderBadge reason={candidate.reason} /> : <span className="provider-badge provider-manual">人工候选</span>} · {candidate.title_type || "通用"} · 质量分 {candidate.quality_score}</p><h3>{candidate.title}</h3><p>{candidate.reason || "—"}</p></div><div className="actions">{candidate.status === "selected" ? <span className="tag">已选定</span> : <button className="primary" onClick={() => selectTitleCandidate(candidate)}>选定此标题</button>}<button className="link danger" disabled={candidate.status === "selected"} onClick={() => deleteTitleCandidate(candidate)}>删除</button></div></article>) : <p className="empty">暂时没有标题候选。生成后会显示在这里。</p>}</div>
          <div className="manual-title"><Input label="人工补充标题" value={manualTitle} onChange={setManualTitle} /><button onClick={addManualTitle}>加入候选</button></div></>}</section>
      </>} />

      <Route path="/content-library/:assetId" element={<section className="panel content-reader-panel"><ContentReader projectId={projectId} /></section>} />
      <Route path="/content-library" element={<section className="panel" id="content-library"><PanelTitle eyebrow="Content Library" title="所有内容" tag={`${contentLibraryAssets.length} 篇已完成`} /><p className="hint">这里只显示后端确认已生成正文的内容；点击阅读全文查看版本化保存的完整文章。</p><ContentLibrary assets={contentLibraryAssets} onDelete={deleteContentAssets} /></section>} />

      <Route path="/content" element={<section className="panel content-system-panel" id="content-system"><PanelTitle eyebrow="Content System" title="内容系统" tag={`${contentAssets.length} 篇内容`} /><p className="hint">从已选标题建立内容资产，以 Brief → 大纲 → 每个 H2 独立生成 → 长文组装的工作流生成可追溯的 SEO 内容。</p><p className="tag">{contentStatus}</p><ContentWorkspace titles={titleLibrary} assets={contentAssets} projectId={projectId} onCreate={createContentFromTitle} onRefresh={refreshContentData} onDelete={deleteContentAssets} contentModels={{ openai: aiProfiles.openai.model, gemini: aiProfiles.gemini.model, deepseek: aiProfiles.deepseek.model }} /></section>} />

      <Route path="/title-library" element={<section className="panel" id="title-library"><PanelTitle eyebrow="Content Assets" title="标题库" tag={`${titleLibrary.length} 条标题`} /><p className="hint">每次 AI 或人工生成的标题都会自动保存到这里；“已选定”代表该关键词当前唯一可进入后续内容流程的标题。</p><div className="actions"><button onClick={() => loadTitleLibrary().catch((error) => setTitleStatus(error.message))}>刷新标题库</button></div><TitleLibrary titles={titleLibrary} onCreateContent={createContentFromTitle} onSelectTitle={selectLibraryTitle} onDelete={deleteTitleLibraryCandidates} /></section>} />

      <Route path="/settings" element={<section className="panel" id="ai-settings"><PanelTitle eyebrow="Multi Provider" title="AI 配置" tag={aiStatus} /><p className="hint">三套配置独立保存、独立测试。下方可为关键词审核和标题生成分别指定提供商；后续模块会继续复用这些配置。</p><div className="provider-grid"><ProviderCard provider="openai" title="ChatGPT（OpenAI）" profile={aiProfiles.openai} configured={aiConfigured.openai} onChange={updateAiProfile} onTest={testAiSettings} onSave={saveAiProvider} /><ProviderCard provider="gemini" title="Gemini（Google）" profile={aiProfiles.gemini} configured={aiConfigured.gemini} onChange={updateAiProfile} onTest={testAiSettings} onSave={saveAiProvider} /><ProviderCard provider="deepseek" title="DeepSeek" profile={aiProfiles.deepseek} configured={aiConfigured.deepseek} onChange={updateAiProfile} onTest={testAiSettings} onSave={saveAiProvider} /></div><section className="assignment-panel"><div><strong>功能使用分配</strong><p>每个功能只使用此处选定的 AI；不会因为配置其他服务而自动切换。</p></div><div className="assignment-controls"><Select label="关键词审核" value={aiAssignments.keyword_review} onChange={(value) => setAiAssignments((current) => ({ ...current, keyword_review: value as AiProvider }))} options={[["openai", "ChatGPT"], ["gemini", "Gemini"], ["deepseek", "DeepSeek"]]} /><Select label="标题生成" value={aiAssignments.title_generation} onChange={(value) => setAiAssignments((current) => ({ ...current, title_generation: value as AiProvider }))} options={[["openai", "ChatGPT"], ["gemini", "Gemini"], ["deepseek", "DeepSeek"]]} /></div><div className="actions"><button onClick={saveAiAssignments}>保存功能分配</button></div></section></section>} />

      <Route path="/scoring" element={<>
      <section className="panel" id="score"><PanelTitle eyebrow="SEO 机会评分" title="VOL · KD · 机会分" /><p className="hint">VOL 请使用 Google Ads CSV/API 数据；其余指标来自 SERP 前 10 名。缺数据时不要填猜测值。</p><div className="score-grid"><Input label="关键词" value={scoreInputs.keyword} onChange={(value) => setScoreField("keyword", value)} /><Input label="月搜索量 VOL" type="number" value={scoreInputs.volume} onChange={(value) => setScoreField("volume", value)} /><Input label="平均 DA (0-100)" type="number" value={scoreInputs.authority} onChange={(value) => setScoreField("authority", value)} /><Input label="平均引用域" type="number" value={scoreInputs.domains} onChange={(value) => setScoreField("domains", value)} /><Input label="标题完全匹配率 %" type="number" value={scoreInputs.titleMatch} onChange={(value) => setScoreField("titleMatch", value)} /><Input label="大站占比 %" type="number" value={scoreInputs.authoritySites} onChange={(value) => setScoreField("authoritySites", value)} /><Select label="意图竞争" value={scoreInputs.intent} onChange={(value) => setScoreField("intent", value)} options={[["1", "1 - 很低"], ["2", "2 - 较低"], ["3", "3 - 中等"], ["4", "4 - 较高"], ["5", "5 - 很高"]]} /><Input label="相关性 %" type="number" value={scoreInputs.relevance} onChange={(value) => setScoreField("relevance", value)} /><Input label="商业价值 %" type="number" value={scoreInputs.businessValue} onChange={(value) => setScoreField("businessValue", value)} /></div><div className="actions"><button id="calculate-keyword-score" className="primary" onClick={calculateScore}>计算 KD 与机会分</button><strong id="keyword-score-result" className="score-result">{score ? `KD ${score.keyword_difficulty}（${readableLevel(score.difficulty_level)}） · 机会分 ${score.opportunity_score}/100` : "等待 VOL 与 SERP 数据"}</strong></div></section>
      </>} />
      <Route path="*" element={<Navigate to="/research" replace />} />
      </Routes>
    </section>
  </main>;
}

function Metric({ label, value }: { label: string; value: number }) { return <article className="metric"><span>{label}</span><strong>{value}</strong></article>; }
function PanelTitle({ eyebrow, title, tag }: { eyebrow: string; title: string; tag?: string }) { return <div className="panel-title"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div>{tag && <span className="tag">{tag}</span>}</div>; }
function Select({ id, label, value, onChange, options }: { id?: string; label: string; value: string; onChange: (value: string) => void; options: string[][] }) { return <label>{label}<select id={id} value={value} onChange={(event) => onChange(event.target.value)}>{options.map(([key, text]) => <option value={key} key={key}>{text}</option>)}</select></label>; }
function Input({ label, value, onChange, type = "text" }: { label: string; value: string; onChange: (value: string) => void; type?: string }) { return <label>{label}<input type={type} value={value} onChange={(event) => onChange(event.target.value)} /></label>; }
function ProviderCard({ provider, title, profile, configured, onChange, onTest, onSave }: { provider: AiProvider; title: string; profile: AiProfile; configured: boolean; onChange: (provider: AiProvider, field: keyof AiProfile, value: string) => void; onTest: (provider: AiProvider) => void; onSave: (provider: AiProvider) => void }) { return <article className="provider-card"><div className="provider-card-title"><div><p className="eyebrow">独立 AI 配置</p><h3>{title}</h3></div><span className="tag">{profile.apiKey ? "待保存新 Key" : configured ? "已保存" : "待配置"}</span></div><Input label="兼容接口地址" value={profile.baseUrl} onChange={(value) => onChange(provider, "baseUrl", value)} /><Input label="模型名称" value={profile.model} onChange={(value) => onChange(provider, "model", value)} /><label>API Key<input type="password" value={profile.apiKey} onChange={(event) => onChange(provider, "apiKey", event.target.value)} placeholder="留空则保留已保存的 Key" autoComplete="off" /></label><div className="actions"><button className="primary" onClick={() => onSave(provider)}>保存 {title}</button><button onClick={() => onTest(provider)}>测试 {title}</button></div></article>; }
function Results({ run, reviews }: { run: RunState; reviews: Record<string, Review> }) { if (!run) return <p className="empty">输入种子词并开始扩展后，结果会显示在这里。</p>; return <div className="table-wrap"><table><thead><tr><th>关键词</th><th>意图</th><th>分类</th><th>需求预估</th><th>SEO 审核</th></tr></thead><tbody id="suggest-keyword-table-body">{run.result.keywords.map((keyword) => { const review = reviews[keyword]; return <tr key={keyword}><td><strong>{keyword}</strong></td><td>{readableIntent(review?.search_intent)}</td><td>{review ? categoryOf(keyword, review) : "待审核"}</td><td>{demandEstimate(keyword)}/100</td><td>{review ? (review.is_seo_content_fit && review.same_topic_as_seed ? "适合" : "需人工确认") : "待审核"}</td></tr>; })}</tbody></table></div>; }
function Library({ keywords, onDelete, onOpenTitles }: { keywords: LibraryKeyword[]; onDelete: (id: number) => void; onOpenTitles: (keyword: LibraryKeyword) => void }) { if (!keywords.length) return <p className="empty">当前项目还没有保存关键词。</p>; return <div className="table-wrap"><table><thead><tr><th>关键词</th><th>分类</th><th>意图</th><th>需求预估</th><th>真实 VOL</th><th>标题</th><th>操作</th></tr></thead><tbody>{keywords.map((keyword) => <tr key={keyword.id}><td><strong>{keyword.keyword}</strong></td><td>{keyword.category || "未分类"}</td><td>{readableIntent(keyword.search_intent || undefined)}</td><td>{keyword.demand_estimate ?? "—"}/100</td><td>{keyword.search_volume ?? "待 Ads"}</td><td>{keyword.selected_title || (keyword.title_candidate_count ? `待选择（${keyword.title_candidate_count}）` : "未生成")}</td><td><div className="actions"><button id="open-title-workspace" className="link" disabled={keyword.is_seo_content_fit !== 1} onClick={() => onOpenTitles(keyword)}>生成标题</button><button className="link danger" onClick={() => onDelete(keyword.id)}>删除</button></div></td></tr>)}</tbody></table></div>; }
function TitleLibrary({ titles, onCreateContent, onSelectTitle, onDelete }: { titles: TitleCandidate[]; onCreateContent: (title: TitleCandidate) => void; onSelectTitle: (title: TitleCandidate) => void; onDelete: (candidateIds: number[]) => Promise<void> }) {
  const removableTitles = titles.filter((title) => title.status !== "selected");
  const [selectedTitleIds, setSelectedTitleIds] = useState<number[]>([]);
  useEffect(() => setSelectedTitleIds((current) => current.filter((titleId) => removableTitles.some((title) => title.id === titleId))), [titles]);
  const toggleTitle = (titleId: number) => setSelectedTitleIds((current) => current.includes(titleId) ? current.filter((id) => id !== titleId) : [...current, titleId]);
  const deleteSelected = async () => { if (!selectedTitleIds.length || !window.confirm(`确认删除 ${selectedTitleIds.length} 条未选定标题吗？`)) return; await onDelete(selectedTitleIds); setSelectedTitleIds([]); };
  if (!titles.length) return <p className="empty">还没有已保存标题。请先从关键词库生成标题。</p>;
  return <><div className="title-library-bulk"><label><input type="checkbox" checked={removableTitles.length > 0 && selectedTitleIds.length === removableTitles.length} onChange={(event) => setSelectedTitleIds(event.target.checked ? removableTitles.map((title) => title.id) : [])} /> 全选可删除标题</label><button className="danger" disabled={!selectedTitleIds.length} onClick={() => void deleteSelected}>批量删除标题（{selectedTitleIds.length}）</button><span>已选定标题不可删除</span></div><div className="table-wrap"><table><thead><tr><th>选择</th><th>标题</th><th>关联关键词</th><th>来源</th><th>质量分</th><th>状态</th><th>操作</th></tr></thead><tbody>{titles.map((title) => <tr key={title.id}><td>{title.status === "selected" ? <span className="locked-title" title="已选定标题不可删除">锁定</span> : <input type="checkbox" checked={selectedTitleIds.includes(title.id)} onChange={() => toggleTitle(title.id)} aria-label={`选择 ${title.title}`} />}</td><td><strong>{title.title}</strong></td><td>{title.keyword || "—"}</td><td>{title.source_type === "ai" ? <ProviderBadge reason={title.reason} /> : <span className="provider-badge provider-manual">人工录入</span>}</td><td>{title.quality_score}/100</td><td>{title.status === "selected" ? "已选定" : title.status === "candidate" ? "待选择" : "未选定"}</td><td><div className="actions">{title.status === "selected" ? <button className="primary" onClick={() => onCreateContent(title)}>加入内容生成</button> : <><button onClick={() => onSelectTitle(title)}>选定标题</button><button className="link danger" onClick={() => void onDelete([title.id])}>删除</button></>}</div></td></tr>)}</tbody></table></div></>;
}
function ProviderBadge({ reason }: { reason: string | null }) { return <span className={`provider-badge provider-${providerKey(reason)}`}>{providerLabel(reason)}</span>; }
function providerKey(reason: string | null) { const provider = reason?.match(/^\[(ChatGPT|Gemini|DeepSeek)\]/)?.[1]; return ({ ChatGPT: "chatgpt", Gemini: "gemini", DeepSeek: "deepseek" } as Record<string, string>)[provider || ""] || "ai"; }
function providerLabel(reason: string | null) { const provider = reason?.match(/^\[(ChatGPT|Gemini|DeepSeek)\]/)?.[1]; return provider ? `${provider} 生成` : "AI 生成"; }
function readableLevel(level: string) { return ({ low: "低竞争", medium: "中等竞争", high: "高竞争", very_high: "很高竞争" } as Record<string, string>)[level] || level; }
