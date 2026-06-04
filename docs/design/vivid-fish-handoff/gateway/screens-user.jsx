// screens-user.jsx — user-facing artboards for ai.vivid.fish

// ============================================================
// 01 — Sign in (post-invite landing)
// ============================================================
function ScreenSignIn() {
  return (
    <Phone>
      <div style={{ flex: 1, display:'flex', flexDirection:'column', justifyContent:'space-between', padding: '20px 22px 32px' }}>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div style={{ display:'flex', alignItems:'baseline', gap: 0 }}>
            <span style={{ fontFamily: F.serif, fontStyle:'italic', fontWeight: 500, fontSize: 24, color: D.fg, letterSpacing:'-0.015em' }}>vivid</span>
            <span style={{ fontFamily: F.mono, color: D.accent, fontSize: 22 }}>.</span>
            <span style={{ fontFamily: F.mono, fontSize: 19, letterSpacing:'0.02em' }}>fish</span>
          </div>
          <Pill tone="accent" size="sm">invite only</Pill>
        </div>

        <div>
          <div style={{ fontFamily: F.mono, fontSize: 10.5, color: D['fg-subtle'], letterSpacing:'0.18em', textTransform:'uppercase', marginBottom: 10 }}>ai.vivid.fish</div>
          <h1 style={{ fontFamily: F.display, fontWeight: 700, fontSize: 44, lineHeight: 1.02, letterSpacing:'-0.035em', color: D.fg, margin: 0, marginBottom: 14 }}>
            Tim's gateway.<br/>
            <span style={{ color: D['fg-muted'] }}>Now yours, too.</span>
          </h1>
          <Sentence>
            One endpoint for the model collection in the closet down the hall. Sign in to get a key and pick up where your tools left off.
          </Sentence>
        </div>

        <div style={{ display:'flex', flexDirection:'column', gap: 12 }}>
          <Btn variant="primary" size="lg" fullWidth iconRight={<I.arrow size={16} stroke="#0A1620"/>}>
            Continue with vivid.fish ID
          </Btn>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'center', gap: 12, color: D['fg-subtle'], fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', textTransform:'uppercase' }}>
            <Hairline color={D.border}/>
            <span>or</span>
            <Hairline color={D.border}/>
          </div>
          <Btn variant="secondary" size="lg" fullWidth>I have an existing key</Btn>
          <div style={{ textAlign:'center', fontFamily: F.body, fontSize: 12, color: D['fg-subtle'], marginTop: 4 }}>
            Invited by <span style={{ color: D['fg-muted'] }}>Tim Nunamak</span>
          </div>
        </div>
      </div>
    </Phone>
  );
}

// ============================================================
// 02 — Home, empty (just signed in)
// ============================================================
function ScreenHomeEmpty() {
  return (
    <Phone>
      <TopBar
        kicker="ai.vivid.fish"
        title="Welcome, Ada."
        right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.user/></button>}
      />
      <Body>
        <Sentence>
          You're in. Your account is wired up but nothing's connected yet — let's make a key for the first app you'll point at us.
        </Sentence>

        <Card padding={18} style={{ marginTop: 4 }}>
          <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 8 }}>base url</div>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 8 }}>
            <span style={{ fontFamily: F.mono, fontSize: 15, color: D.fg }}>https://ai.vivid.fish/v1</span>
            <button style={{ background:'transparent',border:0,color:D.accent,padding:8,cursor:'pointer',marginRight:-8 }}><I.copy size={18}/></button>
          </div>
          <div style={{ display:'flex', alignItems:'center', gap: 6, marginTop: 10 }}>
            <StatusDot tone="success"/>
            <span style={{ fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'] }}>Reachable · last check 4s ago</span>
          </div>
        </Card>

        <Card padding={20} style={{ background: 'linear-gradient(180deg, rgba(123,196,200,0.06), transparent)', borderColor: D['border-strong'] }}>
          <div style={{ display:'flex', alignItems:'center', gap: 10, marginBottom: 10 }}>
            <div style={{ width: 36, height: 36, borderRadius: 10, background: D['surface-2'], display:'flex', alignItems:'center', justifyContent:'center', color: D.accent }}><I.key size={18}/></div>
            <div>
              <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 16, color: D.fg }}>Make your first key</div>
              <div style={{ fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'] }}>One key per app you'll connect.</div>
            </div>
          </div>
          <Btn variant="primary" fullWidth iconRight={<I.arrow size={15} stroke="#0A1620"/>}>Get started</Btn>
        </Card>

        <Section kicker="next steps" title="When you're ready">
          <Card padding={14} onClick={() => {}}>
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
              <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
                <I.capability size={20} stroke={D['fg-muted']}/>
                <div>
                  <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14.5 }}>What works today</div>
                  <div style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>Models, queues, what's down</div>
                </div>
              </div>
              <I.chevron size={16} stroke={D['fg-subtle']}/>
            </div>
          </Card>
          <Card padding={14} onClick={() => {}}>
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
              <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
                <I.signal size={20} stroke={D['fg-muted']}/>
                <div>
                  <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14.5 }}>Activity</div>
                  <div style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>Empty for now — calls show up here</div>
                </div>
              </div>
              <I.chevron size={16} stroke={D['fg-subtle']}/>
            </div>
          </Card>
        </Section>
      </Body>
      <Tabs active="home"/>
    </Phone>
  );
}

