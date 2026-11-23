"""Palette, type scale, geometry and copy constants of the main window. Shared by the GUI modules."""




# Palette from the approved mockup (opt/mockup/a-focus.html, CSS variables). Dark theme only.
# One accent colour (Apple system blue) marks every on, selected or primary state.
# Red is reserved for errors and orange for warnings. No green, no glow.
COL_WINDOW = (28, 28, 30)           # --win
COL_CARD = (44, 44, 46)             # --card
COL_CARD_HOVER = (58, 58, 60)       # --card-hi
COL_SEPARATOR = (84, 84, 88, 140)   # --sep (55 % opacity)
COL_TEXT = (245, 245, 247)          # --text
COL_TEXT_2 = (161, 161, 166)        # --text2
COL_TEXT_3 = (110, 110, 115)        # --text3
COL_FILL = (118, 118, 128, 61)      # --fill (24 %)
COL_FILL_2 = (118, 118, 128, 92)    # --fill2 (36 %)
COL_FILL_3 = (118, 118, 128, 112)   # --fill3 (44 %)
COL_ACCENT = (10, 132, 255)         # --accent
COL_ACCENT_HOVER = (47, 150, 255)   # --accent-hi
COL_ACCENT_PRESSED = (8, 104, 210)
COL_LINK_HOVER = (64, 156, 255)     # text button hover: a lighter tint of the accent (#409CFF)
COL_LINK_PRESSED = (0, 110, 220)    # text button while pressed: a little dimmer than the rest colour
COL_ACCENT_SOFT = (10, 132, 255, 46)     # --accent-soft (18 %)
COL_ACCENT_SOFT_2 = (10, 132, 255, 71)   # --accent-soft2 (28 %)
COL_WARNING = (255, 159, 10)        # --orange
COL_ERROR = (255, 69, 58)           # --red
COL_WHITE = (255, 255, 255)

# Windows 11 DWM title bar. COLORREF is 0x00BBGGRR: caption #1C1C1E, text #A1A1A6, border #2C2C2E.
TITLE_CAPTION_COLOR = 0x001E1C1C
TITLE_TEXT_COLOR = 0x00A6A1A1
TITLE_BORDER_COLOR = 0x002E2C2C

# Type scale in CSS px, the design size of the mockup. build_ui converts each step with ui_type.font_px(),
# which applies the bundled font's line-height factor and the monitor DPI scale (DearPyGui sizes fonts by
# line height, see ui_type). Steam-like proportions at 125 %: capitals of row labels about 13 px tall.
TYPE_STATE = 20     # state title ("Noise cancellation on"), SemiBold
TYPE_TITLE = 18     # panel titles and the first-run card title, SemiBold
TYPE_ROW = 15       # microphone name in the card and in the list, SemiBold
TYPE_BODY = 15      # body text and settings labels
TYPE_BUTTON = 15    # buttons, SemiBold
TYPE_SUB = 13       # secondary and sub text, segmented labels (SemiBold)
TYPE_FOOTER = 13    # footer line
TYPE_SMALL = 12     # small labels (help step numbers)

# Settings panel only: one CSS px smaller than the shared steps above (owner feedback at 125 %).
TYPE_SET_TITLE = 17   # "Settings" panel title, SemiBold
TYPE_SET_LABEL = 14   # settings row labels
TYPE_SET_DETAIL = 12  # settings row detail text (two lines)
TYPE_SET_SEG = 12     # mic boost segment labels, SemiBold

# Window geometry (logical pixels, scaled with self.s()): 420 px wide, 24 px side gutters.
WINDOW_W = 460       # widened from 420: hero notices up to 382 px wide and the microphone name column need it
CONTENT_PAD = 24
CONTENT_W = WINDOW_W - 2 * CONTENT_PAD  # 412 px between the gutters
TEXT_LEAD = 1.5     # logical px of space above and below every text line (ImGui FramePadding.y). Inter's line box
                     # is 1.21 em; the mockup sets line-height 1.4, which adds about 1.5 px per side at 12-17 px.
POWER_D = 96        # power button diameter (was 104: the text carries more weight next to it)
HERO_TOP = 24        # space above the power button
HERO_H = 214         # logical px: hero block and first-run card share this slot. The hero content (power button,
                     # state, sub line, meter) measures 214 px with the text leading (TEXT_LEAD), so the meter is not clipped
ACTION_H = 48        # action buttons (Hear myself, Show settings, Install cable, Not now, Got it)
MIC_ROW_H = 68       # microphone row in the card and rows of the microphone list (name + sub line, 16 px padding)
MIC_TEXT_W = CONTENT_W - 92        # name and sub line width in the card (tile, gaps, chevron, right padding)
MIC_LIST_TEXT_W = CONTENT_W - 88   # name and sub line width in the microphone list (icon, tick, gaps)
SHEET_HEAD_H = 56    # logical px: panel title row with Done
HELP_STEP_H = 64     # one help step: title, description and (step 3) the chip line
HELP_BODY_H = 312    # help steps (3 x 64 + 3 x 12), Got it (48), 4 + 8 px gaps and the version line (about 18 px)
SETTINGS_ROW_H = 80  # logical px: one settings row; a two-line detail with 14 px above and below
SET_LABEL_W = 234    # label column of a settings row (the detail wraps inside it); 16 + 234 + 146 + 16 = 412
SET_CTRL_W = 146     # control column of a settings row: slider, boost segments, switch
VAD_MIN, VAD_MAX = 0.40, 0.90  # Sensitivity range (vad_threshold)
BOOST_DBS = (0, 5, 10, 15)     # mic boost segments, left to right
SET_BOOST_H = 30               # mic boost track height (logical px)
SET_BOOST_INSET = 2            # selected pill inset from its segment, on all sides (logical px)

# First-run card copy (mockup "First run" and "Installing cable" screens).
SETUP_BODY_OFFER = ("Getsu needs a free virtual audio cable to send your clean voice to Discord, Zoom and games. "
                    "It takes about a minute.")
SETUP_BODY_INSTALLING = "Windows may ask for permission. Click Yes. This window updates by itself."

# Features whose backend lands in a later task. Their controls are built but hidden.
FEATURE_EARBUD_HINT = False    # "Earbud mic drops audio to call quality." (earbud work: M5.x)
FEATURE_LEARN_ROOM = False     # "Learn my room" row in Settings (backend: M2.3)
