# =============================================================================
# ALERTMANAGER — Telegram
# =============================================================================
# Rendered by bin/stack-deploy (envsubst) into config/alertmanager.yml, which is
# written 0600 and mounted read-only. Both the chat id and the bot token are
# interpolated in.
#
# The token is NOT a docker secret, and that is a considered trade. A secret
# cannot be absent, so referencing one would stop the whole monitoring stack
# from deploying while alerting is unconfigured; and a secret cannot be changed
# in place, so the panel could never edit it. A root-only file on the master is
# the same trade every component's credentials already make. It does not appear
# in `docker service inspect`, because it is a bind mount rather than an env var.
#
# If no chat id is configured, bin/stack-deploy installs
# config/alertmanager-none.yml instead — a receiver that deliberately drops everything — rather than leaving
# a half-filled config that stops Alertmanager from starting at all. That state
# is logged on every deploy and reported by `smoke-test`, because alerting that
# quietly discards is the failure this whole path was rebuilt to avoid.
#
# parse_mode is OFF on purpose. Telegram rejects the whole message with a 400
# when HTML does not parse, and alert text contains `<`, `>` and `&` routinely —
# a service name, a PromQL fragment, an error string. Formatting is not worth an
# alert that silently fails to arrive.
# =============================================================================

route:
  receiver: default
  group_by: [alertname, cluster]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

  routes:
    # The heartbeat fires permanently by design, so it must not ride the 4h
    # repeat with everything else. Its ABSENCE is the signal.
    - matchers:
        - alertname="Watchdog"
      receiver: heartbeat
      group_wait: 0s
      repeat_interval: 24h

receivers:
  - name: default
    telegram_configs:
      - bot_token: "${ALERT_TELEGRAM_BOT_TOKEN}"
        chat_id: ${ALERT_TELEGRAM_CHAT_ID}
        parse_mode: ''
        send_resolved: true
        # Literal block, not folded: a folded scalar would join every alert in
        # the group onto one unreadable line.
        message: |-
          [{{ .Status | toUpper }}{{ if eq .Status "firing" }} x{{ .Alerts.Firing | len }}{{ end }}] {{ .CommonLabels.alertname }}
          {{ range .Alerts }}{{ .Annotations.summary }}
          {{ end }}

  - name: heartbeat
    telegram_configs:
      - bot_token: "${ALERT_TELEGRAM_BOT_TOKEN}"
        chat_id: ${ALERT_TELEGRAM_CHAT_ID}
        parse_mode: ''
        send_resolved: false
        message: "Watchdog: vmalert -> alertmanager -> Telegram is working. Silence here means it is not."

# Not Telegram? Replace BOTH receivers with one of these shapes. Everything
# above the `receivers:` key stays as it is.
#
#   Slack, or Discord with /slack appended to the webhook:
#     - name: default
#       slack_configs:
#         - api_url: "${ALERT_WEBHOOK_URL}"
#           send_resolved: true
#
#   Anything that speaks JSON:
#     - name: default
#       webhook_configs:
#         - url: "${ALERT_WEBHOOK_URL}"
#           send_resolved: true
#
# Both need ALERT_WEBHOOK_URL added to infra.env, and the emptiness check in
# bin/stack-deploy widened to look at it.
