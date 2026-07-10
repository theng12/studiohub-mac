module.exports = {
  version: "3.6",
  title: "Studio Hub KH",
  description: "Control plane for the KH Studio family — live health, unified model catalog, and unified-memory monitoring for Image/Music/Voice/Chat/Video Studio.",
  icon: "icon.png",
  menu: async (kernel, info) => {
    const installed = info.exists("conda_env")
    // Always-on launchd service installed? (marker dropped by install_service.sh)
    const serviceInstalled = info.exists("service/.installed")
    const servicePort = 47873
    // Offered in the normal menus so the user can convert to a background
    // service. When the service IS installed we return a dedicated "service
    // mode" menu below instead.
    const serviceItem = { icon: "fa-solid fa-heart-pulse", text: "Install as Startup Service", href: "service.js" }
    const running = {
      install: info.running("install.js"),
      start: info.running("start.js"),
      update: info.running("update.js"),
      reset: info.running("reset.js")
    }

    if (running.install) {
      return [{ default: true, icon: "fa-solid fa-plug", text: "Installing", href: "install.js" }]
    }
    if (running.update) {
      return [{ default: true, icon: "fa-solid fa-rotate", text: "Updating", href: "update.js" }]
    }
    if (running.reset) {
      return [{ default: true, icon: "fa-solid fa-broom", text: "Resetting", href: "reset.js" }]
    }

    if (!installed) {
      return [{ default: true, icon: "fa-solid fa-plug", text: "Install", href: "install.js" }]
    }

    // ── Service mode ──
    // The launchd service runs the Hub itself (on the fixed port), so Pinokio
    // doesn't "see" it as running. Show a dedicated menu: open the running
    // dashboard, check status, restart, view logs, uninstall — and NO "Start"
    // button (that would fight the service for the port).
    if (serviceInstalled) {
      const cb = Date.now()
      const svcUrl = `http://localhost:${servicePort}`
      return [
        { default: true, icon: "fa-solid fa-satellite-dish", text: "Open Dashboard (service)", href: `${svcUrl}/?_cb=${cb}` },
        { icon: "fa-solid fa-arrow-up-right-from-square", text: `Port ${servicePort} · Open in Browser`, href: "open_external.js", params: { url: svcUrl } },
        { icon: "fa-solid fa-stethoscope", text: "Check Service Status", href: "service_status.js" },
        { icon: "fa-solid fa-rotate-right", text: "Restart Service", href: "service_restart.js" },
        { icon: "fa-solid fa-screwdriver-wrench", text: "Repair · take over port", href: "service.js" },
        { icon: "fa-solid fa-folder-open", text: "Service Logs", href: "logs/service?fs=true" },
        { icon: "fa-regular fa-circle-xmark", text: "Uninstall Startup Service", href: "unservice.js" },
        { icon: "fa-solid fa-rotate", text: "Update", href: "update.js" }
      ]
    }

    if (running.start) {
      const local = info.local("start.js")
      if (local && local.url) {
        // Cache-bust so Pinokio's embedded webview can't serve a stale build.
        const cb = Date.now()
        // Browser-friendly URL: replace 0.0.0.0 (server-bind) with localhost.
        const browserUrl = local.url.replace("0.0.0.0", "localhost")
        const portMatch = local.url.match(/:(\d+)/)
        const port = portMatch ? portMatch[1] : "?"
        return [
          { default: true, icon: "fa-solid fa-satellite-dish", text: "Open Dashboard", href: `${local.url}/?_cb=${cb}` },
          { icon: "fa-solid fa-arrow-up-right-from-square", text: `Port ${port} · Open in Browser`, href: "open_external.js", params: { url: browserUrl } },
          { icon: "fa-solid fa-terminal", text: "Terminal", href: "start.js" },
          { icon: "fa-solid fa-rotate", text: "Update", href: "update.js" },
          serviceItem
        ]
      }
      return [{ default: true, icon: "fa-solid fa-terminal", text: "Terminal", href: "start.js" }]
    }

    return [
      { default: true, icon: "fa-solid fa-power-off", text: "Start", href: "start.js" },
      serviceItem,
      { icon: "fa-solid fa-rotate", text: "Update", href: "update.js" },
      { icon: "fa-solid fa-plug", text: "Reinstall", href: "install.js" },
      { icon: "fa-regular fa-circle-xmark", text: "Reset", href: "reset.js" }
    ]
  }
}
