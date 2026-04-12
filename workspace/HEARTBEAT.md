# Heartbeat Checks

## Every cycle (~30 min)
- [ ] Check Gmail for urgent unread messages (last 30 min)
- [ ] Check calendar for events in next 2 hours

## 2-4x daily
- [ ] Review run_log.jsonl for failures or unusual patterns
- [ ] Check goal progress (goals.yaml → goal_state.json)
- [ ] Review budget spend vs daily/weekly limits

## Daily
- [ ] Memory maintenance: review recent daily logs → update MEMORY.md
- [ ] Clean up temp files and stale sandbox artifacts

## State Tracking
<!-- Update these after each check -->
last_email_check: null
last_calendar_check: null
last_log_review: null
last_memory_maintenance: null
