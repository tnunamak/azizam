// screens-desktop.jsx — desktop adaptations of the screens that earn it.
// Mobile is the canon. Desktop is "the same thing, with the room it deserves."
// Three screens: User Home, Receipt v2, Admin Home, plus a fuller People list.

// ============================================================
// Shared desktop chrome: sidebar + main
// ============================================================
function DesktopShell({ active = 'home', admin = false, user = { name:'Ada Lovelace', email:'ada@vivid.fish' }, children, title, kicker, primary }) {
  const nav = admin
    ? [
        { i: I.home,       l:'Operating', id:'home' },
        { i: I.user,       l:'People',    id:'people' },
        { i: I.shield,     l:'Profiles',  id:'profiles' },
        { i: I.bot,        l:'Service apps', id:'services' },
        { i: I.search,     l:'Debug',     id:'debug' },
      ]
    : [
        { i: I.home,       l:'Home',      id:'home' },
        { i: I.key,        l:'Keys',      id:'keys' },
        { i: I.signal,     l:'Receipts',  id:'receipts' },
      ];
  return (
    <div style={{ width:'100%', height:'100%', background: D.bg, color: D.fg, fontFamily: F.body, display:'flex', overflow:'hidden' }}>
      <aside style={{ width: 224, borderRight: `1px solid ${D.border}`, padding:'18px 14px', background: D['surface-1'], display:'flex', flexDirection:'column', flex:'0 0 224px' }}>
        <div style={{ display:'flex', alignItems:'center', gap: 8, padding:'4px 8px 22px' }}>
          <div style={{ width: 26, height: 26, borderRadius: 8, background: D.accent, display:'flex',alignItems:'center',justifyContent:'center' }}><I.globe size={13} stroke="#0A1620"/></div>
          <span style={{ fontFamily: F.display, fontWeight: 700, fontSize: 15, letterSpacing:'-0.01em' }}>ai<span style={{ color: D.accent }}>.</span>vivid<span style={{ color: D.accent }}>.</span>fish</span>
        </div>
        {admin && <div style={{ fontFamily: F.mono, fontSize: 9.5, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', padding:'0 8px 6px' }}>admin</div>}
        {nav.map(n => (
          <div key={n.id} style={{ display:'flex', alignItems:'center', gap: 10, padding:'9px 10px', borderRadius: 8, color: n.id===active?D.fg:D['fg-muted'], background: n.id===active?D['surface-2']:'transparent', fontSize: 13.5, cursor:'pointer', marginBottom: 2 }}>
            <n.i size={16}/>{n.l}
          </div>
        ))}
        <div style={{ flex: 1 }}/>
        {!admin && (
          <div style={{ padding: 10, background: D['surface-2'], border: `1px solid ${D.border}`, borderRadius: 10, display:'flex', gap: 10, alignItems:'center', marginTop: 8 }}>
            <div style={{ width: 30, height: 30, borderRadius:'50%', background: D['surface-3'], color: D.fg, display:'flex',alignItems:'center',justifyContent:'center', fontFamily: F.display, fontWeight: 700, fontSize: 12 }}>AL</div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontFamily: F.body, fontSize: 12.5, color: D.fg, fontWeight: 500, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{user.name}</div>
              <div style={{ fontFamily: F.mono, fontSize: 10, color: D['fg-subtle'] }}>{user.email}</div>
            </div>
          </div>
        )}
        {admin && (
          <div style={{ padding: 10, background: D['surface-2'], border: `1px solid ${D.border}`, borderRadius: 10, display:'flex', gap: 10, alignItems:'center', marginTop: 8 }}>
            <span style={{ width: 8, height: 8, borderRadius:'50%', background: D.warning }}/>
            <span style={{ fontFamily: F.mono, fontSize: 10.5, color: D['fg-muted'], letterSpacing:'0.06em' }}>1 thing wants a look</span>
          </div>
        )}
      </aside>
      <main style={{ flex: 1, display:'flex', flexDirection:'column', minWidth: 0 }}>
        <header style={{ padding:'18px 32px', borderBottom: `1px solid ${D.border}`, display:'flex', alignItems:'center', justifyContent:'space-between', flex:'0 0 auto' }}>
          <div>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.18em', color: D['fg-subtle'], textTransform:'uppercase' }}>{kicker}</div>
            <h1 style={{ fontFamily: F.display, fontWeight: 700, fontSize: 28, letterSpacing:'-0.025em', margin: '4px 0 0' }}>{title}</h1>
          </div>
          {primary}
        </header>
        <div style={{ flex: 1, overflow:'auto', padding:'24px 32px' }}>{children}</div>
      </main>
    </div>
  );
}

// ============================================================
// DT-1 — User Home (desktop)
// ============================================================
function ScreenDesktopUserHome() {
  return (
    <DesktopShell
      active="home"
      kicker="ai.vivid.fish"
      title="Hi, Ada."
      primary={<Btn variant="primary" icon={<I.plus size={14} stroke="#0A1620"/>}>New key</Btn>}
    >
      <div style={{ display:'grid', gridTemplateColumns:'1.4fr 1fr', gap: 18 }}>
        <Card padding={20}>
          <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', marginBottom: 14 }}>
            <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 17 }}>Your apps</div>
            <span style={{ fontFamily: F.mono, fontSize: 10.5, color: D['fg-subtle'], letterSpacing:'0.14em' }}>3 ACTIVE</span>
          </div>
          {[
            { app:'Claude Code', mask:'opk_•••• a9c2', last:'2m ago', calls:'142 / h', tone:'success' },
            { app:'Continue', mask:'opk_•••• 7d44', last:'14m ago', calls:'38 / h', tone:'success' },
            { app:'Daisy bot', mask:'opk_•••• 0e91', last:'yesterday', calls:'0 / h', tone:'neutral' },
          ].map((k, i) => (
            <div key={i} style={{ display:'grid', gridTemplateColumns:'36px 1fr auto auto', gap: 14, alignItems:'center', padding:'14px 0', borderTop: i?`1px solid ${D.border}`:0 }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, background: D['surface-2'], display:'flex', alignItems:'center', justifyContent:'center', color: D.accent }}>
                {k.app==='Claude Code' ? <I.bot size={17}/> : k.app==='Continue' ? <I.edit size={15}/> : <I.chat size={17}/>}
              </div>
              <div>
                <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 14 }}>{k.app}</div>
                <div style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-subtle'] }}>{k.mask}</div>
              </div>
              <div style={{ textAlign:'right' }}>
                <div style={{ fontFamily: F.mono, fontSize: 12, color: D.fg }}>{k.calls}</div>
                <div style={{ fontFamily: F.body, fontSize: 11, color: D['fg-subtle'] }}>{k.last}</div>
              </div>
              <StatusDot tone={k.tone}/>
            </div>
          ))}
        </Card>
        <div style={{ display:'flex', flexDirection:'column', gap: 16 }}>
          <Card padding={20}>
            <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
              <StatusDot tone="success"/>
              <div style={{ flex: 1 }}>
                <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14, color: D.fg }}>Gateway reachable</div>
                <div style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>Endpoint: <span style={{ fontFamily: F.mono, color: D.fg }}>https://ai.vivid.fish/v1</span></div>
              </div>
            </div>
            <Hairline color={D.border} style={{ margin:'12px 0' }}/>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 8 }}>What you can do today</div>
            {[
              ['Chat models',  '5 stable'],
              ['Embeddings',   '1 stable'],
              ['Images',       'off · ask operator'],
              ['Audio',        'off · ask operator'],
            ].map(([k,v],i) => (
              <div key={k} style={{ display:'flex', justifyContent:'space-between', padding:'7px 0', borderTop: i?`1px solid ${D.border}`:0 }}>
                <span style={{ fontFamily: F.body, fontSize: 13, color: D.fg }}>{k}</span>
                <span style={{ fontFamily: F.mono, fontSize: 12, color: v.startsWith('off')?D['fg-subtle']:D.accent }}>{v}</span>
              </div>
            ))}
          </Card>
        </div>
      </div>

      <Card padding={20} style={{ marginTop: 16 }}>
        <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', marginBottom: 14 }}>
          <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 17 }}>Recent · last hour</div>
          <button style={{ background:'transparent', border:0, color: D.accent, fontFamily: F.body, fontSize: 13, cursor:'pointer' }}>See all →</button>
        </div>
        <div style={{ display:'grid', gridTemplateColumns:'80px 1fr 1fr 80px 80px', gap: 10, fontFamily: F.mono, fontSize: 10, color: D['fg-subtle'], letterSpacing:'0.14em', textTransform:'uppercase', padding:'8px 0', borderBottom: `1px solid ${D.border}` }}>
          <span>time</span><span>app</span><span>endpoint</span><span style={{textAlign:'right'}}>status</span><span style={{textAlign:'right'}}>latency</span>
        </div>
        {[
          { t:'14:02', app:'Claude Code', ep:'chat.completions', s:200, ms:482, tone:'success' },
          { t:'13:59', app:'Claude Code', ep:'chat.completions', s:200, ms:198, tone:'success' },
          { t:'13:56', app:'Continue',    ep:'chat.completions', s:200, ms:1240, tone:'success' },
          { t:'13:48', app:'Daisy bot',   ep:'chat.completions', s:200, ms:340, tone:'success' },
          { t:'13:42', app:'Continue',    ep:'embeddings',       s:200, ms:14,  tone:'success' },
        ].map((r, i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'80px 1fr 1fr 80px 80px', gap: 10, padding:'10px 0', borderTop: `1px solid ${D.border}`, alignItems:'center' }}>
            <span style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-subtle'] }}>{r.t}</span>
            <span style={{ fontFamily: F.body, fontSize: 13 }}>{r.app}</span>
            <span style={{ fontFamily: F.mono, fontSize: 12, color: D['fg-muted'] }}>{r.ep}</span>
            <span style={{ fontFamily: F.mono, fontSize: 12, color: r.s===200?D['fg-muted']:D.warning, textAlign:'right' }}>{r.s}</span>
            <span style={{ fontFamily: F.mono, fontSize: 12, color: D['fg-subtle'], textAlign:'right' }}>{r.ms}ms</span>
          </div>
        ))}
      </Card>
    </DesktopShell>
  );
}