// ============================================================
// 03 — Home, active (returning user with keys + activity)
// ============================================================
function ScreenHomeActive() {
  const keys = [
    { app:'Claude Code', mask:'opk_•••• a9c2', last:'2m ago', tone:'success' },
    { app:'Continue', mask:'opk_•••• 7d44', last:'14m ago', tone:'success' },
    { app:'Daisy bot', mask:'opk_•••• 0e91', last:'yesterday', tone:'neutral' },
  ];
  return (
    <Phone>
      <TopBar
        kicker="ai.vivid.fish"
        title="Hi, Ada."
        right={<>
          <button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.search/></button>
          <button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.user/></button>
        </>}
      />
      <Body>
        <Card padding={14} style={{ display:'flex', alignItems:'center', gap: 12 }}>
          <StatusDot tone="success"/>
          <div style={{ flex: 1 }}>
            <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14.5, color: D.fg }}>Gateway reachable</div>
            <div style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>Tap for details</div>
          </div>
          <I.chevron size={16} stroke={D['fg-subtle']}/>
        </Card>

        <Section kicker="3 keys" title="Your apps" action={
          <Btn variant="ghost" size="sm" icon={<I.plus size={14}/>}>New</Btn>
        }>
          {keys.map((k, i) => (
            <Card key={i} padding={14} onClick={()=>{}}>
              <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 10 }}>
                <div style={{ display:'flex', alignItems:'center', gap: 12, minWidth: 0 }}>
                  <div style={{ width: 34, height: 34, borderRadius: 10, background: D['surface-2'], display:'flex', alignItems:'center', justifyContent:'center', color: D.accent }}>
                    {k.app==='Claude Code' ? <I.bot size={18}/> : k.app==='Continue' ? <I.edit size={16}/> : <I.chat size={18}/>}
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 14.5, color: D.fg, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{k.app}</div>
                    <div style={{ fontFamily: F.mono, fontSize: 11.5, color: D['fg-subtle'] }}>{k.mask}</div>
                  </div>
                </div>
                <div style={{ display:'flex', alignItems:'center', gap: 8 }}>
                  <StatusDot tone={k.tone}/>
                  <span style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-muted'], whiteSpace:'nowrap' }}>{k.last}</span>
                </div>
              </div>
            </Card>
          ))}
        </Section>

        <Section kicker="last hour" title="Lately">
          <Card padding={0}>
            {[
              { app:'Claude Code', endpoint:'chat.completions', status: 200, ms: 482, tone:'success' },
              { app:'Continue', endpoint:'chat.completions', status: 200, ms: 1240, tone:'success' },
              { app:'Continue', endpoint:'embeddings', status: 429, ms: 12, tone:'warning' },
              { app:'Daisy bot', endpoint:'chat.completions', status: 200, ms: 612, tone:'success' },
            ].map((r, i) => (
              <div key={i} style={{ padding:'12px 14px', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'center', gap: 10 }}>
                <StatusDot tone={r.tone}/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontFamily: F.body, fontWeight: 500, fontSize: 13.5, color: D.fg, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{r.app}</div>
                  <div style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'] }}>{r.endpoint}</div>
                </div>
                <div style={{ textAlign:'right' }}>
                  <div style={{ fontFamily: F.mono, fontSize: 12, color: r.status === 200 ? D['fg-muted'] : D.warning }}>{r.status}</div>
                  <div style={{ fontFamily: F.mono, fontSize: 10.5, color: D['fg-subtle'] }}>{r.ms}ms</div>
                </div>
              </div>
            ))}
          </Card>
        </Section>
      </Body>
      <Tabs active="home"/>
    </Phone>
  );
}

