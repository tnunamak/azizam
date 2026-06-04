// atoms.jsx — design tokens, icons, and small UI primitives for ai.vivid.fish
// Mobile-first. Calmed Vivid Fish palette. Tap-friendly. No glow tax.

const D = /*EDITMODE-BEGIN*/{
  "bg":           "#0A1620",
  "surface-1":    "#0E1E2C",
  "surface-2":    "#142A3D",
  "surface-3":    "#1C3850",
  "border":       "rgba(95,145,210,0.14)",
  "border-strong":"rgba(125,180,230,0.26)",
  "fg":           "#E8E2D3",
  "fg-muted":     "#8FA8BD",
  "fg-subtle":    "#5C7589",
  "accent":       "#7BC4C8",
  "accent-2":     "#D49AA6",
  "success":      "#6FB89A",
  "warning":      "#D9B86A",
  "danger":       "#D88370",
  "info":         "#7FB4C7"
}/*EDITMODE-END*/;

const F = {
  display: "'Geist',system-ui,sans-serif",
  body:    "'Geist',system-ui,sans-serif",
  serif:   "'Newsreader',ui-serif,serif",
  mono:    "'JetBrains Mono',ui-monospace,monospace",
};

// ============================================================
// ICONS — single stroke weight, 24-grid, rounded caps
// ============================================================
const Ico = ({ children, size = 20, stroke = 'currentColor', strokeWidth = 1.6, fill = 'none' }) =>
  <svg width={size} height={size} viewBox="0 0 24 24" fill={fill} stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" style={{ display:'inline-block', verticalAlign:'middle', flex:'0 0 auto' }}>{children}</svg>;

const I = {
  home:     (p)=> <Ico {...p}><path d="M3 11l9-7 9 7v9a2 2 0 0 1-2 2h-4v-7h-6v7H5a2 2 0 0 1-2-2z"/></Ico>,
  key:      (p)=> <Ico {...p}><circle cx="8" cy="12" r="3"/><path d="M11 12h10v3M16 12v3"/></Ico>,
  capability:(p)=> <Ico {...p}><circle cx="12" cy="12" r="2"/><path d="M12 4v3M12 17v3M4 12h3M17 12h3M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2"/></Ico>,
  signal:   (p)=> <Ico {...p}><path d="M4 19h2v-5H4zM10 19h2v-9h-2zM16 19h2V5h-2z"/></Ico>,
  user:     (p)=> <Ico {...p}><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-7 8-7s8 3 8 7"/></Ico>,
  plus:     (p)=> <Ico {...p}><path d="M12 5v14M5 12h14"/></Ico>,
  arrow:    (p)=> <Ico {...p}><path d="M5 12h14M13 5l7 7-7 7"/></Ico>,
  back:     (p)=> <Ico {...p}><path d="M19 12H5M11 5l-6 7 6 7"/></Ico>,
  chevron:  (p)=> <Ico {...p}><path d="M9 6l6 6-6 6"/></Ico>,
  check:    (p)=> <Ico {...p}><path d="M5 13l4 4L19 7"/></Ico>,
  close:    (p)=> <Ico {...p}><path d="M6 6l12 12M18 6L6 18"/></Ico>,
  copy:     (p)=> <Ico {...p}><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></Ico>,
  eye:      (p)=> <Ico {...p}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></Ico>,
  eyeOff:   (p)=> <Ico {...p}><path d="M3 3l18 18M9.6 5.2A10 10 0 0 1 12 5c6.5 0 10 7 10 7a18 18 0 0 1-3.4 4.4M6.6 6.6A18 18 0 0 0 2 12s3.5 7 10 7a10 10 0 0 0 4.6-1.1"/><path d="M14.1 14.1A3 3 0 0 1 9.9 9.9"/></Ico>,
  refresh:  (p)=> <Ico {...p}><path d="M3 12a9 9 0 0 1 15.5-6.3L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.3L3 16M3 21v-5h5"/></Ico>,
  trash:    (p)=> <Ico {...p}><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M6 6l1 14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-14"/></Ico>,
  edit:     (p)=> <Ico {...p}><path d="M14 4l6 6-12 12H2v-6z"/></Ico>,
  cog:      (p)=> <Ico {...p}><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3.9a7 7 0 0 0-2-1.2L14 3h-4l-.6 2.6a7 7 0 0 0-2 1.2L5.1 6 3.1 9.3l2 1.5A7 7 0 0 0 5 12a7 7 0 0 0 .1 1.2l-2 1.5 2 3.4 2.3-.9a7 7 0 0 0 2 1.2L10 21h4l.6-2.6a7 7 0 0 0 2-1.2l2.3.9 2-3.4-2-1.5A7 7 0 0 0 19 12z"/></Ico>,
  search:   (p)=> <Ico {...p}><circle cx="11" cy="11" r="6"/><path d="M20 20l-3.5-3.5"/></Ico>,
  info:     (p)=> <Ico {...p}><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v5h1"/></Ico>,
  warning:  (p)=> <Ico {...p}><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/></Ico>,
  external: (p)=> <Ico {...p}><path d="M14 4h6v6M20 4l-9 9M10 6H5a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5"/></Ico>,
  globe:    (p)=> <Ico {...p}><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></Ico>,
  shield:   (p)=> <Ico {...p}><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></Ico>,
  chat:     (p)=> <Ico {...p}><path d="M21 11a7 7 0 0 1-7 7H8l-4 3v-4a7 7 0 0 1-1-3.5C3 9 6 6 10 6h4c3.9 0 7 2.2 7 5z"/></Ico>,
  image:    (p)=> <Ico {...p}><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 15l-5-5-9 10"/></Ico>,
  embed:    (p)=> <Ico {...p}><path d="M9 8l-5 4 5 4M15 8l5 4-5 4M14 4l-4 16"/></Ico>,
  audio:    (p)=> <Ico {...p}><path d="M5 8h3l5-4v16l-5-4H5z"/><path d="M17 9a4 4 0 0 1 0 6M20 6a8 8 0 0 1 0 12"/></Ico>,
  video:    (p)=> <Ico {...p}><rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z"/></Ico>,
  bot:      (p)=> <Ico {...p}><rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9 16h6"/></Ico>,
  more:     (p)=> <Ico {...p}><circle cx="6" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="18" cy="12" r="1.4" fill="currentColor"/></Ico>,
};

