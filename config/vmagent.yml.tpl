global:
  scrape_interval: 15s
  external_labels:
    cluster: "${APP_NAME}"

scrape_configs:

  # --- every node in the swarm, present and future -------------------------
  - job_name: node
    dockerswarm_sd_configs:
      - host: unix:///var/run/docker.sock
        role: nodes
        port: 9100
    relabel_configs:
      - source_labels: [__meta_dockerswarm_node_address]
        target_label: __address__
        replacement: "$1:9100"
      - source_labels: [__meta_dockerswarm_node_hostname]
        target_label: instance
      - source_labels: [__meta_dockerswarm_node_id]
        target_label: node_id
      - source_labels: [__meta_dockerswarm_node_role]
        target_label: node_role
      - source_labels: [__meta_dockerswarm_node_status]
        regex: ready
        action: keep

  # --- container-level metrics on every node -------------------------------
  - job_name: cadvisor
    dockerswarm_sd_configs:
      - host: unix:///var/run/docker.sock
        role: nodes
        port: 8081
    relabel_configs:
      - source_labels: [__meta_dockerswarm_node_address]
        target_label: __address__
        replacement: "$1:8081"
      - source_labels: [__meta_dockerswarm_node_hostname]
        target_label: instance
      - source_labels: [__meta_dockerswarm_node_status]
        regex: ready
        action: keep

  # --- any service that opts in via deploy labels --------------------------
  # Add these three labels to any future service and it is scraped
  # automatically. No edit to this file is ever needed again.
  - job_name: swarm-services
    # A scraped metric's OWN `service` label wins over the target label below.
    #
    # Without this, the two collide and Prometheus renames the metric's one to
    # `exported_service` — which silently broke every per-component alert. The
    # autoscaler publishes autoscaler_service_*{service="<component>_app"} for
    # each component it manages, and all of those arrived tagged
    # service="monitoring_autoscaler" instead, because that is the Swarm
    # service doing the exporting. NoHealthyReplicas then named the autoscaler
    # rather than the component that was down, and `and on (service)` joined
    # every component to every other one, since they all shared that one value.
    #
    # Targets that do not publish a `service` label of their own are unaffected
    # and still get the Swarm service name, which is what `up{job=...}` and the
    # scrape-health rules join on.
    honor_labels: true
    dockerswarm_sd_configs:
      - host: unix:///var/run/docker.sock
        role: tasks
    relabel_configs:
      - source_labels: [__meta_dockerswarm_service_label_prometheus_scrape]
        regex: "true"
        action: keep
      - source_labels: [__meta_dockerswarm_task_desired_state]
        regex: running
        action: keep

      # ONE target per task, not one per network it is attached to.
      #
      # dockerswarm_sd emits a target for every (task, network) pair. A
      # component sits on `edge` AND `monitoring`, so each of its tasks produced
      # two targets — and vmagent is only attached to `monitoring`, so the
      # `edge` one (172.20.1.x) is unreachable by construction. Half of every
      # component's targets were permanently down, which is exactly what
      # ReplicasNotScraped is designed to catch, so it fired on healthy
      # components and could not be trusted.
      #
      # Keeping the monitoring address is the correct half: it is the network
      # vmagent shares with everything it scrapes, and the one that exists
      # whether or not a component is exposed to the tunnel.
      - source_labels: [__meta_dockerswarm_network_name]
        regex: monitoring
        action: keep
      - source_labels: [__address__, __meta_dockerswarm_service_label_prometheus_port]
        regex: "([^:]+)(?::\\d+)?;(\\d+)"
        target_label: __address__
        replacement: "$1:$2"
      - source_labels: [__meta_dockerswarm_service_label_prometheus_path]
        target_label: __metrics_path__
        regex: "(.+)"
      - source_labels: [__meta_dockerswarm_service_name]
        target_label: service
      - source_labels: [__meta_dockerswarm_node_hostname]
        target_label: instance
      - source_labels: [__meta_dockerswarm_service_label_app_env]
        target_label: env
      - source_labels: [__meta_dockerswarm_task_id]
        target_label: task_id

  # --- self -----------------------------------------------------------------
  - job_name: victoriametrics
    static_configs:
      - targets: ["victoriametrics:8428", "vmagent:8429", "loki:3100"]
