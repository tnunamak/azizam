# Azizam redesign: red team, round 2 (Opus, screenshots only)

Source: `~/.tmp/azizam-redesign/d1-10/` (1,434 files: 6 scenarios × owner/member (+ restricted, signed-out in `default/`) × home/status/library (+ library states) × light/dark × 1440/390, with `_viewport` crops and `.txt` dumps).
File paths below are relative to that folder. `{light,dark}` / `{1440,390}` means the defect is in every listed variant.

**Method caveat.** The scenario folders were captured at different times, between 00:54 and 01:33. `default/` was captured last, and its library captions are truncated in a way the other folders are not, so the build probably changed during the run. I rate contradictions **inside one scenario folder** as the strongest evidence. I mark contradictions that cross folders as such.

**Coverage gap (affects all three personas).** No screenshot shows the creation flow: the **Try** page, "Make a picture" or "Read text aloud" after a tap, the prompt form, progress or result. Persona 2's two core tasks therefore **cannot be verified end to end** from this set. The next capture round needs `try_*` surfaces for image, speech and video in ready, busy, degraded, refused (budget) and failed states.

---

## Persona 1: Principal product designer (Linear)

**Overall.** The bones are good. One sidebar, a calm palette, one card grammar on Status (title, one-line action, then label/value rows), and the library grid looks good with real images. The problems are in the system layer: the same fact has different names, different severities and different counts on different screens. Several surfaces also have tells of generated UI (filler sections, duplicate links, machine-phrased sentences, a leaked fixture string).

### D1. P1: One incident has five severity words
`default/default_owner_status_populated_light_1440.png`, `default/default_member_status_populated_light_1440.png`, `default/default_restricted_status_restricted_light_1440.png`, `budget-exhausted/budget-exhausted_member_status_populated_light_1440.png`
The Video studio incident is called "degraded" (headline), "answering with errors" (card title), "failing again since about 1:30 PM" (history line), "Down 4" (bar legend), "reported busy, and requests have failed" (member) and "Busy: working on other requests" (restricted and budget members). The health strip shows the last two hours as **Degraded** (amber), but the sentence says **failing**.
**Fix:** Define one state set with one meaning each: Ready / Slow (busy, queueing) / Failing (errors) / Down (no answer) / Unknown (no fresh data) / Off. Derive the headline, card, chip, bar colour and member copy from the same enum. "Busy" must never represent errors.

### D2. P1: The "issues" pill counts different things on different screens
- `default/*_owner_*` "1 issue" = 1 card. `budget-exhausted/*_owner_*` "2 issues" = 2 cards. `stale-health/*_status_populated_dark_1440.png` "5 issues" = 5 capabilities, but the page shows only 2 cards.
- In the same state the member sees "1 issue" (`budget-exhausted/budget-exhausted_member_home_populated_light_390.png`) and the owner sees "2 issues" (`budget-exhausted/budget-exhausted_owner_home_populated_dark_390_viewport.png`).
- `new-member/new-member_member_home_populated_light_390.png` and `default/default_restricted_status_restricted_light_1440.png` show a blue "Busy" pill while the owner in the same folder sees "1 issue".
- The pill says "1 issue" while the page cannot load status (`default/default_member_status_error_light_390_viewport.png`) and while it is still loading (`default/default_member_status_loading_light_390_viewport.png`). The equivalent Home error shows "Status unknown" (`default/default_member_home_error_light_390_viewport.png`).
- Owner Status-empty shows "1 issue" (`default/default_owner_status_empty_light_1440.png`), but owner Home-empty shows "Nothing set up" (`default/default_owner_home_empty_dark_1440.png`).
**Fix:** The pill is the same headline in a smaller size. Its count = number of "Needs attention" cards for the owner, or number of non-Ready capabilities for the member, and the label says which ("2 problems", "2 things not working"). When the status fetch fails or is pending, the pill says "Status unknown" or "Checking".

