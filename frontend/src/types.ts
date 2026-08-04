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

export type ContentAsset = { id: number; project_id: number; keyword_id: number; selected_title_candidate_id: number; title_snapshot: string; keyword?: string; locale: string; country_code: string; content_type: string; status: string; tags?: string[]; current_brief_id: number | null; current_outline_id: number | null; current_draft_id?: number | null; outline_status?: string; outline_status_label?: string; content_status?: string; content_status_label?: string; };
export type ContentBrief = { id: number; target_audience: string; business_goal: string; target_length: number; sources: unknown[]; brief: Record<string, unknown> };
export type AuthoritySource = { id: number; project_id: number; title: string; source_type: "first_party" | "standard" | "certification" | "government" | "industry_research"; url?: string | null; publisher?: string | null; published_at?: string | null; content: string; authority_level: "primary" | "authoritative" | "supporting" | "needs_review"; tags: string[]; classification: Record<string, unknown>; summary?: string | null; created_at: string; updated_at: string };
export type ContentOutline = { id: number; status: string; sections: Array<{ id: number; heading: string; purpose: string; word_budget: number; keyword_requirements?: { supporting_terms?: string[]; minimum_supporting_terms?: number }; depth_requirements?: { minimum_subtopics?: number; reader_outcome?: string; practical_detail?: string } }> };
export type ContentDraft = { id: number; version: number; parent_draft_id?: number | null; title: string; meta_description: string; markdown: string; qa_status: string; qa?: { checks?: Array<{ name: string; status: string; note: string }>; targeted_rewrite?: Array<{ target: string; issue: string; instruction: string }>; reviewer?: { provider?: string; model?: string | null } }; unresolved_verify: string[]; created_at?: string; provider?: string; model?: string };
export type ContentGenerationRun = { id: number; generation_job_id?: number | null; stage: string; provider: string; model: string | null; status: string; error_summary?: string | null; started_at?: string; completed_at?: string };
export type ContentGenerationJob = { id: number; requested_action: string; provider: string; model: string | null; reviewer_provider?: string | null; reviewer_model?: string | null; routing_mode?: "manual" | "auto_collaborate"; routing_summary?: string; status: "running" | "completed" | "failed"; failed_stage?: string | null; error_summary?: string | null; started_at?: string; completed_at?: string };
export type CompetitorResearch = { id: number; status: "running" | "completed" | "insufficient" | "failed"; query: string; discovered_count: number; usable_count: number; provider?: string | null; model?: string | null; error_summary?: string | null; analysis: { search_intent?: string; entities?: string[]; missing_gaps?: string[]; dynamic_outline?: Array<{ heading: string; purpose?: string; reader_question?: string; key_points?: string[]; source_ids?: string[]; format?: "paragraphs" | "list" | "table" }>; faq_heading?: string }; items: Array<{ id: number; rank: number; search_title: string; url: string; domain: string; status: string; page_title?: string; error_summary?: string | null }> };
export type ContentMemoryItem = { id: number; url: string; domain: string; page_title: string; first_captured_at: string; last_captured_at: string; structure: { sample_lines?: string[] } };
export type CompetitorUrlArchiveItem = { id: number; url: string; domain: string; search_title: string; status: "robots_blocked"; last_rank?: number | null; last_query: string; error_summary: string; discovered_count: number; first_seen_at: string; last_seen_at: string };
export type CompetitorUrlCatalogItem = { id: number; url: string; domain: string; search_title: string; collection_status: "discovered" | "queued" | "collected" | "excluded" | "robots_blocked" | "failed"; exclusion_reason: string; last_rank?: number | null; last_query: string; memory_id?: number | null; discovered_count: number; first_seen_at: string; last_seen_at: string; last_collected_at?: string | null };
export type CompetitorCatalogCollectionRun = { id: number; project_id: number; status: "queued" | "running" | "completed" | "failed"; total_count: number; collected_count: number; unchanged_count: number; already_collected_count: number; robots_blocked_count: number; failed_count: number; recovered_count: number; last_heartbeat_at?: string | null; error_summary?: string | null; started_at?: string | null; completed_at?: string | null; created_at: string; updated_at: string };
export type CollectionRunItem = { id: number; project_id: number; run_id: number; catalog_id?: number | null; normalized_url: string; source_url: string; status: "queued" | "running" | "collected" | "unchanged" | "robots_blocked" | "excluded" | "failed"; attempt_count: number; memory_id?: number | null; version_id?: number | null; extractor: string; error_summary: string; domain?: string; search_title?: string; started_at?: string | null; completed_at?: string | null };
export type CollectionPlan = { id: number; project_id: number; source_type: "domain" | "keyword" | "first_party"; source_value: string; status: "active" | "paused"; schedule: string; settings: Record<string, unknown>; discovered_count: number; last_discovered_at?: string | null; created_at: string; updated_at: string };
export type CollectedCompetitorContentLearningRun = { id: number; project_id: number; status: "queued" | "running" | "completed" | "failed"; source_count: number; processed_count: number; memories_created_count: number; memories_updated_count: number; memories_rejected_count: number; provider: "openai" | "gemini" | "deepseek"; model?: string | null; error_summary?: string | null; started_at?: string | null; completed_at?: string | null; created_at: string; updated_at: string };
export type ContentLearningMemory = { id: number; project_id: number; memory_type: "style" | "brand" | "fact" | "performance" | "editorial"; card_type: "general" | "writing_style" | "content_structure" | "topic_gap"; role?: "style" | "brand" | "fact" | "performance"; topic: string; summary: string; evidence: Record<string, unknown>; applicability: { search_intents?: string[]; content_types?: string[]; topics?: string[] }; source_url?: string; quality_score: number; confidence_score: number; freshness_status: "current" | "stale" | "needs_review"; evidence_count: number; inference_level: "observed" | "synthesized" | "unverified"; relevance_score?: number; selection_reason?: string; status: "active" | "disabled" | "superseded"; manual_priority: number; pinned: number; positive_feedback_count: number; negative_feedback_count: number; last_feedback_at?: string | null; selected_by_model?: number; selected_by_user?: number; linked_at?: string };
export type ContentLearningMemoryDetail = ContentLearningMemory & { sources: Array<{ source_type: string; source_id: string; source_url: string; source_content_hash: string; source_version_id?: number | null; evidence_excerpt: string; captured_at: string }>; content_links: Array<{ id: number; content_asset_id: number; title_snapshot?: string; role: string; relevance_score: number; selected_by_model: number; selected_by_user: number; created_at: string }>; feedback: Array<{ id: number; decision: "useful" | "not_useful"; note: string; created_at: string }> };
export type CompetitorLearningSchedule = { id?: number; project_id: number; topics: string[]; interval_days: number; enabled: number; provider?: "openai" | "gemini" | "deepseek" | null; model?: string | null; last_run_at?: string | null; next_run_at?: string | null };
export type CompetitorStyleCard = { id: number; project_id: number; schedule_id?: number | null; learning_run_id?: number | null; topic: string; card_title: string; summary: string; evidence: { source_count?: number; sources?: Array<{ url?: string; title?: string; domain?: string }>; policy?: string }; quality_score: number; status: "active" | "disabled" | "superseded"; updated_at: string };
export type CompetitorLearningRun = { id: number; project_id: number; schedule_id?: number | null; content_asset_id?: number | null; topic: string; trigger_type: "manual" | "scheduled"; status: "queued" | "running" | "completed" | "insufficient" | "failed"; source_count: number; cards_created: number; error_summary?: string | null; created_at: string; started_at?: string | null; completed_at?: string | null };
export type CompetitorLearningDashboard = { schedule: CompetitorLearningSchedule; cards: CompetitorStyleCard[]; runs: CompetitorLearningRun[] };
export type ContentEffectiveness = { articles: Array<{ content_asset_id: number; draft_id: number; title: string; published_url: string; quality_score: number; quality_breakdown: Array<{ key: string; label: string; points: number; max: number; note: string }>; performance_status: "qualified" | "observing" | "insufficient"; performance_score: number | null; performance_breakdown: Array<{ key: string; label: string; points: number; max: number; note: string }>; combined_score: number | null; latest_snapshot: { clicks: number; impressions: number; ctr: number; average_position: number; query_count: number; collected_at: string } | null; memory_assisted: boolean; evaluation_group: "memory_assisted" | "no_memory_baseline"; used_memories: Array<{ memory_id: number; memory_type: string; card_type: string; topic: string; source_url: string; sources: Array<{ source_type: string; source_id: string; source_url: string; source_content_hash: string; captured_at: string }>; relevance_score: number; selection_reason: string; selected_at: string }> }>; summary: { published_articles: number; qualified_articles: number; memory_assisted_articles: number; correlation_state: "insufficient" | "descriptive"; spearman_correlation: number | null; note: string } };
export type ArticleAuthoritySource = AuthoritySource & { section_heading?: string | null; claim_topic?: string | null; linked_at?: string | null };
export type AuthoritySearchResult = { id: number; search_run_id: string; section_heading?: string | null; claim_topic?: string | null; search_query: string; rank?: number | null; title?: string | null; url?: string | null; domain?: string | null; status: "pending" | "accepted" | "skipped" | "search_error"; error_summary?: string | null };
export type SectionImage = { id: number; section_heading: string; position: number; prompt: string; alt_text: string; seo_filename?: string | null; status: "prompt_ready" | "generating" | "ready" | "failed"; image_url?: string | null; error_summary?: string | null };
export type WordPressPublication = { id: number; wordpress_post_id?: number | null; wordpress_url?: string | null; status: "draft" | "publish" | "failed"; error_summary?: string | null; created_at?: string };
export type PublishGateReport = { status: "ready" | "blocked"; draft_id: number | null; requested_status: "draft" | "publish"; allow_without_images: boolean; checks: Array<{ code: string; passed: boolean; message: string }>; issues: Array<{ code: string; message: string }> };
export type PrepareWordPressPublish = { status: "ready" | "blocked"; gate_report_id: number; report: PublishGateReport; job_id?: number; approval_id?: number; reused?: boolean };
export type ContentProductionReadiness = { research_run_id?: number; research_status?: string; collected_count: number; usable_count?: number; analysis_ready: boolean; learning_ready: boolean; can_generate_outline: boolean; blocked_reason: string };
export type CompetitorResearchPreview = { query: string; discovered_count: number; available_count: number; recommended_learning_count: number; items: Array<{ rank: number; title: string; url: string; domain: string; status: "available" | "failed" | "skipped"; reason: string; content_chars: number }> };
export type CompetitorOutlinePreview = { source_count: number; persisted: false; analysis: { search_intent?: string; missing_gaps?: string[]; dynamic_outline: Array<{ heading: string; reader_question?: string; purpose?: string; key_points?: string[]; format?: string }> } };
export type ContentAssetDetail = ContentAsset & { brief: ContentBrief | null; outline: ContentOutline | null; current_draft?: ContentDraft | null; drafts?: ContentDraft[]; generation_runs?: ContentGenerationRun[]; generation_jobs?: ContentGenerationJob[]; competitor_research?: CompetitorResearch | null; production_readiness?: ContentProductionReadiness; learning_memories?: ContentLearningMemory[]; authority_sources?: ArticleAuthoritySource[]; authority_search_results?: AuthoritySearchResult[]; section_images?: SectionImage[]; wordpress_publications?: WordPressPublication[] };
export type ContentGenerationResult = { brief?: ContentBrief | null; outline?: ContentOutline | null; draft?: ContentDraft | null; current_draft?: ContentDraft | null; quality_review?: { draft: ContentDraft; review: ContentDraft["qa"] }; competitor_research?: CompetitorResearch; generation_runs?: ContentAssetDetail["generation_runs"]; runs?: ContentAssetDetail["generation_runs"]; generation_job?: ContentGenerationJob | null; asset?: ContentAsset };
export type ContentPromptPreview = {
  prompt_version: string;
  requested_action: string;
  article: { title: string; primary_keyword: string; locale: string; target_audience: string; business_goal: string };
  source_summary: Array<{ key: string; label: string; count: number; rule: string; examples: string[] }>;
  full_article_requirements?: { sections: Array<{ heading: string; keyword_requirements: { primary_keyword: string; primary_keyword_rule: string; supporting_terms: string[]; minimum_supporting_terms: number }; depth_requirements: { minimum_non_overlapping_subtopics: number; minimum_words: number; reader_outcome: string; required_practical_detail: string } }>; overall: { minimum_total_words: number; required_h2_count: number; depth_rule: string; keyword_rule: string } | null };
  system_prompt: string;
  stages: Array<{ stage: string; instruction: string }>;
};
export type SystemTask = {
  task_type: string;
  id: number;
  project_id: number;
  project_name: string;
  status: string;
  updated_at: string;
  message: string;
};

