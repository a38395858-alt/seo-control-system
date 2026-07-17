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