### D3. P1: Home elevates issues inconsistently
- Budget gets a full card on owner and member Home (`budget-exhausted/budget-exhausted_owner_home_populated_light_1440.png`).
- The higher-ranked Video studio failure, and Remote backup down (`provider-down/provider-down_owner_home_populated_light_1440.png`), get only the one-line strip.
- On owner Home the order is the reverse of Status, where Video is card 1 and budget is card 2.
**Fix:** Home shows the top "Needs attention" card in compact form (title, since, one action), in Status order, plus "+N more on Status".

### D4. P1: Leaked fixture string
`default/default_owner_status_error_{light,dark}_{1440,390}.png` shows the text "Simulated outage for the screenshot". This reads as unfinished or generated. The same page says "Couldn't check status", yet it still renders "1 remote provider is set up · Show all" from a list it just said it could not fetch.
**Fix:** Never show raw upstream error text as body copy. Put it in "Technical details". Hide sections whose data failed to load, or mark them "last known".

### D5. P2: Duplicate and orphan controls
- "Open Status" appears twice, as a link and a button, in `budget-exhausted/budget-exhausted_owner_home_populated_{light,dark}_{1440,390}.png`.
- "Connect an app" appears twice on mobile Home, as a header button and as the bottom card (`default/default_member_home_populated_light_390_viewport.png` and the full-page version).
- "Model names" is an unexplained grey link under the capability grid on every member Status page.
**Fix:** Keep one "Open Status" (inside the card). On mobile, drop the header button, or the card if the user already has apps. Rename "Model names" to "Which models can I use?" or move it into a disclosure.

### D6. P2: Templated "Make something" grid has a hole
`*_home_populated_*_1440.png`: seven pastel icon tiles in a 2-column grid leave an empty bordered cell at bottom right. With the floating "Connect an app" card at right, this is a stock dashboard template. **Fix:** Use a 4+3 layout, or make the last tile "Chat" span the full width, or move "Turn a recording into text" into a secondary row.

### D7. P2: Detail views disagree with each other
- Image detail has a "..." menu and prev/next arrows (`default/default_owner_library_library-detail_light_1440.png`).
- Desktop video detail has neither (`default/default_owner_library_library-video_light_1440.png`).
- Desktop audio detail has arrows but no "..." (`default/default_owner_library_library-audio_light_1440.png`). Mobile audio has "..." (`default/default_member_library_library-audio_dark_390_viewport.png`).
- Grid caption and detail title are different strings: "Backyard raised vegetable bed…" vs "An overhead photograph of a backyard raised vegetable bed with tomatoes" (`default/default_member_library_library-detail_light_390_viewport.png`), and "Lived-in family kitchen…" vs "A lived-in family kitchen in warm morning light".
**Fix:** Use one detail shell with the same actions and navigation for every media type. Generate the title once and use it in both places.

### D8. P2: Inconsistent verbs and labels
- Status card actions: "Inspect Video studio" (filled), "Open Remote backup", "Edit the Everyone plan", "Inspect Home Assistant voice" (outlined).
- Refresh button: "Refresh" (owner) vs "Refresh status" (member).
- Media naming: "Make a picture" (Home) vs "Make an image" (Library empty) vs "Images" (capabilities).
- App naming: "Personal access" (`new-member/new-member_member_home_populated_light_1440.txt`) vs "Robin's personal app".
**Fix:** Use one verb for "go to the thing" ("Open …"), one noun per media type ("picture" for members), and one name for the default key.

### D9. P2: Machine-phrased sentences
"Down 4 and degraded 2 of the last 24 hours, in 3 spells"; "Its capabilities read Unknown until it answers."; "The gateway did not return your Home."; "Your app reached the gateway."; "Spent: Not reported by the gateway" (the product talks about itself in the third person).
**Fix:** Write for a human: "Video has failed 3 times today. Failing now since 1:30 PM (2 h)." / "Couldn't load your home page. Try again."

### D10. P2: Layout drift
- Owner Status-empty uses a narrower max-width (content ends at about 1180 px; other Status pages end at 1388): `default/default_owner_status_empty_light_1440.png`.
- In the same table, a spend bar ("USD 2.02 of USD 8.00 this month") sits in the column headed "Health checks, last 24 h".
- Voice cards in the grid put the date under the card, image cards put it beside the caption, and the two collide: "Backyard vegetable ga… Oct 20 Oct 20" (`default/default_member_library_expired_light_1440.png`).

