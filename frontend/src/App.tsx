import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { ExpansionResult, LibraryKeyword, Review, Score } from "./types";

type RunState = { seeds: string[]; language: string; country: string; result: ExpansionResult } | null;
type ScoreInputs = Record<"keyword" | "volume" | "authority" | "domains" | "titleMatch" | "authoritySites" | "intent" | "relevance" | "businessValue", string>;

const projectKey = "seo-keyword-project-id";

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
  const [projectId, setProjectId] = useState<number | null>(() => Number(localStorage.getItem(projectKey)) || null);
  const [library, setLibrary] = useState<LibraryKeyword[]>([]);
  const [libraryStatus, setLibraryStatus] = useState("审核通过的扩展词可加入关键词库。");
  const [scoreInputs, setScoreInputs] = useState<ScoreInputs>({ keyword: "", volume: "", authority: "", domains: "", titleMatch: "", authoritySites: "", intent: "3", relevance: "80", businessValue: "80" });
  const [score, setScore] = useState<Score | null>(null);
  const reviewedCount = useMemo(() => Object.keys(reviews).length, [reviews]);

  const loadLibrary = async (id = projectId) => {
    if (!id) return;
    const keywords = await api.listKeywords(id);
    setLibrary(keywords);
  };

  useEffect(() => {
    loadLibrary().catch(() => { localStorage.removeItem(projectKey); setProjectId(null); });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ensureProject = async () => {
    if (projectId) return projectId;
    const project = await api.createProject({ name: "默认关键词项目", country_code: country, language_code: language });
    localStorage.setItem(projectKey, String(project.id));
    setProjectId(project.id);
    return project.id;
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

  const reviewKeywords = async () => {
    if (!run) return;
    setBusy(true);
    const next: Record<string, Review> = {};
    try {
      for (const [index, keyword] of run.result.keywords.entries()) {
        setReviewStatus(`正在审核 ${index + 1}/${run.result.keywords.length}`);
        next[keyword] = (await api.review({ seed_keyword: run.seeds[0], keyword, language: run.language })).review;
      }
      setReviews(next);
      const approved = Object.values(next).filter((review) => review.is_seo_content_fit && review.same_topic_as_seed).length;
      setReviewStatus(`审核完成：${approved}/${run.result.keywords.length} 个词适合优先做 SEO 内容。`);
    } catch (error) {
      setReviewStatus(error instanceof Error ? error.message : "审核失败。");
    } finally {
      setBusy(false);
    }
  };

  const saveReviewed = async () => {
    if (!run || reviewedCount !== run.result.keywords.length) return setLibraryStatus("请先完成本次关键词审核。");
    setBusy(true);
    try {
      const id = await ensureProject();
      const keywords = run.result.keywords.map((keyword) => ({ keyword, review: reviews[keyword] }))
        .filter(({ review }) => review.is_seo_content_fit && review.same_topic_as_seed)
        .map(({ keyword, review }) => ({ keyword, category: categoryOf(keyword, review), search_intent: review.search_intent, is_seo_content_fit: review.is_seo_content_fit, same_topic_as_seed: review.same_topic_as_seed, recommended_action: review.recommended_action, review_reason: review.reason, review_confidence: review.confidence, demand_estimate: demandEstimate(keyword) }));
      const result = await api.saveExpanded({ project_id: id, country_code: run.country, language_code: run.language, seed_keyword: run.seeds[0], keywords });
      setLibraryStatus(`入库完成：新增 ${result.inserted} 个，已存在 ${result.existing} 个。`);
      await loadLibrary(id);
    } catch (error) {
      setLibraryStatus(error instanceof Error ? error.message : "入库失败。");
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
    <aside className="sidebar"><div className="brand"><span>SEO</span><small>Keyword Intelligence</small></div><nav><a href="#research">关键词挖掘</a><a href="#review">内容审核</a><a href="#library">关键词库</a><a href="#score">SEO 评分</a></nav><p className="connection">● 本地工作台已连接</p></aside>
    <section className="workspace">
      <header><div><p className="eyebrow">SEO 中控系统</p><h1>关键词工作台</h1><p>挖掘、审核、分类、入库和机会评估。</p></div><span className="project-chip">项目：{projectId ? `#${projectId}` : "未创建"}</span></header>

      <section className="metrics"><Metric label="本次扩展关键词" value={run?.result.keywords.length ?? 0} /><Metric label="下拉词请求数" value={run?.result.requests_made ?? 0} /><Metric label="已审核关键词" value={reviewedCount} /><Metric label="已入库关键词" value={library.length} /></section>

      <section className="panel research-panel" id="research"><PanelTitle eyebrow="Google Suggest" title="递归关键词扩展" tag={busy ? "执行中" : "就绪"} /><textarea id="seed-keywords" value={seedsText} onChange={(event) => setSeedsText(event.target.value)} aria-label="输入种子关键词" placeholder="每行一个种子关键词" />
        <div className="controls"><Select id="suggest-language" label="建议语言" value={language} onChange={setLanguage} options={[["en", "English"], ["zh-CN", "简体中文"], ["zh-TW", "繁體中文"]]} /><Select id="suggest-country" label="目标国家/地区" value={country} onChange={setCountry} options={[["US", "美国"], ["CN", "中国"], ["GB", "英国"], ["SG", "新加坡"]]} /><Select label="最大请求数" value={maxRequests} onChange={setMaxRequests} options={[["20", "20（快速）"], ["50", "50（推荐）"], ["200", "200（深度）"]]} /><Select label="递归层数" value={maxDepth} onChange={setMaxDepth} options={[["2", "2 层"], ["3", "3 层"], ["5", "5 层"]]} /><button id="start-suggest-expansion" className="primary" disabled={busy} onClick={expand}>开始扩展关键词</button></div>
        <p className="hint">Google 下拉用于发现词，不代表真实 VOL。遇到单个子词网络失败时保留已有结果并写入日志。</p></section>

      <section className="two-columns"><section className="panel" id="review"><PanelTitle eyebrow="内容适配" title="SEO 关键词审核" /><p id="ai-review-status" className="tag">{reviewStatus}</p><p className="hint">未配置服务端 AI 时使用本地规则初筛；AI 审核只辅助分流，不自动删除关键词。</p><div className="actions"><button disabled={busy || !run} onClick={reviewKeywords} id="review-expanded-keywords">审核本次扩展词</button><button disabled={busy || !run || reviewedCount !== run.result.keywords.length} onClick={saveReviewed}>审核后加入关键词库</button></div></section><section className="panel"><PanelTitle eyebrow="任务诊断" title="扩展日志" /><ol id="suggest-debug-log" className="logs">{logs.map((log, index) => <li key={`${log}-${index}`}>{log}</li>)}</ol></section></section>

      <section className="panel results"><PanelTitle eyebrow="扩展结果" title="Google 下拉关键词" tag={run?.result.stop_reason || "等待任务"} /><Results run={run} reviews={reviews} /></section>

      <section className="panel" id="library"><PanelTitle eyebrow="关键词资产" title="关键词库" tag={libraryStatus} /><div className="actions"><button onClick={() => loadLibrary().catch((error) => setLibraryStatus(error.message))}>刷新</button><button className="danger" disabled={!projectId} onClick={() => removeKeywords([], true)}>清空当前项目</button></div><Library keywords={library} onDelete={(id) => removeKeywords([id])} /></section>

      <section className="panel" id="score"><PanelTitle eyebrow="SEO 机会评分" title="VOL · KD · 机会分" /><p className="hint">VOL 请使用 Google Ads CSV/API 数据；其余指标来自 SERP 前 10 名。缺数据时不要填猜测值。</p><div className="score-grid"><Input label="关键词" value={scoreInputs.keyword} onChange={(value) => setScoreField("keyword", value)} /><Input label="月搜索量 VOL" type="number" value={scoreInputs.volume} onChange={(value) => setScoreField("volume", value)} /><Input label="平均 DA (0-100)" type="number" value={scoreInputs.authority} onChange={(value) => setScoreField("authority", value)} /><Input label="平均引用域" type="number" value={scoreInputs.domains} onChange={(value) => setScoreField("domains", value)} /><Input label="标题完全匹配率 %" type="number" value={scoreInputs.titleMatch} onChange={(value) => setScoreField("titleMatch", value)} /><Input label="大站占比 %" type="number" value={scoreInputs.authoritySites} onChange={(value) => setScoreField("authoritySites", value)} /><Select label="意图竞争" value={scoreInputs.intent} onChange={(value) => setScoreField("intent", value)} options={[["1", "1 - 很低"], ["2", "2 - 较低"], ["3", "3 - 中等"], ["4", "4 - 较高"], ["5", "5 - 很高"]]} /><Input label="相关性 %" type="number" value={scoreInputs.relevance} onChange={(value) => setScoreField("relevance", value)} /><Input label="商业价值 %" type="number" value={scoreInputs.businessValue} onChange={(value) => setScoreField("businessValue", value)} /></div><div className="actions"><button id="calculate-keyword-score" className="primary" onClick={calculateScore}>计算 KD 与机会分</button><strong id="keyword-score-result" className="score-result">{score ? `KD ${score.keyword_difficulty}（${readableLevel(score.difficulty_level)}） · 机会分 ${score.opportunity_score}/100` : "等待 VOL 与 SERP 数据"}</strong></div></section>
    </section>
  </main>;
}

function Metric({ label, value }: { label: string; value: number }) { return <article className="metric"><span>{label}</span><strong>{value}</strong></article>; }
function PanelTitle({ eyebrow, title, tag }: { eyebrow: string; title: string; tag?: string }) { return <div className="panel-title"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div>{tag && <span className="tag">{tag}</span>}</div>; }
function Select({ id, label, value, onChange, options }: { id?: string; label: string; value: string; onChange: (value: string) => void; options: string[][] }) { return <label>{label}<select id={id} value={value} onChange={(event) => onChange(event.target.value)}>{options.map(([key, text]) => <option value={key} key={key}>{text}</option>)}</select></label>; }
function Input({ label, value, onChange, type = "text" }: { label: string; value: string; onChange: (value: string) => void; type?: string }) { return <label>{label}<input type={type} value={value} onChange={(event) => onChange(event.target.value)} /></label>; }
function Results({ run, reviews }: { run: RunState; reviews: Record<string, Review> }) { if (!run) return <p className="empty">输入种子词并开始扩展后，结果会显示在这里。</p>; return <div className="table-wrap"><table><thead><tr><th>关键词</th><th>意图</th><th>分类</th><th>需求预估</th><th>SEO 审核</th></tr></thead><tbody id="suggest-keyword-table-body">{run.result.keywords.map((keyword) => { const review = reviews[keyword]; return <tr key={keyword}><td><strong>{keyword}</strong></td><td>{readableIntent(review?.search_intent)}</td><td>{review ? categoryOf(keyword, review) : "待审核"}</td><td>{demandEstimate(keyword)}/100</td><td>{review ? (review.is_seo_content_fit && review.same_topic_as_seed ? "适合" : "需人工确认") : "待审核"}</td></tr>; })}</tbody></table></div>; }
function Library({ keywords, onDelete }: { keywords: LibraryKeyword[]; onDelete: (id: number) => void }) { if (!keywords.length) return <p className="empty">当前项目还没有保存关键词。</p>; return <div className="table-wrap"><table><thead><tr><th>关键词</th><th>分类</th><th>意图</th><th>需求预估</th><th>真实 VOL</th><th>操作</th></tr></thead><tbody>{keywords.map((keyword) => <tr key={keyword.id}><td><strong>{keyword.keyword}</strong></td><td>{keyword.category || "未分类"}</td><td>{readableIntent(keyword.search_intent || undefined)}</td><td>{keyword.demand_estimate ?? "—"}/100</td><td>{keyword.search_volume ?? "待 Ads"}</td><td><button className="link danger" onClick={() => onDelete(keyword.id)}>删除</button></td></tr>)}</tbody></table></div>; }
function readableLevel(level: string) { return ({ low: "低竞争", medium: "中等竞争", high: "高竞争", very_high: "很高竞争" } as Record<string, string>)[level] || level; }
