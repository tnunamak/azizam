// screens-admin-v2.jsx — admin views derived from the actual durable model.
// Two new things the prior design missed:
//   1. Service Apps — apps with no human owner (LibreChat, bots, shared tools)
//   2. Profile templates — the "Chat only / Media / Full runtime" presets
//      that previously got hidden inside an opaque key-creation form.
//   3. Policy simulator (rebuilt) — "why was this denied?" via request_id
//      OR a guided form, not a raw fact dump.

// ============================================================
// AD-V2 · 1 — Service Apps list
// ============================================================
function ScreenAdminServiceApps() {
  const apps = [
    { name:'LibreChat', profile:'agent-broad',  keys:1, last:'4m',  tone:'success' },
    { name:'Daisy bot · Telegram', profile:'family-chat', keys:1, last:'3h', tone:'success' },
    { name:'open-webui (shared)',  profile:'agent-broad', keys:2, last:'2d', tone:'neutral' },
    { name:'key-rotation script',  profile:'admin-tools', keys:1, last:'never', tone:'neutral', oak:true },
  ];
  return (
    <Phone>
      <TopBar
        kicker="admin"
        title="Service apps"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<Btn size="sm" icon={<I.plus size={14} stroke="#0A1620"/>}>New</Btn>}
      />
      <Body>
        <Sentence>
          Apps the gateway owns directly — shared tools, bots, scripts. People apps are managed by their people.
        </Sentence>
        {apps.map(a => (
          <Card key={a.name} padding={14} onClick={()=>{}}>
            <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, background: D['surface-2'], display:'flex',alignItems:'center',justifyContent:'center', color: a.oak?D.warning:D.accent, flex:'0 0 36px' }}>
                {a.oak ? <I.cog size={17}/> : <I.bot size={17}/>}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display:'flex', alignItems:'center', gap: 6 }}>
                  <span style={{ fontFamily: F.display, fontWeight: 600, fontSize: 14.5, color: D.fg }}>{a.name}</span>
                  {a.oak && <Pill tone="warning" size="sm">oak_</Pill>}
                </div>
                <div style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-muted'] }}>
                  {a.profile} · {a.keys} key{a.keys===1?'':'s'} · {a.last}
                </div>
              </div>
              <StatusDot tone={a.tone}/>
            </div>
          </Card>
        ))}
        <Card padding={12} style={{ background: D['surface-1'], border: `1px dashed ${D.border}` }}>
          <div style={{ display:'flex', alignItems:'center', gap: 10 }}>
            <I.info size={14} stroke={D['fg-subtle']}/>
            <span style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>People's apps live in their account portal. You see counts on the People list.</span>
          </div>
        </Card>
      </Body>
      <Tabs active="me" admin/>
    </Phone>
  );
}

