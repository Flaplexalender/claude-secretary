# Cost-Monitoring Alert Specification (v1.0)

**Status:** Final | **Effective:** 2026-03-11 | **Owner:** Finance + DevOps | **SLA:** Real-time

---

## 1. Budget Thresholds

### Daily Limits
| Tier | Daily Limit | Weekly Limit | Monthly Limit | Action |
|------|------------|--------------|---------------|--------|
| Development | $5.00 | $25.00 | $80.00 | 🟡 Warn at 80%, 🔴 Block at 100% |
| Staging | $15.00 | $80.00 | $250.00 | 🟡 Warn at 80%, 🔴 Block at 100% |
| Production | $100.00 | $600.00 | $2,000.00 | 🟡 Warn at 70%, 🟠 Escalate at 85%, 🔴 Block at 100% |
| Research | Unlimited | Unlimited | Unlimited | 🟡 Tracking only (no blocks) |

**Escalation thresholds trigger alerts at 80% spend (warning), 90% spend (critical).**

---

## 2. Metrics Triggering Alerts

### 2.1 Premium Spend Overages
**Trigger:** Cost exceeds daily/weekly budget threshold
- **Metric**: `premium_cost` from run_log.jsonl
- **Multipliers tracked**: Haiku (0.33x), Sonnet (1.0x), Opus (3.0x)
- **Alert Level**: WARN (80%) → CRITICAL (90%) → BLOCK (100%)
- **Example**: If daily Production limit is $100 and current spend is $85, fire CRITICAL alert

### 2.2 Token Count Warnings
**Trigger:** Single request exceeds token quota
- **Metrics**: `input_tokens`, `output_tokens` from API response
- **Thresholds**:
  - Haiku: 8,000 tokens/request (daily: 500K)
  - Sonnet: 15,000 tokens/request (daily: 1M)
  - Opus: 25,000 tokens/request (daily: 500K)
- **Alert Level**: WARN (80%) → CRITICAL (95%) → BLOCK (100%)
- **Message**: "Token quota exceeded for Sonnet (14,200/15,000)"

### 2.3 Latency Degradation
**Trigger:** Response time exceeds SLA or increases >50% vs. baseline
- **Baseline latencies**: Haiku 2s, Sonnet 4s, Opus 8s (p50 from last 100 runs)
- **Alert threshold**: +50% or absolute max (Haiku 3s, Sonnet 6s, Opus 12s)
- **Alert Level**: INFO (yellow) → WARN (red if >2x baseline)
- **Metric source**: `duration_s` from run_log.jsonl

### 2.4 Error Rate Spike
**Trigger:** Failure rate >5% in last 100 runs
- **Metric**: Count of `"success": false` in run_log.jsonl
- **Rolling window**: Last 100 tasks per tier
- **Alert Level**: WARN at >5%, CRITICAL at >10%
- **Message**: "Sonnet failure rate 8% (8/100 tasks) — review logs"

### 2.5 Model Tier Abuse
**Trigger:** Task routed to high-cost tier unexpectedly
- **Metric**: Router complexity score vs. assigned tier
- **Alert if**: Haiku task assigned to Opus, or excessive Opus usage (>20% of daily tasks)
- **Alert Level**: INFO (recommendation) or WARN (if suspicious pattern detected)
- **Message**: "Task 'check email' scored low complexity but routed to Opus — consider routing override"

---

## 3. Alert Recipients & Channels

### 3.1 Alert Distribution Matrix

| Alert Type | Severity | Email | Slack | Dashboard | Escalation |
|------------|----------|-------|-------|-----------|------------|
| Daily budget warn (70%) | 🟡 WARN | Product Owner | #cost-monitoring | ✓ | None |
| Daily budget critical (85%) | 🟠 CRITICAL | Product + Finance | #cost-monitoring | ✓ | Slack mention: @on-call |
| Daily budget blocked (100%) | 🔴 BLOCK | VP Engineering + Finance | #cost-critical | ✓ | PagerDuty trigger |
| Token quota exceeded | 🟡 WARN | DevOps | #cost-monitoring | ✓ | None |
| Latency >2x baseline | 🟡 WARN | Product | #cost-monitoring | ✓ | None |
| Error rate spike (>5%) | 🟠 CRITICAL | DevOps + SRE | #alerts | ✓ | PagerDuty (if prod) |
| Model tier abuse | ℹ️ INFO | Product Owner | #cost-monitoring | ✓ | None |

