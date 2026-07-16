import type { ExpansionResult, LibraryKeyword, Review, Score } from "./types";

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
  saveExpanded: (body: object) => request<{ inserted: number; existing: number }>("/api/expanded-keywords", json(body)),
  listKeywords: (projectId: number) => request<LibraryKeyword[]>(`/api/keywords?project_id=${projectId}`),
  deleteKeywords: (body: object) => request<{ deleted: number }>("/api/keywords", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  score: (body: object) => request<{ scores: Score[] }>("/api/keyword-opportunity-scores", json(body)),
};
