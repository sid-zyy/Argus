# Argus · launch-style demo video

**Length:** about 2 minutes 50 seconds. **Format:** 1920×1080, 60 fps if possible, light theme.
**Feel:** a product launch film, not a screen recording. Big, confident type. Lots of white space. Slow, smooth
camera moves over the real UI. Cuts land on the beat of the music. Every sentence on screen is short.

**Story:** one innocent commit breaks a real shop → Argus notices by itself → a team of AI agents finds the exact
commit and proves a fix → one person approves → the shop recovers → Argus reports what it saved.

---

## The look

| Element | How it looks |
|---|---|
| **Stage** | The app never fills the screen edge to edge. It sits in a floating browser window (rounded corners, soft shadow) on an off-white background (#F5F5F7) with a very faint violet glow behind it. |
| **Title cards** | Pure white or pure black full-screen cards with one line of text, centred. SF Pro Display / Inter, weight 700–800, 96–120 px, tight letter-spacing (−2%). |
| **Kinetic type** | Words rise 20 px and un-blur in, one by one, 80 ms apart. The key word is in gradient violet (#5B4DD6 → #2B9FC4). Lines leave by fading up. |
| **Captions over the UI** | Small (34–40 px), weight 600, dark grey, placed in the white space beside the floating window. Never more than 6 words. |
| **Camera** | Slow push-ins (1.0× → 1.08× over 3 s) on every shot. Feature zooms go to 1.8–2.4× with a soft 0.5 s ease, and the rest of the window gently blurs. |
| **3D moments** | For the big reveals (the diff, the sphere, the recovery) the window tilts in 3D: starts rotated 12° on Y and 6° on X, settles flat over 1.2 s. |
| **Cursor** | Large and smooth (motion-smoothed). Clicks get a soft violet ripple. Hide the cursor when nothing is clicked. |
| **Speed** | Waiting is never shown in real time. Ramp to 4–8× with motion blur; a tiny `4×` pill sits in the corner. |
| **Transitions** | Mostly hard cuts on the beat. Tab switches use a fast horizontal slide (250 ms). Title cards fade through white. |
| **Colour cues** | Red only for the breakage, green only for the recovery, violet for Argus. |

## Music

**Yes, use music.** A launch film without voiceover is driven by music and on-screen text, as Apple's product films
usually are. The edit is cut to its beats.

- **Style:** minimal electronic with a clean pulse, about 110–120 BPM. Three parts: calm and curious → tense →
  big lift. No vocals.
- **Shape:**
  - **0:00–0:25:** light and confident.
  - **0:25–1:05:** the beat drops out to a low, tense pulse while the shop breaks.
  - **1:05–2:20:** the beat builds as the agents work.
  - **2:20:** a clear lift at the moment of approval.
  - **2:20–2:50:** full and resolved through the recovery and results.
- **Where to get it (free for this use):** YouTube Audio Library, Pixabay Music, or Uppbeat (credit the artist in the
  video description if the licence asks). Search for "tech", "corporate minimal", "product launch", "inspiring electronic".
- **Sound design (subtle):** a soft click on each button press, a low "whoosh" on title cards, a short muted error
  tone when the shop breaks, and a bright chime when the fix goes live.
- **Voiceover:** optional. The film is written to work with music and on-screen text alone. If you add a voice, use
  the lines in the last section, and duck the music by about 8 dB under it.

---

## Before recording

1. Start both servers (two terminals), Argus first:
   - `uvicorn ui.app:app --port 8000 --reload`
   - `python -m shop.server`
2. Open two browser tabs: **Tab A** `http://localhost:8000` (Argus), **Tab B** `http://localhost:8100` (ShopFast).
3. In Tab A, top right user menu, choose **Siddhartha (Developer)**. Light theme (sun/moon button).
4. Tidy the board: on the dashboard, click **Clear** on *Needs a person* and *Fixed*.
5. In Tab B, click **Reset shop** → OK, so its history is clean.
6. Browser: zoom 100%, bookmarks bar hidden, other tabs closed, full screen (F11), window 1920×1080.
7. Check the AI keys: Groq and Gemini in `.env`, OpenRouter as the backup.
8. Wait one minute after starting the shop so its error chart starts with a flat healthy line.

> Record every scene as its own clip, at full speed, with slow deliberate mouse moves. All styling, zooms, text
> and speed changes happen in the edit.

---

## The film

Each scene lists: **what you do** (exact clicks), **what the camera does**, and **on-screen text** (in the order it
appears). `▌` marks a cut on the beat.

### 1 · Cold open (0:00–0:08)
- **Card (black):** **Every minute of downtime costs money.**
- ▌ **Card (black):** "So we built something that fixes it." → the line fades up and away.
- ▌ **Card (white):** the Argus logo scales from 0.9 to 1.0 with a blur-in; **Argus** appears below it, then
  *An AI incident commander.* in grey.
- **Sound:** soft whoosh into the logo.

### 2 · A real shop (0:08–0:22) · Tab B
- **You do:** nothing for 2 s, then **click** *Buy now* on **Trail running shoes**.
- **Camera:** the ShopFast window floats in from below with the 3D tilt, then a slow push-in. Zoom to the green toast *Order placed: Trail running shoes*. Then glide to the right panel: *Error rate 0.0%*, *Database connections 0 of 20*.
- **On-screen text:**
  - beside the window: **Meet ShopFast.**
  - then: *A real store. Real code. Real traffic.*
  - on the zoom to the panel: *All systems normal.*

### 3 · One small change (0:22–0:50) · Tab B
- **You do:** scroll the right column to *Developer controls*, **click** **Push a bad change**. Let the popup play to the end. Don't touch anything until *Live on ShopFast* is green and the amber note is showing. Then **click** **Watch the shop**.
- **Camera:**
  - **Typing:** cut to a tight shot of the typed command.
  - **Diff:** 3D-tilt reveal as it fills in line by line, then a slow push-in.
  - **Steps:** zoom to *Pushed → Deploying → Live on ShopFast*.
  - **Bug:** tight zoom on the two amber lines (the removed `finally:` and the new early `return`). The rest of the screen blurs and darkens slightly.
- **On-screen text:**
  - ▌ **Card (white, 0.8 s):** **Then, one small change.**
  - over the typing: *A tidy refactor.*
  - over the steps: *Reviewed. Pushed. Live.*
  - ▌ over the amber lines, in red: **One line returns too early.**
  - small, under it: *The database connection is never given back.*

### 4 · Things go wrong (0:50–1:08) · Tab B
- **You do:**
  - wait while the shop breaks (30–60 s, sped up in the edit);
  - then **click** *Buy now* on **Noise-cancelling headphones**.
- **Camera:**
  - **Split screen.** Left: zoom on the connections bar filling from green to amber to red, *20 of 20*. Right: the error chart climbing past the dashed line, with the orange pin *Arjun …*. Ramp `6×`.
  - **Breakage:** cut to the header pill turning red, *Checkout failing*, and the red banner.
  - **Customer:** zoom on the red toast *Sorry, checkout failed (error 503)*.
- **On-screen text:**
  - over the split screen: *Every declined card leaks a connection.*
  - ▌ on the pill turning red: **Checkout is down.**
  - on the 503 toast: *Customers can't pay.*
- **Sound:** the beat drops out; a short muted error tone on the 503.

### 5 · Argus is already on it (1:08–1:18) · Tab B → Tab A
- **You do:** **click** the **INC-…** link in the shop's *Argus* panel. It opens in Tab A.
- **Camera:** zoom on the panel's text *Alert sent. Argus opened INC-… and is investigating.* Then a fast slide to the incident page.
- **On-screen text:**
  - ▌ **Card (black, 1 s):** **Nobody filed a ticket.**
  - ▌ **Card (black, 1 s):** **Argus noticed by itself.**
- **Sound:** the beat comes back in and starts building.

### 6 · Meet the team (1:18–1:58) · Tab A, incident page
- **You do:** nothing. Scroll slowly from the header down to the sphere, and later to the live panel.
- **Camera:**
  - **Header:** zoom on *INC-… · Alert · from Prometheus Alertmanager*, then on the impact strip: *Revenue at risk ₹…/min*, and *Cost so far* ticking up.
  - **Sphere:** 3D-tilt reveal of the sphere with its seven agent cards, then a slow orbit-style push-in.
  - **Agents:** while the agents work, ramp `8×`, dropping to 1× at each labelled handoff (*Plan*, *Check C1*, *C1 ruled out*, *Commit …*, *Code fix*, *Tested fix*). On each handoff, a tight zoom follows the moving label from one card to the next.
  - **Live panel:** short zooms on the plan checklist ticking off and on one line of the agent's reasoning.
- **On-screen text:**
  - on the impact strip: *Live business impact.*
  - ▌ on the sphere reveal: **A team of AI agents.**
  - name cards, one per agent at its first handoff (white rounded card, name bold, role grey):
    **Athena** *Commander* · **Theia** *Investigator* · **Themis** *Fact checker* · **Chronos** *Change finder* · **Hephaestus** *Fixer* · **Talos** *Test lab*
  - **If a guess is ruled out** (Themis card shows *Ruled out*, plan shows *New plan*):
    ▌ **Wrong guess. Caught.** then small: *So it makes a new plan.*
  - on the *Commit …* handoff: **Found the exact commit.**

### 7 · Proof, not guesses (1:58–2:15) · Tab A
- **You do:** scroll to the **Findings** tab sections.
- **Camera:**
  - **Causes:** zoom on *Possible causes*: the struck-through *Ruled out* card, then the *Confirmed* card with its quoted log lines.
  - **The commit:** zoom on *Change that caused it*. A floating card from Scene 3 (Arjun's commit id) slides in beside it, and a violet line connects the two matching ids.
  - **The fix:** zoom on *Suggested fix*, on the green `try:` and `finally: pool.release(conn)` lines, and on the test result *Pass*.
- **On-screen text:**
  - on the quotes: *Every quote checked against the real files.*
  - on the matching ids: ▌ **Same commit. Found in minutes.**
  - on the fix: *The smallest fix.*
  - on *Pass*: **Tested before anyone is asked.**

### 8 · You decide (2:15–2:24) · Tab A
- **You do:** scroll to **Your decision**, **click** the comment box, type *Checked the change, looks safe*, **click** **Approve fix**.
- **Camera:** zoom on *Please decide within …*, then a tight shot of the cursor on **Approve fix**. On the click, a 0.2 s freeze with a soft flash.
- **On-screen text:**
  - before the click: **Nothing changes without you.**
  - ▌ on the click: **Approved.**
- **Sound:** the music lifts on the click.

### 9 · Back in business (2:24–2:38) · Tab A → Tab B
- **You do:**
  - in Tab A, let the timeline show *Deployed the fix to ShopFast as commit …*, then switch to Tab B;
  - wait for recovery, then **click** *Buy now* on **Mechanical keyboard**.
- **Camera:**
  - **Deploy:** zoom on the timeline line, then a fast slide to the shop.
  - **Recovery:** 3D-tilt reveal of the shop, then ramp `4×` while the error rate falls to *0.0%*, the connections bar drains back to green, and the pill turns green.
  - **Pins:** zoom on the chart, where the orange pin *Arjun …* and the green pin *Argus fix …* sit side by side.
  - **Customer:** zoom on the green toast *Order placed*.
- **On-screen text:**
  - on the deploy line: *Deployed by Argus.*
  - ▌ on the pill turning green: **Back online.**
  - on the two pins: *Broken. Fixed.* (each word under its pin)
- **Sound:** a bright chime on *Back online*.

### 10 · What it saved (2:38–2:46) · Tab A
- **You do:** in the incident, **click** the **Report** tab, scroll slowly; then **click** **Results** in the top bar.
- **Camera:**
  - **Report:** zoom on the *Business impact* table (revenue protected, time to fix vs a manual fix), then on *Decisions made by people*.
  - **Results:** cut to the page. The headline blurs in and the cards count up (*100%* of quoted evidence is real, *3 of 3* culprits named). Zoom on those two.
- **On-screen text:**
  - on the report: *A report for engineers and managers.*
  - on revenue protected: **₹… lakh protected.** (use the real number on screen)
  - on Results: **100% of its evidence is real.** then small: *A chatbot managed 25%.*

### 11 · End card (2:46–2:52)
- **Card (white):** the Argus logo, then **Argus.** in large type.
- Below, word by word: *Finds the cause. Tests the fix.* ▌ ***You decide.*** (*You decide* in the violet gradient)
- Small, at the bottom: the GitHub link and team names.
- **Sound:** the music resolves on the last word.

---

## Backup plan

- **If the live investigation fails or is too slow:** on the dashboard, click **Try a demo incident → Checkout is
  failing → Replay**, and use that for Scenes 6–8. It's a recorded real run of the same bug and it includes a
  ruled-out guess. Crop out the *Replay of a recorded run* chip, or keep it and add *Recorded run* in small text.
- **Live runs vary.** If no guess is ruled out in your take, drop the "Wrong guess. Caught." beat. If a different
  guess is ruled out, the text still works.
- **If an AI service pauses** (*Pausing … free-tier limit*), cut those stretches.
- **Record two full takes**, and keep the best clip of each scene.

## Optional voiceover

Calm and confident, one line per beat. Leave silence for the title cards.

> Every minute of downtime costs money. So we built Argus.
> This is ShopFast. A real store, running real code.
> Then, one small change. It looks like a tidy refactor. But one line returns too early, and the database
> connection is never given back.
> In a minute, checkout is down.
> Nobody filed a ticket. Argus noticed by itself.
> Argus is a team of AI agents. They plan, investigate, and try to prove each other wrong.
> Every quote is checked against the real files. It finds the exact commit, writes the smallest fix, and tests it.
> Then you decide.
> Argus deploys the fix, and the shop is back.
> It reports what happened, what it cost, and what it saved.
> Argus. Finds the cause. Tests the fix. You decide.