// ============================================================
// DT-2 — Receipt detail v2 (desktop)
// ============================================================
function ScreenDesktopReceipt() {
  return (
    <DesktopShell active="receipts" kicker="receipt · req_3f9c2a8b1e" title="200 OK · chat.completions"
      primary={<Btn variant="secondary" size="sm" icon={<I.copy size={14}/>}>Copy request ID</Btn>}
    >
      <div style={{ display:'grid', gridTemplateColumns:'minmax(0, 1.4fr) minmax(280px, 1fr)', gap: 22 }}>
        <div style={{ display:'flex', flexDirection:'column', gap: 16 }}>
          <Card padding={22}>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 12 }}>Model</div>
            <div style={{ display:'flex', alignItems:'center', gap: 14, flexWrap:'wrap' }}>
              <span style={{ fontFamily: F.mono, fontSize: 18, color: D['fg-muted'], textDecoration:'line-through', textDecorationThickness: 1 }}>gpt-4o-mini</span>
              <I.arrow size={18} stroke={D['fg-subtle']}/>
              <span style={{ fontFamily: F.mono, fontSize: 18, color: D.fg }}>llama-3.3-70b-instruct</span>
            </div>
            <div style={{ fontFamily: F.serif, fontStyle:'italic', fontSize: 15, color: D['fg-muted'], marginTop: 10 }}>You asked for OpenAI; the gateway routed to local.</div>
          </Card>

          <Card padding={22}>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 10 }}>Inputs · accepted (3)</div>
            <div style={{ display:'grid', gridTemplateColumns:'180px 1fr', gap: '8px 18px' }}>
              {[['messages','[5 turns · 1,840 tokens]'],['temperature','0.7'],['stream','true']].map(([k,v]) => (
                <React.Fragment key={k}>
                  <span style={{ fontFamily: F.body, fontSize: 13, color: D.fg }}>{k}</span>
                  <span style={{ fontFamily: F.mono, fontSize: 12.5, color: D['fg-muted'] }}>{v}</span>
                </React.Fragment>
              ))}
            </div>
          </Card>

          <Card padding={22} style={{ borderColor: 'rgba(217,184,106,0.32)' }}>
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 10 }}>
              <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D.warning, textTransform:'uppercase' }}>Inputs · ignored (1) · heads up</div>
            </div>
            <div style={{ display:'grid', gridTemplateColumns:'180px 1fr', gap: '8px 18px' }}>
              <span style={{ fontFamily: F.mono, fontSize: 13, color: D.warning }}>seed</span>
              <span style={{ fontFamily: F.body, fontSize: 13, color: D['fg-muted'], lineHeight: 1.5 }}>llama.cpp backend doesn't honor seed for this model. Your call still succeeded; results may differ between runs.</span>
            </div>
          </Card>
        </div>
        <aside style={{ display:'flex', flexDirection:'column', gap: 16 }}>
          <Card padding={18}>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 10 }}>Facts</div>
            {[
              ['App',        'Claude Code'],
              ['Key',        'opk_•••• a9c2'],
              ['Endpoint',   'POST /v1/chat/completions'],
              ['Backend',    'llama.cpp · cuda:1'],
              ['Latency',    '482 ms'],
              ['Started',    '14:02:34 UTC'],
            ].map(([k,v],i) => (
              <div key={k} style={{ display:'flex', justifyContent:'space-between', padding:'9px 0', borderTop: i?`1px solid ${D.border}`:0 }}>
                <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>{k}</span>
                <span style={{ fontFamily: F.mono, fontSize: 12, color: D.fg }}>{v}</span>
              </div>
            ))}
          </Card>
          <Card padding={18}>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 10 }}>Decision chain</div>
            {[
              { ok:true, t:'profile.protocols allows openai.chat' },
              { ok:true, t:'requested model resolved to local default' },
              { ok:true, t:'capability available · cuda:1 free' },
              { ok:true, t:'no policy concerns' },
            ].map((s,i) => (
              <div key={i} style={{ display:'flex', alignItems:'center', gap: 8, padding:'6px 0' }}>
                <I.check size={13} stroke={D.success}/>
                <span style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-muted'] }}>{s.t}</span>
              </div>
            ))}
          </Card>
        </aside>
      </div>
    </DesktopShell>
  );
}

