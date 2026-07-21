export type Review = {
  is_seo_content_fit: boolean;
  same_topic_as_seed: boolean;
  search_intent: string;
  recommended_action: string;
  reason: string;
  confidence: number;
};

export type ExpansionResult = {
  keywords: string[];
  requests_made: number;
  stop_reason: string;
  debug_logs: Array<{ level: string; event: string; code: string; message: string }>;
};

export type LibraryKeyword = {
  id: number;
  keyword: string;
  category: string | null;
  search_intent: string | null;
  demand_estimate: number | null;
  search_volume: number | null;
  is_seo_content_fit: number | null;
  selected_title: string | null;
  title_candidate_count: number;
};

export type Score = { keyword: string; keyword_difficulty: number; difficulty_level: string; opportunity_score: number };

export type TitleCandidate = {
  id: number;
  keyword_id: number;
  generation_job_id: number | null;
  title: string;
  title_type: string | null;
  search_intent: string | null;
  reason: string | null;
  source_type: "ai" | "manual";
  quality_score: number;
  quality_details: { keyword_coverage?: boolean; length_ok?: boolean };
  status: "candidate" | "selected" | "not_selected" | "archived";
  keyword?: string;
};

export type TitleGenerationJob = {
  id: number;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  generated_count: number;
};

export type SerpTitle = { rank: number; title: string; source: string | null };
export type SerpTitleMemory = SerpTitle & { source_type: "browser" | "ai"; locale: string; captured_at: string };

export type ContentAsset = { id: number; project_id: number; keyword_id: number; selected_title_candidate_id: number; title_snapshot: string; keyword?: string; locale: string; country_code: string; content_type: string; status: string; current_brief_id: number | null; current_outline_id: number | null; current_draft_id?: number | null; outline_status?: string; outline_status_label?: string; content_status?: string; content_status_label?: string; };
export type ContentBrief = { id: number; target_audience: string; business_goal: string; target_length: number; sources: unknown[]; brief: Record<string, unknown> };
export type AuthoritySource = { id: number; project_id: number; title: string; source_type: "first_party" | "standard" | "certification" | "government" | "industry_research"; url?: string | null; publisher?: string | null; published_at?: string | null; content: string; authority_level: "primary" | "authoritative" | "supporting" | "needs_review"; tags: string[]; classification: Record<string, unknown>; summary?: string | null; created_at: string; updated_at: string };
export type ContentOutline = { id: number; status: string; sections: Array<{ id: number; heading: string; purpose: string; word_budget: number }> };
export type ContentDraft = { id: number; version: number; title: string; meta_description: string; markdown: string; qa_status: string; unresolved_verify: string[]; created_at?: string; provider?: string; model?: string };
export type ContentGenerationRun = { id: number; generation_job_id?: number | null; stage: string; provider: string; model: string | null; status: string; error_summary?: string | null; started_at?: string; completed_at?: string };
export type ContentGenerationJob = { id: number; requested_action: string; provider: string; model: string | null; status: "running" | "completed" | "failed"; failed_stage?: string | null; error_summary?: string | null; started_at?: string; completed_at?: string };
export type CompetitorResearch = { id: number; status: "running" | "completed" | "insufficient" | "failed"; query: string; discovered_count: number; usable_count: number; provider?: string | null; model?: string | null; error_summary?: string | null; analysis: { search_intent?: string; entities?: string[]; missing_gaps?: string[]; dynamic_outline?: Array<{ heading: string; purpose?: string; reader_question?: string; key_points?: string[]; source_ids?: string[]; format?: "paragraphs" | "list" | "table" }>; faq_heading?: string }; items: Array<{ id: number; rank: number; search_title: string; url: string; domain: string; status: string; page_title?: string; error_summary?: string | null }> };
export type ContentMemoryItem = { id: number; url: string; domain: string; page_title: string; first_captured_at: string; last_captured_at: string; structure: { sample_lines?: string[] } };
export type ArticleAuthoritySource = AuthoritySource & { section_heading?: string | null; claim_topic?: string | null; linked_at?: string | null };
export type ContentAssetDetail = ContentAsset & { brief: ContentBrief | null; outline: ContentOutline | null; current_draft?: ContentDraft | null; drafts?: ContentDraft[]; generation_runs?: ContentGenerationRun[]; generation_jobs?: ContentGenerationJob[]; competitor_research?: CompetitorResearch | null; authority_sources?: ArticleAuthoritySource[] };
export type ContentGenerationResult = { brief?: ContentBrief | null; outline?: ContentOutline | null; draft?: ContentDraft | null; current_draft?: ContentDraft | null; competitor_research?: CompetitorResearch; generation_runs?: ContentAssetDetail["generation_runs"]; runs?: ContentAssetDetail["generation_runs"]; generation_job?: ContentGenerationJob | null; asset?: ContentAsset };