// ============================================================
// AD-V2 · 2 — Create Service App (admin)
// ============================================================
function ScreenAdminCreateServiceApp() {
  return (
    <Phone>
      <TopBar
        kicker="new service app"
        title="Add a shared tool"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.close/></button>}
        right={<button style={{ background:'transparent',border:0,color:D.fg,padding:6,cursor:'pointer',fontFamily:F.body,fontSize:14,fontWeight:500 }}>Save</button>}
      />
      <Body>
        <Sentence>
          For things without a person — a chat UI shared by your household, a bot, a script.
        </Sentence>
        <Field label="App name" value="LibreChat" autoFocus helper="What this is to you. Appears in receipts."/>

        <Section kicker="what it can do" title="Profile">
          <Card padding={14} onClick={()=>{}}>
            <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
              <I.shield size={16} stroke={D.accent} style={{ flex:'0 0 16px' }}/>
              <div style={{ flex: 1 }}>
                <div style={{ fontFamily: F.mono, fontSize: 13, color: D.fg }}>agent-broad</div>
                <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>Chat + embeddings + tools</div>
              </div>
              <I.chevron size={14} stroke={D['fg-subtle']}/>
            </div>
          </Card>
          <button style={{ background:'transparent', border:0, color: D.accent, fontFamily: F.body, fontSize: 13, fontWeight: 500, padding: '4px 0 0', textAlign:'left', cursor:'pointer' }}>+ Make a new profile for this app</button>
        </Section>

        <Section kicker="optional" title="Owner">
          <Card padding={14}>
            <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', gap: 10 }}>
              <div>
                <div style={{ fontFamily: F.body, fontSize: 13.5, color: D.fg }}>No owner</div>
                <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'], marginTop: 2 }}>You manage the keys directly.</div>
              </div>
              <label style={{ position:'relative', width: 40, height: 22, cursor:'pointer', flex:'0 0 40px' }}>
                <span style={{ position:'absolute', inset: 0, background: D.accent, borderRadius: 999 }}/>
                <span style={{ position:'absolute', top: 2, left: 20, width: 18, height: 18, background:'#fff', borderRadius:'50%' }}/>
              </label>
            </div>
          </Card>
        </Section>

        <div style={{ padding:'10px 14px', background: D['surface-1'], border: `1px dashed ${D.border}`, borderRadius: 10, display:'flex', alignItems:'center', gap: 8 }}>
          <I.info size={14} stroke={D['fg-subtle']}/>
          <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>You'll get the <span style={{ fontFamily: F.mono, color: D.fg }}>opk_</span> key on the next screen — shown once.</span>
        </div>
      </Body>
      <div style={{ padding: '14px 18px 22px', borderTop: `1px solid ${D.border}` }}>
        <Btn variant="primary" size="lg" fullWidth iconRight={<I.arrow size={15} stroke="#0A1620"/>}>Create app + key</Btn>
      </div>
    </Phone>
  );
}

// ============================================================
// AD-V2 · 3 — Create profile from template
// ============================================================
function ScreenAdminProfileFromTemplate() {
  const templates = [
    { id:'chat',  name:'Chat only',          desc:'Talk to stable local language models. Streaming, tools, vision when supported.', active:true, recipe:'protocols: openai.chat, openai.responses, anthropic.messages · trust: stable' },
    { id:'embed', name:'Chat + embeddings',  desc:'Add retrieval and search. For RAG, code search, anything indexed.', recipe:'+ openai.embeddings' },
    { id:'media', name:'Media workflows',    desc:'Image, audio, video workflows. Counts against GPU queues.', recipe:'+ openai.images, openai.audio, gateway.video' },
    { id:'full',  name:'Full runtime',       desc:'Everything above plus +experimental and opt-in models.', recipe:'+ trust: experimental, exposure: opt_in' },
    { id:'blank', name:'Blank · advanced',   desc:'Start from nothing. For unusual policies.', recipe:'(opens editor empty)' },
  ];
  return (
    <Phone>
      <TopBar
        kicker="new profile"
        title="Start from a template"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.close/></button>}
      />
      <Body>
        <Sentence>
          Templates are starting points — you can edit anything after. They name the most common policies so you don't reinvent them.
        </Sentence>
        {templates.map(t => (
          <Card key={t.id} padding={14} style={{ background: t.active?'rgba(123,196,200,0.08)':D['surface-1'], borderColor: t.active?D.accent:D.border }}>
            <div style={{ display:'flex', alignItems:'flex-start', gap: 12 }}>
              <span style={{ width: 18, height: 18, borderRadius:'50%', border:`1.5px solid ${t.active?D.accent:D['border-strong']}`, display:'flex',alignItems:'center', justifyContent:'center', flex:'0 0 18px', marginTop: 2 }}>
                {t.active && <span style={{ width: 8, height: 8, borderRadius:'50%', background: D.accent }}/>}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 15, color: D.fg }}>{t.name}</div>
                <div style={{ fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'], marginTop: 4, lineHeight: 1.45 }}>{t.desc}</div>
                <div style={{ fontFamily: F.mono, fontSize: 10.5, color: D['fg-subtle'], marginTop: 8, letterSpacing:'0.02em' }}>{t.recipe}</div>
              </div>
            </div>
          </Card>
        ))}
      </Body>
      <div style={{ padding: '14px 18px 22px', borderTop: `1px solid ${D.border}` }}>
        <Btn variant="primary" size="lg" fullWidth iconRight={<I.arrow size={15} stroke="#0A1620"/>}>Continue · name your profile</Btn>
      </div>
    </Phone>
  );
}