// ============================================================
// 04 — Create key, step 1: name + app type
// ============================================================
function ScreenCreate1() {
  return (
    <Phone>
      <TopBar
        kicker="new key · 1 of 2"
        title="Name this app"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.close/></button>}
      />
      <Body>
        <Sentence>
          What's going to use this key? You'll see this name in your activity log when something breaks.
        </Sentence>
        <Field label="App name" value="Claude Code" autoFocus/>
      </Body>
      <div style={{ padding: '14px 18px 22px', borderTop: `1px solid ${D.border}` }}>
        <Btn variant="primary" size="lg" fullWidth iconRight={<I.arrow size={15} stroke="#0A1620"/>}>Create the key</Btn>
      </div>
    </Phone>
  );
}

// Step 2 (formerly Step 3) — reveal once.

// Step 2 (formerly Step 3) — reveal once.

// ============================================================
// 05 — Create key, step 2: access level
// ============================================================
function ScreenCreate2() {
  // TODAY: pick a profile (admin-defined). Profiles exist; named tiers don't yet.
  const profiles = [
    { id:'default-local', name:'Default', desc:'What Tim hands out by default. Chat, embeddings, basic images.', icon: I.chat, active: true, builtin: true },
    { id:'agent-broad',   name:'Agents · broad', desc:'For coding agents. Adds tools, larger context, longer timeouts.', icon: I.bot },
    { id:'family-chat',   name:'Family chat', desc:'Just chat. Stable models only.', icon: I.user },
  ];
  return (
    <Phone>
      <TopBar
        kicker="step 2 of 3"
        title="Which profile?"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.close/></button>}
      />
      <Body>
        <Sentence>
          Profiles are policies your operator wrote. Pick the one that matches what this app needs.
        </Sentence>
        <div style={{ display:'flex', flexDirection:'column', gap: 10 }}>
          {profiles.map(t => (
            <Card key={t.id} padding={16} style={{ background: t.active ? 'rgba(123,196,200,0.08)' : D['surface-1'], borderColor: t.active ? D.accent : D.border }}>
              <div style={{ display:'flex', alignItems:'flex-start', gap: 14 }}>
                <div style={{ width: 36, height: 36, borderRadius: 10, background: D['surface-2'], display:'flex', alignItems:'center', justifyContent:'center', color: t.active ? D.accent : D['fg-muted'], flex:'0 0 36px' }}>
                  <t.icon size={18}/>
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 8 }}>
                    <div style={{ display:'flex', alignItems:'center', gap: 8 }}>
                      <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 15, color: D.fg }}>{t.name}</div>
                      {t.builtin && <Pill tone="neutral" size="sm">default</Pill>}
                    </div>
                    {t.active && <I.check size={18} stroke={D.accent}/>}
                  </div>
                  <div style={{ fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'], marginTop: 4, lineHeight: 1.45 }}>{t.desc}</div>
                </div>
              </div>
            </Card>
          ))}
        </div>
        <div style={{ display:'flex', alignItems:'center', gap: 8, padding: '10px 14px', background: D['surface-1'], border: `1px dashed ${D.border}`, borderRadius: 10, marginTop: 4 }}>
          <I.info size={16} stroke={D['fg-subtle']}/>
          <span style={{ fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'] }}>You can only pick profiles your operator has shared with you.</span>
        </div>
      </Body>
      <div style={{ padding: '14px 18px 22px', borderTop: `1px solid ${D.border}` }}>
        <Btn variant="primary" size="lg" fullWidth iconRight={<I.arrow size={15} stroke="#0A1620"/>}>Create the key</Btn>
      </div>
    </Phone>
  );
}