export type AgentStep = {
  id: number;
  node_name: string;
  attempt: number;
  status: string;
  input_summary: string;
  duration_ms: number;
  error_summary?: string | null;
  completed_at?: string | null;
};

export type AgentToolAudit = {
  id: number;
  tool_name: string;
  status: "completed" | "failed";
  duration_ms: number;
  error_summary?: string | null;
  completed_at?: string | null;
};

export type AgentJob = {
  id: number;
  project_id: number;
  content_asset_id?: number | null;
  requested_action: string;
  status: string;
  lifecycle_state: string;
  current_node: string;
  workflow_version: string;
  input: Record<string, unknown>;
  result: Record<string, unknown>;
  checkpoint: Record<string, unknown>;
  error_summary?: string | null;
  created_at: string;
  updated_at: string;
  steps?: AgentStep[];
  tool_audits?: AgentToolAudit[];
  approvals?: Array<{
    id: number;
    approval_type: string;
    status: string;
    created_at: string;
    payload?: {
      title?: string;
      brief_id?: number;
      outline_id?: number;
      outline?: { sections?: Array<{ heading?: string; purpose?: string }> };
      memories?: Array<{ memory_id?: number; topic?: string; selection_reason?: string; source_url?: string }>;
      content_asset_id?: number;
      draft_id?: number;
      requested_status?: "draft" | "publish";
      gate_report_id?: number;
      report?: PublishGateReport;
    };
  }>;
  basis_report?: {
    draft_id?: number | null;
    memories?: Array<{
      memory_id?: number;
      memory_type?: string;
      topic?: string;
      selection_reason?: string;
      source_url?: string;
      sources?: Array<{ source_url?: string; source_content_hash?: string; source_version_id?: number }>;
    }>;
    qa?: { status?: string | null; rewrite_count?: number; history?: Array<{ draft_id?: number; status?: string; rewrite_targets?: number }> };
    unresolved_verify?: unknown[];
    model_route?: Record<string, unknown>;
  } | null;
};
