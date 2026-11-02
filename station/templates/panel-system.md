---
tags: [system]
order: 10
cooldown: 60
command: |
  uptime | sed 's/^ *//'
  free -h | awk 'NR==2 {print "memory", $3 "/" $2}'
  df -h . | awk 'NR==2 {print "disk", $3 "/" $2, "(" $5 ")"}'
---