### D11. P2: Mobile type filter is hidden
At 390 px the All/Images/Video/Audio segmented control is removed and folded into "Filters" (`default/default_member_library_library-audio-grid_light_390_viewport.png`). Media type is the most common filter. Keep it as a horizontal chip row.

---

## Persona 2: 62-year-old family member on a phone (birthday picture + read a message aloud)

**Overall.** The first screen is good. "Make a picture" and "Read text aloud" are the first two rows with plain verbs. Problems start as soon as anything is not perfect: the words become engineering words, the most important information is on a page she won't find, and sometimes the page tells her the wrong story.

### G1. P0: She is told video is "Busy" when it is broken
`budget-exhausted/budget-exhausted_member_home_populated_light_390.png`, `budget-exhausted/budget-exhausted_member_status_populated_dark_390.png`, `new-member/new-member_member_home_populated_light_390.png`, `default/default_restricted_status_restricted_light_1440.png`
"Make a video · Busy", "Working on other requests." In the same folder the owner sees "Video studio is answering with errors… failing again since about 1:30 PM". She will wait and retry. The video will fail, and she will think she did something wrong.
**Fix:** Same truth as the owner, in her words: "Videos aren't working right now. Sam knows. Pictures and voice still work."

### G2. P0: "Nothing is set up yet" while her pictures are on screen, and the Make buttons are gone
`default/default_member_home_empty_{light,dark}_{1440,390}.png`
The header says "Nothing is set up yet" (pill: "Nothing set up"). The **Recent** row shows four of her pictures, and "Make something" is gone, so she cannot start a picture from Home. In the same "empty" state her Status page lists Chat, Images, Voice and the rest as **Ready** (`default/default_member_status_empty_light_1440.png`).
**Fix:** If engines exist, show Make something. If none exist, say "Sam hasn't turned on picture-making yet" and hide Recent only when it is actually empty. Never tell a member something they cannot act on ("set up").

### G3. P1: Failures are visible only on a page she won't find
- Robin's own video failure appears only on Status ("Your video request failed", `default/default_member_status_populated_light_1440.png`). Home shows a small grey "Video degraded" with **no link** (`default/default_member_home_populated_light_390_viewport.png`; the owner gets "Open Status").
- On the phone, Status is under **More**. The bottom tabs are Home / Library / **Apps** / More.
- Failed creations never appear in Library (`default/default_owner_library_library-video-grid_dark_1440.png` shows only the Oct 17 video). Running ones do ("Making your video", `running/running_owner_home_populated_light_1440.png`).
**Fix:** Put a "Your video didn't finish. Try again" tile in Recent and Library, where the work would have been. Make the Home strip tappable. Swap the "Apps" tab for "Status" (or "Help") for members.

### G4. P1: Jargon wall on member screens
On member Status and detail (`*_member_status_*`, `default/default_member_library_library-detail_light_390_viewport.png`, `default/default_member_library_library-audio_dark_390_viewport.png`): "Degraded", "Embeddings · Turned off by the owner", "Transcription", "Model names", "Krea2 Turbo", "TTS 1", "Receipt", "Charge: Cost not recorded", "Copy prompt", "Your app reached the gateway", "Reported busy, and requests have failed since the last success." "5 of 7 capabilities need attention" (`stale-health/stale-health_member_home_populated_light_390_viewport.png`) tells her she must do something.
**Fix:** Member vocabulary: Working / Slow / Not working / Not sure / Turned off. Hide Embeddings for members. Put Model, Size, Made in, Charge and Receipt under "Details". Change "need attention" to "aren't working right now".

### G5. P1: "Unknown" next to "Read text aloud" gives no guidance
`stale-health/stale-health_member_home_populated_light_390_viewport.png`
She wants to read a message aloud and sees "Read text aloud · Unknown". Can she try? No copy says.
**Fix:** "Might not work right now. Try it. If it fails, Sam will see why." Keep the tile tappable.