// ============================================================
// LAYOUT — phone shell, screen header, screen body, bottom tabs
// ============================================================
const Phone = ({ children, title, statusBar = true }) => (
  <div style={{ width:'100%', height:'100%', background: D.bg, color: D.fg, fontFamily: F.body, position:'relative', overflow:'hidden', display:'flex', flexDirection:'column', borderRadius: 28 }}>
    {statusBar && (
      <div style={{ height: 24, padding: '4px 22px', display:'flex', justifyContent:'space-between', alignItems:'center', fontFamily: F.mono, fontSize: 11, color: D.fg, flex:'0 0 auto' }}>
        <span>9:41</span>
        <div style={{ display:'flex', gap: 6, alignItems:'center', opacity: 0.85 }}>
          <span style={{ fontSize: 10 }}>●●●●</span>
          <span style={{ fontSize: 10 }}>5G</span>
          <svg width="20" height="11" viewBox="0 0 20 11" fill="none" stroke={D.fg} strokeWidth="1"><rect x="0.5" y="0.5" width="16" height="10" rx="2"/><rect x="2" y="2" width="13" height="7" fill={D.fg} stroke="none"/><rect x="17" y="3.5" width="1.5" height="4" rx="0.5" fill={D.fg} stroke="none"/></svg>
        </div>
      </div>
    )}
    <div style={{ flex:1, display:'flex', flexDirection:'column', minHeight: 0 }}>{children}</div>
  </div>
);

const TopBar = ({ title, kicker, left, right }) => (
  <div style={{ display:'flex', alignItems:'flex-end', justifyContent:'space-between', padding: '14px 18px 12px', borderBottom: `1px solid ${D.border}`, background: D.bg, flex:'0 0 auto' }}>
    <div style={{ display:'flex', alignItems:'center', gap: 12, minWidth: 0 }}>
      {left}
      <div style={{ minWidth: 0 }}>
        {kicker && <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 2 }}>{kicker}</div>}
        <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 22, letterSpacing:'-0.02em', color: D.fg, lineHeight: 1, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{title}</div>
      </div>
    </div>
    <div style={{ display:'flex', gap: 4 }}>{right}</div>
  </div>
);