// ============================================================
// AD-V2 · 4 — Policy simulator · rebuilt
// ============================================================
function ScreenAdminSimulator() {
  const Mode = ({ id, label, active }) => (
    <span style={{ padding:'8px 14px', borderRadius: 999, fontFamily: F.body, fontSize: 13, fontWeight: 500, background: active?D.accent:'transparent', color: active?'#0A1620':D['fg-muted'], border: `1px solid ${active?D.accent:D.border}`, cursor:'pointer' }}>{label}</span>
  );
  return (
    <Phone>
      <TopBar
        kicker="admin · simulator"
        title="Try a request"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
      />
      <Body>
        <Sentence>
          Ask the gateway whether a request would go through — without making it. Useful before changing a profile, useful after a denial.
        </Sentence>

        <div style={{ display:'flex', gap: 6 }}>
          <Mode id="replay" label="Replay a request"/>
          <Mode id="guided" label="Build one" active/>
        </div>

        <Section kicker="who" title="Identity">
          <Card padding={14}>
            <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
              <div style={{ width: 30, height: 30, borderRadius:'50%', background: D['surface-3'], display:'flex',alignItems:'center',justifyContent:'center', color: D.fg, fontFamily: F.display, fontWeight: 700, fontSize: 12 }}>AL</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontFamily: F.body, fontSize: 13.5, color: D.fg }}>Ada Lovelace</div>
                <div style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'] }}>profile · agent-broad</div>
              </div>
              <I.chevron size={14} stroke={D['fg-subtle']}/>
            </div>
          </Card>
        </Section>

        <Section kicker="what" title="The call">
          <Card padding={14}>
            <div style={{ display:'flex', flexDirection:'column', gap: 14 }}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
                <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>Endpoint</span>
                <span style={{ fontFamily: F.mono, fontSize: 12.5, color: D.fg }}>POST /v1/embeddings</span>
              </div>
              <Hairline/>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
                <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>Model</span>
                <span style={{ fontFamily: F.mono, fontSize: 12.5, color: D.fg }}>mxbai-embed-large-v1</span>
              </div>
            </div>
          </Card>
        </Section>

        <Card padding={16} style={{ background:'rgba(111,184,154,0.06)', borderColor:'rgba(111,184,154,0.32)' }}>
          <div style={{ display:'flex', alignItems:'center', gap: 12, marginBottom: 10 }}>
            <I.check size={18} stroke={D.success}/>
            <div>
              <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 14, color: D.success }}>Allowed</div>
              <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'] }}>Would route to mxbai-embed-large-v1 on cuda:0</div>
            </div>
          </div>
          <div style={{ paddingTop: 10, borderTop: `1px solid ${D.border}` }}>
            <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 8 }}>Decision chain</div>
            {[
              { ok:true, t:'profile.protocols allows openai.embeddings' },
              { ok:true, t:'model trust_tier = stable ∈ profile.trust' },
              { ok:true, t:'capability available · queue 0' },
            ].map((s,i) => (
              <div key={i} style={{ display:'flex', alignItems:'center', gap: 8, padding:'6px 0' }}>
                <I.check size={13} stroke={D.success}/>
                <span style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-muted'] }}>{s.t}</span>
              </div>
            ))}
          </div>
        </Card>

        <Btn variant="ghost" size="sm" icon={<I.copy size={14}/>} fullWidth>Copy as curl</Btn>
      </Body>
    </Phone>
  );
}