### G6. P1: Budget card is half right
`budget-exhausted/budget-exhausted_member_home_populated_light_390.png`
"Paid models were paused for you… Ask Sam if you need them sooner." It is good that the card names Sam and a date. But "for you" sounds like a punishment (it is household-wide), "Chat · Degraded" doesn't say whether chat still works, and her list shows three "Refused" chats with no next step.
**Fix:** "The family's monthly allowance for paid AI is used up until Nov 1. Chat still works with the home model, but may be slower or less clever. Ask Sam if you need more."

### G7. P1: No Share on the phone
`default/default_member_library_library-detail_light_390_viewport.png`
The goal is "send the birthday picture to the family". The only actions are Download / Copy prompt / Receipt.
**Fix:** On mobile, make "Share" the primary action (Web Share API) and put Download in "…". Hide Copy prompt and Receipt from members by default.

### G8. P1: Scary or confusing system states
- Signed-out shows an avatar "Y", a green "Gateway reachable" pill, a red ✕ error icon and the full tab bar, for what is just a sign-in screen (`default/default_signed-out_home_signed-out_light_390_viewport.png`). Fix: a neutral sign-in card with a short line about what this place is, and no avatar or tabs.
- Library error is a red ✕ "Couldn't load your library" with no reassurance (`default/default_member_library_error_light_390_viewport.png`). Fix: "Your pictures are safe. We couldn't reach the home server. Try again."
- "App still not working? Check your connection" on every member Status page blames her network even when the server is the fault.

### G9. P2: "Connect an app" leads a grandparent's Home
`default/default_member_home_populated_light_390_viewport.png`
"Connect an app" is the only top-right button, and "Apps" is a primary tab. For most members the job is "make something", not "configure keys". Show it only to members who have used apps, or put it under More.

---

## Persona 3: Owner (Sam) at 11 pm; video failing, paid budget exhausted

**What works:** The Status card grammar answers "what" and partly "since" and "who": "Video studio is answering with errors · since about 1:30 PM · A request from Robin's personal app failed 1h ago · Inspect Video studio". The budget card names the limit, the reset date and who was refused. That is the right shape.

### O1. P0: Members get a different, softer story than the owner in the same scenario
`budget-exhausted/budget-exhausted_owner_status_populated_light_1440.png` vs `budget-exhausted/budget-exhausted_member_status_populated_light_1440.png` (and `new-member/*`, `default/default_restricted_*`)
The owner sees "Video studio is answering with errors". The member sees "Video · Busy · Working on other requests". Also, in the budget folder the owner card says "A request from Robin's personal app failed 1h ago", but Robin's own recent requests in that folder show **no failed video** (only Chat Refused ×3 and Images). The two views of the same household disagree about whether Robin was affected.
**Fix:** Derive the member chip and the owner card from one incident record. Member request history must include every request the owner card attributes to that member.

### O2. P0: Stale health is shown as fresh
`stale-health/stale-health_owner_status_populated_dark_1440.png`, `stale-health/stale-health_member_status_populated_dark_1440.png`, `stale-health/stale-health_member_home_populated_light_390_viewport.png`
- The headline says "**Checked just now**", but the Home Assistant voice card says "Last check **7 h ago**". The member page says "No answer to the latest check", which turns "no fresh data" into "engine not answering".
- The card title says the engine "is not answering checks", which is a different claim from "hasn't been checked in 7 h".
- "**Affects: Music, Sound effects**", but Voice and Transcription are also Unknown on the same page, and no card explains them.
- At 11 pm Sam cannot tell whether health checking stopped, the engine died, or both.
**Fix:** "Checked" must be the oldest relevant check time, not the time of the page fetch. Split "Health checks stopped (last 7 h ago)" from "Engine not answering (N failed checks)". Fill "Affects" from the capabilities the page marks Unknown.

### O3. P0: Library expiry contradicts the retention copy
`default/default_owner_library_expired_light_1440.txt` / `.png`, `default/default_member_library_expired_light_1440.png`
"4 files are no longer stored. The gateway keeps pictures for 30 days." The 4 missing files are the **newest** (Today, Today, Yesterday, Monday for the owner; Today, Monday, Sunday, Friday for Robin), while an Oct 17 video is still stored. Either the copy is false or recent work was lost. That is a data-loss signal hidden in a footer. The member version gives a different reason ("Their prompts and receipts remain.").
**Fix:** State the real cause per file (expired, deleted, storage error, missing on disk). If newer-than-retention files are missing, treat it as an incident on Status, not a library footer. Make the owner and member reason text agree.

