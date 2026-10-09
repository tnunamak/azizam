# Azizam UI red-team review — round 2

Scope: PNG screenshots in `~/.tmp/azizam-redesign/d1-10/` only. I compared owner and member views, 390 px and 1440 px layouts, light and dark themes, and the six named scenarios. Findings describe what the captures show; interaction and screen-reader behavior are unverified.

## P0

### 1. Video failure is reported to a member as ordinary queueing

**Screenshots:** `new-member/new-member_owner_status_populated_light_390.png`, `new-member/new-member_member_status_populated_light_390.png`, `budget-exhausted/budget-exhausted_owner_status_populated_dark_390.png`, `budget-exhausted/budget-exhausted_member_status_populated_dark_390.png`.

Sam sees “Video studio is answering with errors,” a failed request, and “Video: no other engine serves it.” On the same scenario, the member sees “Video busy” and “Working on other requests.” This tells the member to wait for a queue when the only video service is failing. The mismatch appears in both light and dark captures and in two scenarios.

**Fix:** Derive the member's video status from the same failing service state. Say “Video is having errors; requests may fail” and keep any queue state as a separate detail only when it is current and verified.

## P1

### 2. “Checked just now” masks a seven-hour-old health result

**Screenshots:** `stale-health/stale-health_owner_status_populated_light_390.png`, `stale-health/stale-health_member_status_populated_dark_390.png`, `stale-health/stale-health_owner_home_populated_light_390.png`.

The page and Home banner say “Checked just now,” while the Home Assistant voice card says its last check was 7h ago and Voice, Transcription, Music, and Sound effects are Unknown. A member can read the top timestamp as a fresh check of those capabilities.

**Fix:** Label the top time “Page refreshed just now.” Give the affected capabilities their own “Last successful health check: 7h ago” time beside Unknown. Do not imply the backend answered the latest refresh.

### 3. The budget card asserts spend that the gateway says it cannot measure

**Screenshots:** `budget-exhausted/budget-exhausted_owner_status_populated_light_1440_viewport.png`, `budget-exhausted/budget-exhausted_owner_home_populated_light_390.png`, `budget-exhausted/budget-exhausted_member_status_populated_light_390.png`.

Sam's status card says “Spent: Not reported by the gateway” but also says the Everyone plan “has used its daily and monthly remote spending allowance.” The member is told the household “reached its monthly limit.” The rejected requests support a limit problem, but these captures do not support a measured amount spent or that both limits were actually used. This matters when Sam decides whether to raise a paid limit.

**Fix:** Say “Paid requests were refused because a spending limit was reported. Current spend is unavailable.” Name the exact limit only if the refusal identifies it; otherwise say the daily or monthly limit may have been reached.

### 4. Mobile member Home gives no route from a warning to its explanation

**Screenshots:** `default/default_member_home_populated_light_390_viewport.png`, `default/default_owner_home_populated_light_390_viewport.png`, `default/default_member_home_populated_dark_1440_viewport.png`.

The member's phone Home says “Video degraded” but offers no “Open Status” link. Sam's phone Home does. On desktop the member has a visible Status item in the sidebar; on the phone it is hidden under More. The person with a failed video request has to discover where the explanation lives.

**Fix:** Make the member's warning a clear link to Status, with a short plain-language summary such as “Video requests may fail. See what is happening.”

### 5. Missing files are disclosed only after the library grid

**Screenshots:** `default/default_member_library_expired_light_390.png`, `default/default_owner_library_expired_light_390.png`, `default/default_member_library_expired_dark_390_viewport.png`.

The heading counts 11 member creations or 24 owner creations, but four files are no longer stored and are hidden from the grid. The disclosure and “Show them” control sit after the entire grid, below the phone's first screen. The initial count therefore appears to promise files that are unavailable. The member text says prompts and receipts remain; the owner text instead says pictures are kept for 30 days, so retention is explained differently by role.

**Fix:** Put “7 available · 4 files expired” (or the correct counts) beside the heading, with a visible “Show expired items” control. Use the same retention explanation for both roles and state what remains downloadable.

### 6. Status failures retain an unexplained “1 issue” badge

**Screenshots:** `default/default_owner_status_error_light_390_viewport.png`, `default/default_member_status_error_dark_390_viewport.png`, `default/default_owner_status_error_light_1440_viewport.png`.

The page says it could not check status, but the global badge still says “1 issue.” The captures do not label that count as cached or give its age. A user may read the count as a current, complete status result despite the failed check.

**Fix:** Show “Status unknown” in the global badge when the check fails, or explicitly label the prior count “Last known: 1 issue” with its timestamp.

### 7. The member is sent to connection troubleshooting after server-side refusals

**Screenshots:** `budget-exhausted/budget-exhausted_member_status_populated_light_390.png`, `provider-down/provider-down_member_status_populated_dark_390.png`, `default/default_member_status_populated_light_1440_viewport.png`.

The footer asks “App still not working? Check your connection” beneath requests that reached the gateway and were refused by a spending limit, or failed while Chat/Video was degraded. The generic next step points a non-technical member toward their phone or app even when the screen has identified a household service problem.

**Fix:** Make the next step depend on the shown failure: “Ask Sam about the spending limit,” “Try again after video recovers,” or connection help only when the gateway was not reached.

### 8. Test-only outage wording is visible to the owner

**Screenshots:** `default/default_owner_status_error_light_390_viewport.png`, `default/default_owner_status_error_dark_390_viewport.png`, `default/default_owner_status_error_light_1440_viewport.png`.

The engine-list error card says “Simulated outage for the screenshot.” This reads as internal test text in an operational status page and undermines confidence in whether the outage is real.

**Fix:** Replace it with a user-facing explanation such as “The gateway could not load the engine list. Refresh to try again,” or hide fixture-only text from the UI.

## P2

### 9. Status words require translation for a household member

**Screenshots:** `default/default_member_home_populated_dark_390_viewport.png`, `stale-health/stale-health_member_status_populated_light_390.png`, `budget-exhausted/budget-exhausted_member_status_populated_dark_390.png`.

“Degraded,” “Unknown,” “Embeddings,” and “models” appear without a simple explanation on the first phone screen. “Voice” in Status also maps to “Read text aloud” on Home. A member must infer whether a task will work.

**Fix:** Use task names in the member view and explain status in place: “Video may fail,” “We cannot confirm text-to-speech right now,” and “Some paid chat options are paused.” Keep technical labels in an optional detail view.

### 10. Library metadata is hard to scan on a phone

**Screenshots:** `default/default_member_library_populated_light_390_viewport.png`, `default/default_owner_library_populated_dark_390_viewport.png`, `default/default_member_library_library-video-grid_light_390_viewport.png`.

Two-column cards truncate almost every creation title to one line. Dates and durations use very small text, and a video card looks much like an image until its tiny duration badge is noticed. A member looking for a specific creation has to open several cards.

**Fix:** Allow two title lines, increase secondary-text size and contrast, and add an explicit Video/Audio type label on media cards.