const Body = ({ children, pad = 18, style }) => (
  <div style={{ flex: 1, overflow:'auto', padding: pad, display:'flex', flexDirection:'column', gap: 14, ...style }}>{children}</div>
);

const Tabs = ({ active = 'home', admin = false }) => {
  const items = [
    { id:'home', label:'Home', Icon: I.home },
    { id:'keys', label:'Keys', Icon: I.key },
    { id:'caps', label:'What works', Icon: I.capability },
    { id:'activity', label:'Activity', Icon: I.signal },
    { id:'me', label: admin ? 'Admin' : 'You', Icon: admin ? I.shield : I.user },
  ];
  return (
    <div style={{ borderTop: `1px solid ${D.border}`, background: D.bg, padding: '8px 4px 22px', display:'grid', gridTemplateColumns: `repeat(${items.length},1fr)`, flex:'0 0 auto' }}>
      {items.map(t => (
        <button key={t.id} style={{ background:'transparent', border:0, color: active===t.id?D.accent:D['fg-subtle'], padding: '8px 4px 6px', display:'flex', flexDirection:'column', alignItems:'center', gap: 4, cursor:'pointer', fontFamily: F.body, fontSize: 10.5, letterSpacing:'0.02em', fontWeight: 500 }}>
          <t.Icon size={22}/>
          {t.label}
        </button>
      ))}
    </div>
  );
};

// ============================================================
// CONTENT BLOCKS
// ============================================================
const Card = ({ children, onClick, style, padding = 16, raised = false }) => (
  <div onClick={onClick} style={{ background: raised ? D['surface-2'] : D['surface-1'], border: `1px solid ${D.border}`, borderRadius: 14, padding, cursor: onClick ? 'pointer' : 'default', ...style }}>{children}</div>
);

const Btn = ({ children, variant = 'primary', size = 'md', icon, iconRight, fullWidth, disabled, style }) => {
  const sz = { sm:{h:32,px:14,fs:12.5}, md:{h:44,px:18,fs:14}, lg:{h:52,px:22,fs:15.5} }[size];
  const variants = {
    primary:   { bg: D.accent, fg: '#0A1620', border: 'transparent', weight: 600 },
    secondary: { bg: D['surface-2'], fg: D.fg, border: D['border-strong'], weight: 500 },
    ghost:     { bg: 'transparent', fg: D['fg-muted'], border: 'transparent', weight: 500 },
    danger:    { bg: 'transparent', fg: D.danger, border: D.danger, weight: 600 },
  }[variant];
  return (
    <button disabled={disabled} style={{
      height: sz.h, padding: `0 ${sz.px}px`, fontSize: sz.fs, fontFamily: F.body, fontWeight: variants.weight, letterSpacing:'-0.005em',
      background: variants.bg, color: variants.fg, border: `1px solid ${variants.border}`, borderRadius: 999,
      display:'inline-flex', alignItems:'center', justifyContent:'center', gap: 8,
      width: fullWidth ? '100%' : 'auto', opacity: disabled?0.4:1, cursor: disabled?'not-allowed':'pointer',
      boxShadow: variant==='primary' ? '0 1px 0 rgba(255,255,255,0.12) inset, 0 4px 14px rgba(0,0,0,0.18)' : 'none',
      ...style
    }}>
      {icon}{children}{iconRight}
    </button>
  );
};

const Pill = ({ children, tone = 'neutral', icon, size = 'md' }) => {
  const tones = {
    neutral: { bg: D['surface-3'], fg: D['fg-muted'] },
    accent:  { bg: 'rgba(123,196,200,0.14)', fg: D.accent },
    success: { bg: 'rgba(111,184,154,0.14)', fg: D.success },
    warning: { bg: 'rgba(217,184,106,0.14)', fg: D.warning },
    danger:  { bg: 'rgba(216,131,112,0.14)', fg: D.danger },
    info:    { bg: 'rgba(127,180,199,0.14)', fg: D.info },
  }[tone];
  const sz = size==='sm'?{h:18,px:8,fs:9.5}:{h:22,px:10,fs:10.5};
  return <span style={{ display:'inline-flex', alignItems:'center', gap: 4, height: sz.h, padding: `0 ${sz.px}px`, borderRadius: 999, background: tones.bg, color: tones.fg, fontFamily: F.mono, fontSize: sz.fs, letterSpacing:'0.06em', textTransform:'uppercase', whiteSpace:'nowrap' }}>
    {icon}{children}
  </span>;
};