### O4. P1: Budget card doesn't answer "which provider, how much, what still works"
`budget-exhausted/budget-exhausted_owner_status_populated_light_1440.png`
- "Spent: **Not reported by the gateway**", yet the provider row in another state shows "USD 2.02 of USD 8.00 this month" (`default/default_owner_status_empty_light_1440.png`, different folder).
- The card has no **Affects** row. Video and Remote-backup cards have one, and Chat turned Degraded because of this card.
- It never names the paid provider. The same thing is called "Everyone plan" (owner), "household" / "outside AI services" / "Paid models" (member), "remote provider" (summary) and "Remote backup" (provider-down).
- Daily and monthly limits are both listed as reached, with different reset rules. Sam cannot tell when service returns.
**Fix:** Spent "USD 8.00 of 8.00 this month (since Oct 1)"; "Affects: Chat (paid models). Local chat still answers."; "Service returns: Nov 1, 7:00 PM CDT (monthly). Raising the limit takes effect now." Use one name for the provider everywhere.

### O5. P1: Remote backup down has no "since", and Chat is marked Degraded without saying fallback works
`provider-down/provider-down_owner_status_populated_light_1440.png`, `provider-down/provider-down_member_status_populated_light_390.png`
The owner card says "Last check 2m ago" but not when the outage began, though the Video card has "since about 1:30 PM". "Affects: Chat (an engine on this computer still serves it)", yet 7 requests failed. Either fallback is not happening or failures happen before fallback. The member is told only "1 request failed in the last hour", with no "try again, it should work now".
**Fix:** Add "Down since …" to every incident card. If fallback exists, say whether it is working ("0 failures since fallback at 3:18 PM"). Tell members "Try again. The home model will answer."

### O6. P1: A new video job runs on an engine that is failing, with no warning
`running/running_owner_home_populated_light_1440.png`, `running/running_owner_status_populated_light_1440.png`
"Making your video · Started 2m ago" and "Video for Sam · In progress", next to the card saying Video studio has been failing since 1:30 PM. There is no ETA, no cancel, and no "likely to fail" note.
**Fix:** At submit time and on the running tile: "Video is failing right now; this may not finish." Show elapsed time against a typical duration and a Cancel action.

### O7. P1: "Home Assistant voice" is both an app and an engine
`stale-health/stale-health_owner_status_populated_dark_1440.png` vs every owner Home "Your apps: Home Assistant voice · Used 6h ago"
On Status it is an engine that serves Music and Sound effects (odd for a voice assistant) and was last checked 7 h ago. On Home it is a client app used 6 h ago. At 11 pm this sends Sam to the wrong system.
**Fix:** Give engines and apps distinct names and icons ("HA voice (app)" vs "Audio engine"). On the Status card, add "Used by: …" where an engine is named after its consumer.

### O8. P1: Actions need a terminal, and mobile hides the evidence
"Check the engine's log and restart it." has no Restart button and no log link visible on the card (`budget-exhausted/budget-exhausted_owner_status_populated_dark_390.png`). On the phone the health strip and failed-request count are behind "Show evidence", and Status itself is behind **More**.
**Fix:** Add "Restart engine" and "View last 50 log lines" on the card (owner only). Make Status a primary tab for the owner on mobile.

### O9. P1: Error, empty and loading states lie or dead-end
- Member Status loading shows "Checking" **and** "Checked just now" (`default/default_member_status_loading_light_390_viewport.png`).
- Owner Home error says "Status may show why." with no link (`default/default_owner_home_error_light_1440.png`).
- Owner Status empty says "No engines yet" with no "Add an engine" action (`default/default_owner_status_empty_light_1440.png`), but Home-empty has one.
- Owner Home empty says "Nothing is set up yet" while showing 4 recent pictures (`default/default_owner_home_empty_dark_1440.png`).
**Fix:** Show no "Checked" line until a check completes. Add a link to Status. Add the "Add an engine" CTA on Status. Base "nothing set up" on the engine count, and still show Make something when engines exist.