// ============================================================
// DT-3 — Admin Home (desktop) · the operating loop
// ============================================================
function ScreenDesktopAdminHome() {
  return (
    <DesktopShell admin active="home" kicker="admin · ai.vivid.fish" title="Operating"
      primary={<div style={{ display:'flex', gap: 8 }}>
        <Btn variant="secondary" size="sm" icon={<I.search size={13}/>}>Debug</Btn>
        <Btn variant="primary" size="sm" icon={<I.plus size={13} stroke="#0A1620"/>}>Invite</Btn>
      </div>}
    >
      <div style={{ display:'grid', gridTemplateColumns:'minmax(0, 1.4fr) minmax(280px, 1fr)', gap: 22 }}>
        <div style={{ display:'flex', flexDirection:'column', gap: 16 }}>
          <Card padding={22}>
            <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', marginBottom: 16 }}>
              <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 17 }}>Needs attention</div>
              <span style={{ fontFamily: F.mono, fontSize: 10.5, color: D['fg-subtle'], letterSpacing:'0.14em' }}>3 ITEMS</span>
            </div>
            {[
              { icon: I.shield, fg: D.warning, t:'12 denials from "family-chat" tried /v1/embeddings', sub:'Yesterday · 7 distinct keys · same profile · pattern began 3d ago', cta:'Resolve' },
              { icon: I.signal, fg: D.danger,  t:'Video backend in maintenance', sub:'comfyui.local · standby · 0 jobs queued · last alive 13:54', cta:'Drain' },
              { icon: I.user,   fg: D.info,    t:'2 invites pending', sub:'esme@example.com · 5 days ago — rin@vivid.fish · 7 days ago', cta:'Resend' },
            ].map((d, i) => (
              <div key={i} style={{ padding:'16px 0', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'flex-start', gap: 14 }}>
                <d.icon size={20} stroke={d.fg} style={{ marginTop: 2, flex:'0 0 20px' }}/>
                <div style={{ flex: 1 }}>
                  <div style={{ fontFamily: F.body, fontSize: 14, color: D.fg, lineHeight: 1.4 }}>{d.t}</div>
                  <div style={{ fontFamily: F.body, fontSize: 12, color: D['fg-subtle'], marginTop: 4 }}>{d.sub}</div>
                </div>
                <Btn variant="secondary" size="sm">{d.cta}</Btn>
              </div>
            ))}
          </Card>

          <Card padding={22}>
            <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 17, marginBottom: 14 }}>People</div>
            <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr auto auto auto', gap: '10px 16px', fontFamily: F.mono, fontSize: 10, letterSpacing:'0.14em', textTransform:'uppercase', color: D['fg-subtle'], paddingBottom: 8, borderBottom: `1px solid ${D.border}` }}>
              <span>name</span><span>profile</span><span style={{textAlign:'right'}}>apps</span><span style={{textAlign:'right'}}>calls / 24h</span><span>last seen</span>
            </div>
            {[
              { name:'Ada Lovelace', profile:'agent-broad', apps: 3, calls:'4.2k', last:'2m', tone:'success' },
              { name:'Tomás Sea',    profile:'agent-broad', apps: 2, calls:'612', last:'1h',  tone:'success' },
              { name:'River Quill',  profile:'family-chat', apps: 1, calls:'18',  last:'1d',  tone:'neutral' },
              { name:'Yuki Bay',     profile:'family-chat', apps: 1, calls:'2',   last:'3d',  tone:'neutral' },
              { name:'Esme Cole',    profile:'—',           apps: 0, calls:'—',   last:'invited 5d ago', tone:'warning' },
            ].map((u, i) => (
              <div key={u.name} style={{ display:'grid', gridTemplateColumns:'1fr 1fr auto auto auto', gap: '12px 16px', padding:'12px 0', borderTop: `1px solid ${D.border}`, alignItems:'center' }}>
                <div style={{ display:'flex', alignItems:'center', gap: 10 }}>
                  <StatusDot tone={u.tone}/>
                  <span style={{ fontFamily: F.body, fontSize: 13.5, color: D.fg }}>{u.name}</span>
                </div>
                <span style={{ fontFamily: F.mono, fontSize: 12, color: u.profile==='—'?D['fg-subtle']:D.accent }}>{u.profile}</span>
                <span style={{ fontFamily: F.mono, fontSize: 12, color: D['fg-muted'], textAlign:'right' }}>{u.apps}</span>
                <span style={{ fontFamily: F.mono, fontSize: 12, color: D['fg-muted'], textAlign:'right' }}>{u.calls}</span>
                <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-subtle'] }}>{u.last}</span>
              </div>
            ))}
          </Card>
        </div>

        <aside style={{ display:'flex', flexDirection:'column', gap: 16 }}>
          <Card padding={18}>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 12 }}>Backends</div>
            {[
              { name:'llama.cpp',  role:'required', state:'available',   note:'cuda:1 · 22.1/24G' },
              { name:'comfyui',    role:'standby',  state:'maintenance', note:'0 queued · since 13:54' },
              { name:'tabby',      role:'standby',  state:'idle',        note:'expected · cold start ok' },
              { name:'parakeet',   role:'optional', state:'available',   note:'cuda:0 · 4.2/24G' },
              { name:'whisper',    role:'optional', state:'available',   note:'cuda:0' },
            ].map((b,i) => {
              const tone =
                b.state==='available'   ? 'success' :
                b.state==='maintenance' ? 'warning' :
                b.state==='idle'        ? 'neutral' :
                'danger';
              return (
                <div key={b.name} style={{ padding:'10px 0', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'flex-start', gap: 10 }}>
                  <StatusDot tone={tone}/>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display:'flex', alignItems:'center', gap: 6 }}>
                      <span style={{ fontFamily: F.mono, fontSize: 12.5, color: D.fg }}>{b.name}</span>
                      <Pill tone={b.role==='required'?'accent':'neutral'} size="sm">{b.role}</Pill>
                    </div>
                    <div style={{ fontFamily: F.body, fontSize: 11, color: D['fg-subtle'], marginTop: 2 }}>{b.state} · {b.note}</div>
                  </div>
                </div>
              );
            })}
          </Card>
          <Card padding={18}>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 12 }}>Profiles in use</div>
            {[
              { name:'agent-broad',  count: 6, denials: 0 },
              { name:'family-chat',  count: 4, denials: 12 },
              { name:'media-creator',count: 2, denials: 0 },
              { name:'admin',        count: 2, denials: 0 },
            ].map((p, i) => (
              <div key={p.name} style={{ padding:'10px 0', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
                <div>
                  <div style={{ fontFamily: F.mono, fontSize: 12.5, color: D.fg }}>{p.name}</div>
                  <div style={{ fontFamily: F.body, fontSize: 11, color: D['fg-subtle'] }}>{p.count} people</div>
                </div>
                <span style={{ fontFamily: F.mono, fontSize: 11.5, color: p.denials?D.warning:D['fg-subtle'] }}>{p.denials?`${p.denials} denials / 24h`:'—'}</span>
              </div>
            ))}
          </Card>
        </aside>
      </div>
    </DesktopShell>
  );
}

Object.assign(window, { DesktopShell, ScreenDesktopUserHome, ScreenDesktopReceipt, ScreenDesktopAdminHome });
