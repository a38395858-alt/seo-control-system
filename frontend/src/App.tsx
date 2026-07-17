import { useEffect, useMemo, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api } from "./api";
import type { ContentAsset, ExpansionResult, LibraryKeyword, Review, Score, SerpTitle, TitleCandidate } from "./types";

type RunState = { seeds: string[]; language: string; country: string; result: ExpansionResult } | null;
type ScoreInputs = Record<"keyword" | "volume" | "authority" | "domains" | "titleMatch" | "authoritySites" | "intent" | "relevance" | "businessValue", string>;

const projectKey = "seo-keyword-project-id";
type AiProvider = "openai" | "gemini" | "deepseek";
type AiProfile = { baseUrl: string; model: string; apiKey: string };
type AiAssignments = { keyword_review: AiProvider; title_generation: AiProvider };
function RecentTitleLibrary({ titles }: { titles: TitleCandidate[] }) { return <section className="recent-title-library"><div><strong>最近入库标题</strong><p>每次生成完成后会自动写入标题库，不会因为刷新或切换页面而丢失。</p></div><NavLink className="primary" to="/title-library">打开标题库（{titles.length} 条）</NavLink>{titles.length ? <div className="recent-title-list">{titles.slice(0, 5).map((title) => <article key={title.id}><ProviderBadge reason={title.reason} /><strong>{title.title}</strong><span>{title.keyword || "—"}</span></article>)}</div> : <p className="empty">暂时还没有标题。生成后会自动写入标题库。</p>}</section>; }
function ContentAssets({ titles, assets, onCreate }: { titles: TitleCandidate[]; assets: ContentAsset[]; onCreate: (title: TitleCandidate) => void }) { const available = titles.filter((title) => title.status === "selected"); const createdByTitle = new Map(assets.map((asset) => [asset.selected_title_candidate_id, asset])); return <div className="content-assets"><h3>已选标题</h3>{available.length ? available.map((title) => { const asset = createdByTitle.get(title.id); return <article key={title.id}><div><ProviderBadge reason={title.reason} /><strong>{title.title}</strong><p>{title.keyword || "—"}</p></div>{asset ? <span className="tag">已创建</span> : <button className="primary" onClick={() => onCreate(title)}>创建内容</button>}</article>; }) : <p className="empty">请先在标题库选定一个标题。</p>}<h3>内容资产</h3>{assets.length ? assets.map((asset) => <article key={asset.id}><div><strong>{asset.title_snapshot}</strong><p>{asset.keyword || "—"} · {asset.status} · {asset.locale}</p></div><span className="tag">下一步：Brief</span></article>) : <p className="empty">尚未创建内容资产。</p>}</div>; }const aiProviderPresets: Record<AiProvider, { baseUrl: string; model: string }> = {
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
      const settings = await api.getAiSettings();
      setAiProfiles((current) => (Object.keys(aiProviderPresets) as AiProvider[]).reduce((profiles, provider) => {
        const saved = settings.providers?.[provider];
        profiles[provider] = { baseUrl: saved?.base_url || current[provider].baseUrl, model: saved?.model || current[provider].model, apiKey: "" };
        return profiles;
      }, {} as Record<AiProvider, AiProfile>));
      setAiConfigured((Object.keys(aiProviderPresets) as AiProvider[]).reduce((profiles, provider) => ({ ...profiles, [provider]: Boolean(settings.providers?.[provider]?.configured) }), {} as Record<AiProvider, boolean>));
      if (settings.assignments?.keyword_review && settings.assignments?.title_generation) setAiAssignments(settings.assignments as AiAssignments);
      setAiStatus(settings.configured ? `已配置：${settings.provider || "兼容接口"}` : "尚未配置 AI 接口。");
      if (activeProjectId) await Promise.all([loadLibrary(activeProjectId), loadTitleLibrary(activeProjectId), loadContentAssets(activeProjectId)]);
    };
    restoreProjectAndAssets().catch(() => { localStorage.removeItem(projectKey); setProjectId(null); });
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
  const createContentFromTitle = async (title: TitleCandidate) => {
    if (!projectId) return;
    try {
      const asset = await api.createContentAsset({ project_id: projectId, selected_title_candidate_id: title.id, content_type: "guide" });
      setContentStatus(`已创建内容：${asset.title_snapshot}`);
      await loadContentAssets();
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
    <aside className="sidebar"><div className="brand"><span>SEO</span><small>Keyword Intelligence</small></div><nav><NavLink to="/research" className={({ isActive }) => isActive ? "active" : ""}>关键词挖掘</NavLink><NavLink to="/keywords" className={({ isActive }) => isActive ? "active" : ""}>关键词库</NavLink><NavLink to="/titles" className={({ isActive }) => isActive ? "active" : ""}>SEO 标题</NavLink><NavLink to="/title-library" className={({ isActive }) => isActive ? "active" : ""}>标题库</NavLink><NavLink to="/content" className={({ isActive }) => isActive ? "active" : ""}>内容系统</NavLink><NavLink to="/scoring" className={({ isActive }) => isActive ? "active" : ""}>SEO 评分</NavLink><NavLink to="/settings" className={({ isActive }) => isActive ? "active" : ""}>AI 配置</NavLink></nav><p className="connection">● 本地工作台已连接</p></aside>
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

      <Route path="/content" element={<section className="panel" id="content-system"><PanelTitle eyebrow="Content System" title="内容系统" tag={`${contentAssets.length} 篇内容`} /><p className="hint">从已选标题建立内容资产。基础版先保存 Brief 和人工大纲；AI 分段写作与审核将在下一步接入。</p><p className="tag">{contentStatus}</p><ContentAssets titles={titleLibrary} assets={contentAssets} onCreate={createContentFromTitle} /></section>} />`r`n`r`n      <Route path="/title-library" element={<section className="panel" id="title-library"><PanelTitle eyebrow="Content Assets" title="标题库" tag={`${titleLibrary.length} 条标题`} /><p className="hint">每次 AI 或人工生成的标题都会自动保存到这里；“已选定”代表该关键词当前唯一可进入后续内容流程的标题。</p><div className="actions"><button onClick={() => loadTitleLibrary().catch((error) => setTitleStatus(error.message))}>刷新标题库</button></div><TitleLibrary titles={titleLibrary} onCreateContent={createContentFromTitle} onSelectTitle={selectLibraryTitle} /></section>} />

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
function TitleLibrary({ titles, onCreateContent, onSelectTitle }: { titles: TitleCandidate[]; onCreateContent: (title: TitleCandidate) => void; onSelectTitle: (title: TitleCandidate) => void }) { if (!titles.length) return <p className="empty">还没有已保存标题。请先从关键词库生成标题。</p>; return <div className="table-wrap"><table><thead><tr><th>标题</th><th>关联关键词</th><th>来源</th><th>质量分</th><th>状态</th><th>操作</th></tr></thead><tbody>{titles.map((title) => <tr key={title.id}><td><strong>{title.title}</strong></td><td>{title.keyword || "—"}</td><td>{title.source_type === "ai" ? <ProviderBadge reason={title.reason} /> : <span className="provider-badge provider-manual">人工录入</span>}</td><td>{title.quality_score}/100</td><td>{title.status === "selected" ? "已选定" : title.status === "candidate" ? "待选择" : "未选定"}</td><td><div className="actions">{title.status === "selected" ? <button className="primary" onClick={() => onCreateContent(title)}>加入内容生成</button> : <button onClick={() => onSelectTitle(title)}>选定标题</button>}</div></td></tr>)}</tbody></table></div>; }function ProviderBadge({ reason }: { reason: string | null }) { return <span className={`provider-badge provider-${providerKey(reason)}`}>{providerLabel(reason)}</span>; }
function providerKey(reason: string | null) { const provider = reason?.match(/^\[(ChatGPT|Gemini|DeepSeek)\]/)?.[1]; return ({ ChatGPT: "chatgpt", Gemini: "gemini", DeepSeek: "deepseek" } as Record<string, string>)[provider || ""] || "ai"; }
function providerLabel(reason: string | null) { const provider = reason?.match(/^\[(ChatGPT|Gemini|DeepSeek)\]/)?.[1]; return provider ? `${provider} 生成` : "AI 生成"; }
function readableLevel(level: string) { return ({ low: "低竞争", medium: "中等竞争", high: "高竞争", very_high: "很高竞争" } as Record<string, string>)[level] || level; }