// ============================================================
// AD-V2 · 5 — Receipt detail · rebuilt
// Surfaces:  requested vs resolved model, accepted/rejected/ignored inputs
// ============================================================
function ScreenReceiptDetailV2() {
  return (
    <Phone>
      <TopBar
        kicker="receipt · 14:02:34"
        title="200 OK"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
      />
      <Body>
        {/* The most useful single fact: requested → resolved */}
        <Card padding={18}>
          <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 8 }}>Model</div>
          <div style={{ display:'flex', alignItems:'center', gap: 12, flexWrap:'wrap' }}>
            <span style={{ fontFamily: F.mono, fontSize: 14, color: D['fg-muted'], textDecoration:'line-through', textDecorationThickness: 1 }}>gpt-4o-mini</span>
            <I.arrow size={14} stroke={D['fg-subtle']}/>
            <span style={{ fontFamily: F.mono, fontSize: 14, color: D.fg }}>llama-3.3-70b-instruct</span>
          </div>
          <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'], marginTop: 8, fontStyle:'italic' }}>You asked for OpenAI; the gateway routed to local.</div>
        </Card>

        <Section kicker="3 of 3" title="Inputs · accepted">
          <Card padding={0}>
            {[
              ['messages', '[5 turns]'],
              ['temperature', '0.7'],
              ['stream', 'true'],
            ].map(([k,v],i) => (
              <div key={k} style={{ display:'flex', justifyContent:'space-between', padding:'10px 14px', borderTop: i?`1px solid ${D.border}`:0 }}>
                <span style={{ fontFamily: F.body, fontSize: 12.5, color: D.fg }}>{k}</span>
                <span style={{ fontFamily: F.mono, fontSize: 12, color: D['fg-muted'], textAlign:'right' }}>{v}</span>
              </div>
            ))}
          </Card>
        </Section>

        <Section kicker="1 silently dropped — heads up" title="Inputs · ignored">
          <Card padding={0} style={{ borderColor: 'rgba(217,184,106,0.32)' }}>
            <div style={{ padding:'12px 14px' }}>
              <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom: 4 }}>
                <span style={{ fontFamily: F.mono, fontSize: 12.5, color: D.warning }}>seed</span>
                <Pill tone="warning" size="sm">ignored</Pill>
              </div>
              <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'], lineHeight: 1.45 }}>llama.cpp backend doesn't honor seed for this model. Your call still succeeded; results may differ between runs.</div>
            </div>
          </Card>
        </Section>

        <Section kicker="facts" title="The request">
          <Card padding={0}>
            {[
              ['App',        'Claude Code'],
              ['Key',        'opk_•••• a9c2'],
              ['Endpoint',   'POST /v1/chat/completions'],
              ['Backend',    'llama.cpp · cuda:1'],
              ['Latency',    '482 ms'],
              ['Request ID', 'req_3f9c2a8b1e'],
            ].map(([k,v],i) => (
              <div key={k} style={{ display:'flex', justifyContent:'space-between', padding:'10px 14px', borderTop: i?`1px solid ${D.border}`:0 }}>
                <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>{k}</span>
                <span style={{ fontFamily: F.mono, fontSize: 12, color: D.fg, textAlign:'right' }}>{v}</span>
              </div>
            ))}
          </Card>
        </Section>

        <Btn variant="ghost" size="sm" icon={<I.copy size={14}/>} fullWidth>Copy request ID</Btn>
      </Body>
    </Phone>
  );
}

Object.assign(window, {
  ScreenAdminServiceApps,
  ScreenAdminCreateServiceApp,
  ScreenAdminProfileFromTemplate,
  ScreenAdminSimulator,
  ScreenReceiptDetailV2,
});
