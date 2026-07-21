import type { AuthoritySource, CompetitorResearch, ContentAsset, ContentAssetDetail, ContentBrief, ContentGenerationResult, ContentMemoryItem, ContentOutline, ExpansionResult, LibraryKeyword, Review, Score, SerpTitle, SerpTitleMemory, TitleCandidate, TitleGenerationJob } from "./types";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const payload = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(payload.error || "请求失败，请稍后重试。");
  return payload;
}

const json = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const api = {
  expand: (body: object) => request<ExpansionResult>("/api/suggest-expansions", json(body)),
  review: (body: object) => request<{ review: Review }>("/api/ai-keyword-reviews", json(body)),
  createProject: (body: object) => request<{ id: number }>("/api/projects", json(body)),
  listProjects: () => request<Array<{ id: number; name: string }>>("/api/projects"),
  getAiSettings: () => request<{ configured: boolean; base_url: string | null; model: string | null; provider: string | null; providers?: Record<string, { configured: boolean; base_url: string | null; model: string | null }>; assignments?: { keyword_review: string; title_generation: string } }>("/api/settings/ai"),
  saveAiSettings: (body: object) => request<{ configured: boolean; base_url: string; model: string; provider: string; providers?: Record<string, { configured: boolean; base_url: string | null; model: string | null }>; assignments?: { keyword_review: string; title_generation: string } }>("/api/settings/ai", json(body)),
  testAiSettings: (body: object = {}) => request<{ status: string; provider: string; model: string }>("/api/settings/ai/test", json(body)),
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
  createContentBrief: (assetId: number, body: object) => request<ContentBrief>(`/api/content-assets/${assetId}/briefs`, json(body)),
  createContentOutline: (assetId: number, body: object) => request<ContentOutline>(`/api/content-assets/${assetId}/outlines`, json(body)),
  generateContentBrief: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate-brief`, json(body)),
  generateContentOutline: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate-outline`, json(body)),
  generateContentDraft: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate-draft`, json(body)),
  generateContent: (assetId: number, body: object) => request<ContentGenerationResult>(`/api/content-assets/${assetId}/generate`, json(body)),
  researchCompetitors: (assetId: number, body: object) => request<CompetitorResearch>(`/api/content-assets/${assetId}/research-competitors`, json(body)),
  listContentMemory: (projectId: number, query = "") => request<ContentMemoryItem[]>(`/api/content-memory?project_id=${projectId}&q=${encodeURIComponent(query)}`),
  deleteContentMemory: (memoryId: number, body: object) => request<{ deleted: number }>(`/api/content-memory/${memoryId}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
};