// ============================================================
// 06 — Create key, step 3: reveal once
// ============================================================
function ScreenCreate3() {
  return (
    <Phone>
      <TopBar
        kicker="new key · step 2 of 2"
        title="Save this key"
        right={<button style={{ background:'transparent',border:0,color:D.fg,padding:6,cursor:'pointer',fontFamily:F.body,fontSize:14,fontWeight:500 }}>Done</button>}
      />
      <Body>
        <Card padding={14} style={{ background:'rgba(217,184,106,0.06)', borderColor:'rgba(217,184,106,0.3)' }}>
          <div style={{ display:'flex', alignItems:'flex-start', gap: 10 }}>
            <I.warning size={18} stroke={D.warning} style={{ marginTop: 2 }}/>
            <div>
              <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 13.5, color: D.warning }}>You'll see this once</div>
              <div style={{ fontFamily: F.body, fontSize: 12.5, color: D['fg-muted'], marginTop: 2, lineHeight: 1.4 }}>We don't store the full key. If you lose it, rotate.</div>
            </div>
          </div>
        </Card>

        <div>
          <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 8 }}>Key for "Claude Code"</div>
          <Card padding={16} raised>
            <div style={{ fontFamily: F.mono, fontSize: 14, color: D.fg, wordBreak:'break-all', lineHeight: 1.45 }}>
              opk_live_a47bce41f3e8a2c919d4b6f0c7a9c2b1e5d83f76
            </div>
            <Hairline color={D.border} style={{ margin:'14px 0' }}/>
            <Btn variant="primary" fullWidth icon={<I.copy size={16} stroke="#0A1620"/>}>Copy the key</Btn>
          </Card>
        </div>

        <Section kicker="where to put it" title="Quick paste">
          <Card padding={14}>
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom: 8 }}>
              <span style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>For OpenAI-compatible SDKs</span>
              <button style={{ background:'transparent', border:0, color: D.accent, cursor:'pointer', padding: 4 }}><I.copy size={14}/></button>
            </div>
            <pre style={{ margin: 0, fontFamily: F.mono, fontSize: 11.5, color: D.fg, lineHeight: 1.55, whiteSpace:'pre-wrap', wordBreak:'break-all' }}>
{`OPENAI_API_KEY=opk_live_a47b...3f76
OPENAI_BASE_URL=https://ai.vivid.fish/v1`}
            </pre>
          </Card>
        </Section>

        <label style={{ display:'flex', alignItems:'center', gap: 10, padding:'10px 0', marginTop: 4 }}>
          <span style={{ width: 22, height: 22, borderRadius: 6, border: `1.5px solid ${D.accent}`, background: D.accent, display:'inline-flex', alignItems:'center', justifyContent:'center' }}>
            <I.check size={14} stroke="#0A1620" strokeWidth={2.5}/>
          </span>
          <span style={{ fontFamily: F.body, fontSize: 13.5, color: D.fg }}>I've copied it somewhere safe</span>
        </label>
      </Body>
      <div style={{ padding: '14px 18px 22px', borderTop: `1px solid ${D.border}` }}>
        <Btn variant="primary" size="lg" fullWidth>Finish</Btn>
      </div>
    </Phone>
  );
}

