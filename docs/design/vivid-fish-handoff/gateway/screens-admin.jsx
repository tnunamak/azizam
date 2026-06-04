// screens-admin.jsx — the meat. Admin views where the hard UX lives.
// Profiles are admin-facing: they describe what users can do.

// ============================================================
// AD1 — Admin home (operating overview, what needs attention)
// ============================================================
function ScreenAdminHome() {
  return (
    <Phone>
      <TopBar kicker="admin · ai.vivid.fish" title="Operating"
        right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.search/></button>}
      />
      <Body>
        <Card padding={14} style={{ display:'flex', alignItems:'flex-start', gap: 12, borderColor: 'rgba(217,184,106,0.4)' }}>
          <I.warning size={18} stroke={D.warning} style={{ marginTop: 2, flex:'0 0 18px' }}/>
          <div style={{ flex: 1 }}>
            <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14, color: D.fg }}>3 things want a look</div>
            <ul style={{ margin:'6px 0 0', paddingLeft: 14, fontFamily: F.body, fontSize: 12, color: D['fg-muted'], lineHeight: 1.6 }}>
              <li>Video backend in <span style={{ color: D.warning }}>maintenance</span> since 13:54</li>
              <li>12 denials from <span style={{ fontFamily: F.mono, color: D.fg }}>family-chat</span> on /v1/embeddings</li>
              <li>2 invites pending 5+ days</li>
            </ul>
          </div>
        </Card>

        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap: 8 }}>
          {[
            { l:'People', n:'7', sub:'5 active', tone:'neutral' },
            { l:'Apps', n:'23', sub:'+3 / wk', tone:'neutral' },
            { l:'Profiles', n:'4', sub:'1 default', tone:'neutral' },
          ].map(s => (
            <Card key={s.l} padding={12}>
              <div style={{ fontFamily: F.mono, fontSize: 9.5, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase' }}>{s.l}</div>
              <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 24, color: D.fg, marginTop: 4, letterSpacing:'-0.02em' }}>{s.n}</div>
              <div style={{ fontFamily: F.body, fontSize: 10.5, color: D['fg-muted'] }}>{s.sub}</div>
            </Card>
          ))}
        </div>

        <Section kicker="needs attention" title="Patterns to address">
          <Card padding={0}>
            {[
              { icon: I.shield, fg: D.warning, t:'12 denials from "family-chat" tried /v1/embeddings', sub:'Yesterday \u00b7 7 distinct keys', cta:'Review' },
              { icon: I.signal, fg: D.danger,  t:'Video backend down since 13:54', sub:'No active impact \u2014 0 video calls today', cta:'Restart' },
              { icon: I.user,   fg: D.info,    t:'2 invites still pending', sub:'Sent 5 days ago', cta:'Resend' },
            ].map((d, i) => (
              <div key={i} style={{ padding:'14px', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'flex-start', gap: 12 }}>
                <d.icon size={18} stroke={d.fg} style={{ marginTop: 2, flex:'0 0 18px' }}/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontFamily: F.body, fontSize: 13.5, color: D.fg, lineHeight: 1.4 }}>{d.t}</div>
                  <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-subtle'], marginTop: 2 }}>{d.sub}</div>
                </div>
                <button style={{ background:'transparent', border: `1px solid ${D.border}`, borderRadius: 8, color: D.fg, padding:'5px 10px', fontFamily: F.body, fontSize: 11.5, cursor:'pointer', flex:'0 0 auto' }}>{d.cta}</button>
              </div>
            ))}
          </Card>
        </Section>

        <Section kicker="quick actions" title="Operate">
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 8 }}>
            {[
              { icon: I.user, label:'Invite someone' },
              { icon: I.shield, label:'Edit a profile' },
              { icon: I.search, label:'Debug a denial' },
              { icon: I.cog, label:'Automation keys' },
            ].map((a,i) => (
              <Card key={i} padding={14}>
                <div style={{ display:'flex', alignItems:'center', gap: 10 }}>
                  <a.icon size={16} stroke={D.accent}/>
                  <span style={{ fontFamily: F.body, fontSize: 13, color: D.fg, fontWeight: 500 }}>{a.label}</span>
                </div>
              </Card>
            ))}
          </div>
        </Section>
      </Body>
      <Tabs active="me" admin/>
    </Phone>
  );
}

