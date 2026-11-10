"""Research runtime guards; values and incident rationale live here."""

MAX_CALLS = 30  # Default research loop guard, not a money cap.
MONTHLY_MAX_CALLS = 21  # Owner-authorized automatic batch: 30 companies * 21 + 6 = 636.
RUN_SECONDS = 1800
CALL_TIMEOUT_SECONDS = 600

# The 601600 external-condition smoke needed more than 24 ordinary evidence fetches.
FETCH_LIMIT = 48  # runaway guard; time, rounds and total calls still bound exploration
# Model turns shared by explore and repair. 12 stopped a normal repair in production (688578
# research, report 83, hit round_limit mid-repair), so it is a runaway guard, not a depth quota.
ROUND_LIMIT = 24
CALL_LIMIT = 120  # runaway guard only, so calculations cannot swell the prompt without bound
REPAIR_CALL_RESERVE = 48  # shared by both repairs; exploration cannot consume it
# Shared by explore and repair and measured from the start of exploration. 72 calls already took 562 s
# (report 108), so 120 need this; the runner's 1800 s must still leave review and repair their ~400 s.
ACQUISITION_SECONDS = 1200
# Model calls a draft needs once acquisition stops: the tool-free draft, one declined tool turn
# (Gemini still calls tools when none are offered) and a review with its one format correction.
DRAFT_CALLS = 4

