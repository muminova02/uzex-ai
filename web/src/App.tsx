import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { Meta, Options } from "./api";
import Icon from "./components/Icon";
import { useApi } from "./filters";
import { t, type Lang } from "./i18n";
import Assistant from "./pages/Assistant";
import Dashboard from "./pages/Dashboard";
import Explorer from "./pages/Explorer";

interface Ctx { lang: Lang; setLang: (l: Lang) => void; meta: Meta | null; options: Options | null; refreshMeta: () => void }
const AppCtx = createContext<Ctx>({ lang: "uz", setLang: () => {}, meta: null, options: null, refreshMeta: () => {} });
export const useApp = () => useContext(AppCtx);

function Brand() {
  return (
    <span className="brand">
      <span className="brand-logo" aria-hidden="true"><i style={{ height: 9 }} /><i style={{ height: 15 }} /><i style={{ height: 22 }} /></span>
      <span className="brand-name">UZEX<small>Data Assistant</small></span>
    </span>
  );
}

function Nav({ lang, llmState, onNavigate }: { lang: Lang; llmState: string | null; onNavigate?: () => void }) {
  const loc = useLocation();
  const keep = (path: string) => path + (loc.search || "");  // carry the visible filter state between pages
  const items: [string, string, string][] = [["/dashboard", "home", t("nav_dashboard", lang)], ["/explorer", "layers", t("nav_explorer", lang)],
    ["/assistant", "sparkles", t("nav_assistant", lang)]];
  return (
    <nav className="nav" aria-label={t("main_nav", lang)}>
      {items.map(([path, icon, label]) => (
        <NavLink key={path} to={keep(path)} onClick={onNavigate}>
          <Icon name={icon} /> <span>{label}</span>
          {path === "/assistant" && llmState && llmState !== "ok" && (
            <span className="dot">{llmState === "limited" ? (lang === "uz" ? "limit" : "лимит") : (lang === "uz" ? "o'chiq" : "выкл.")}</span>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

function SideFoot({ lang }: { lang: Lang }) {
  return (
    <div className="side-foot">
      <a className="tg-link" href="https://t.me/uzex_ai_agent_1_bot" target="_blank" rel="noopener noreferrer"
         aria-label={lang === "uz" ? "UZEX Assistant Telegram-boti (yangi oynada ochiladi)" : "Telegram-бот UZEX Assistant (откроется в новом окне)"}>
        <svg className="tg-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="12" fill="#229ED9" />
          <path fill="#fff" d="M5.4 11.7 16.9 7.3c.5-.2 1 .1.8.9l-2 9.3c-.1.6-.5.8-1 .5l-2.9-2.1-1.4 1.3c-.2.2-.3.3-.6.3l.2-3 5.5-5c.2-.2 0-.3-.3-.1l-6.8 4.3-2.9-.9c-.6-.2-.6-.6.1-.9Z" />
        </svg>
        <span><b>UZEX Assistant bot</b><small>Telegram</small></span>
      </a>
      <span className="pill sage">{t("synthetic", lang)}</span>
      <span className="muted">{t("prototype_short", lang)} · {t("not_official", lang)}</span>
    </div>
  );
}

function LangSwitch({ lang, setLang }: { lang: Lang; setLang: (l: Lang) => void }) {
  return (
    <div className="lang-switch" role="group" aria-label={t("lang_group", lang)}>
      <button aria-pressed={lang === "uz"} onClick={() => setLang("uz")} lang="uz" title="O'zbekcha">UZ</button>
      <span className="sep" aria-hidden="true">/</span>
      <button aria-pressed={lang === "ru"} onClick={() => setLang("ru")} lang="ru" title="Русский">RU</button>
    </div>
  );
}

/** Narrow screens: modal navigation drawer (focus moves in, Tab is trapped, Escape/overlay close, focus returns). */
function NavDrawer({ open, onClose, lang, llmState }: { open: boolean; onClose: () => void; lang: Lang; llmState: string | null }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const el = ref.current;
    el?.querySelector<HTMLElement>("a, button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
      if (e.key !== "Tab" || !el) return;
      const f = Array.from(el.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"));
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div className="drawer-overlay" onClick={onClose} aria-hidden="true" />
      <div className="nav-drawer" ref={ref} role="dialog" aria-modal="true" aria-label={t("main_nav", lang)} id="nav-drawer">
        <div className="drawer-top">
          <Brand />
          <button className="icon-btn" onClick={onClose} aria-label={t("menu_close", lang)}><Icon name="close" /></button>
        </div>
        <Nav lang={lang} llmState={llmState} onNavigate={onClose} />
        <SideFoot lang={lang} />
      </div>
    </>
  );
}

export default function App() {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem("uzex_lang") === "ru" ? "ru" : "uz"));
  const setLang = (l: Lang) => { setLangState(l); localStorage.setItem("uzex_lang", l); };
  useEffect(() => { document.documentElement.lang = lang; }, [lang]);
  const meta = useApi<Meta>("/api/meta");
  const options = useApi<Options>("/api/filters/options");
  const a = meta.data?.assistant;
  const llmState = !a ? null : !a.configured ? "off" : a.cooldown_s > 0 ? "limited" : "ok";
  const [drawer, setDrawer] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const closeDrawer = useCallback(() => { setDrawer(false); setTimeout(() => trigger.current?.focus(), 0); }, []);
  const loc = useLocation();
  useEffect(() => { document.getElementById("main")?.focus({ preventScroll: true }); }, [loc.pathname]);
  return (
    <AppCtx.Provider value={{ lang, setLang, meta: meta.data, options: options.data, refreshMeta: meta.reload }}>
      <a className="skip" href="#main">{lang === "uz" ? "Asosiy qismga o'tish" : "Перейти к содержимому"}</a>
      <div className="layout">
        <aside className="sidebar" aria-label="UZEX Data Assistant">
          <Brand />
          <Nav lang={lang} llmState={llmState} />
          <SideFoot lang={lang} />
        </aside>
        <NavDrawer open={drawer} onClose={closeDrawer} lang={lang} llmState={llmState} />
        <div className="content">
          <div className="topbar">
            <span className="mobile-only">
              <button ref={trigger} className="icon-btn" onClick={() => setDrawer(true)} aria-label={t("menu_open", lang)}
                      aria-expanded={drawer} aria-controls="nav-drawer"><Icon name="menu" /></button>
              <span className="brand-mini">UZEX Data Assistant</span>
            </span>
            <LangSwitch lang={lang} setLang={setLang} />
          </div>
          <main id="main" tabIndex={-1} style={{ outline: "none" }}>
            {meta.error && <div className="banner danger" role="alert"><Icon name="alert" /> {t("error", lang)}: {meta.error.message}</div>}
            {meta.data?.mode === "demo" && (
              <div className="banner demo" role="status">
                {lang === "uz" ? "DEMO REJIMI: chat javoblari skriptlangan (haqiqiy til modeli yo'q). Dashboard va bitimlar — haqiqiy baza."
                               : "ДЕМО-РЕЖИМ: ответы чата заскриптованы (без настоящей языковой модели). Дашборд и сделки — реальная база."}
              </div>
            )}
            <Routes>
              <Route path="/" element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/explorer" element={<Explorer />} />
              <Route path="/assistant" element={<Assistant />} />
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
          </main>
          <footer className="site-foot">
            <span>{t("footer_note", lang)}</span>
            <span>{t("prototype_short", lang)} · {t("not_official", lang)}</span>
          </footer>
        </div>
      </div>
    </AppCtx.Provider>
  );
}