// ============================================================
// AD2 — People list (admin: who has access)
// ============================================================
function ScreenAdminPeople() {
  const users = [
    { name:'Ada Lovelace', email:'ada@vivid.fish', profile:'agent-broad', apps:3, last:'2m', tone:'success' },
    { name:'Tomás Sea',    email:'tomas@vivid.fish', profile:'agent-broad', apps:2, last:'1h', tone:'success' },
    { name:'River Quill',  email:'river@vivid.fish', profile:'family-chat', apps:1, last:'yesterday', tone:'neutral' },
    { name:'Yuki Bay',     email:'yuki@vivid.fish', profile:'family-chat', apps:1, last:'3d', tone:'neutral' },
    { name:'Esme Cole',    email:'esme@example.com', profile:'(pending)', apps:0, last:'invited 5d ago', tone:'warning' },
  ];
  return (
    <Phone>
      <TopBar
        kicker="admin"
        title="People"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<Btn size="sm" icon={<I.plus size={14} stroke="#0A1620"/>}>Invite</Btn>}
      />
      <Body>
        <div style={{ display:'flex', alignItems:'center', gap: 8, padding:'10px 12px', background: D['surface-1'], border: `1px solid ${D.border}`, borderRadius: 12 }}>
          <I.search size={15} stroke={D['fg-subtle']}/>
          <span style={{ fontFamily: F.body, fontSize: 13, color: D['fg-subtle'] }}>Search people</span>
        </div>
        {users.map(u => (
          <Card key={u.email} padding={14} onClick={()=>{}}>
            <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
              <div style={{ width: 36, height: 36, borderRadius: '50%', background: D['surface-3'], display:'flex', alignItems:'center', justifyContent:'center', color: D.fg, fontFamily: F.display, fontWeight: 700, fontSize: 13 }}>
                {u.name.split(' ').map(n=>n[0]).slice(0,2).join('')}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 14.5, color: D.fg }}>{u.name}</div>
                <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>
                  <span style={{ fontFamily: F.mono, color: D.accent }}>{u.profile}</span> · {u.apps} apps · {u.last}
                </div>
              </div>
              <StatusDot tone={u.tone}/>
            </div>
          </Card>
        ))}
      </Body>
      <Tabs active="me" admin/>
    </Phone>
  );
}

// ============================================================
// AD3 — User detail (admin viewing one person)
// ============================================================
function ScreenAdminUserDetail() {
  return (
    <Phone>
      <TopBar
        kicker="admin · person"
        title="Ada Lovelace"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.more/></button>}
      />
      <Body>
        <Card padding={16}>
          <div style={{ display:'flex', alignItems:'center', gap: 12, marginBottom: 14 }}>
            <div style={{ width: 48, height: 48, borderRadius: '50%', background: D['surface-3'], display:'flex', alignItems:'center', justifyContent:'center', color: D.fg, fontFamily: F.display, fontWeight: 700, fontSize: 16 }}>AL</div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontFamily: F.body, fontSize: 13, color: D['fg-muted'] }}>ada@vivid.fish</div>
              <div style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-subtle'] }}>oidc · sub · 9f2c1a…</div>
            </div>
          </div>
          <div style={{ display:'flex', justifyContent:'space-between', padding:'10px 0', borderTop: `1px solid ${D.border}` }}>
            <span style={{ fontFamily: F.body, fontSize: 13, color: D['fg-muted'] }}>Profile</span>
            <button style={{ background:'transparent', border: 0, color: D.accent, padding: 0, fontFamily: F.mono, fontSize: 13, cursor:'pointer' }}>agent-broad</button>
          </div>
          <div style={{ display:'flex', justifyContent:'space-between', padding:'10px 0', borderTop: `1px solid ${D.border}` }}>
            <span style={{ fontFamily: F.body, fontSize: 13, color: D['fg-muted'] }}>Joined</span>
            <span style={{ fontFamily: F.body, fontSize: 13, color: D.fg }}>Apr 2 · 6 weeks ago</span>
          </div>
          <div style={{ display:'flex', justifyContent:'space-between', padding:'10px 0', borderTop: `1px solid ${D.border}` }}>
            <span style={{ fontFamily: F.body, fontSize: 13, color: D['fg-muted'] }}>Calls · last 7d</span>
            <span style={{ fontFamily: F.mono, fontSize: 13, color: D.fg }}>4,182 / 99.4% ok</span>
          </div>
        </Card>

        <Section kicker="3 apps" title="What Ada is running">
          {[
            { app:'Claude Code', mask:'opk_•••• a9c2', last:'2m' },
            { app:'Continue', mask:'opk_•••• 7d44', last:'14m' },
            { app:'Daisy bot', mask:'opk_•••• 0e91', last:'yesterday' },
          ].map((k,i) => (
            <Card key={i} padding={12}>
              <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
                <div>
                  <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 13.5 }}>{k.app}</div>
                  <div style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-subtle'] }}>{k.mask}</div>
                </div>
                <span style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>{k.last}</span>
              </div>
            </Card>
          ))}
        </Section>

        <Section kicker="careful" title="Operator actions">
          <Btn variant="secondary" fullWidth icon={<I.edit size={14}/>}>Change Ada's profile</Btn>
          <Btn variant="secondary" fullWidth icon={<I.shield size={14}/>}>Make Ada an admin</Btn>
          <Btn variant="danger" fullWidth icon={<I.trash size={14}/>}>Revoke all of Ada's keys</Btn>
        </Section>
      </Body>
    </Phone>
  );
}

