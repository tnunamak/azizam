// brief.jsx — design brief, the artifact handed to the implementing dev agent

function Brief() {
  const Pad = ({ children }) => (
    <div style={{ width:'100%', height:'100%', background: D.bg, color: D.fg, fontFamily: F.body, padding: '40px 56px', overflow:'auto' }}>{children}</div>
  );
  const SectionH = ({ kicker, title, sub }) => (
    <div style={{ marginTop: 36, marginBottom: 14, borderTop: `1px solid ${D.border}`, paddingTop: 28 }}>
      <div style={{ fontFamily: F.mono, fontSize: 11, letterSpacing:'0.22em', textTransform:'uppercase', color: D.accent }}>{kicker}</div>
      <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 32, letterSpacing:'-0.025em', color: D.fg, marginTop: 6 }}>{title}</div>
      {sub && <div style={{ fontFamily: F.serif, fontStyle:'italic', fontSize: 16, color: D['fg-muted'], marginTop: 4 }}>{sub}</div>}
    </div>
  );
  const P = ({ children }) => <p style={{ margin: '0 0 12px', fontFamily: F.body, fontSize: 14.5, lineHeight: 1.55, color: D.fg, letterSpacing:'-0.005em' }}>{children}</p>;
  const M = ({ children }) => <span style={{ color: D['fg-muted'] }}>{children}</span>;
  const Em = ({ children }) => <em style={{ fontFamily: F.serif, fontStyle:'italic', color: D.fg, fontWeight: 500 }}>{children}</em>;

  return (
    <Pad>
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start' }}>
        <div>
          <div style={{ fontFamily: F.mono, fontSize: 11, letterSpacing:'0.22em', textTransform:'uppercase', color: D['fg-subtle'] }}>ai.vivid.fish · ux brief · v0.1 · {new Date().toISOString().slice(0,10)}</div>
          <h1 style={{ fontFamily: F.display, fontWeight: 800, fontSize: 64, letterSpacing:'-0.035em', lineHeight: 0.98, margin: '10px 0 12px', color: D.fg, maxWidth: 880 }}>Profiles are admin tools. Build the admin tools well.</h1>
          <p style={{ margin: 0, fontFamily: F.serif, fontStyle:'italic', fontSize: 19, color: D['fg-muted'], maxWidth: 760, lineHeight: 1.45 }}>Users do four things — sign in, make a key, copy it, learn why a call failed. Admins do the actual work. The shape of this product is one thin app for everyone, and one heavy app for one or two operators.</p>
        </div>
        <div style={{ textAlign:'right' }}>
          <Pill tone="accent">brief for dev agent</Pill>
          <div style={{ fontFamily: F.mono, fontSize: 11, color: D['fg-subtle'], letterSpacing:'0.16em', textTransform:'uppercase', marginTop: 6 }}>uses @vivid-fish/brand</div>
        </div>
      </div>

      <SectionH kicker="01" title="Who this is for" sub="Two audiences. One is heavy. The other is light. Design accordingly."/>
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 18 }}>
        <Card padding={20}>
          <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', marginBottom: 6 }}>
            <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 18 }}>The invited user</div>
            <Pill tone="neutral" size="sm">thin</Pill>
          </div>
          <P><M>"My friend gave me a base URL and a sign-in link."</M> They sign in, make a key, copy it, and leave. Their UI is deliberately small: endpoint, keys, what those keys can do, why a call failed.</P>
          <P style={{margin:0}}><M>What they should never have to think about: profiles, intents, protocols, policy.</M> Those are admin concerns. Users get assigned a profile by the admin; the UI just renders what that profile lets them do.</P>
        </Card>
        <Card padding={20}>
          <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', marginBottom: 6 }}>
            <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 18 }}>The operator</div>
            <Pill tone="accent" size="sm">heavy</Pill>
          </div>
          <P>This is where the real UX work is. The operator defines what people can do, brings new people in, debugs denials, watches the floor. Every primitive in the durable model surfaces here — modalities, intents, protocols, limits, visibility levels.</P>
          <P style={{margin:0}}>The hard screen: the <Em>profile editor</Em>. It has to translate a structured policy object into something an admin can hold in their head without seeing a YAML file.</P>
        </Card>
      </div>

      <SectionH kicker="02" title="The five jobs, in order" sub="Build for these. Cut the rest."/>
      <Card padding={20}>
        {[
          ['Get from signed-in to working call', 'In under 60 seconds. The first key flow is the hero.'],
          ['See what your key can actually do', 'In a sentence. Not as a JSON object. Not as a permission matrix.'],
          ['Understand why a call failed', 'Receipts are the killer feature. A 429 must answer "what to do next."'],
          ['Trust the place', 'Honest health. Plain English when something is napping or restarting.'],
          ['Bring more people in (admin)', 'Task-shaped, never schema-shaped. Profiles are templates, not forms.'],
        ].map(([h, b], i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'40px 1fr', gap: 16, padding:'14px 0', borderTop: i?`1px solid ${D.border}`:0 }}>
            <div style={{ fontFamily: F.mono, fontSize: 13, color: D.accent }}>{String(i+1).padStart(2,'0')}</div>
            <div>
              <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 16, color: D.fg }}>{h}</div>
              <div style={{ fontFamily: F.serif, fontStyle:'italic', fontSize: 14.5, color: D['fg-muted'], marginTop: 4 }}>{b}</div>
            </div>
          </div>
        ))}
      </Card>

      <SectionH kicker="03" title="Principles · Hickey lens" sub="Essential complexity, kept. Incidental complexity, culled."/>
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 18 }}>
        <Card padding={20}>
          <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing:'0.16em', textTransform:'uppercase', color: D.accent, marginBottom: 4 }}>essential</div>
          <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 17, marginBottom: 10 }}>What we keep</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontFamily: F.body, fontSize: 13.5, color: D.fg, lineHeight: 1.65 }}>
            <li><Em>Request ·</Em> has an identity, a profile, an intent.</li>
            <li><Em>Profile ·</Em> says yes or no to that request. <M>One per person; many people share one.</M></li>
            <li><Em>Backend ·</Em> available / degraded / draining / maintenance / unavailable. Roles: required / optional / standby.</li>
            <li><Em>Receipt ·</Em> every request produces exactly one, body-free, audience-filtered.</li>
            <li><Em>Capability ·</Em> each backend publishes what it can actually do.</li>
          </ul>
        </Card>
        <Card padding={20}>
          <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing:'0.16em', textTransform:'uppercase', color: D.danger, marginBottom: 4 }}>incidental</div>
          <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 17, marginBottom: 10 }}>What we cull</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontFamily: F.body, fontSize: 13.5, color: D.fg, lineHeight: 1.65 }}>
            <li><M>No "pick a profile" affordance on the user side.</M> The user has one. They don't pick it.</li>
            <li><M>No suggested-apps grid in create-key.</M> People know what they're naming.</li>
            <li><M>No tiers picker on the user side.</M> Tiers are admin policy.</li>
            <li><M>No queue-priority / daily-cap UI</M> until those things are enforced by the backend.</li>
            <li><M>No "all systems healthy" pulse on idle.</M> Only the abnormal moves.</li>
            <li><M>No service-account / profile-id / intent-list language</M> in user-facing flows.</li>
            <li><M>No dead rows for revoked keys.</M> Revoke disappears the row.</li>
          </ul>
        </Card>
      </div>

      <Card padding={18} style={{ marginTop: 14, background:'rgba(123,196,200,0.04)' }}>
        <div style={{ fontFamily: F.serif, fontStyle:'italic', fontSize: 15, color: D.fg, lineHeight: 1.55 }}>
          "Simple" and "easy" are not the same. The user side optimizes for easy — you accomplish what you came for in two taps. The admin side optimizes for simple — every concept maps to exactly one durable thing. Don't let easy on one side leak incidental complexity onto the other.
        </div>
      </Card>

      <SectionH kicker="04" title="The IA" sub="Bottom tab bar on mobile. Sidebar on desktop. Same destinations."/>
      <Card padding={20}>
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr 1fr 1fr', gap: 14, paddingBottom: 14, borderBottom: `1px solid ${D.border}` }}>
          {[
            { i: I.home, t:'Home', d:'Apps. Lately. Health.' },
            { i: I.key, t:'Keys', d:'Manage all your keys.' },
            { i: I.capability, t:'What works', d:'Models, queues, status.' },
            { i: I.signal, t:'Activity', d:'Receipts feed.' },
            { i: I.user, t:'You / Admin', d:'Account · admin tools.' },
          ].map((s, i) => (
            <div key={i} style={{ textAlign:'center' }}>
              <div style={{ display:'inline-flex', alignItems:'center', justifyContent:'center', width: 48, height: 48, borderRadius: 14, background: D['surface-2'], color: D.accent, marginBottom: 8 }}><s.i size={22}/></div>
              <div style={{ fontFamily: F.display, fontWeight: 600, fontSize: 14, color: D.fg }}>{s.t}</div>
              <div style={{ fontFamily: F.body, fontSize: 12, color: D['fg-muted'] }}>{s.d}</div>
            </div>
          ))}
        </div>
        <div style={{ display:'grid', gridTemplateColumns:'auto 1fr', gap: 14, marginTop: 16 }}>
          <div style={{ fontFamily: F.mono, fontSize: 11, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'] }}>admin only</div>
          <div style={{ display:'flex', gap: 10, flexWrap:'wrap' }}>
            {['People & apps','Profiles','Automation keys','Debug access'].map(t => <Pill key={t} tone="info">{t}</Pill>)}
          </div>
        </div>
      </Card>

      <SectionH kicker="05" title="Where the wins live" sub="Three admin screens. One user screen. That's the design."/>
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 14 }}>
        {[
          { n:'B.6 · Profile editor', tag: 'admin', body: <>The centerpiece. Capability cube (modality × level) replaces every per-protocol allow-list flow. Plain-English label, blast-radius warning, recent denials sidebar, advanced YAML behind a disclosure. <Em>Get this right and the rest of the admin UX falls into place.</Em></>},
          { n:'B.4 · Invite + B.3 · Person', tag:'admin', body: <>The operating loop. Bring someone in by picking the profile they get, then watch them work. The user object is small because the profile carries the weight.</>},
          { n:'B.7 · Debug a denial', tag:'admin', body: <>A receipt-shaped UI for the admin side. "Continue tried embeddings; Ada's profile is family-chat; here are two fixes and their blast radius." Lets operators resolve a real problem in three taps.</>},
          { n:'A.7 · Receipt detail', tag:'user', body: <>The only place a user really needs to read the system. A 403 becomes a sentence: "This key can't do that." Pair with a suggested next step, not a stack trace.</>},
        ].map((c, i) => (
          <Card key={i} padding={18}>
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
              <div style={{ fontFamily: F.mono, fontSize: 11, letterSpacing:'0.16em', textTransform:'uppercase', color: D.accent }}>{c.n}</div>
              <Pill tone={c.tag==='admin'?'accent':'neutral'} size="sm">{c.tag}</Pill>
            </div>
            <p style={{ margin:'10px 0 0', fontFamily: F.body, fontSize: 13.5, color: D['fg-muted'], lineHeight: 1.55 }}>{c.body}</p>
          </Card>
        ))}
      </div>

      <SectionH kicker="06" title="What to drop" sub="Not in v1. Not in v1.1 unless someone asks for it twice."/>
      <Card padding={20}>
        {[
          ['Per-tool config templates','We won\'t do this well. The bearer token + base URL is enough.'],
          ['Marketing copy on the home page','The invite is the marketing.'],
          ['Two separate apps for user / admin','One app, gated sections.'],
          ['Permission matrices','See P02.'],
          ['Soft-deleted dead rows','Revoke means revoke. Disappear is the right behavior.'],
          ['Service-account / profile-id language in user flows','Hidden behind "Advanced."'],
          ['Multiple primary actions per screen','One primary, one secondary, never three.'],
          ['Animations on idle state','See P05.'],
        ].map(([h, b], i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'24px 1fr 1.5fr', gap: 14, padding:'12px 0', borderTop: i?`1px solid ${D.border}`:0, alignItems:'baseline' }}>
            <I.close size={16} stroke={D.danger}/>
            <span style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14, color: D.fg }}>{h}</span>
            <span style={{ fontFamily: F.body, fontSize: 13, color: D['fg-muted'], fontStyle:'italic' }}>{b}</span>
          </div>
        ))}
      </Card>

      <SectionH kicker="06.5" title="Today vs reference" sub="The canvas is split. Build TODAY first; treat REFERENCE as direction, not commitment."/>
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap: 14 }}>
        <Card padding={20}>
          <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing:'0.16em', textTransform:'uppercase', color: D.success }}>TODAY · sections A–F</div>
          <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 17, marginTop: 6 }}>Maps onto existing backend</div>
          <ul style={{ margin:'10px 0 0', paddingLeft: 18, fontFamily: F.body, fontSize: 13, color: D['fg-muted'], lineHeight: 1.6 }}>
            <li>OIDC sign-in (backend exists)</li>
            <li>Personal apps + opk_ key creation/revocation (exists)</li>
            <li>Profile picker driven by what the admin has defined</li>
            <li>Capabilities & workflow discovery from /gateway/v1/capabilities, /gateway/v1/workflows</li>
            <li>Activity feed from /gateway/v1/receipts (audience-filtered)</li>
            <li>Receipt detail showing real receipt fields (profile decision, intent, reasons)</li>
            <li>Admin: profile list/edit (durable model exists)</li>
          </ul>
        </Card>
        <Card padding={20}>
          <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing:'0.16em', textTransform:'uppercase', color: D.warning }}>REFERENCE · sections Z</div>
          <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 17, marginTop: 6 }}>Direction, not yet wired</div>
          <ul style={{ margin:'10px 0 0', paddingLeft: 18, fontFamily: F.body, fontSize: 13, color: D['fg-muted'], lineHeight: 1.6 }}>
            <li>Named-tier access picker (Chat only / Chat+Embeddings / Media / Full)</li>
            <li>Operating overview with denials feed, GPU floor, request rates</li>
            <li>Multi-key rotation per app (UI for key rotation)</li>
            <li>Automation key (oak_) dedicated creation flow</li>
            <li>Access debugger / policy simulator surfaced as a screen</li>
            <li>Cloud-fallback toggles, status visibility levels in profile edit</li>
            <li>Health-summary copy with backend-specific ETAs</li>
          </ul>
        </Card>
      </div>

      <SectionH kicker="07" title="What to build first" sub="A two-week slice that proves the design without committing to all of it."/>
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap: 14 }}>
        {[
          { w:'Week 1', t:'The shell + first key', list:['App shell · bottom tabs on mobile, sidebar on desktop','OIDC sign-in → Home empty state','3-step new-key flow · name, profile, one-time reveal','App/key detail · copy + revoke','Plain-language profile sentence on key detail']},
          { w:'Week 2', t:'Reading the gateway', list:['Capabilities page from /gateway/v1/capabilities','Activity feed from /gateway/v1/receipts','Receipt detail with profile decision rendering','Health glance card on home','Profiles list/edit (read existing, edit one)']},
          { w:'Later (REFERENCE)', t:'Operator-grade admin', list:['Operating overview with denials feed','Automation key (oak_) flow','Named-tier access picker','Policy simulator UI','Key rotation flow']},
        ].map((c, i) => (
          <Card key={i} padding={18}>
            <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'] }}>{c.w}</div>
            <div style={{ fontFamily: F.display, fontWeight: 700, fontSize: 17, color: D.fg, marginTop: 6 }}>{c.t}</div>
            <ul style={{ margin:'12px 0 0', paddingLeft: 18, fontFamily: F.body, fontSize: 13, color: D['fg-muted'], lineHeight: 1.6 }}>
              {c.list.map(l => <li key={l} style={{ marginBottom: 4 }}>{l}</li>)}
            </ul>
          </Card>
        ))}
      </div>

      <SectionH kicker="08" title="Engineering notes" sub="From the brand package critique. Fix these as you adopt @vivid-fish/brand."/>
      <Card padding={20}>
        {[
          ['Drop the glow-on-idle','Replace shadow-vf-glow with shadow-caustic-2 (inset + soft black drop). Keep glow available, never apply it by default.'],
          ['Honor prefers-reduced-motion','No animation should auto-run without the media query gate.'],
          ['Add real component classes','Button, Input, Select, Switch, Modal, Sheet, Toast, Tabs, Table. Today the package ships banner+pill+card and calls it a system.'],
          ['Unify token prefix','Pick `vf-*` everywhere. Tailwind `@theme` should derive from CSS vars, not duplicate them.'],
          ['Self-host fonts','Remove the Google Fonts @import from the package entry. Provide a docs section instead.'],
          ['Define focus + hit targets','`:focus-visible` outlines, 44pt min hit target tokens, contrast tokens for text on each surface.'],
          ['Re-tier the type scale','Add display/h1/h2/h3 + body + small. The current 10.5–18 scale is a status page, not a product.'],
          ['Light theme path','Add `[data-theme="shore"]` selector and a Shore palette set, even if v1 ships dark-only.'],
        ].map(([h,b], i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'24px 1fr 1.5fr', gap: 14, padding:'12px 0', borderTop: i?`1px solid ${D.border}`:0, alignItems:'baseline' }}>
            <I.warning size={15} stroke={D.warning}/>
            <span style={{ fontFamily: F.display, fontWeight: 500, fontSize: 14, color: D.fg }}>{h}</span>
            <span style={{ fontFamily: F.body, fontSize: 13, color: D['fg-muted'] }}>{b}</span>
          </div>
        ))}
      </Card>

      <SectionH kicker="09" title="Open questions" sub="Things to answer before week 1 ends."/>
      <Card padding={20}>
        {[
          ['How does the OIDC sign-in actually look — popup, redirect, embedded? This affects the sign-in screen.'],
          ['When a user upgrades a key\'s tier, does that require admin approval, or is it auto-allowed within their cap?'],
          ['What\'s the SLA on receipt persistence? If retention is 7 days, the activity feed needs an "older →" gate.'],
          ['Do we want a one-time "what is this place" screen for first-signin, or zero (current direction)? Test with one new user.'],
          ['Notification preference: in-app only, email, both? Affects the "you" page scope.'],
        ].map((q, i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'24px 1fr', gap: 14, padding:'10px 0', borderTop: i?`1px solid ${D.border}`:0 }}>
            <I.info size={15} stroke={D.info}/>
            <span style={{ fontFamily: F.serif, fontStyle:'italic', fontSize: 14.5, color: D.fg, lineHeight: 1.45 }}>{q[0]}</span>
          </div>
        ))}
      </Card>

      <div style={{ marginTop: 48, paddingTop: 18, borderTop: `1px solid ${D.border}`, fontFamily: F.mono, fontSize: 10.5, letterSpacing:'0.16em', textTransform:'uppercase', color: D['fg-subtle'], display:'flex', justifyContent:'space-between' }}>
        <span>vivid.fish · design · ai gateway v0.1</span>
        <span>read me, then drift through the artboards →</span>
      </div>
    </Pad>
  );
}

window.Brief = Brief;
