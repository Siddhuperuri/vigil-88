# 06. NVDA screen-reader pass

**Claim:** a blind operator can set up, monitor, respond to an incident and leave, using NVDA and the keyboard.

Automated checks (axe at WCAG 2.2 AA on every screen, keyboard tests, focus management, a live region for
new incidents) catch many faults and **cannot hear anything**. They do not show that the experience makes sense
when read aloud. That needs NVDA and a person. **This has not been done.**

## Needs

- Windows with NVDA (the free screen reader) installed, a recent version.
- A person comfortable with NVDA, or one prepared to learn its basics. Ideally someone who uses it daily.
- The application with two cameras and one confirmed incident (the synthetic source plus the intrusion
  scenario will do).
- Speech viewer on (NVDA menu, *Tools*, *Speech viewer*) so the output can be copied into the evidence.

## Script

Do each with the **monitor off or the screen unread**. Note what NVDA says and where it gets lost.

1. **Sign in and first run.** Tab through the setup form. Every field is announced with its label and its
   hint. A wrong password is announced as an error tied to the field, not only coloured.
2. **Landmarks.** Press `D` through the landmarks: banner, navigation *Main*, main. `1` jumps between headings.
   The skip link works.
3. **Live status.** The strip says LIVE, engine health, cameras. Pull a camera's cable: the change is
   announced or readable without leaving the page. Unavailable values read as *unavailable*, not as 0.
4. **A new HIGH or CRITICAL incident** is announced once, with its reference and camera, without moving focus.
5. **Incident desk.** Open an incident. The severity is a word, not a colour. Acknowledge it, then dismiss one
   with a reason: the dialog traps focus, `Escape` closes it, focus returns to where it was.
6. **Evidence.** The image and video have meaningful names. A gap in the recording is read as a warning.
7. **Zones.** The drawing canvas is not usable by a screen reader; the same polygon must be editable as typed
   numbers (arrow keys move a point and the numbers are offered). Note how that reads.
8. **Degradation.** When the engine sheds load, the banner is announced as a status, once.
9. **Settings and restart.** Dialogs announce their title and the cost ("Every camera stops for a few seconds").
10. **Sign out.** Focus lands somewhere sensible.

## What to record

For each step: what NVDA said, and **pass**, **works but awkward** or **blocked**. Quote the text. A step
that works only with a workaround is awkward, not a pass. Use [the template](evidence/TEMPLATE.md).