// ============================================================
// 07 — Key detail (app page)
// ============================================================
function ScreenKeyDetail() {
  return (
    <Phone>
      <TopBar
        title="Claude Code"
        kicker="opk_•••• a9c2"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
        right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.more/></button>}
      />
      <Body>
        <Card padding={16}>
          <div style={{ fontFamily: F.mono, fontSize: 10, letterSpacing:'0.16em', color: D['fg-subtle'], textTransform:'uppercase', marginBottom: 12 }}>What this key can do</div>
          <p style={{ margin: 0, fontFamily: F.serif, fontSize: 17, lineHeight: 1.45, color: D.fg, letterSpacing:'-0.005em' }}>
            Call <strong style={{ color: D.accent }}>chat models</strong> on this gateway. Stream responses and use tools. <span style={{ color: D['fg-muted'] }}>No images, no embeddings, no cloud fallback.</span>
          </p>
          <div style={{ display:'flex', gap: 6, marginTop: 14, flexWrap:'wrap' }}>
            <Pill tone="accent" icon={<I.chat size={11}/>}>chat</Pill>
            <Pill tone="neutral">tools</Pill>
            <Pill tone="neutral">streaming</Pill>
            <Pill tone="neutral">vision</Pill>
          </div>
          <button style={{ background:'transparent', border:0, color: D.accent, fontFamily: F.body, fontSize: 13, fontWeight: 500, padding:0, marginTop: 14, cursor:'pointer', display:'inline-flex', alignItems:'center', gap: 4 }}>
            Request more access <I.arrow size={13}/>
          </button>
        </Card>

        <Section kicker="1 key" title="Key">
          <Card padding={14} raised>
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap: 10 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontFamily: F.mono, fontSize: 13.5, color: D.fg }}>opk_•••• a9c2</div>
                <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-subtle'] }}>active · last used 2m ago</div>
              </div>
              <button style={{ background:'transparent',border:`1px solid ${D.border}`,borderRadius:8,color:D.danger,padding:'6px 10px',cursor:'pointer',fontFamily:F.body,fontSize:12 }}>Revoke</button>
            </div>
          </Card>
        </Section>

        <Section kicker="last hour · 142 calls" title="Activity">
          <Card padding={0}>
            {[
              { ep:'chat.completions', s:200, ms:482 },
              { ep:'chat.completions', s:200, ms:1180 },
              { ep:'chat.completions', s:200, ms:298 },
              { ep:'embeddings', s:403, ms:8, denied:true },
            ].map((r, i) => (
              <div key={i} style={{ padding:'10px 14px', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'center', gap: 10 }}>
                <StatusDot tone={r.s===200?'success':r.denied?'danger':'warning'}/>
                <span style={{ flex: 1, fontFamily: F.mono, fontSize: 12, color: D.fg }}>{r.ep}</span>
                <span style={{ fontFamily: F.mono, fontSize: 11.5, color: r.s===200?D['fg-muted']:D.danger }}>{r.s}</span>
                <span style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'], minWidth: 44, textAlign:'right' }}>{r.ms}ms</span>
              </div>
            ))}
          </Card>
        </Section>

        <Btn variant="danger" fullWidth icon={<I.trash size={15}/>}>Revoke this app</Btn>
      </Body>
    </Phone>
  );
}