### O10. P2: Time zone mismatch
Home says "resets on Nov 1". Status says "Monthly: on Nov 1 (UTC)". For America/Chicago that is Oct 31, 7 PM. Show local time and date everywhere.

### Light vs dark
The text dumps are the same in both themes, apart from a transient "Updating" label captured in a few dark video/audio grid shots. The incident and health colours map correctly (red becomes salmon, amber becomes yellow, legend matches). I found no theme-specific honesty defect. Dark mode is fine on contrast. The voice-card backgrounds (olive, plum, teal) are heavy in light mode next to photos (P2).

---

## Consolidated P0 / P1

| # | Sev | Finding | Key files |
|---|---|---|---|
| O1/G1 | P0 | Members see "Video busy / working on other requests" while owner sees "answering with errors, failing since 1:30 PM". In the budget folder, Robin's history omits the failed video that the owner card attributes to him | `budget-exhausted/*_member_{home,status}_*`, `new-member/*_member_*`, `default/default_restricted_*` vs `*_owner_status_*` |
| O2 | P0 | Stale health shown as "Checked just now" while the engine's last check was 7 h ago. "Affects" omits Voice/Transcription, which are also Unknown | `stale-health/stale-health_{owner,member}_status_*` |
| O3 | P0 | Expired-files footer says "keeps pictures for 30 days" but the newest files are the missing ones. Owner and member give different reasons | `default/default_{owner,member}_library_expired_*` |
| G2 | P0 | "Nothing is set up yet" with 4 pictures in Recent. Make something removed (blocks the core task). Member Status in the same state lists all capabilities Ready | `default/default_{member,owner}_home_empty_*`, `default/default_member_status_empty_*` |
| D1 | P1 | Five severity words for one incident (degraded / answering with errors / failing / down / busy) | all `*_status_populated_*` |
| D2 | P1 | Issue-count pill counts different things; shows "1 issue" during error/loading; "Busy" vs "1 issue" for the same state | see D2 |
| D3/G3 | P1 | Home elevates budget but not video or provider failures; member Home strip has no Status link; Status hidden under More; failed jobs never appear in Library | `*_home_populated_*`, `*_library-video-grid_*` |
| D4 | P1 | Leaked "Simulated outage for the screenshot"; failed list still rendered as "1 remote provider is set up" | `default/default_owner_status_error_*` |
| O4/G6 | P1 | Budget: spend "not reported", no Affects, provider unnamed, five names for it, unclear when service returns, "paused for you" | `budget-exhausted/*_status_*`, `*_home_*` |
| O5 | P1 | Provider down: no "since"; Chat degraded despite stated local fallback; member not told to retry | `provider-down/*_status_*` |
| O6 | P1 | Video job started on a failing engine with no warning, ETA or cancel | `running/running_owner_{home,status}_*` |
| O7 | P1 | "Home Assistant voice" is both an app and an engine | `stale-health/*_owner_status_*`, all owner Home |
| O8 | P1 | "Restart it" needs a terminal; mobile hides evidence and Status | `*_owner_status_*_390*` |
| O9 | P1 | Loading shows "Checked just now"; Home error points to Status with no link; Status-empty has no Add-engine CTA | `default/*_loading_*`, `*_error_*`, `*_empty_*` |
| G4/G5 | P1 | Member-facing jargon (Degraded, Embeddings, Krea2 Turbo, TTS 1, Receipt, "need attention", Unknown with no guidance) | `*_member_status_*`, `*_member_library_library-*` |
| G7 | P1 | No Share on mobile detail for the "send to family" job | `default/default_member_library_library-detail_*_390*` |
| G8 | P1 | Signed-out shows avatar "Y", "Gateway reachable" and a red error icon; library error has no "your pictures are safe" | `default/default_signed-out_*`, `default/default_member_library_error_*` |
| — | P1 (process) | Try/creation flow not captured, so the core picture and read-aloud tasks are unverified | whole set |

P2 items: D5–D11, G9, O10 and the light-mode voice-card colours.