// ============================================================
// AD4 — Invite flow (admin)
// ============================================================
function ScreenAdminInvite() {
  return (
    <Phone>
      <TopBar
        kicker="invite"
        title="Bring someone in"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.close/></button>}
      />
      <Body>
        <Sentence>
          They sign in with their OIDC account. You pre-decide what they can do by picking a profile now.
        </Sentence>
        <Field label="Display name" value="Esme Cole" autoFocus/>
        <Field label="Email · OIDC" value="esme@example.com" mono/>

        <Section kicker="what they get" title="Default profile">
          {[
            { name:'family-chat',   desc:'Chat with stable local models. No embeddings, no media.', active: true },
            { name:'agent-broad',   desc:'Chat + embeddings + tools. For coding agents.' },
            { name:'media-creator', desc:'Adds image/audio workflows on top of agent-broad.' },
          ].map((p,i) => (
            <Card key={i} padding={14} style={{ background: p.active ? 'rgba(123,196,200,0.08)' : D['surface-1'], borderColor: p.active ? D.accent : D.border }}>
              <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
                <span style={{ width: 18, height: 18, borderRadius:'50%', border:`1.5px solid ${p.active?D.accent:D['border-strong']}`, display:'flex',alignItems:'center', justifyContent:'center', flex:'0 0 18px' }}>
                  {p.active && <span style={{ width: 8, height: 8, borderRadius:'50%', background: D.accent }}/>}
                </span>
                <div style={{ flex: 1 }}>
                  <div style={{ fontFamily: F.mono, fontSize: 13, color: D.fg }}>{p.name}</div>
                  <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'], marginTop: 2 }}>{p.desc}</div>
                </div>
              </div>
            </Card>
          ))}
        </Section>

        <Card padding={14}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
            <div>
              <div style={{ fontFamily: F.body, fontSize: 13.5, color: D.fg }}>Make admin</div>
              <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'], marginTop: 2 }}>Can manage profiles, people, and keys.</div>
            </div>
            <label style={{ position:'relative', width: 40, height: 22, cursor:'pointer' }}>
              <span style={{ position:'absolute', inset: 0, background: D['surface-3'], borderRadius: 999, border: `1px solid ${D.border}` }}/>
              <span style={{ position:'absolute', top: 2, left: 2, width: 18, height: 18, background: D['fg-muted'], borderRadius:'50%' }}/>
            </label>
          </div>
        </Card>
      </Body>
      <div style={{ padding: '14px 18px 22px', borderTop: `1px solid ${D.border}` }}>
        <Btn variant="primary" size="lg" fullWidth>Send invite</Btn>
      </div>
    </Phone>
  );
}