const StatusDot = ({ tone = 'success', pulse }) => {
  const c = { success: D.success, warning: D.warning, danger: D.danger, info: D.info, neutral: D['fg-subtle'] }[tone];
  return <span style={{ display:'inline-block', width: 8, height: 8, borderRadius: '50%', background: c, animation: pulse ? 'ag-pulse 2.2s ease-in-out infinite' : 'none', flex:'0 0 auto' }}/>;
};

const Field = ({ label, value, placeholder, helper, error, prefix, suffix, mono = false, fullWidth = true, autoFocus }) => (
  <label style={{ display:'flex', flexDirection:'column', gap: 6, width: fullWidth ? '100%' : 'auto' }}>
    {label && <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'], letterSpacing:'-0.005em' }}>{label}</span>}
    <div style={{ display:'flex', alignItems:'center', gap: 8, height: 48, padding:'0 14px', background: D['surface-1'], border:`1.5px solid ${error?D.danger:autoFocus?D.accent:D.border}`, borderRadius: 12, boxShadow: autoFocus ? `0 0 0 4px rgba(123,196,200,0.12)` : 'none' }}>
      {prefix && <span style={{ color: D['fg-subtle'], display:'flex' }}>{prefix}</span>}
      <span style={{ flex: 1, fontFamily: mono ? F.mono : F.body, fontSize: 15, color: value ? D.fg : D['fg-subtle'], letterSpacing: mono?'0':'-0.005em', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{value || placeholder}</span>
      {suffix && <span style={{ color: D['fg-subtle'], display:'flex' }}>{suffix}</span>}
    </div>
    {(error || helper) && <span style={{ fontFamily: F.body, fontSize: 12, color: error?D.danger:D['fg-subtle'], lineHeight: 1.4 }}>{error || helper}</span>}
  </label>
);

const Sentence = ({ children, italic = true }) => (
  <p style={{ margin: 0, fontFamily: F.serif, fontStyle: italic ? 'italic' : 'normal', fontSize: 15, lineHeight: 1.5, color: D.fg, letterSpacing:'-0.005em' }}>{children}</p>
);

const Hairline = ({ vertical, color }) => vertical
  ? <span style={{ display:'inline-block', width: 1, alignSelf:'stretch', background: color || D.border }}/>
  : <hr style={{ border: 0, height: 1, background: color || D.border, margin: '0' }}/>;

const Kbd = ({ children }) => <span style={{ display:'inline-flex', alignItems:'center', justifyContent:'center', minWidth: 22, height: 22, padding:'0 6px', borderRadius: 6, background: D['surface-3'], border: `1px solid ${D.border}`, borderBottomWidth: 2, fontFamily: F.mono, fontSize: 11, color: D['fg-muted'] }}>{children}</span>;

// Section heading inside a screen body
const Section = ({ title, action, children, kicker }) => (
  <div style={{ display:'flex', flexDirection:'column', gap: 10 }}>
    <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between' }}>
      <div>
        {kicker && <div style={{ fontFamily: F.mono, fontSize: 9.5, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase' }}>{kicker}</div>}
        <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 13, letterSpacing:'0.02em', color: D['fg-muted'], textTransform:'uppercase' }}>{title}</div>
      </div>
      {action}
    </div>
    {children}
  </div>
);

// Inject animation keyframes
(function(){
  if (typeof document==='undefined' || document.getElementById('ag-kf')) return;
  const s = document.createElement('style'); s.id = 'ag-kf';
  s.textContent = `
    @keyframes ag-pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
    @keyframes ag-blink { 50% { opacity: 0.3; } }
  `;
  document.head.appendChild(s);
})();

Object.assign(window, { D, F, I, Phone, TopBar, Body, Tabs, Card, Btn, Pill, StatusDot, Field, Sentence, Hairline, Kbd, Section });