### 3.2 Recipients
- **Product Owner**: alice@company.com
- **Finance**: finance@company.com
- **VP Engineering**: vp-eng@company.com
- **DevOps/SRE**: devops@company.com, devops-oncall@opsgenie.com
- **Slack Channels**: #cost-monitoring, #cost-critical, #alerts
- **Dashboard**: Cloud run cost dashboard (auto-refresh, 1-min updates)

### 3.3 Notification Method
- **Email**: Templated with threshold, current spend, action required
- **Slack**: Thread with spend graph, link to dashboard, quick action buttons
- **SMS**: For BLOCK and PagerDuty escalations only
- **Dashboard**: Real-time spend gauge + historical trend

---

## 4. Alert Delivery SLA

| Alert Type | Target Delivery | p95 Delivery | Retry Policy |
|------------|-----------------|--------------|--------------|
| WARN (budget <85%) | <5 min | <10 min | Retry every 5 min if unacknowledged |
| CRITICAL (budget 85-95%) | <2 min | <5 min | Retry every 2 min + Slack escalation |
| BLOCK (budget ≥100%) | <1 min | <2 min | Immediate + PagerDuty trigger + email |
| Token quota | <1 min | <2 min | No retry (one-shot) |
| Error spike (>5%) | <3 min | <5 min | Retry every 3 min until rate normalizes |

**Delivery confirmation:** Email and Slack receipts tracked; dashboard verified as updated within SLA window.

**Escalation policy:**
- WARN unacknowledged >30 min → escalate to CRITICAL
- CRITICAL unacknowledged >10 min → trigger PagerDuty
- BLOCK: Automatic PagerDuty page to on-call engineer

---

## 5. Implementation Details

### 5.1 Data Source: run_log.jsonl
```json
{
  "timestamp": "2026-03-11T14:30:45Z",
  "cycle": 42,
  "task": "Check unread emails",
  "tier": "low",
  "model": "claude-haiku-4.5",
  "success": true,
  "input_tokens": 1250,
  "output_tokens": 342,
  "duration_s": 2.4,
  "premium_cost": 0.33,
  "cost_usd": 0.005
}
```

### 5.2 Configuration (config.yaml)
```yaml
alerts:
  enabled: true
  budget_thresholds:
    dev:
      daily_usd: 5.0
      warn_pct: 80
      critical_pct: 90
    staging:
      daily_usd: 15.0
      warn_pct: 80
      critical_pct: 90
    production:
      daily_usd: 100.0
      warn_pct: 70
      critical_pct: 85
  metrics:
    token_quota:
      haiku: 8000
      sonnet: 15000
      opus: 25000
    latency_sla_s:
      haiku: 2
      sonnet: 4
      opus: 8
    error_rate_threshold: 0.05
  recipients:
    warn: ["alice@company.com"]
    critical: ["alice@company.com", "finance@company.com"]
    block: ["vp-eng@company.com", "finance@company.com"]
  channels:
    email: true
    slack: true
    pagerduty: true
  sla_sec:
    warn: 300
    critical: 120
    block: 60
```

### 5.3 Alert Module Architecture
- **monitor.py**: Real-time spend tracking, threshold evaluation
- **alerter.py**: Multi-channel dispatch (Email, Slack, PagerDuty)
- **run_log_analyzer.py**: Historical trend analysis, error spike detection
- **dashboard_sync.py**: Push metrics to Cloud Monitoring API (1-min refresh)

---

## 6. Stakeholder Sign-Off

| Role | Name | Signature | Date | Notes |
|------|------|-----------|------|-------|
| Product Owner | Alice D. | ___________ | ___________ | Approved daily/weekly limits |
| Finance | Bob S. | ___________ | ___________ | Approved escalation to VP Eng at 85% |
| VP Engineering | Carol M. | ___________ | ___________ | Approved PagerDuty trigger for BLOCK |
| DevOps/SRE | David T. | ___________ | ___________ | Will implement and monitor |

**Review Cadence:** Quarterly (Q2, Q3, Q4 2026) or on-demand if cost trends shift >20%.

---

## 7. FAQ

**Q: What if a task legitimately needs >$100 on a single day?**  
A: Pre-approve via `--max-budget-override` flag in campaign.yaml or request exception from Finance.

**Q: Who can acknowledge CRITICAL alerts to prevent escalation?**  
A: Product Owner or VP Engineering (tracked in audit log).

**Q: Can we set per-model-tier daily caps instead of global?**  
A: Yes — config.yaml supports tier-specific budgets. See section 5.2.

**Q: How do we prevent runaway loops (e.g., retry storm)?**  
A: Watcher enforces `max_retries: 2` + exponential backoff + daily premium cap (unblockable).

---

**Document Version:** 1.0 | **Last Updated:** 2026-03-11 | **Next Review:** 2026-06-11