// ============================================================
// 08 — Capabilities ("What works today")
// ============================================================
function ScreenCapabilities() {
  const groups = [
    { name:'Chat models', icon: I.chat, items: [
      { name:'llama-3.3-70b-instruct', tag:'default', status:'success', detail:'~38 t/s' },
      { name:'qwen3-coder-30b', tag:null, status:'success', detail:'~52 t/s' },
      { name:'mistral-large-2407', tag:null, status:'warning', detail:'queued · 2 ahead' },
    ]},
    { name:'Embeddings', icon: I.embed, items: [
      { name:'mxbai-embed-large-v1', tag:'default', status:'success', detail:'2.4 ms p50' },
    ]},
    { name:'Images', icon: I.image, items: [
      { name:'flux-schnell', tag:null, status:'success', detail:'queue empty' },
      { name:'flux-dev-controlnet', tag:'workflow', status:'warning', detail:'1 in queue' },
    ]},
    { name:'Audio · TTS', icon: I.audio, items: [
      { name:'parakeet-1.0', tag:null, status:'success', detail:'~120 ms p50' },
    ]},
    { name:'Video', icon: I.video, items: [
      { name:'wan-2.2-i2v-fast', tag:'beta', status:'danger', detail:'unavailable · GPU restart' },
    ]},
  ];
  return (
    <Phone>
      <TopBar
        kicker="ai.vivid.fish"
        title="What works"
        right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.refresh/></button>}
      />
      <Body>
        <Card padding={14}>
          <div style={{ display:'flex', alignItems:'center', gap: 12 }}>
            <StatusDot tone="warning" pulse/>
            <div style={{ flex: 1 }}>
              <div style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14, color: D.fg }}>Mostly healthy</div>
              <div style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>Video is down for a restart · ETA ~5 min</div>
            </div>
            <span style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'] }}>Updated 4s</span>
          </div>
        </Card>

        {groups.map(g => (
          <div key={g.name} style={{ display:'flex', flexDirection:'column', gap: 8 }}>
            <div style={{ display:'flex', alignItems:'center', gap: 8, padding:'4px 0' }}>
              <g.icon size={15} stroke={D['fg-muted']}/>
              <span style={{ fontFamily: F.body, fontWeight: 500, fontSize: 13.5, color: D.fg, letterSpacing:'-0.005em' }}>{g.name}</span>
              <span style={{ fontFamily: F.mono, fontSize: 10, color: D['fg-subtle'], letterSpacing:'0.16em', textTransform:'uppercase' }}>· {g.items.length}</span>
            </div>
            <Card padding={0}>
              {g.items.map((it, i) => (
                <div key={i} style={{ padding:'12px 14px', borderTop: i?`1px solid ${D.border}`:0, display:'flex', alignItems:'center', gap: 10 }}>
                  <StatusDot tone={it.status}/>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontFamily: F.mono, fontSize: 12.5, color: D.fg, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{it.name}</div>
                    <div style={{ fontFamily: F.body, fontSize: 11.5, color: D['fg-subtle'] }}>{it.detail}</div>
                  </div>
                  {it.tag && <Pill tone={it.tag==='beta'?'warning':'neutral'} size="sm">{it.tag}</Pill>}
                </div>
              ))}
            </Card>
          </div>
        ))}
      </Body>
      <Tabs active="caps"/>
    </Phone>
  );
}

// ============================================================
// 09 — Activity feed
// ============================================================
function ScreenActivity() {
  const events = [
    { time:'just now', app:'Continue', ep:'embeddings', s:429, ms:12, tone:'warning' },
    { time:'2m', app:'Claude Code', ep:'chat.completions', s:200, ms:482, tone:'success' },
    { time:'2m', app:'Claude Code', ep:'chat.completions', s:200, ms:198, tone:'success' },
    { time:'4m', app:'Claude Code', ep:'chat.completions', s:200, ms:912, tone:'success' },
    { time:'12m', app:'Daisy bot', ep:'chat.completions', s:200, ms:340, tone:'success' },
    { time:'18m', app:'Continue', ep:'chat.completions', s:200, ms:1240, tone:'success' },
    { time:'34m', app:'Continue', ep:'embeddings', s:200, ms:14, tone:'success' },
    { time:'1h', app:'Daisy bot', ep:'chat.completions', s:503, ms:8, tone:'danger' },
  ];
  return (
    <Phone>
      <TopBar kicker="last 24 hours" title="Activity" right={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,cursor:'pointer' }}><I.search/></button>}/>
      <Body pad={0}>
        <div style={{ padding:'14px 18px 4px', display:'flex', gap: 6, overflow:'auto', borderBottom: `1px solid ${D.border}` }}>
          {[{ name:'All', active: true }, { name:'Claude Code' }, { name:'Continue' }, { name:'Daisy bot' }, { name:'+ filter' }].map((c,i) => (
            <span key={i} style={{ padding:'6px 12px', borderRadius: 999, background: c.active ? D.accent : 'transparent', border: `1px solid ${c.active ? D.accent : D.border}`, color: c.active ? '#0A1620' : D['fg-muted'], fontFamily: F.body, fontSize: 12.5, fontWeight: 500, whiteSpace:'nowrap', flex:'0 0 auto' }}>{c.name}</span>
          ))}
        </div>
        <div style={{ padding: 18, display:'flex', flexDirection:'column', gap: 0 }}>
          {events.map((e, i) => (
            <div key={i} onClick={()=>{}} style={{ display:'flex', alignItems:'center', gap: 12, padding:'14px 0', borderBottom: `1px solid ${D.border}`, cursor:'pointer' }}>
              <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap: 4, minWidth: 44, paddingTop: 2 }}>
                <StatusDot tone={e.tone}/>
                <span style={{ fontFamily: F.mono, fontSize: 10, color: D['fg-subtle'] }}>{e.time}</span>
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontFamily: F.body, fontWeight: 500, fontSize: 14, color: D.fg }}>{e.app}</div>
                <div style={{ fontFamily: F.mono, fontSize: 12, color: D['fg-subtle'] }}>{e.ep}</div>
              </div>
              <div style={{ textAlign:'right', display:'flex', flexDirection:'column', gap: 2, alignItems:'flex-end' }}>
                <span style={{ fontFamily: F.mono, fontSize: 12.5, color: e.s===200?D['fg-muted']:e.tone==='danger'?D.danger:D.warning, fontWeight: 500 }}>{e.s}</span>
                <span style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'] }}>{e.ms}ms</span>
              </div>
            </div>
          ))}
        </div>
      </Body>
      <Tabs active="activity"/>
    </Phone>
  );
}

