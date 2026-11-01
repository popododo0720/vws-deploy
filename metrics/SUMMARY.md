# Factory metrics summary

Through 2026-11-01 (KST days). Written by .github/metrics/metrics.py in vws-deploy (#345); one file per day in metrics/daily/.

- Last 7 days (7 with data): 252 PRs merged, 296 opened, 156 DeepSeek dispatches (144 done, 14 failed, 0 timed out), 96 grok swaps.
- Last 7 days wakes: 184 verify wakes, 156 DeepSeek and 122 grok dispatches (47 reworks), lead wakes: 34 (6 days with input); tokens per merged PR: 6980554 (4 days with input).
- Last 28 days (19 with data): 681 PRs merged, 871 opened, 244 DeepSeek dispatches (216 done, 33 failed, 0 timed out), 292 grok swaps.
- Last 28 days wakes: 245 verify wakes, 244 DeepSeek and 130 grok dispatches (63 reworks) (10 days with data), lead wakes: 34 (6 days with input); tokens per merged PR: 6980554 (4 days with input).

| day | prs_opened | prs_merged | lead_time_median_h | verify_pass_rate | deepseek_dispatched | deepseek_done | deepseek_failed | deepseek_timeout | grok_swaps | ci_failure_rate | verify_backlog | stalls | tokens | verify_wakes | grok_dispatches | rework_dispatches | lead_wakes | alerts |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-01 | 25 | 26 | 0.93 | 0.824 | 6 | 7 | 1 | 0 | 15 | 0.01 | 12 | 141 | 64499789 | 18 | 20 | 5 | 0 | stall grok_escalation_rate |
| 2026-10-31 | 73 | 66 | 0.94 | 0.867 | 55 | 51 | 4 | 0 | 29 | 0.013 | 15 | 0 | 560115751 | 29 | 35 | 13 | 0 | - |
| 2026-10-30 | 85 | 71 | 0.89 | 0.831 | 53 | 48 | 8 | 0 | 33 | 0.016 | 18 | 0 | 523563644 | 36 | 45 | 13 | 0 | - |
| 2026-10-29 | 21 | 17 | 0.92 | 0.857 | 13 | 9 | 0 | 0 | 5 | 0.024 | 16 | 104 | 108320490 | 15 | 9 | 9 | 2 | stall |
| 2026-10-28 | 22 | 14 | 0.85 | 1.0 | 19 | 20 | 0 | 0 | 1 | 0.008 | 16 | 95 | - | 8 | 3 | 2 | 1 | stall |
| 2026-10-27 | 40 | 28 | 0.58 | 0.906 | 10 | 9 | 1 | 0 | 8 | 0.031 | 16 | 103 | - | 26 | 6 | 5 | 31 | stall |
| 2026-10-26 | 30 | 30 | 0.64 | 1.0 | 0 | 0 | 0 | 0 | 5 | 0.004 | 18 | 114 | - | 52 | 4 | 0 | - | stall |
| 2026-10-25 | 26 | 28 | 1.01 | 0.938 | 0 | 0 | 0 | 0 | 7 | 0.01 | 18 | 103 | - | 26 | 2 | 13 | - | stall |
| 2026-10-24 | 54 | 38 | 1.11 | 0.902 | 2 | 2 | 0 | 0 | 9 | 0.0 | 29 | 1 | - | 17 | 1 | 1 | - | stall |
| 2026-10-23 | 95 | 52 | 0.55 | 0.74 | 24 | 14 | 13 | 0 | 59 | 0.015 | 16 | 89 | - | 18 | 5 | 2 | - | stall deepseek_failure_rate |
| 2026-10-22 | 61 | 49 | 0.27 | 0.818 | 31 | 31 | 2 | 0 | 32 | 0.0 | 4 | 0 | - | - | - | - | - | - |
| 2026-10-21 | 64 | 45 | 1.05 | 0.641 | 31 | 25 | 4 | 0 | 89 | 0.041 | 10 | 0 | - | - | - | - | - | grok_escalation_rate |
| 2026-10-20 | 3 | 0 | - | - | 0 | 0 | 0 | 0 | 0 | 0.0 | 7 | 0 | - | - | - | - | - | - |
| 2026-10-19 | 0 | 0 | - | - | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | - | - | - | - |
| 2026-10-18 | 0 | 0 | - | - | 0 | 0 | 0 | 0 | 0 | - | - | - | - | - | - | - | - | - |
| 2026-10-17 | 11 | 5 | 1.48 | 0.615 | 0 | 0 | 0 | 0 | 0 | 0.0 | - | - | - | - | - | - | - | - |
| 2026-10-16 | 9 | 12 | 4.33 | 0.917 | 0 | 0 | 0 | 0 | 0 | 0.053 | - | - | - | - | - | - | - | - |
| 2026-10-15 | 101 | 71 | 0.26 | 0.727 | 0 | 0 | 0 | 0 | 0 | 0.023 | - | - | - | - | - | - | - | - |
| 2026-10-14 | 151 | 129 | 0.05 | 0.615 | 0 | 0 | 0 | 0 | 0 | 0.018 | - | - | - | - | - | - | - | - |