// ============================================================
// AD5 — Profiles list (admin)
// ============================================================
function ScreenAdminProfiles() {
  const profiles = [
    { id:'family-chat',   name:'Family chat',   desc:'Chat with stable local models. No embeddings, no media.', count:4, builtin:false, isDefault:false },
    { id:'agent-broad',   name:'Agent · broad', desc:'Chat + embeddings + tools.',                              count:6, builtin:false, isDefault:true  },
    { id:'media-creator', name:'Media creator', desc:'Image workflows on top of agent-broad.',                  count:2, builtin:false, isDefault:false },
    { id:'admin',         name:'Admin',         desc:'Same as agent-broad plus the admin plane.',               count:2, builtin:true,  isDefault:false },
  ];
  return (
    <Phone>
      <TopBar
        kicker="admin"
        title="Profiles"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<Btn size="sm" icon={<I.plus size={14} stroke="#0A1620"/>}>New</Btn>}
      />
      <Body>
        <Sentence>
          A profile says what a person can do. Each user has one. Each user's keys inherit it.
        </Sentence>
        {profiles.map(p => (
          <Card key={p.id} padding={14} onClick={()=>{}}>
            <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', gap: 12 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display:'flex', alignItems:'center', gap: 8, flexWrap:'wrap' }}>
                  <span style={{ fontFamily: F.display, fontWeight: 600, fontSize: 15, color: D.fg }}>{p.name}</span>
                  {p.isDefault && <Pill tone="accent" size="sm">default</Pill>}
                  {p.builtin && <Pill tone="neutral" size="sm">built-in</Pill>}
                </div>
                <div style={{ fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'], marginTop: 4 }}>{p.desc}</div>
                <div style={{ display:'flex', alignItems:'center', gap: 6, marginTop: 8, fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'] }}>
                  <I.user size={11}/><span>{p.count} people</span>
                </div>
              </div>
              <I.chevron size={16} stroke={D['fg-subtle']}/>
            </div>
          </Card>
        ))}
      </Body>
      <Tabs active="me" admin/>
    </Phone>
  );
}

// ============================================================
// AD6 — Profile editor · MOBILE · the centerpiece
// ============================================================
function ScreenAdminProfileEditor() {
  // Modalities are a human-grouping of backend protocols + intents.
  // Levels match the durable model's model-tag scheme: off / default-set / all.
  const modalities = [
    { name:'Chat',       icon: I.chat,  level:'stable', protocols:'openai.chat · openai.responses · anthropic.messages', detail:'5 models match' },
    { name:'Embeddings', icon: I.embed, level:'stable', protocols:'openai.embeddings', detail:'1 model matches' },
    { name:'Images',     icon: I.image, level:'off',     protocols:'openai.images', detail:'2 workflows match' },
    { name:'Audio',      icon: I.audio, level:'off',     protocols:'openai.audio', detail:'2 models match' },
    { name:'Video',      icon: I.video, level:'off',     protocols:'gateway.video', detail:'1 workflow matches' },
  ];
  const Tri = ({ level }) => {
    const opts = [
      { id:'off',          label:'Off' },
      { id:'stable',       label:'Stable' },
      { id:'experimental', label:'+ Exp' },
    ];
    return (
      <div style={{ display:'inline-flex', padding: 3, background: D['surface-2'], border: `1px solid ${D.border}`, borderRadius: 999 }}>
        {opts.map(o => (
          <span key={o.id} style={{ padding:'5px 10px', borderRadius: 999, fontFamily: F.body, fontSize: 11, fontWeight: 500, background: o.id===level?D.accent:'transparent', color: o.id===level?'#0A1620':D['fg-muted'] }}>{o.label}</span>
        ))}
      </div>
    );
  };
  return (
    <Phone>
      <TopBar
        kicker="profile · used by 6 people"
        title="Agent · broad"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<button style={{ background:'transparent',border:0,color:D.fg,padding:6,cursor:'pointer',fontFamily:F.body,fontSize:14,fontWeight:500 }}>Save</button>}
      />
      <Body>
        <Card padding={14} style={{ background:'rgba(217,184,106,0.06)', borderColor:'rgba(217,184,106,0.32)' }}>
          <div style={{ display:'flex', alignItems:'flex-start', gap: 10 }}>
            <I.warning size={16} stroke={D.warning} style={{ marginTop:2 }}/>
            <div style={{ fontFamily: F.body, fontSize: 12.5, color: D.fg, lineHeight: 1.5 }}>
              <strong>Blast radius:</strong> changes here affect <span style={{ color: D.warning }}>6 people · 15 keys</span>. Their tools may stop working until they retry.
            </div>
          </div>
        </Card>

        <Section kicker="about" title="What it is">
          <Field label="Display name" value="Agent · broad"/>
          <Field label="Slug" value="agent-broad" mono helper="Visible in receipts and logs."/>
          <Field label="One-line summary" value="For coding agents that read code and call tools." helper="Shows up on your users' key detail screen."/>
        </Section>

        <Section kicker="what they can do" title="Modalities">
          <div style={{ display:'flex', flexDirection:'column', gap: 0 }}>
            {modalities.map((m, i) => (
              <div key={m.name} style={{ display:'flex', alignItems:'center', gap: 12, padding:'14px 4px', borderTop: i?`1px solid ${D.border}`:0 }}>
                <m.icon size={18} stroke={m.level==='off'?D['fg-subtle']:D.accent} style={{ flex:'0 0 18px' }}/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontFamily: F.body, fontSize: 13.5, fontWeight: 500, color: m.level==='off'?D['fg-muted']:D.fg }}>{m.name}</div>
                  <div style={{ fontFamily: F.mono, fontSize: 10.5, color: D['fg-subtle'], whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{m.protocols}</div>
                </div>
                <Tri level={m.level}/>
              </div>
            ))}
          </div>
          <Card padding={10} style={{ marginTop: 6, background: D['surface-1'] }}>
            <div style={{ display:'flex', alignItems:'center', gap: 8 }}>
              <I.info size={13} stroke={D['fg-subtle']}/>
              <span style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>Stable = curated, audited models. +Exp adds experimental and opt-in models.</span>
            </div>
          </Card>
        </Section>

        <Section kicker="how much" title="Limits">
          <Card padding={0}>
            {[
              { l:'Concurrent calls per key', v:'4', helper:'max_parallel in the durable model' },
              { l:'Cloud fallback',           v:'off', helper:'route to OpenAI/Anthropic when local is unavailable' },
            ].map((it, i) => (
              <div key={it.l} style={{ padding:'12px 14px', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'flex-start', justifyContent:'space-between', gap: 12 }}>
                <div>
                  <div style={{ fontFamily: F.body, fontSize: 13, color: D.fg }}>{it.l}</div>
                  {it.helper && <div style={{ fontFamily: F.body, fontSize: 11, color: D['fg-subtle'], marginTop: 2 }}>{it.helper}</div>}
                </div>
                <span style={{ fontFamily: F.mono, fontSize: 13, color: D.fg, whiteSpace:'nowrap' }}>{it.v}</span>
              </div>
            ))}
          </Card>
        </Section>

        <Section kicker="visibility" title="What users see">
          <Card padding={14}>
            <div style={{ fontFamily: F.body, fontSize: 13, color: D.fg, marginBottom: 10 }}>Status detail</div>
            <div style={{ display:'flex', flexDirection:'column', gap: 6 }}>
              {[
                { id:'none',     label:'None',     desc:'No /v1/status. The gateway is just an endpoint.' },
                { id:'basic',    label:'Basic',    desc:'Up / degraded / down per modality. No model names.', active: true },
                { id:'standard', label:'Standard', desc:'Model names, queues, latency.' },
                { id:'full',     label:'Full',     desc:'Plus paths, GPU UUIDs, process state. Admin tier.' },
              ].map(o => (
                <label key={o.id} style={{ display:'flex', alignItems:'flex-start', gap: 10, padding:'8px 4px', cursor:'pointer' }}>
                  <span style={{ width: 16, height: 16, borderRadius:'50%', border:`1.5px solid ${o.active?D.accent:D['border-strong']}`, display:'flex',alignItems:'center', justifyContent:'center', flex:'0 0 16px', marginTop: 2 }}>
                    {o.active && <span style={{ width: 7, height: 7, borderRadius:'50%', background: D.accent }}/>}
                  </span>
                  <div>
                    <div style={{ fontFamily: F.body, fontSize: 13, color: D.fg, fontWeight: o.active?500:400 }}>{o.label}</div>
                    <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-subtle'] }}>{o.desc}</div>
                  </div>
                </label>
              ))}
            </div>
          </Card>
        </Section>

        <Section kicker="opt-in" title="Native passthrough">
          <Card padding={14}>
            <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', gap: 10, marginBottom: 8 }}>
              <div>
                <div style={{ fontFamily: F.body, fontSize: 13.5, color: D.fg }}>Allow backend-native calls</div>
                <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'], marginTop: 2 }}>e.g. raw ComfyUI graph submission. Audited in every receipt.</div>
              </div>
              <label style={{ position:'relative', width: 40, height: 22, cursor:'pointer', flex:'0 0 40px' }}>
                <span style={{ position:'absolute', inset: 0, background: D['surface-3'], borderRadius: 999, border: `1px solid ${D.border}` }}/>
                <span style={{ position:'absolute', top: 2, left: 2, width: 18, height: 18, background: D['fg-muted'], borderRadius:'50%' }}/>
              </label>
            </div>
            <div style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'], padding: '8px 0 0', borderTop: `1px solid ${D.border}` }}>Scope by backend + operation when on.</div>
          </Card>
        </Section>

        <details>
          <summary style={{ cursor:'pointer', fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'], letterSpacing:'0.16em', textTransform:'uppercase', padding: '12px 0' }}>Advanced · YAML override</summary>
          <Card padding={14} style={{ marginTop: 4 }}>
            <pre style={{ margin: 0, fontFamily: F.mono, fontSize: 11, color: D['fg-muted'], lineHeight: 1.55 }}>
{`protocols: [openai.chat, openai.responses, anthropic.messages,
            openai.embeddings]
intents:   [chat, tool_call, embed]
models:    { trust_tier: stable, exposure: default }
max_parallel: 4
status_visibility: basic
cloud_fallback: false
native_passthrough: false`}
            </pre>
          </Card>
        </details>

        <Btn variant="danger" fullWidth icon={<I.trash size={14}/>}>Delete this profile</Btn>
      </Body>
    </Phone>
  );
}

// ============================================================
// AD7 — Access debugger (the "why was this denied?" tool)
// ============================================================
function ScreenAdminDebug() {
  return (
    <Phone>
      <TopBar
        kicker="admin · debug"
        title="Why was this denied?"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
      />
      <Body>
        <Card padding={16}>
          <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 8 }}>req_3f9c2a8b1e · 14:02:34</div>
          <p style={{ margin: 0, fontFamily: F.serif, fontSize: 16, lineHeight: 1.45, color: D.fg }}>
            <strong style={{ color: D.warning }}>Continue</strong> (Ada's key) tried <span style={{ fontFamily: F.mono, color: D['fg-muted'] }}>POST /v1/embeddings</span>.
          </p>
          <p style={{ margin: '8px 0 0', fontFamily: F.serif, fontStyle:'italic', fontSize: 14.5, color: D['fg-muted'], lineHeight: 1.5 }}>
            Ada's profile is <strong style={{ color: D.accent }}>family-chat</strong>, which doesn't include embeddings.
          </p>
        </Card>

        <Section kicker="the chain" title="What happened">
          <Card padding={0}>
            {[
              { ok:true,  step:'Authenticated as opk_••• 7d44 · Ada Lovelace' },
              { ok:true,  step:'Resolved profile · family-chat' },
              { ok:true,  step:'Intent · openai.embeddings' },
              { ok:false, step:'Profile does not include embeddings' },
            ].map((s, i) => (
              <div key={i} style={{ display:'flex', alignItems:'flex-start', gap: 10, padding:'12px 14px', borderTop: i?`1px solid ${D.border}`:0 }}>
                <div style={{ width: 18, height: 18, borderRadius:'50%', background: s.ok?D.success:D.danger, display:'flex',alignItems:'center',justifyContent:'center', flex:'0 0 18px' }}>
                  {s.ok ? <I.check size={11} stroke="#0A1620" strokeWidth={2.5}/> : <I.close size={11} stroke="#0A1620" strokeWidth={2.5}/>}
                </div>
                <span style={{ fontFamily: F.body, fontSize: 13, color: D.fg, lineHeight: 1.4 }}>{s.step}</span>
              </div>
            ))}
          </Card>
        </Section>

        <Section kicker="if you want this to work" title="Fix">
          <Card padding={14}>
            <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
              <I.shield size={16} stroke={D.accent}/>
              <div style={{ flex: 1 }}>
                <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 13.5, color: D.fg }}>Move Ada to "agent-broad"</div>
                <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>Affects only Ada · 3 apps</div>
              </div>
              <I.chevron size={14} stroke={D['fg-subtle']}/>
            </div>
          </Card>
          <Card padding={14}>
            <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
              <I.cog size={16} stroke={D.accent}/>
              <div style={{ flex: 1 }}>
                <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 13.5, color: D.fg }}>Add embeddings to "family-chat"</div>
                <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>Affects 4 people · 11 keys</div>
              </div>
              <I.chevron size={14} stroke={D['fg-subtle']}/>
            </div>
          </Card>
        </Section>
      </Body>
    </Phone>
  );
}