// ============================================================
// 10 — Receipt detail (the "why did this fail" screen)
// ============================================================
function ScreenReceipt() {
  // Uses a profile_denied receipt — a real failure mode the gateway already emits today.
  return (
    <Phone>
      <TopBar
        kicker="receipt · 14:02:34"
        title="403 Not allowed"
        left={<button style={{ background:'transparent',border:0,color:D['fg-muted'],padding:6,marginLeft:-6,cursor:'pointer' }}><I.back/></button>}
      />
      <Body>
        <Card padding={18} style={{ background:'rgba(216,131,112,0.06)', borderColor:'rgba(216,131,112,0.3)' }}>
          <div style={{ display:'flex', alignItems:'center', gap: 12, marginBottom: 12 }}>
            <div style={{ width: 36, height: 36, borderRadius:'50%', background:'rgba(216,131,112,0.15)', display:'flex', alignItems:'center', justifyContent:'center', color: D.danger }}>
              <I.shield size={18}/>
            </div>
            <div>
              <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 15, color: D.danger }}>This key can't do that</div>
              <div style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'] }}>profile denied</div>
            </div>
          </div>
          <p style={{ margin: 0, fontFamily: F.serif, fontSize: 15, lineHeight: 1.5, color: D.fg }}>
            Continue tried to call <span style={{ fontFamily: F.mono, color: D.fg }}>/v1/embeddings</span>, but its key uses the <strong style={{ color: D.accent }}>Family chat</strong> profile — which only allows chat.
          </p>
        </Card>

        <Section kicker="what to try" title="Fix it">
          {[
            { label:'Make a new key on a profile with embeddings', primary: true },
            { label:'Ask your operator to expand this profile' },
          ].map((s, i) => (
            <Card key={i} padding={14} onClick={()=>{}}>
              <div style={{ display:'flex', alignItems:'center', gap: 10 }}>
                <I.arrow size={16} stroke={s.primary?D.accent:D['fg-subtle']}/>
                <span style={{ fontFamily: F.body, fontSize: 14, color: D.fg, flex: 1 }}>{s.label}</span>
                <I.chevron size={14} stroke={D['fg-subtle']}/>
              </div>
            </Card>
          ))}
        </Section>

        <Section kicker="details" title="The request">
          <Card padding={0}>
            {[
              ['App','Continue'],
              ['Key','opk_•••• 7d44'],
              ['Profile','family-chat'],
              ['Endpoint','POST /v1/embeddings'],
              ['Decision','denied · protocol not allowed'],
              ['Latency','8 ms'],
              ['Request ID','req_3f9c2a8b1e'],
            ].map(([k,v],i) => (
              <div key={i} style={{ display:'flex', justifyContent:'space-between', padding:'10px 14px', borderTop: i?`1px solid ${D.border}`:0 }}>
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

Object.assign(window, { ScreenSignIn, ScreenHomeEmpty, ScreenHomeActive, ScreenCreate1, ScreenCreate2, ScreenCreate3, ScreenKeyDetail, ScreenCapabilities, ScreenActivity, ScreenReceipt });
