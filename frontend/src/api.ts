import type { AuthoritySource, CompetitorResearch, CompetitorUrlArchiveItem, ContentAsset, ContentAssetDetail, ContentBrief, ContentGenerationResult, ContentMemoryItem, ContentOutline, ExpansionResult, LibraryKeyword, Review, Score, SerpTitle, SerpTitleMemory, TitleCandidate, TitleGenerationJob } from "./types";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const raw = await response.text();
  let payload: (T & { error?: string }) | undefined;
  try {
    payload = raw ? JSON.parse(raw) as T & { error?: string } : undefined;
  } catch {
    const detail = contentType.includes("text/html") ? "服务返回了网页而不是接口数据，请刷新或重启本地服务后重试。" : "服务返回了无法识别的数据，请稍后重试。";
    throw new Error(`${detail}（${response.status} ${response.statusText}）`);
  }
  if (!response.ok) throw new Error(payload?.error || `请求失败（${response.status} ${response.statusText}）。`);
  if (!payload) throw new Error("服务未返回数据，请稍后重试。");
  return payload;
}

const json = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const api = {
  expand: (body: object) => request<ExpansionResult>("/api/suggest-expansions", json(body)),
  review: (body: object) => request<{ review: Review }>("/api/ai-keyword-reviews", json(body)),
  createProject: (body: object) => request<{ id: number }>("/api/projects", json(body)),
  listProjects: () => request<Array<{ id: number; name: string; site_url?: string | null; industry?: string; default_country?: string; default_language?: string }>>("/api/projects"),
  listProjectSummaries: () => request<Array<{ id: number; name: string; site_url?: string | null; industry: string; default_country: string; default_language: string; keyword_count: number; selected_title_count: number; content_count: number; knowledge_count: number; latest_content_status?: string | null }>>("/api/projects/summary"),
  updateProject: (projectId: number, body: object) => request<{ id: number; name: string; site_url?: string | null; industry: string; default_country: string; default_language: string }>(`/api/projects/${projectId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  deleteProject: (projectId: number) => request<{ deleted: number }>(`/api/projects/${projectId}`, { method: "DELETE" }),
  listSystemTasks: () => request<Array<{ task_type: string; id: number; project_id: number; project_name: string; status: string; updated_at: string; message: string }>>("/api/system-tasks"),
  listProjectKnowledge: (projectId: number) => request<Array<{ id: number; project_id: number; title: string; source_type: string; url: string; content: string; knowledge_type: string; status: string; created_at: string; updated_at: string }>>(`/api/projects/${projectId}/knowledge`),
  createProjectKnowledge: (projectId: number, body: { title: string; content: string; source_type?: string; url?: string; knowledge_type?: string }) => request<{ id: number }>(`/api/projects/${projectId}/knowledge`, json(body)),
  deleteProjectKnowledge: (projectId: number, documentId: number) => request<{ deleted: number }>(`/api/projects/${projectId}/knowledge/${documentId}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) }),
  crawlProjectKnowledge: (projectId: number, max_pages = 20) => request<{ id: number; status: string; message: string; accepted_count: number; skipped_count: number; failed_count: number; pages: Array<{ url: string; title: string; knowledge_type: string; status: string; reason: string }> }>(`/api/projects/${projectId}/knowledge/crawl`, json({ max_pages })),
  getProjectKnowledgeCrawl: (projectId: number, runId: number) => request<{ id: number; status: string; message: string; accepted_count: number; skipped_count: number; failed_count: number; pages: Array<{ url: string; title: string; knowledge_type: string; status: string; reason: string }> }>(`/api/projects/${projectId}/knowledge/crawl/${runId}`),
  getAiSettings: () => request<{ configured: boolean; base_url: string | null; model: string | null; provider: string | null; providers?: Record<string, { configured: boolean; base_url: string | null; model: string | null }>; assignments?: { keyword_review: string; title_generation: string } }>("/api/settings/ai"),
  saveAiSettings: (body: object) => request<{ configured: boolean; base_url: string; model: string; provider: string; providers?: Record<string, { configured: boolean; base_url: string | null; model: string | null }>; assignments?: { keyword_review: string; title_generation: string } }>("/api/settings/ai", json(body)),
  testAiSettings: (body: object = {}) => request<{ status: string; provider: string; model: string }>("/api/settings/ai/test", json(body)),
  getSerperSettings: () => request<{ configured: boolean; provider: string; website: string }>("/api/settings/serper"),
  saveSerperSettings: (body: { api_key: string }) => request<{ configured: boolean; provider: string; website: string }>("/api/settings/serper", json(body)),
  testSerperSettings: (body: { api_key?: string } = {}) => request<{ status: string; provider: string; website: string; sample_title: string }>("/api/settings/serper/test", json(body)),
  getImageGenerationSettings: () => request<{ configured: boolean; base_url?: string | null; model: string; provider: "openai" | "siliconflow"; provider_label: string; api_key_saved: boolean }>("/api/settings/images"),
  saveImageGenerationSettings: (body: { provider: "openai" | "siliconflow"; model: string; api_key?: string }) => request<{ configured: boolean; base_url?: string | null; model: string; provider: "openai" | "siliconflow"; provider_label: string; api_key_saved: boolean }>("/api/settings/images", json(body)),
  testImageGenerationSettings: () => request<{ status: string; provider: string; base_url?: string; model: string }>("/api/settings/images/test", json({})),
  openProjectGscBrowser: (projectId: number) => request<{ status: string; message: string }>(`/api/projects/${projectId}/gsc/browser/open`),
  captureProjectGscBrowser: (projectId: number) => request<{ anchors: Array<{ query: string; page_url: string; clicks: number; impressions: number; ctr: number; position: number }>; synced?: number }>(`/api/projects/${projectId}/gsc/browser/capture`, json({})),
  captureProjectGscRankedPages: (projectId: number) => request<{ anchors: Array<{ query: string; page_url: string; clicks: number; impressions: number; ctr: number; position: number }>; synced?: number; capture?: { queries_checked: number; queries_without_page: number; queries_non_english?: number; max_position: number; cleared?: number } }>(`/api/projects/${projectId}/gsc/browser/capture-ranked-pages`, json({})),
  listProjectGscAnchors: (projectId: number) => request<{ anchors: Array<{ query: string; page_url: string; clicks: number; impressions: number; ctr: number; position: number; collected_at: string }> }>(`/api/projects/${projectId}/gsc/anchors`),
  learnFromPublishedGscContent: (projectId: number, body: { days?: number } = {}) => request<{ snapshots: Array<{ content_asset_id: number; snapshot_id: number; title: string; published_url: string; query_count: number; impressions: number; learning_status: "observing" | "qualified" | "insufficient"; memory_id?: number | null; summary: string }>; memories_created: number; message: string }>(`/api/projects/${projectId}/gsc/learn-content`, json(body)),
  listPublishedGscPerformance: (projectId: number) => request<Array<{ id: number; content_asset_id: number; title_snapshot: string; published_url: string; clicks: number; impressions: number; ctr: number; average_position: number; query_count: number; learning_status: "observing" | "qualified" | "insufficient"; summary: string; collected_at: string }>>(`/api/projects/${projectId}/gsc/content-performance`),
  getContentEffectiveness: (projectId: number) => request<import("./types").ContentEffectiveness>(`/api/projects/${projectId}/gsc/content-effectiveness`),
  saveExpanded: (body: object) => request<{ inserted: number; existing: number }>("/api/expanded-keywords", json(body)),
  listKeywords: (projectId: number) => request<LibraryKeyword[]>(`/api/keywords?project_id=${projectId}`),
  deleteKeywords: (body: object) => request<{ deleted: number }>("/api/keywords", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  score: (body: object) => request<{ scores: Score[] }>("/api/keyword-opportunity-scores", json(body)),
  researchSerpTitles: (body: object) => request<{ keyword: string; titles: SerpTitle[]; warning?: string; saved_count: number }>("/api/serp-title-research", json(body)),
  researchBrowserSerpTitles: (body: object) => request<{ keyword: string; titles: SerpTitle[]; source_type: "browser"; saved_count?: number; verification_required?: boolean; verification_image?: string | null }>("/api/browser-serp-title-research", json(body)),
  listSerpTitleMemory: (projectId: number, keywordId: number) => request<{ titles: SerpTitleMemory[] }>(`/api/serp-title-samples?project_id=${projectId}&keyword_id=${keywordId}`),
  createTitleJob: (body: object) => request<TitleGenerationJob>("/api/title-generation-jobs", json(body)),
  generateMultiProviderTitles: (body: object) => request<TitleGenerationJob & { failures: string[] }>("/api/multi-provider-title-generation-jobs", json(body)),
  listTitleCandidates: (projectId: number, keywordId: number) => request<{ candidates: TitleCandidate[]; selected_title: TitleCandidate | null }>(`/api/keywords/${keywordId}/title-candidates?project_id=${projectId}`),
  listTitleLibrary: (projectId: number) => request<TitleCandidate[]>(`/api/title-library?project_id=${projectId}`),
  selectTitleCandidate: (candidateId: number, body: object) => request<TitleCandidate>(`/api/title-candidates/${candidateId}/select`, json(body)),
  createTitleCandidate: (body: object) => request<TitleCandidate>("/api/title-candidates", json(body)),
  deleteTitleCandidate: (candidateId: number, body: object) => request<{ deleted: number }>(`/api/title-candidates/${candidateId}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  deleteTitleCandidates: (body: object) => request<{ deleted: number }>("/api/title-candidates", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  listContentAssets: (projectId: number) => request<ContentAsset[]>(`/api/content-assets?project_id=${projectId}`),
  listContentLibrary: (projectId: number) => request<ContentAsset[]>(`/api/content-library?project_id=${projectId}`),
  listAuthoritySources: (projectId: number) => request<AuthoritySource[]>(`/api/authority-sources?project_id=${projectId}`),
  createAuthoritySource: (body: object) => request<AuthoritySource>("/api/authority-sources", json(body)),
  researchAuthoritySources: (body: object) => request<{ article: string; candidates_checked: number; saved: AuthoritySource[]; skipped: Array<{ url: string; title: string; reason: string }> }>("/api/authority-sources/research", json(body)),
  deleteAuthoritySource: (sourceId: number, projectId: number) => request<{ deleted: number }>(`/api/authority-sources/${sourceId}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId }) }),
  createContentAsset: (body: object) => request<ContentAsset>("/api/content-assets", json(body)),
  deleteContentAssets: (body: object) => request<{ deleted: number }>("/api/content-assets", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  getContentAsset: (assetId: number, projectId: number) => request<ContentAssetDetail>(`/api/content-assets/${assetId}?project_id=${projectId}`),
  createSectionImagePrompts: (assetId: number, projectId: number) => request<{ images: import("./types").SectionImage[] }>(`/api/content-assets/${assetId}/image-prompts`, json({ project_id: projectId })),
  generateAllSectionImages: (assetId: number, projectId: number) => request<{ generated: import("./types").SectionImage[]; failed: Array<{ id: string; error: string }> }>(`/api/content-assets/${assetId}/generate-images`, json({ project_id: projectId })),
  generateSectionImage: (imageId: number, projectId: number) => request<import("./types").SectionImage>(`/api/content-images/${imageId}/generate`, json({ project_id: projectId })),
  getWordPressConfig: (projectId: number) => request<{ configured: boolean; site_url?: string; username?: string }>(`/api/projects/${projectId}/wordpress`),
  saveWordPressConfig: (projectId: number, body: object) => request<{ configured: boolean; site_url?: string; username?: string }>(`/api/projects/${projectId}/wordpress`, json(body)),
  testWordPressConfig: (projectId: number) => request<{ status: string; username: string }>(`/api/projects/${projectId}/wordpress/test`, json({})),
  prepareWordPressPublish: (assetId: number, body: object) => request<import("./types").PrepareWordPressPublish>(`/api/content-assets/${assetId}/prepare-publish`, json(body)),
  decideAgentApproval: (approvalId: number, body: object) => request<unknown>(`/api/agent-approvals/${approvalId}`, json(body)),
  publishWordPress: (assetId: number, body: object) => request<import("./types").WordPressPublication>(`/api/content-assets/${assetId}/publish-wordpress`, json(body)),
  createContentBrief: (assetId: number, body: object) => request<ContentBrief>(`/api/content-assets/${assetId}/briefs`, json(body)),
  createContentOutline: (assetId: number, body: object) => request<ContentOutline>(`/api/content-assets/${assetId}/outlines`, json(body)),
  generateContentBrief: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate-brief`, json(body)),
  generateContentOutline: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate-outline`, json(body)),
  generateContentDraft: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate-draft`, json(body)),
  reviewContentQuality: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/review-quality`, json(body)),
  rewriteContentTargeted: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/rewrite-targeted`, json(body)),
  generateContent: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate`, json(body)),
  researchCompetitors: (assetId: number, body: object) => request<CompetitorResearch>(`/api/content-assets/${assetId}/research-competitors`, json(body)),
  listContentMemory: (projectId: number, query = "") => request<ContentMemoryItem[]>(`/api/content-memory?project_id=${projectId}&q=${encodeURIComponent(query)}`),
  listCompetitorUrlArchive: (projectId: number) => request<CompetitorUrlArchiveItem[]>(`/api/competitor-url-archive?project_id=${projectId}`),
  deleteContentMemory: (memoryId: number, body: object) => request<{ deleted: number }>(`/api/content-memory/${memoryId}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  createContentLearningMemory: (body: { project_id: number; memory_type: "style" | "brand" | "fact" | "performance" | "editorial"; topic: string; summary: string; quality_score: number; evidence: Record<string, unknown> }) => request<import("./types").ContentLearningMemory>("/api/content-learning-memories", json(body)),
  listContentLearningMemories: (projectId: number) => request<import("./types").ContentLearningMemory[]>(`/api/content-learning-memories?project_id=${projectId}`),
  getContentLearningMemory: (memoryId: number, projectId: number) => request<import("./types").ContentLearningMemoryDetail>(`/api/content-learning-memories/${memoryId}?project_id=${projectId}`),
  updateContentLearningMemory: (memoryId: number, body: { project_id: number; topic?: string; summary?: string; manual_priority?: number }) => request<import("./types").ContentLearningMemory>(`/api/content-learning-memories/${memoryId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  setContentLearningMemoryPin: (memoryId: number, projectId: number, pinned: boolean) => request<import("./types").ContentLearningMemory>(`/api/content-learning-memories/${memoryId}/${pinned ? "pin" : "unpin"}`, json({ project_id: projectId })),
  setContentLearningMemoryStatus: (memoryId: number, projectId: number, enabled: boolean) => request<import("./types").ContentLearningMemory>(`/api/content-learning-memories/${memoryId}/${enabled ? "enable" : "disable"}`, json({ project_id: projectId })),
  addContentLearningMemoryFeedback: (memoryId: number, body: { project_id: number; decision: "useful" | "not_useful"; note?: string }) => request<import("./types").ContentLearningMemory>(`/api/content-learning-memories/${memoryId}/feedback`, json(body)),
  getCompetitorLearning: (projectId: number, includeRuns = true) => request<import("./types").CompetitorLearningDashboard>(`/api/projects/${projectId}/competitor-learning${includeRuns ? "/runs" : ""}`),
  saveCompetitorLearningSchedule: (projectId: number, body: { topics: string[]; interval_days: number; enabled: boolean; provider?: "openai" | "gemini" | "deepseek"; model?: string }) => request<import("./types").CompetitorLearningSchedule>(`/api/projects/${projectId}/competitor-learning`, json(body)),
  runCompetitorLearningNow: (projectId: number, topic?: string) => request<import("./types").CompetitorLearningRun>(`/api/projects/${projectId}/competitor-learning/run`, json(topic ? { topic } : {})),
};