// ============================================================
// AD8 — Desktop · profile editor (the real workspace)
// ============================================================
function ScreenDesktopProfileEditor() {
  const modalities = [
    { name:'Chat',       icon: I.chat,  level:'stable', detail:'llama-3.3-70b-instruct · qwen3-coder-30b · mistral-large-2407' },
    { name:'Embeddings', icon: I.embed, level:'stable', detail:'mxbai-embed-large-v1' },
    { name:'Images',     icon: I.image, level:'off',    detail:'flux-schnell · flux-dev-controlnet' },
    { name:'Audio',      icon: I.audio, level:'off',    detail:'parakeet-1.0 · whisper-large-v3' },
    { name:'Video',      icon: I.video, level:'off',    detail:'wan-2.2-i2v-fast' },
  ];
  const Tri = ({ level }) => {
    const opts = [
      { id:'off', label:'Off' },
      { id:'stable', label:'Stable' },
      { id:'experimental', label:'+ Experimental' },
    ];
    return (
      <div style={{ display:'inline-flex', padding: 3, background: D['surface-2'], border: `1px solid ${D.border}`, borderRadius: 999 }}>
        {opts.map(o => (
          <span key={o.id} style={{ padding:'6px 14px', borderRadius: 999, fontFamily: F.body, fontSize: 12, fontWeight: 500, background: o.id===level?D.accent:'transparent', color: o.id===level?'#0A1620':D['fg-muted'], cursor:'pointer' }}>{o.label}</span>
        ))}
      </div>
    );
  };
  return (
    <div style={{ width:'100%', height:'100%', background: D.bg, color: D.fg, fontFamily: F.body, display:'flex', overflow:'hidden' }}>
      <aside style={{ width: 220, borderRight: `1px solid ${D.border}`, padding:'16px 12px', background: D['surface-1'] }}>
        <div style={{ display:'flex', alignItems:'center', gap: 8, padding:'4px 8px 18px' }}>
          <div style={{ width: 24, height: 24, borderRadius: 7, background: D.accent, display:'flex',alignItems:'center',justifyContent:'center' }}><I.globe size={12} stroke="#0A1620"/></div>
          <span style={{ fontFamily: F.display, fontWeight: 700, fontSize: 14 }}>ai<span style={{ color: D.accent }}>.</span>vivid<span style={{ color: D.accent }}>.</span>fish</span>
        </div>
        <div style={{ fontFamily: F.mono, fontSize: 9.5, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', padding:'0 8px 6px' }}>admin</div>
        {[
          { i: I.home, l:'Overview' },
          { i: I.user, l:'People' },
          { i: I.shield, l:'Profiles', active: true },
          { i: I.key, l:'All apps' },
          { i: I.signal, l:'Activity' },
          { i: I.search, l:'Debug access' },
          { i: I.cog, l:'Automation' },
        ].map((n,i)=>(
          <div key={i} style={{ display:'flex', alignItems:'center', gap: 10, padding:'8px 10px', borderRadius: 7, color: n.active?D.fg:D['fg-muted'], background: n.active?D['surface-2']:'transparent', fontSize: 13, cursor:'pointer' }}>
            <n.i size={15}/>{n.l}
          </div>
        ))}
      </aside>
      <main style={{ flex: 1, display:'flex', flexDirection:'column', minWidth: 0 }}>
        <header style={{ padding:'18px 28px', borderBottom: `1px solid ${D.border}`, display:'flex', alignItems:'center', justifyContent:'space-between', flex:'0 0 auto' }}>
          <div>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase' }}>profiles › agent-broad</div>
            <div style={{ display:'flex', alignItems:'center', gap: 12, marginTop: 4 }}>
              <h1 style={{ fontFamily: F.display, fontWeight: 700, fontSize: 28, letterSpacing:'-0.02em', margin: 0 }}>Agent · broad</h1>
              <Pill tone="accent" size="sm">used by 6</Pill>
            </div>
          </div>
          <div style={{ display:'flex', gap: 8 }}>
            <Btn variant="ghost" size="sm">Discard</Btn>
            <Btn variant="primary" size="sm">Save · 6 keys affected</Btn>
          </div>
        </header>
        <div style={{ flex: 1, overflow:'auto', padding: '20px 28px', display:'grid', gridTemplateColumns:'minmax(0, 1.5fr) minmax(280px, 1fr)', gap: 22 }}>
          <div style={{ display:'flex', flexDirection:'column', gap: 18 }}>
            <Card padding={18}>
              <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'], marginBottom: 10 }}>About</div>
              <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 12 }}>
                <Field label="Display name" value="Agent · broad"/>
                <Field label="Slug" value="agent-broad" mono/>
              </div>
              <div style={{ marginTop: 12 }}>
                <Field label="What is this profile for?" value="For coding agents that read code and call tools." helper="Shows up on your users' key detail screen."/>
              </div>
            </Card>

            <Card padding={18}>
              <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 12 }}>
                <div>
                  <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'] }}>What they can do</div>
                  <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 16, color: D.fg, marginTop: 4 }}>Modalities</div>
                </div>
                <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>2 of 5 enabled</span>
              </div>
              {modalities.map((m, i) => (
                <div key={m.name} style={{ display:'grid', gridTemplateColumns:'24px 1fr auto', gap: 14, alignItems:'center', padding:'14px 0', borderTop: i?`1px solid ${D.border}`:0 }}>
                  <m.icon size={18} stroke={m.level==='off'?D['fg-subtle']:D.accent}/>
                  <div>
                    <div style={{ fontFamily: F.body, fontWeight: 500, fontSize: 14, color: m.level==='off'?D['fg-muted']:D.fg }}>{m.name}</div>
                    <div style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-subtle'], whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{m.detail}</div>
                  </div>
                  <Tri level={m.level}/>
                </div>
              ))}
            </Card>

            <Card padding={18}>
              <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'], marginBottom: 12 }}>Limits & visibility</div>
              <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 12 }}>
                <Field label="Concurrent calls / key" value="4" mono/>
                <Field label="Daily call cap" value="unlimited" mono/>
                <Field label="Queue priority" value="normal" mono/>
                <Field label="Max context" value="131072" mono/>
              </div>
              <div style={{ display:'flex', flexDirection:'column', gap: 8, marginTop: 14 }}>
                {[
                  ['Status detail visible to users', 'basic'],
                  ['Cloud fallback when local is down', 'off'],
                  ['Native passthrough', 'off'],
                ].map(([l, v]) => (
                  <div key={l} style={{ display:'flex', justifyContent:'space-between', padding:'10px 0', borderTop: `1px solid ${D.border}` }}>
                    <span style={{ fontFamily: F.body, fontSize: 13, color: D.fg }}>{l}</span>
                    <span style={{ fontFamily: F.mono, fontSize: 12, color: D['fg-muted'] }}>{v}</span>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          <aside style={{ display:'flex', flexDirection:'column', gap: 18 }}>
            <Card padding={16} style={{ background:'rgba(217,184,106,0.06)', borderColor:'rgba(217,184,106,0.32)' }}>
              <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', textTransform:'uppercase', color: D.warning, marginBottom: 8 }}>Blast radius</div>
              <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 24, color: D.fg, letterSpacing:'-0.02em' }}>6 people · 15 keys</div>
              <p style={{ margin: '8px 0 0', fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'], lineHeight: 1.5 }}>Saving may interrupt their tools. Make changes during a quiet hour if you can.</p>
            </Card>
            <Card padding={16}>
              <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'], marginBottom: 10 }}>Currently using this profile</div>
              {['Ada Lovelace · 3 apps','Tomás Sea · 2 apps','Owen Reef · 4 apps','Rin Marsh · 1 app'].map((p,i)=>(
                <div key={i} style={{ padding:'8px 0', borderTop: i?`1px solid ${D.border}`:0, fontFamily: F.body, fontSize: 12.5, color: D.fg }}>{p}</div>
              ))}
              <button style={{ background:'transparent', border:0, color: D.accent, padding:'8px 0 0', fontFamily: F.body, fontSize: 12.5, cursor:'pointer' }}>See all 6 →</button>
            </Card>
            <Card padding={16}>
              <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'], marginBottom: 10 }}>Recent denials with this profile</div>
              {[
                ['embeddings · 12','yesterday'],
                ['images · 4','3d ago'],
                ['video · 1','5d ago'],
              ].map(([t,when],i)=>(
                <div key={i} style={{ display:'flex', justifyContent:'space-between', padding:'8px 0', borderTop: i?`1px solid ${D.border}`:0 }}>
                  <span style={{ fontFamily: F.mono, fontSize: 12, color: D.fg }}>{t}</span>
                  <span style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-subtle'] }}>{when}</span>
                </div>
              ))}
              <p style={{ margin:'10px 0 0', fontFamily: F.serif, fontStyle:'italic', fontSize: 12.5, color: D['fg-muted'], lineHeight: 1.4 }}>If users are bumping into a wall, you'll see it here first.</p>
            </Card>
          </aside>
        </div>
      </main>
    </div>
  );
}

Object.assign(window, {
  ScreenAdminHome,
  ScreenAdminPeople,
  ScreenAdminUserDetail,
  ScreenAdminInvite,
  ScreenAdminProfiles,
  ScreenAdminProfileEditor,
  ScreenAdminDebug,
  ScreenDesktopProfileEditor,
});

// Backwards-compatible aliases for main.jsx wiring
window.ScreenAdminCreateProfile = ScreenAdminProfileEditor;
window.ScreenAdminAutomation = ScreenAdminInvite; // placeholder; oak_ flow deferred
window.ScreenDesktopHome = ScreenDesktopProfileEditor;
