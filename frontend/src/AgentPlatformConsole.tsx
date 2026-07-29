import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import "./AgentPlatformConsole.css";

const platform = "http://127.0.0.1:8010";

type Site = { id: number; domain: string; industry: string; country_code: string; language_code: string; workspace_project_id: number | null; is_legacy: boolean };
type Knowledge = { id: number; title: string; source_type: "upload" | "domain" | "note"; status: string; content: string; url: string; knowledge_type?: string; created_at: string };
type CrawlPage = { url: string; title?: string; status: "ready" | "skipped" | "failed"; reason?: string };
type CrawlStart = { run_id: number; status: string; max_pages: number };
type CrawlResult = { status: "queued" | "running" | "completed" | "failed"; max_pages: number; discovered_count: number; accepted_count: number; skipped_count: number; failed_count: number; message: string; failure_reason: string; pages: CrawlPage[] };

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(platform + path, { headers: { "Content-Type": "application/json" }, ...init });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "请求失败");
  return body as T;
}

export function AgentPlatformConsole() {
  const nav = useNavigate();
  const { siteId } = useParams();
  const [sites, setSites] = useState<Site[]>([]);
  const [docs, setDocs] = useState<Knowledge[]>([]);
  const [notice, setNotice] = useState("正在加载网站项目…");
  const [form, setForm] = useState({ domain: "", industry: "" });
  const [doc, setDoc] = useState({ title: "", content: "" });
  const [crawlLimit, setCrawlLimit] = useState(20);
  const [crawling, setCrawling] = useState(false);
  const [crawlResult, setCrawlResult] = useState<CrawlResult | null>(null);
  const active = useMemo(() => sites.find((site) => site.id === Number(siteId)), [sites, siteId]);

  const loadDocs = async (id: number) => setDocs(await call<Knowledge[]>(`/api/websites/${id}/knowledge`));
  const refresh = async () => {
    try {
      setSites(await call<Site[]>("/api/websites"));
      setNotice("每个网站使用独立的关键词、标题、内容与公司知识库。");
    } catch (error) { setNotice(error instanceof Error ? error.message : "平台不可用"); }
  };
  useEffect(() => { void refresh(); }, []);
  useEffect(() => { if (active) void loadDocs(active.id).catch((error: Error) => setNotice(error.message)); else setDocs([]); }, [active?.id]);

  const ensureWorkspaceProject = async (site: Site): Promise<Site> => {
    if (site.workspace_project_id) return site;
    const response = await fetch("/api/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: site.domain, country_code: site.country_code || "US", language_code: site.language_code || "en" }) });
    const project = await response.json();
    if (!response.ok || !project.id) throw new Error(project.error || "无法创建网站项目");
    const updated = await call<Site>(`/api/websites/${site.id}`, { method: "PATCH", body: JSON.stringify({ workspace_project_id: project.id }) });
    setSites((current) => current.map((item) => item.id === updated.id ? updated : item));
    return updated;
  };
  const create = async () => {
    try {
      const response = await fetch("/api/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: form.domain, country_code: "US", language_code: "en" }) });
      const project = await response.json();
      if (!response.ok || !project.id) throw new Error(project.error || "无法创建网站项目");
      const site = await call<Site>("/api/websites", { method: "POST", body: JSON.stringify({ ...form, workspace_project_id: project.id }) });
      await refresh(); nav(`/agent-platform/site/${site.id}`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "新建失败"); }
  };
  const removeSite = async (site: Site) => {
    if (!window.confirm(`确认删除网站“${site.domain}”吗？这会删除该网站的新知识库和平台资料。`)) return;
    await call(`/api/websites/${site.id}`, { method: "DELETE" }); await refresh();
    if (site.id === Number(siteId)) nav("/agent-platform");
  };
  const addDoc = async () => {
    if (!active) return;
    await call(`/api/websites/${active.id}/knowledge`, { method: "POST", body: JSON.stringify({ title: doc.title, source_type: "upload", content: doc.content }) });
    setDoc({ title: "", content: "" }); await loadDocs(active.id);
  };
  const deleteDoc = async (id: number) => { if (active) { await call(`/api/websites/${active.id}/knowledge/${id}`, { method: "DELETE" }); await loadDocs(active.id); } };
  const upload = (file: File | undefined) => { if (!file) return; const reader = new FileReader(); reader.onload = () => setDoc({ title: file.name, content: String(reader.result || "") }); reader.readAsText(file); };
  const crawl = async () => {
    if (!active || crawling) return;
    setCrawling(true); setCrawlResult(null); setNotice(`正在从 ${active.domain} 发现并并发提取同域 HTML 页面…`);
    try {
      const start = await call<CrawlStart>(`/api/websites/${active.id}/knowledge/crawl`, { method: "POST", body: JSON.stringify({ max_pages: crawlLimit }) });
      const poll = async (): Promise<void> => {
        const result = await call<CrawlResult>(`/api/websites/${active.id}/knowledge/crawl/${start.run_id}`);
        setCrawlResult(result);
        if (result.status === "queued" || result.status === "running") { await new Promise<void>((resolve) => window.setTimeout(resolve, 1800)); return poll(); }
        if (result.status === "completed") { await loadDocs(active.id); setNotice(`完整扫描完成：已发现 ${result.discovered_count} 页，收录 ${result.accepted_count} 页，跳过 ${result.skipped_count} 页，失败 ${result.failed_count} 页。`); }
        else setNotice(`网站采集失败：${result.failure_reason || result.message}`);
      };
      await poll();
    } catch (error) { setNotice(`网站采集失败：${error instanceof Error ? error.message : "请稍后重试"}`); }
    finally { setCrawling(false); }
  };
  const link = (path: string) => active?.workspace_project_id ? `${path}?project_id=${active.workspace_project_id}&site_id=${active.id}` : "#";
  const openModule = async (event: React.MouseEvent<HTMLAnchorElement>, path: string) => { if (!active) return; event.preventDefault(); try { const site = await ensureWorkspaceProject(active); nav(`${path}?project_id=${site.workspace_project_id}&site_id=${site.id}`); } catch (error) { setNotice(error instanceof Error ? error.message : "项目跳转失败"); } };

  return <section className="simple-console">
    <header className="simple-hero"><div className="simple-hero-copy"><p>多站点 SEO 工作台</p><h2>{active ? active.domain : "选择一个网站开始"}</h2><span>{notice}</span></div></header>
    <nav className="site-tabs">{sites.map((site) => <div key={site.id} className={`site-tab ${site.id === Number(siteId) ? "active" : ""}`}><button onClick={() => nav(`/agent-platform/site/${site.id}`)}>{site.domain}</button><button className="remove-site" title="删除网站" onClick={() => void removeSite(site)}>×</button></div>)}<button className="new" onClick={() => nav("/agent-platform")}>+ 新建网站</button></nav>
    {!active ? <section className="simple-panel new-site"><h3>新建独立网站</h3><input placeholder="域名，例如 example.com" value={form.domain} onChange={(event) => setForm({ ...form, domain: event.target.value })}/><input placeholder="行业，例如 Industrial LED" value={form.industry} onChange={(event) => setForm({ ...form, industry: event.target.value })}/><button disabled={!form.domain || !form.industry} onClick={() => void create()}>创建网站控制台</button></section> : <>
      <section className="site-dashboard">
        <a className="module-card" href={link("/research")} onClick={(event) => void openModule(event, "/research")}><small>01 · 第一步</small><h3>挖掘关键词</h3><p>为当前网站发现、审核并入库关键词。</p><b>开始挖掘 →</b></a>
        <a className="module-card" href={link("/titles")} onClick={(event) => void openModule(event, "/titles")}><small>02 · 第二步</small><h3>关键词生成标题</h3><p>用当前网站关键词生成并选择 SEO 标题。</p><b>生成标题 →</b></a>
        <a className="module-card" href={link("/content")} onClick={(event) => void openModule(event, "/content")}><small>03 · 第三步</small><h3>标题生成内容</h3><p>写作时自动读取本网站知识库资料。</p><b>生成内容 →</b></a>
        <a className="module-card" href="#knowledge"><small>04 · 资料中心</small><h3>公司知识库</h3><p>{docs.length} 份已收录资料，可手工补充或在线采集。</p><b>管理知识库 ↓</b></a>
      </section>
      <section className="simple-panel knowledge" id="knowledge"><h3>公司知识库</h3><p>写内容时会自动读取当前网站的资料。可手工上传，也可从当前项目域名在线采集公司、产品、规格、案例和 FAQ 页面。</p>
        <div className="knowledge-crawl"><div><b>完整采集产品与公司资料</b><span>递归发现并分类当前网站的产品页、About Us/工厂介绍、FAQ、认证、案例和公开业务联系页；自动排除博客、PDF、DOCX、图片、登录、搜索、对比和下载页面。</span></div><label>扫描上限<select value={crawlLimit} onChange={(event) => setCrawlLimit(Number(event.target.value))}><option value={50}>50 页</option><option value={100}>100 页</option><option value={300}>300 页</option></select></label><button className="crawl-button" disabled={crawling} onClick={() => void crawl()}>{crawling ? "正在后台完整采集…" : "完整采集并归类"}</button></div>
        {crawlResult ? <div className="crawl-result"><b>{crawlResult.status === "completed" ? `本次完整采集：发现 ${crawlResult.discovered_count} 页` : crawlResult.status === "failed" ? "采集失败" : "正在后台扫描、分类并入库…"}</b>{crawlResult.pages.map((page) => <article key={page.url} className={page.status}><span>{page.status === "ready" ? "已入库" : page.status === "skipped" ? "已跳过" : "失败"}</span><div><a href={page.url} target="_blank" rel="noreferrer">{page.title || page.url}</a><small>{page.reason || page.url}</small></div></article>)}</div> : null}
        <div className="knowledge-manual"><input placeholder="资料标题" value={doc.title} onChange={(event) => setDoc({ ...doc, title: event.target.value })}/><textarea placeholder="粘贴公司介绍、产品参数、案例、FAQ 等资料…" value={doc.content} onChange={(event) => setDoc({ ...doc, content: event.target.value })}/><label>上传文本资料 <input type="file" accept=".txt,.md,.html,.csv" onChange={(event) => upload(event.target.files?.[0])}/></label><button disabled={!doc.title || !doc.content} onClick={() => void addDoc()}>加入知识库</button></div>
        <h4>已收录资料（{docs.length}）</h4>{docs.map((item) => <article className="knowledge-item" key={item.id}><div><b>{item.title}</b><span>{item.source_type === "domain" ? "网站采集" : "手工资料"} · {({ company: "公司资料", product: "产品资料", faq: "FAQ", certification: "认证资料", case_study: "案例", other: "其他资料" } as Record<string, string>)[item.knowledge_type || "other"]} · {item.status}{item.url ? ` · ${item.url}` : ""}</span></div><button title="删除资料" onClick={() => void deleteDoc(item.id)}>×</button></article>)}
      </section>
    </>}
  </section>;
}
